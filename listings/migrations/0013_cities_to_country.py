"""
Puts every existing city in the site's country (the UAE, or Pakistan when
SITE=pk) and moves the stored redirects to the new URLs:

    /dubai/<slug>/detail/  ->  /uae/dubai/<slug>/
    /dubai/<area>/         ->  /uae/dubai/<area>/

A database without cities (a new one, or the test database) gets no country:
import_listings creates countries from the queries files.
"""
import re

from django.conf import settings
from django.db import migrations

from listings.countries import COUNTRY_DEFAULTS


def new_path(path, city_slugs, country_slug):
    m = re.fullmatch(r"/([^/]+)/(.*?)(?:(?<=/)detail/)?", path)
    if not m or m.group(1) not in city_slugs:
        return path
    return f"/{country_slug}/{m.group(1)}/{m.group(2)}"


def forwards(apps, schema_editor):
    Country = apps.get_model("listings", "Country")
    City = apps.get_model("listings", "City")
    Redirect = apps.get_model("redirects", "Redirect")
    if not City.objects.exists():
        return
    code = "PK" if getattr(settings, "SITE", "") == "pk" else "AE"
    # Only the fields Country has at this point (later migrations add more)
    fields = {f.name for f in Country._meta.get_fields()}
    defaults = {k: v for k, v in COUNTRY_DEFAULTS[code].items() if k in fields}
    country, _ = Country.objects.get_or_create(code=code, defaults=defaults)
    City.objects.filter(country__isnull=True).update(country=country)

    city_slugs = set(City.objects.values_list("slug", flat=True))
    for r in Redirect.objects.all():
        old, new = new_path(r.old_path, city_slugs, country.slug), new_path(r.new_path, city_slugs, country.slug)
        if (old, new) != (r.old_path, r.new_path):
            r.old_path, r.new_path = old, new
            r.save()


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0012_country'),
        ('redirects', '0002_alter_redirect_new_path_help_text'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
