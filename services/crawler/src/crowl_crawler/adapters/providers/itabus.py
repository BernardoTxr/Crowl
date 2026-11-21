"""Itabus ordinary public search-page extractor."""

from ._bus_public_html import PublicHtmlBusAdapter


class ItabusAdapter(PublicHtmlBusAdapter):
    source_slug = "itabus"
    public_hosts = ("itabus.it",)
    provider_names = ("Itabus",)
    parser_version = "itabus-public-html-v1"


ADAPTER_CLASS = ItabusAdapter
