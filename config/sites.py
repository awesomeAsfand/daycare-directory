"""
Per-site settings: one codebase runs a separate directory per country.

settings.SITE (from SITE in .env) picks the entry below. Templates read it
as {{ site.name }}, {{ site.nouns }} and so on (listings.context_processors),
and a site's own templates in templates/sites/<site>/ take priority over
the shared ones in templates/ (used for pages such as About, whose wording
differs entirely between sites).

    name           brand shown in the header, titles and footer
    country        full country name, for structured data
    country_code   ISO 3166 code, for structured data
    in_country     country as written after "in": "Pakistan", "the UAE"
    noun / nouns   what a listing is called in running text
    title_nouns    the same in page titles and headings
    area_kinds     how areas are described: "Browse by <area_kinds>"
    time_zone      Django TIME_ZONE
"""

SITES = {
    # Parked 2026-10-05 (git tag pakistan-parked)
    "pk": {
        "name": "DaycaresPK",
        "country": "Pakistan",
        "country_code": "PK",
        "in_country": "Pakistan",
        "noun": "daycare",
        "nouns": "daycares",
        "title_nouns": "Daycare Centers",
        "area_kinds": "sector, DHA phase or town",
        "time_zone": "Asia/Karachi",
    },
    # The brand name is a placeholder until one is chosen; SITE_NAME in .env
    # overrides it without a code change
    "uae": {
        "name": "NurseriesUAE",
        "country": "United Arab Emirates",
        "country_code": "AE",
        "in_country": "the UAE",
        "noun": "nursery",
        "nouns": "nurseries",
        "title_nouns": "Nurseries",
        "area_kinds": "community or neighbourhood",
        "time_zone": "Asia/Dubai",
    },
}
