"""
python manage.py apply_details --file scraper/details/dubai_details.csv --dry-run
python manage.py apply_details --file scraper/details/dubai_details.csv
python manage.py apply_details --file ... --confirm    # the CSV was reviewed: show on the site

Load reviewed nursery details into listings as unconfirmed, for checking in
the admin (Nursery details filter: "Found, not confirmed yet"), or confirmed
straight away with --confirm.

The CSV is the one draft_details writes, reviewed in a spreadsheet: one row
per website, with ages as text ("45 days", "5 years"), curriculum as names
separated by ";" ("British (EYFS); Montessori"), licensed_by ("KHDA") and
fees in AED a year. Delete a row, or clear a cell, to leave it out.

A .json file works too: a list of records, one per website:
    {"url": ..., "listing_ids": [...], "source": "<page the details came from>",
     "age_from_months": 1.5, "age_to_months": 60, "curriculum": ["eyfs"],
     "licensed_by": "KHDA", "fees_from_aed": null, "fees_to_aed": null, "fees_note": ""}
Missing or null values are left as they are. Listings whose details are
already confirmed are skipped, so checked details are never overwritten.
"""
import csv
import json
import re
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from listings.models import CURRICULA, REGULATOR_CHOICES, DaycareListing

FIELDS = ["age_from_months", "age_to_months", "curriculum", "licensed_by",
          "fees_from_aed", "fees_to_aed", "fees_note"]


def parse_age(text):
    """"45 days" -> 1.5, "6 months" -> 6, "2.5 years" -> 30; "" -> None."""
    text = text.strip().lower()
    if not text:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(days?|months?|years?)", text)
    if not m:
        raise ValueError(f"age {text!r}: write e.g. 45 days, 6 months or 5 years")
    n, unit = float(m.group(1)), m.group(2)
    return round(n / 30, 2) if unit.startswith("day") else n if unit.startswith("month") else n * 12


def parse_curriculum(text):
    """"British (EYFS); Montessori" (names or keys) -> ["eyfs", "montessori"]."""
    by_name = {v.lower(): k for k, v in CURRICULA.items()} | {k: k for k in CURRICULA}
    keys = []
    for part in filter(None, (p.strip().lower() for p in text.split(";"))):
        if part not in by_name:
            raise ValueError(f"curriculum {part!r}: use one of {', '.join(CURRICULA.values())}")
        keys.append(by_name[part])
    return keys


def csv_records(path):
    records = []
    with path.open(encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            try:
                records.append({
                    "url": row["website"], "listing_ids": [int(i) for i in row["listing_ids"].split()],
                    "source": row["source"],
                    "age_from_months": parse_age(row["ages_from"]), "age_to_months": parse_age(row["ages_to"]),
                    "curriculum": parse_curriculum(row["curriculum"]),
                    "licensed_by": row["licensed_by"].strip(),
                    "fees_from_aed": int(float(row["fees_from_aed"])) if row["fees_from_aed"].strip() else None,
                    "fees_to_aed": int(float(row["fees_to_aed"])) if row["fees_to_aed"].strip() else None,
                    # "not published" notes only make sense next to fees
                    "fees_note": row["fees_note"].strip() if row["fees_from_aed"].strip() else "",
                })
            except ValueError as e:
                raise CommandError(f"{path.name} line {n}: {e}")
    return records


class Command(BaseCommand):
    help = "Load reviewed nursery details (ages, curriculum, licence, fees) as unconfirmed"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--confirm", action="store_true",
                            help="Mark the details confirmed (shown on the site): use after reviewing the CSV")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        if path.suffix.lower() == ".csv":
            records = csv_records(path)
        else:
            records = json.loads(path.read_text(encoding="utf-8"))
        regulators = {key for key, _ in REGULATOR_CHOICES}
        updated = skipped_confirmed = 0
        for rec in records:
            values = {f: rec[f] for f in FIELDS if rec.get(f) not in (None, "", [])}
            unknown = set(values.get("curriculum", [])) - set(CURRICULA)
            if unknown or values.get("licensed_by", "KHDA") not in regulators:
                raise CommandError(f"{rec['url']}: unknown curriculum {sorted(unknown)} "
                                   f"or regulator {values.get('licensed_by')!r}")
            if not values:
                continue
            for listing in DaycareListing.objects.filter(pk__in=rec["listing_ids"]):
                if listing.details_confirmed:
                    skipped_confirmed += 1
                    continue
                for field, value in values.items():
                    setattr(listing, field, value)
                listing.details_source = rec.get("source") or rec["url"]
                listing.details_checked = date.today()
                listing.details_confirmed = options["confirm"]
                updated += 1
                if not options["dry_run"]:
                    listing.save()
        self.stdout.write(f"Listings updated: {updated}; skipped (details already confirmed): {skipped_confirmed}"
                          + (" (dry run)" if options["dry_run"] else ""))
