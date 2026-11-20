from .base import CatalogDisabledProviderAdapter


class EasyJetAdapter(CatalogDisabledProviderAdapter):
    source_slug = "easyjet"


ADAPTER_CLASS = EasyJetAdapter
