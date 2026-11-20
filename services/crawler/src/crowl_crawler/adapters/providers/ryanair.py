from .base import CatalogDisabledProviderAdapter


class RyanairAdapter(CatalogDisabledProviderAdapter):
    source_slug = "ryanair"


ADAPTER_CLASS = RyanairAdapter
