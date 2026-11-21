from .rail_html import PublicHtmlRailAdapter


class SncfConnectAdapter(PublicHtmlRailAdapter):
    source_slug = "sncf_connect"
    operator_name = "SNCF"
    empty_markers = (
        "aucun train ne correspond",
        "aucun résultat",
        "no trains found",
    )


ADAPTER_CLASS = SncfConnectAdapter
