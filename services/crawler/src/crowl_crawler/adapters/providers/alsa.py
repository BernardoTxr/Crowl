"""Alsa ordinary public search-page extractor."""

from ._bus_public_html import PublicHtmlBusAdapter


class AlsaAdapter(PublicHtmlBusAdapter):
    source_slug = "alsa"
    public_hosts = ("alsa.com",)
    provider_names = ("Alsa", "ALSA")
    parser_version = "alsa-public-html-v1"


ADAPTER_CLASS = AlsaAdapter
