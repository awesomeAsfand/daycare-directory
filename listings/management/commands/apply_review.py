"""
python manage.py apply_review --file scraper/details/abu_dhabi_review.csv --dry-run
python manage.py apply_review --file scraper/details/abu_dhabi_review.csv

Apply a city's reviewed review CSV (one row per listing, by id):
  decision "remove"  -> listing switched off (is_active=False)
  decision "keep"    -> email, and the nursery details (ages, curriculum,
                        licence) loaded as confirmed, so they show on the site
Any other decision (e.g. "check") stops the command: decide every row first.
Ages are text ("45 days", "6 months", "4 years"), curriculum names are
separated by ";". Empty cells are left as they are. Listings whose details
are already confirmed keep them; the email is still filled if empty.
"""
import csv
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction

from listings.models import REGULATOR_CHOICES, DaycareListing

from .apply_details import parse_age, parse_curriculum


def read_rows(path):
    regulators = {key for key, _ in REGULATOR_CHOICES}
    rows = []
    with path.open(encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            decision = row["decision"].strip().lower()
            if decision not in ("keep", "remove"):
                raise CommandError(f"{path.name} line {n}: decision {row['decision']!r} (use keep or remove)")
            email = row["email"].strip().lower()
            licensed_by = row["licensed_by"].strip()
            try:
                if email:
                    validate_email(email)
                if licensed_by and licensed_by not in regulators:
                    raise ValueError(f"licensed_by {licensed_by!r}")
                details = {
                    "age_from_months": parse_age(row["ages_from"]),
                    "age_to_months": parse_age(row["ages_to"]),
                    "curriculum": parse_curriculum(row["curriculum"]),
                    "licensed_by": licensed_by,
                }
            except (ValueError, ValidationError) as e:
                raise CommandError(f"{path.name} line {n}: {e}")
            rows.append({"id": int(row["id"]), "name": row["name"], "decision": decision, "email": email,
                         "source": row["details_source"].strip(),
                         "details": {k: v for k, v in details.items() if v not in (None, "", [])}})
    return rows


class Command(BaseCommand):
    help = "Apply a reviewed city review CSV: switch off removed listings, load emails and confirmed details"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        rows = read_rows(path)
        listings = DaycareListing.objects.in_bulk([r["id"] for r in rows])
        missing = [r for r in rows if r["id"] not in listings]
        for r in missing:
            self.stdout.write(f"  [missing] {r['id']} {r['name']} (deleted?)")

        switched_off = emails = details = kept_confirmed = 0
        with transaction.atomic():
            for r in rows:
                listing = listings.get(r["id"])
                if listing is None:
                    continue
                if listing.name != r["name"]:
                    self.stdout.write(f"  [renamed] {r['id']}: CSV {r['name']!r}, now {listing.name!r}")
                changed = []
                if r["decision"] == "remove":
                    if listing.is_active:
                        listing.is_active = False
                        changed.append("is_active")
                        switched_off += 1
                else:
                    if r["email"] and listing.email != r["email"]:
                        listing.email = r["email"]
                        changed.append("email")
                        emails += 1
                    if r["details"] and listing.details_confirmed:
                        kept_confirmed += 1
                    elif r["details"]:
                        for field, value in r["details"].items():
                            setattr(listing, field, value)
                        listing.details_source = r["source"] or listing.website
                        listing.details_checked = date.today()
                        listing.details_confirmed = True
                        changed += [*r["details"], "details_source", "details_checked", "details_confirmed"]
                        details += 1
                if changed:
                    listing.save(update_fields=changed + ["updated_at"])
            if options["dry_run"]:
                transaction.set_rollback(True)

        self.stdout.write(
            f"Switched off: {switched_off}; emails set: {emails}; details loaded (confirmed): {details}; "
            f"details already confirmed, left as they were: {kept_confirmed}; missing: {len(missing)}"
            + (" (dry run, nothing saved)" if options["dry_run"] else ""))
