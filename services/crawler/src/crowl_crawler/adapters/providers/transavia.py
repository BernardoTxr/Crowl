from .base import CatalogDisabledProviderAdapter


class TransaviaAdapter(CatalogDisabledProviderAdapter):
    source_slug = "transavia"


ADAPTER_CLASS = TransaviaAdapter
