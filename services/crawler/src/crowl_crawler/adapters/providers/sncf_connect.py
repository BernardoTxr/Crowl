from .base import CatalogDisabledProviderAdapter


class SncfConnectAdapter(CatalogDisabledProviderAdapter):
    source_slug = "sncf_connect"


ADAPTER_CLASS = SncfConnectAdapter
