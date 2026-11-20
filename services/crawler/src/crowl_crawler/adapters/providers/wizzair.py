from .base import CatalogDisabledProviderAdapter


class WizzAirAdapter(CatalogDisabledProviderAdapter):
    source_slug = "wizzair"


ADAPTER_CLASS = WizzAirAdapter
