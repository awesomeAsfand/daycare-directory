"""
Per-site settings: the brand and wording of a deployment.

settings.SITE (from SITE in .env) picks the entry below. Templates read it
as {{ site.name }}, {{ site.nouns }} and so on (listings.context_processors),
and a site's own templates in templates/sites/<site>/ take priority over
the shared ones in templates/ (used for pages such as About, whose wording
differs entirely between sites).

Countries are data, not settings (listings.Country): one site covers every
country that has listings, at /<country>/<city>/.

    name           brand shown in the header, titles and footer
    home_label     the home page in breadcrumbs (default "Home")
    in_region      what the site covers, as written after "in", once it has
                   more than one country ("the Gulf"); with one country the
                   country's own name is used ("the UAE")
    noun / nouns   what a listing is called in running text
    title_nouns    the same in page titles and headings
    area_kinds     how areas are described: "Browse by <area_kinds>"
    type_labels    what each DaycareListing.listing_type is called on this site
    time_zone      Django TIME_ZONE
"""

SITES = {
    # gulfnurseries.com: nurseries in the GCC countries, starting with the UAE
    "gcc": {
        "name": "GulfNurseries",
        "in_region": "the Gulf",
        "home_label": "GCC",
        "noun": "nursery",
        "nouns": "nurseries",
        "title_nouns": "Nurseries",
        "area_kinds": "community or neighbourhood",
        # In the UAE early-years centres are "nurseries" whatever they teach
        "type_labels": {"preschool": "Nursery", "daycare": "Daycare"},
        "time_zone": "Asia/Dubai",
    },
    # Parked 2026-10-05 (git tag pakistan-parked)
    "pk": {
        "name": "DaycaresPK",
        "in_region": "Pakistan",
        "noun": "daycare",
        "nouns": "daycares",
        "title_nouns": "Daycare Centers",
        "area_kinds": "sector, DHA phase or town",
        "type_labels": {"preschool": "Preschool / Montessori", "daycare": "Daycare"},
        "time_zone": "Asia/Karachi",
    },
}

# Earlier names still accepted in .env
SITE_ALIASES = {"uae": "gcc"}
