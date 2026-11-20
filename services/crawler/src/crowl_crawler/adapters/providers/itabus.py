from .base import CatalogDisabledProviderAdapter


class ItabusAdapter(CatalogDisabledProviderAdapter):
    source_slug = "itabus"


ADAPTER_CLASS = ItabusAdapter
