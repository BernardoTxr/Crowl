from .base import CatalogDisabledProviderAdapter


class FlixBusAdapter(CatalogDisabledProviderAdapter):
    source_slug = "flixbus"


ADAPTER_CLASS = FlixBusAdapter
