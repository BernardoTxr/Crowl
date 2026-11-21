"""Visible text extraction for rendered public provider pages."""

import re
from html.parser import HTMLParser


_NON_VISIBLE_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})
_HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def is_hidden(tag: str, attributes: dict[str, str]) -> bool:
    style = re.sub(r"\s+", "", attributes.get("style", "").casefold())
    return (
        tag in _NON_VISIBLE_TEXT_TAGS
        or "hidden" in attributes
        or attributes.get("aria-hidden", "").casefold() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


class _VisibleBodyTextCollector(HTMLParser):
    """Collect rendered body text while ignoring scripts and hidden nodes."""

    def __init__(self, *, body_only: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._body_only = body_only
        self._stack: list[tuple[str, bool, bool]] = []
        self._chunks: list[str] = []
        self._characters = 0

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized_tag = tag.casefold()
        attributes = {name.casefold(): value or "" for name, value in attrs}
        parent_in_body = self._stack[-1][1] if self._stack else False
        parent_hidden = self._stack[-1][2] if self._stack else False
        in_body = parent_in_body or normalized_tag == "body"
        hidden = parent_hidden or is_hidden(normalized_tag, attributes)
        self._stack.append((normalized_tag, in_body, hidden))
        if normalized_tag in _HTML_VOID_ELEMENTS:
            self._stack.pop()

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _HTML_VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        matching_index = next(
            (
                index
                for index in range(len(self._stack) - 1, -1, -1)
                if self._stack[index][0] == normalized_tag
            ),
            None,
        )
        if matching_index is not None:
            del self._stack[matching_index:]

    def handle_data(self, data: str) -> None:
        if (
            not self._stack
            or (self._body_only and not self._stack[-1][1])
            or self._stack[-1][2]
            or self._characters >= 1_000_000
        ):
            return
        remaining = 1_000_000 - self._characters
        chunk = data[:remaining]
        self._chunks.append(chunk)
        self._characters += len(chunk)


def visible_html_text(html: str, *, body_only: bool = True) -> str:
    collector = _VisibleBodyTextCollector(body_only=body_only)
    collector.feed(html)
    collector.close()
    return collector.text
