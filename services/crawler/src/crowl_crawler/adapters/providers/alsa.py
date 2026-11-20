from .base import CatalogDisabledProviderAdapter


class AlsaAdapter(CatalogDisabledProviderAdapter):
    source_slug = "alsa"


ADAPTER_CLASS = AlsaAdapter
