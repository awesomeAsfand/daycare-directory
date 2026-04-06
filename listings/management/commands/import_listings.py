"""
python manage.py import_listings
python manage.py import_listings --file scraper/daycare_listings.json --city Islamabad
"""
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify
from listings.models import DaycareListing, City, Area


class Command(BaseCommand):
    help = "Import scraped daycare listings from a JSON file"

    def add_arguments(self, parser):
        parser.add_argument("--file", default="scraper/daycare_listings.json",
                            help="Path to JSON file produced by the scraper")
        parser.add_argument("--city", default="Islamabad",
                            help="City name to assign listings to")
        parser.add_argument("--dry-run", action="store_true",
                            help="Parse and validate without writing to database")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        self.stdout.write(f"Loaded {len(raw)} records from {path}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes."))

        city, _ = City.objects.get_or_create(
            slug=slugify(options["city"]),
            defaults={"name": options["city"]},
        )

        created = updated = skipped = 0

        for item in raw:
            name = item.get("name", "").strip()
            if not name:
                skipped += 1
                continue

            # Resolve area
            area = None
            area_name = item.get("area", "").strip()
            if area_name and not options["dry_run"]:
                area, _ = Area.objects.get_or_create(
                    city=city,
                    slug=slugify(area_name),
                    defaults={"name": area_name},
                )

            defaults = {
                "name":         name,
                "slug":         slugify(name),
                "area":         area,
                "address":      item.get("address", ""),
                "phone":        item.get("phone", ""),
                "website":      item.get("website", ""),
                "rating":       float(item.get("rating") or 0),
                "review_count": int(item.get("review_count") or 0),
                "latitude":     float(item.get("latitude") or 0),
                "longitude":    float(item.get("longitude") or 0),
                "place_id":     item.get("place_id", ""),
                "categories":   item.get("categories", []),
                "hours":        item.get("hours", {}),
                "description":  item.get("description", ""),
                "maps_url":     item.get("google_maps_url", ""),
            }

            if options["dry_run"]:
                self.stdout.write(f"  [dry] {name} | {area_name}")
                continue

            place_id = item.get("place_id", "")
            if place_id:
                obj, is_new = DaycareListing.objects.update_or_create(
                    place_id=place_id, city=city, defaults=defaults
                )
            else:
                obj, is_new = DaycareListing.objects.update_or_create(
                    slug=slugify(name), city=city, defaults=defaults
                )

            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone: {created} created, {updated} updated, {skipped} skipped."
            )
        )
