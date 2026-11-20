from .base import CatalogDisabledProviderAdapter


class BlaBlaCarBusAdapter(CatalogDisabledProviderAdapter):
    source_slug = "blablacar_bus"


ADAPTER_CLASS = BlaBlaCarBusAdapter
