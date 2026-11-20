from .base import CatalogDisabledProviderAdapter


class VuelingAdapter(CatalogDisabledProviderAdapter):
    source_slug = "vueling"


ADAPTER_CLASS = VuelingAdapter
