from .base import CatalogDisabledProviderAdapter


class OuigoAdapter(CatalogDisabledProviderAdapter):
    source_slug = "ouigo"


ADAPTER_CLASS = OuigoAdapter
