"""
Defaults for the countries the directory can cover, keyed by ISO 3166 code.

import_listings creates a Country from the [country] section of a city's
queries file (code = ae) using these values; anything here can be changed
afterwards in the admin.

    name         full name, for structured data
    short_name   in headings, breadcrumbs and the country menu: "UAE"
    in_name      as written after "in": "the UAE"
    slug         first part of every URL: /uae/dubai/
    currency     ISO 4217, for fees
    phone_code   international dialling code, without "+"
    time_zone    for opening hours ("open now")
    city_label   what its cities are called: "Choose an emirate or city" (Al Ain is a city)
"""

COUNTRY_DEFAULTS = {
    "AE": {"name": "United Arab Emirates", "short_name": "UAE", "in_name": "the UAE",
           "slug": "uae", "currency": "AED", "phone_code": "971", "time_zone": "Asia/Dubai",
           "city_label": "emirate or city"},
    "SA": {"name": "Saudi Arabia", "short_name": "Saudi Arabia", "in_name": "Saudi Arabia",
           "slug": "saudi-arabia", "currency": "SAR", "phone_code": "966", "time_zone": "Asia/Riyadh"},
    "QA": {"name": "Qatar", "short_name": "Qatar", "in_name": "Qatar",
           "slug": "qatar", "currency": "QAR", "phone_code": "974", "time_zone": "Asia/Qatar"},
    "KW": {"name": "Kuwait", "short_name": "Kuwait", "in_name": "Kuwait",
           "slug": "kuwait", "currency": "KWD", "phone_code": "965", "time_zone": "Asia/Kuwait"},
    "BH": {"name": "Bahrain", "short_name": "Bahrain", "in_name": "Bahrain",
           "slug": "bahrain", "currency": "BHD", "phone_code": "973", "time_zone": "Asia/Bahrain"},
    "OM": {"name": "Oman", "short_name": "Oman", "in_name": "Oman",
           "slug": "oman", "currency": "OMR", "phone_code": "968", "time_zone": "Asia/Muscat"},
    # Parked 2026-10-05 (git tag pakistan-parked); runs as its own site (SITE=pk)
    "PK": {"name": "Pakistan", "short_name": "Pakistan", "in_name": "Pakistan",
           "slug": "pakistan", "currency": "PKR", "phone_code": "92", "time_zone": "Asia/Karachi"},
}

# First digits of a mobile number after the country code (or the leading 0):
# these numbers get a WhatsApp button
MOBILE_PREFIXES = {
    "AE": ("5",),
    "SA": ("5",),
    "QA": ("3", "5", "6", "7"),
    "KW": ("5", "6", "9"),
    "BH": ("3", "66"),
    "OM": ("7", "9"),
    "PK": ("3",),
}
