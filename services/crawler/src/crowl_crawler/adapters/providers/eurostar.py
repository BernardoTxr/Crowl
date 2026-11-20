from .base import CatalogDisabledProviderAdapter


class EurostarAdapter(CatalogDisabledProviderAdapter):
    source_slug = "eurostar"


ADAPTER_CLASS = EurostarAdapter
