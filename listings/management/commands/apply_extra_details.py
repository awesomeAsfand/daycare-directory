"""
python manage.py apply_extra_details --file scraper/details/competitor_review.csv --dry-run
python manage.py apply_extra_details --file scraper/details/competitor_review.csv

Load a reviewed CSV of extra details, one row per listing (listing_id):
  decision "keep"   -> the values below are loaded
  decision "remove" -> the listing is switched off (nothing else changes)
  decision "skip"   -> the row is ignored
Columns (empty cells are left alone, so nothing is ever cleared):
  curriculum      names separated by ";", replaces ours (the review combines them)
  email           filled when we have none, or replaces ours when email_replace is "yes"
  transport, meals  "yes" or "no", only filled when unknown
  facilities, activities  names separated by ";", added to what we have
  fees_from_aed, fees_to_aed  a yearly range, only filled when we have no fees;
                  saved with the note "approximate" (not the nursery's own fee list)
Listings that gain a curriculum or fees get their details confirmed, so they show.
"""
import csv
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from listings.models import ACTIVITIES, FACILITIES, DaycareListing

from listings.details import parse_curriculum

FEES_NOTE = "approximate"


def parse_labels(text, choices, what):
    """"Art room; Library / reading corner" (names or keys) -> ["art_room", "library"]."""
    by_name = {v.lower(): k for k, v in choices.items()} | {k: k for k in choices}
    keys = []
    for part in filter(None, (p.strip().lower() for p in text.split(";"))):
        if part not in by_name:
            raise ValueError(f"{what} {part!r}: use one of {', '.join(choices.values())}")
        if by_name[part] not in keys:
            keys.append(by_name[part])
    return keys


def parse_yes_no(text, what):
    text = text.strip().lower()
    if text not in ("", "yes", "no"):
        raise ValueError(f"{what} {text!r}: use yes, no or leave it empty")
    return {"yes": True, "no": False}.get(text)


def parse_fees(row):
    lo, hi = (row.get(k, "").strip() for k in ("fees_from_aed", "fees_to_aed"))
    lo = int(float(lo)) if lo else None
    hi = int(float(hi)) if hi else None
    if lo and hi and lo > hi:
        raise ValueError(f"fees {lo} - {hi}: from is more than to")
    return lo, hi


def read_rows(path):
    rows = []
    with path.open(encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            decision = row["decision"].strip().lower()
            if decision == "skip":
                continue
            if decision not in ("keep", "remove"):
                raise CommandError(f"{path.name} line {n}: decision {row['decision']!r} (use keep, remove or skip)")
            if not row["listing_id"].strip():
                raise CommandError(f"{path.name} line {n}: decision {decision} but no listing_id")
            email = row.get("email", "").strip().lower()
            try:
                if email:
                    validate_email(email)
                fees_from, fees_to = parse_fees(row)
                rows.append({
                    "line": n, "id": int(row["listing_id"]), "name": row.get("our_name", ""), "decision": decision,
                    "curriculum": parse_curriculum(row.get("curriculum", "")),
                    "email": email, "email_replace": row.get("email_replace", "").strip().lower() == "yes",
                    "transport": parse_yes_no(row.get("transport", ""), "transport"),
                    "meals": parse_yes_no(row.get("meals", ""), "meals"),
                    "facilities": parse_labels(row.get("facilities", ""), FACILITIES, "facility"),
                    "activities": parse_labels(row.get("activities", ""), ACTIVITIES, "activity"),
                    "fees_from": fees_from, "fees_to": fees_to,
                })
            except (ValueError, ValidationError) as e:
                raise CommandError(f"{path.name} line {n}: {e}")
    ids = [r["id"] for r in rows]
    twice = {i for i in ids if ids.count(i) > 1}
    if twice:
        raise CommandError(f"{path.name}: listings on more than one row: {sorted(twice)}")
    return rows


def apply_row(listing, r):
    """Set the row's values on the listing; returns the changed field names."""
    changed = []
    if r["decision"] == "remove":
        if listing.is_active:
            listing.is_active = False
            changed.append("is_active")
        return changed

    if r["curriculum"] and listing.curriculum != r["curriculum"]:
        listing.curriculum = r["curriculum"]
        changed.append("curriculum")
    if r["email"] and listing.email != r["email"] and (not listing.email or r["email_replace"]):
        listing.email = r["email"]
        changed.append("email")
    for field in ("transport", "meals"):
        if r[field] is not None and getattr(listing, field) is None:
            setattr(listing, field, r[field])
            changed.append(field)
    for field, order in (("facilities", FACILITIES), ("activities", ACTIVITIES)):
        have = list(getattr(listing, field) or [])
        merged = [k for k in order if k in have or k in r[field]] + [k for k in have if k not in order]
        if merged != have:
            setattr(listing, field, merged)
            changed.append(field)
    if (r["fees_from"] or r["fees_to"]) and not (listing.fees_from_aed or listing.fees_to_aed):
        listing.fees_from_aed, listing.fees_to_aed = r["fees_from"], r["fees_to"]
        listing.fees_note = FEES_NOTE
        changed += ["fees_from_aed", "fees_to_aed", "fees_note"]
    if {"curriculum", "fees_from_aed"} & set(changed) and not listing.details_confirmed:
        listing.details_confirmed = True
        listing.details_checked = date.today()
        changed += ["details_confirmed", "details_checked"]
    return changed


class Command(BaseCommand):
    help = "Load a reviewed CSV of extra details (curriculum, email, transport, meals, facilities, activities, fee ranges)"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        rows = read_rows(path)
        listings = DaycareListing.objects.in_bulk([r["id"] for r in rows])
        counts = {}
        missing = 0
        with transaction.atomic():
            for r in rows:
                listing = listings.get(r["id"])
                if listing is None:
                    missing += 1
                    self.stdout.write(f"  [missing] {r['id']} {r['name']} (deleted?)")
                    continue
                if r["name"] and listing.name != r["name"]:
                    self.stdout.write(f"  [renamed] {r['id']}: CSV {r['name']!r}, now {listing.name!r}")
                newly_confirmed = not listing.details_confirmed
                changed = apply_row(listing, r)
                if not changed:
                    continue
                listing.save(update_fields=changed + ["updated_at"])
                self.stdout.write(f"  {listing.pk} {listing.name[:50]}: {', '.join(changed)}")
                if newly_confirmed and listing.details_confirmed and listing.age_range_label:
                    self.stdout.write(f"    note: its ages ({listing.age_range_label}) now show too")
                for field in changed:
                    counts[field] = counts.get(field, 0) + 1
            if options["dry_run"]:
                transaction.set_rollback(True)

        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing changed"
        self.stdout.write(f"Rows: {len(rows)}; missing: {missing}. Changed: {summary}"
                          + (" (dry run, nothing saved)" if options["dry_run"] else ""))
