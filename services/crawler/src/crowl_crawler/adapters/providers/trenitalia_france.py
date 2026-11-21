from .rail_html import PublicHtmlRailAdapter


class TrenitaliaFranceAdapter(PublicHtmlRailAdapter):
    source_slug = "trenitalia_france"
    operator_name = "Trenitalia France"
    extra_page_hosts = ("lefrecce.it",)
    empty_markers = (
        "nessuna soluzione di viaggio",
        "no travel solutions",
        "aucune solution de voyage",
    )


ADAPTER_CLASS = TrenitaliaFranceAdapter
