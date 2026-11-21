"""BlaBlaCar Bus ordinary public search-page extractor.

Rideshare inventory is intentionally outside this adapter's contract.
"""

from ._bus_public_html import PublicHtmlBusAdapter


class BlaBlaCarBusAdapter(PublicHtmlBusAdapter):
    source_slug = "blablacar_bus"
    public_hosts = ("blablacar.com", "blablacar.fr")
    provider_names = ("BlaBlaCar Bus", "BlaBlaBus")
    consent_labels = (
        *PublicHtmlBusAdapter.consent_labels,
        "Accept and continue",
        "Tout accepter",
    )
    # No stable public deep-link contract is assumed.  The ordinary page URL
    # template must be reviewed and configured before the catalog is enabled.
    parser_version = "blablacar-bus-public-html-v1"


ADAPTER_CLASS = BlaBlaCarBusAdapter
