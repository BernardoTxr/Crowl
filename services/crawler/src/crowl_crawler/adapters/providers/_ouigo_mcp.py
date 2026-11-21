"""Read-only transport for the official OUIGO fare search MCP."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ...models import SearchOutcome


_ENDPOINT = "https://mcp.ouigo.com/"
_PROTOCOL = "2025-11-25"
_MAX_BYTES = 256_000
_TIMEOUT = 8
_MIN_SEARCH_INTERVAL = 10


class OuigoMcpError(ValueError):
    def __init__(self, outcome: SearchOutcome, code: str) -> None:
        super().__init__(code)
        self.outcome = outcome
        self.code = code


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OuigoMcpClient:
    """Only send the documented non-mutating fare tool to the fixed public host."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirects())
        self._session_id: str | None = None
        self._initialized = False
        self._next_search = 0.0
        self._id = 0

    async def search(
        self, *, origin: str, destination: str, departure_date: str
    ) -> Mapping[str, Any]:
        if not self._initialized:
            await asyncio.to_thread(self._initialize)
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, self._next_search - loop.time()))
        self._next_search = loop.time() + _MIN_SEARCH_INTERVAL
        result, _ = await asyncio.to_thread(
            self._request,
            "tools/call",
            {
                "name": "journey_search",
                "arguments": {
                    "originStationCode": origin,
                    "destinationStationCode": destination,
                    "departureDate": departure_date,
                    "passengers": {"adults": 1, "children": 0, "babies": 0},
                    "nbAnimal": 0,
                },
            },
        )
        if result.get("isError") is True:
            raise OuigoMcpError(SearchOutcome.UPSTREAM_ERROR, "OUIGO_MCP_TOOL_ERROR")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_SCHEMA")
        return structured

    def _initialize(self) -> None:
        result, session = self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "Crowl", "version": "0.1.0"},
            },
        )
        if result.get("protocolVersion") != _PROTOCOL:
            raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_PROTOCOL")
        self._session_id = session
        self._request("notifications/initialized", {}, notification=True)
        self._initialized = True

    def _request(
        self, method: str, params: Mapping[str, Any], *, notification: bool = False
    ) -> tuple[Mapping[str, Any], str | None]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            self._id += 1
            payload["id"] = self._id
        request_id = self._id
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "Crowl/0.1 (personal fare monitor)",
        }
        if method != "initialize":
            headers["MCP-Protocol-Version"] = _PROTOCOL
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        request = Request(_ENDPOINT, json.dumps(payload).encode(), headers=headers)
        try:
            with self._opener.open(request, timeout=_TIMEOUT) as response:
                if notification and response.status == 202:
                    return {}, None
                body = response.read(_MAX_BYTES + 1)
                content_type = response.headers.get_content_type()
                session = response.headers.get("Mcp-Session-Id")
        except HTTPError as error:
            error.close()
            if error.code in {401, 403}:
                raise OuigoMcpError(SearchOutcome.BLOCKED, "OUIGO_MCP_BLOCKED") from error
            if error.code == 429:
                raise OuigoMcpError(SearchOutcome.RATE_LIMITED, "OUIGO_MCP_RATE_LIMIT") from error
            if error.code == 404 and self._session_id is not None:
                self._session_id = None
                self._initialized = False
            raise OuigoMcpError(SearchOutcome.UPSTREAM_ERROR, "OUIGO_MCP_HTTP") from error
        except TimeoutError as error:
            raise OuigoMcpError(SearchOutcome.TIMEOUT, "OUIGO_MCP_TIMEOUT") from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise OuigoMcpError(SearchOutcome.TIMEOUT, "OUIGO_MCP_TIMEOUT") from error
            raise OuigoMcpError(SearchOutcome.UPSTREAM_ERROR, "OUIGO_MCP_NETWORK") from error
        except OSError as error:
            raise OuigoMcpError(SearchOutcome.UPSTREAM_ERROR, "OUIGO_MCP_NETWORK") from error
        if session is not None and (
            not 1 <= len(session) <= 256 or not all("!" <= ch <= "~" for ch in session)
        ):
            raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_PROTOCOL")
        if len(body) > _MAX_BYTES:
            raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_RESPONSE_TOO_LARGE")
        return _decode_response(body, content_type, request_id), session


def _decode_response(body: bytes, content_type: str, request_id: int) -> Mapping[str, Any]:
    try:
        if content_type == "application/json":
            messages = [json.loads(body)]
        elif content_type == "text/event-stream":
            events = body.decode().replace("\r\n", "\n").split("\n\n")
            messages = []
            for event in events:
                lines = [
                    line.removeprefix("data:").lstrip(" ")
                    for line in event.splitlines() if line.startswith("data:")
                ]
                if lines:
                    messages.append(json.loads("\n".join(lines)))
        else:
            raise ValueError("unexpected MCP content type")
    except (UnicodeError, ValueError) as error:
        raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_PROTOCOL") from error
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("jsonrpc") == "2.0"
            and message.get("id") == request_id
        ):
            if "error" in message:
                raise OuigoMcpError(SearchOutcome.UPSTREAM_ERROR, "OUIGO_MCP_RPC_ERROR")
            if isinstance(message.get("result"), dict):
                return message["result"]
    raise OuigoMcpError(SearchOutcome.PARSE_SCHEMA_ERROR, "OUIGO_MCP_PROTOCOL")
