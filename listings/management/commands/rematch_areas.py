"""
python manage.py rematch_areas --city "Abu Dhabi" --dry-run
python manage.py rematch_areas --city "Abu Dhabi"

Match each listing of a city to an area again, with the current rules in
scraper/queries/<city>.txt (areas, aliases, centres). Use it after adding
areas or aliases instead of re-importing, which would bring back listings
deleted in the admin. Listings whose area is locked are left alone; a
listing the rules can no longer place keeps its area.
"""
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from listings.models import Area, City, DaycareListing

sys.path.insert(0, str(Path(settings.BASE_DIR) / "scraper"))
from area_match import AreaMatcher  # noqa: E402
from search_plan import load_plan  # noqa: E402


class Command(BaseCommand):
    help = "Match a city's listings to areas again after the area rules changed"

    def add_arguments(self, parser):
        parser.add_argument("--city", required=True)
        parser.add_argument("--areas-file", help="Default: scraper/queries/<city>.txt")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        city = City.objects.filter(slug=slugify(options["city"])).first()
        if not city:
            raise CommandError(f"No city {options['city']!r}")
        city_file = slugify(options["city"]).replace("-", "_")
        areas_file = Path(options["areas_file"] or Path(settings.BASE_DIR) / "scraper" / "queries" / f"{city_file}.txt")
        matcher = AreaMatcher(load_plan(areas_file, city.name))

        changed = locked = 0
        with transaction.atomic():
            for listing in DaycareListing.objects.filter(city=city).select_related("area").order_by("pk"):
                place = matcher.match(listing.address, listing.latitude, listing.longitude, listing.name)
                if not place.area:
                    continue
                now = (listing.area.name if listing.area else "", listing.sub_area)
                if now == (place.area, place.sub_area):
                    continue
                if {"area", "sub_area"} & set(listing.locked_fields or []):
                    locked += 1
                    continue
                listing.area, _ = Area.objects.get_or_create(
                    city=city, slug=slugify(place.area), defaults={"name": place.area})
                listing.sub_area = place.sub_area
                listing.save(update_fields=["area", "sub_area", "updated_at"])
                changed += 1
                self.stdout.write(f"  {listing.pk} {listing.name[:45]}: {now[0] or '-'} -> {place.area}"
                                  f"{f' ({place.sub_area})' if place.sub_area else ''} [{place.method}]"
                                  f"{'' if listing.is_active else ' (switched off)'}")
            left = DaycareListing.objects.filter(city=city, is_active=True, area__isnull=True).count()
            if options["dry_run"]:
                transaction.set_rollback(True)
        self.stdout.write(f"Changed: {changed}; locked, left alone: {locked}; active listings still without an area: "
                          f"{left}" + (" (dry run, nothing saved)" if options["dry_run"] else ""))
