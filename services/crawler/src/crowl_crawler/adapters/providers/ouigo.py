from .rail_html import PublicHtmlRailAdapter


class OuigoAdapter(PublicHtmlRailAdapter):
    source_slug = "ouigo"
    operator_name = "OUIGO"
    empty_markers = ("aucun train", "aucun trajet", "no trains found")


ADAPTER_CLASS = OuigoAdapter
