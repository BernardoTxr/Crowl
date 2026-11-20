from .base import CatalogDisabledProviderAdapter


class TrenitaliaFranceAdapter(CatalogDisabledProviderAdapter):
    source_slug = "trenitalia_france"


ADAPTER_CLASS = TrenitaliaFranceAdapter
