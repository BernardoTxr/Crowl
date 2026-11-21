from .rail_html import PublicHtmlRailAdapter


class EurostarAdapter(PublicHtmlRailAdapter):
    source_slug = "eurostar"
    operator_name = "Eurostar"
    unavailable_markers = ("oups, une erreur s'est produite",)
    empty_markers = (
        "aucun train disponible",
        "no trains available",
        "no journeys found",
    )


ADAPTER_CLASS = EurostarAdapter
