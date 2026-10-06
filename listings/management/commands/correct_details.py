"""
python manage.py correct_details --details scraper/details/dubai_details.csv \
    --corrections "scraper/details/dubai_wrong_info - dubai_wrong_info.csv.csv" --dry-run

Apply a reviewed list of corrections to a details CSV (from draft_details).
The corrections file has one line per nursery and field:
    listing_ids, name, areas, field, current_value, correct_value,
    what_the_site_says, page_checked, note
field is ages_from, ages_to, curriculum, fees_from_aed, fees_to_aed,
"fees (from/to + note)" or status ("closed" removes the row).

Each current_value is checked against the CSV first; a mismatch stops the
run before anything is written. Rows that cover several nurseries (chains
sharing a website) are split into one row per nursery when a correction
touches them, so a correction for one branch doesn't change the others.
Every change is printed and saved next to the CSV as *_corrections.log.
"""
import csv
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from listings.models import DaycareListing

# A starting age of "birth" or "0" is recorded as 45 days: the youngest age
# UAE nurseries may take (decided 2026-10-06)
BIRTH_RE = re.compile(r"^(0|birth)\b|\(0\)|babies", re.I)


def norm_age(value):
    value = value.strip()
    return "45 days" if BIRTH_RE.search(value) else value


def norm_curriculum(value):
    value = value.strip()
    if value.startswith("("):            # "(no single curriculum)"
        return ""
    return "; ".join(p.strip() for p in value.split(";") if p.strip())


def same(a, b):
    return re.sub(r"\s+", " ", a).strip().lower() == re.sub(r"\s+", " ", b).strip().lower()


class Command(BaseCommand):
    help = "Apply a corrections file to a nursery details CSV"

    def add_arguments(self, parser):
        parser.add_argument("--details", required=True)
        parser.add_argument("--corrections", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        details, corrections = Path(options["details"]), Path(options["corrections"])
        with details.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns, rows = reader.fieldnames, list(reader)
        with corrections.open(encoding="utf-8-sig") as f:
            fixes = list(csv.DictReader(f))

        # Split every row a correction touches into one row per nursery
        touched = {i for fix in fixes for i in fix["listing_ids"].split()}
        listings = {str(l.pk): l for l in DaycareListing.objects.select_related("area")
                    .filter(pk__in=[int(i) for i in touched])}
        split_rows = []
        for row in rows:
            ids = row["listing_ids"].split()
            if len(ids) > 1 and touched & set(ids):
                for i in ids:
                    l = listings.get(i) or DaycareListing.objects.select_related("area").get(pk=int(i))
                    split_rows.append(dict(row, listing_ids=i, names=l.name, areas=l.area.name if l.area else ""))
            else:
                split_rows.append(row)
        by_id = {row["listing_ids"]: row for row in split_rows if len(row["listing_ids"].split()) == 1}

        log, problems, removed = [], [], set()
        for n, fix in enumerate(fixes, 2):
            field, current, correct = fix["field"].strip(), fix["current_value"].strip(), fix["correct_value"].strip()
            for i in fix["listing_ids"].split():
                row = by_id.get(i)
                if row is None:
                    problems.append(f"line {n}: listing {i} not in {details.name}")
                    continue
                changes = self.change(row, field, current, correct, fix)
                if isinstance(changes, str):
                    problems.append(f"line {n} ({row['names'][:40]}): {changes}")
                    continue
                for col, old, new in changes:
                    row[col] = new
                    log.append(f"{i:>4} {row['names'][:45]:45} {col}: {old!r} -> {new!r}")
                if field == "status":
                    removed.add(i)
                else:
                    row["source"] = fix["page_checked"] or row["source"]
                    row["check"] = "corrected by hand"

        if problems:
            raise CommandError("Nothing written; corrections that don't match the CSV:\n  " + "\n  ".join(problems))
        out_rows = [r for r in split_rows if r["listing_ids"] not in removed]
        for line in log:
            self.stdout.write(line)
        self.stdout.write(f"\n{len(log)} changes, {len(split_rows) - len(rows)} rows added by splitting chains, "
                          f"{len(removed)} removed; {len(out_rows)} rows" + (" (dry run)" if options["dry_run"] else ""))
        if not options["dry_run"]:
            with details.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                writer.writerows(out_rows)
            details.with_name(details.stem + "_corrections.log").write_text("\n".join(log) + "\n", encoding="utf-8")

    def change(self, row, field, current, correct, fix):
        """[(column, old, new)] for one correction, or a string saying why it doesn't apply."""
        if field in ("ages_from", "ages_to"):
            if same(row[field], norm_age(correct)):
                return []                      # already correct
            if not same(row[field], current):
                return f"{field} is {row[field]!r}, not {current!r}"
            return [(field, row[field], norm_age(correct))]
        if field == "curriculum":
            if {p.strip().lower() for p in row["curriculum"].split(";") if p.strip()} != \
                    {p.strip().lower() for p in current.split(";") if p.strip()}:
                return f"curriculum is {row['curriculum']!r}, not {current!r}"
            return [("curriculum", row["curriculum"], norm_curriculum(correct))]
        if field in ("fees_from_aed", "fees_to_aed"):
            if row[field].strip() != current:
                return f"{field} is {row[field]!r}, not {current!r}"
            return [(field, row[field], correct)]
        if field.startswith("fees"):          # "fees (from/to + note)": "30513-49689 (2026-27)"
            # Amounts only, not the year in brackets
            old = re.findall(r"\d{4,}", re.sub(r"\(.*?\)", "", current))
            new = re.findall(r"\d{4,}", re.sub(r"\(.*?\)", "", correct))
            if old != [row["fees_from_aed"], row["fees_to_aed"]] or len(new) < 2:
                return f"fees are {row['fees_from_aed']}-{row['fees_to_aed']}, not {current!r}"
            year = re.search(r"\((\d{4}-\d{2})\)", correct)
            note = f"{fix['what_the_site_says'].strip()}; yearly = term fee x 3" + (f" ({year.group(1)})" if year else "")
            return [("fees_from_aed", row["fees_from_aed"], new[0]), ("fees_to_aed", row["fees_to_aed"], new[1]),
                    ("fees_note", row["fees_note"], note[:200])]
        if field == "status":
            return [("check", row["check"], f"REMOVED: {correct}")]
        return f"unknown field {field!r}"
