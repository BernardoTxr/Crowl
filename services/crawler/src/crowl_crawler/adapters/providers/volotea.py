from .base import CatalogDisabledProviderAdapter


class VoloteaAdapter(CatalogDisabledProviderAdapter):
    source_slug = "volotea"


ADAPTER_CLASS = VoloteaAdapter
