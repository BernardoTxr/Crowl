"""Provider-specific adapter slots.

Modules are loaded by source id from ``data/sources.yaml``. Keeping this
package free of a second source list makes the YAML catalog authoritative.
"""

from .base import CatalogDisabledProviderAdapter

__all__ = ["CatalogDisabledProviderAdapter"]
