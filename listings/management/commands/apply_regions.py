"""
python manage.py apply_regions --file scraper/regions/uae_area_regions.csv --dry-run
python manage.py apply_regions --file scraper/regions/uae_area_regions.csv

Loads the neighbourhood groups shown on city pages ("Jumeirah & Al Barsha").
CSV columns: city, area, region (others, such as listings, are ignored).
A blank region clears it; leave all of a city's areas blank for a plain A–Z
list. Areas of the CSV's cities that aren't in the file are reported.
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from listings.models import Area, City


class Command(BaseCommand):
    help = "Load area regions (neighbourhood groups) from a CSV"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows and not {"city", "area", "region"} <= set(rows[0]):
            raise CommandError("The CSV needs city, area and region columns")

        changed, missing, seen = 0, [], set()
        with transaction.atomic():
            for r in rows:
                area = Area.objects.filter(city__slug=slugify(r["city"]), slug=slugify(r["area"])).first()
                if not area:
                    missing.append(f"{r['city']} / {r['area']}")
                    continue
                seen.add(area.pk)
                region = r["region"].strip()
                if area.region != region:
                    self.stdout.write(f"  {area.city.name} / {area.name}: {area.region or '-'} -> {region or '-'}")
                    area.region = region
                    area.save(update_fields=["region"])
                    changed += 1
            if options["dry_run"]:
                transaction.set_rollback(True)

        cities = City.objects.filter(slug__in={slugify(r["city"]) for r in rows})
        not_in_file = Area.objects.filter(city__in=cities).exclude(pk__in=seen)
        for area in not_in_file:
            self.stdout.write(self.style.WARNING(f"  not in the file: {area.city.name} / {area.name}"))
        for m in missing:
            self.stdout.write(self.style.WARNING(f"  no such area: {m}"))
        self.stdout.write(f"Changed: {changed}; not in the file: {not_in_file.count()}; no such area: {len(missing)}"
                          + (" (dry run, nothing saved)" if options["dry_run"] else ""))
