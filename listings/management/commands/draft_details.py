"""
python manage.py draft_details --city Dubai

Turn the passages collect_details saved into a first draft of each nursery's
details, as a CSV to review in a spreadsheet:
scraper/details/<city>_details.csv, one row per website.

Ages, curriculum and licensing are read by rule (below), each with the
passage it came from. Fees are only flagged ("fees_to_check"): fee tables
lose their headings (per term / per year, days a week) when read as text,
so they are filled in by hand. Load the reviewed CSV with apply_details.
"""
import csv
import json
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from listings.models import CURRICULA, DaycareListing, months_label

UNIT = r"(days?|weeks?|months?|mths|years?|yrs)"
# "45 days to 5 years", "18 months - 3 years", "from 2 years until 6 years"
RANGE_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*{UNIT}\s*(?:old\s*)?(?:to|-|–|until|till|up to)\s*(\d+(?:\.\d+)?)\s*{UNIT}", re.I)
# "1-4 years old", "ages 2–6", "3 to 5 years"
SAME_UNIT_RE = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*{UNIT}?", re.I)
AGES_WORD_RE = re.compile(r"\bages?d?\b", re.I)
# Passages that mention ages but not the nursery's own range
NOT_AGES_RE = re.compile(r"[“”\"]|anxiety|research|study|studies|camp|ratio|review|my (son|daughter)"
                         r"|experience|why (the )?ages|matter", re.I)
# Sentences about who the nursery admits ("we accept children from 45 days
# to 5 years", "Ages: 1 to 5 years") beat single class names ("Toddlers:
# 18 months - 2 years"), which only give part of the range
ADMISSION_RE = re.compile(r"accept|admit|admission|welcom|enrol|cater|open to|^\W*ages?\b|children (aged|from|of)"
                          r"|for children|from (as young as )?\d", re.I)

CURRICULUM_RE = {
    "eyfs": r"\bEYFS\b|early years foundation stage|british (curriculum|early years|eyfs)|\bUK curriculum",
    "montessori": r"montessori",
    "reggio": r"reggio",
    "ib": r"\bIB\b|international baccalaureate|\bPYP\b|primary years programme",
    # "US" case-sensitive, so "About Us" isn't "US curriculum"
    "american": r"american curriculum|creative curriculum|(?-i:\bUS) curriculum|common core",
    "canadian": r"canadian (curriculum|program|programme|approach)|maple bear",
    "french": r"french (curriculum|national|system)|bilingual (french|en ?/ ?fr)|french[- ](and|&|/)[- ]english"
              r"|english[- ](and|&|/)[- ]french|francophone",
    "indian": r"\bCBSE\b|\bICSE\b|indian curriculum",
    "highscope": r"high\s?scope",
    "waldorf": r"waldorf|steiner",           # whole word: not "Liechtensteiner"
    "forest": r"forest school",
    "arabic": r"bilingual\W{0,3}(arabic|english)\W{1,5}(english|arabic)|(arabic|english)\W{1,5}(english|arabic) bilingual"
              r"|bilingual (arabic|english) (and|&) (english|arabic)",
}
# Curricula of the schools children move on to, and quoted reviews, aren't
# the nursery's own: "helping your child transition to IB schools"
NOT_OWN_CURRICULUM_RE = re.compile(r"[“”\"]|transition|move on|prepare[sd]? (them|children|your)|join(ing)? (a|an|the)|schools\b", re.I)
# Approaches that are often mentioned in passing ("Montessori time" on a
# timetable, a blog title, a teacher's Montessori diploma, "many nurseries
# follow Montessori") count only in a sentence about how the nursery teaches
APPROACHES = {"montessori", "reggio", "waldorf", "forest", "highscope"}
APPROACH_CONTEXT_RE = re.compile(r"curricul|approach|method|philosoph|inspired|based|follow|accredit|programme|program\b"
                                 r"|pedagog|framework|principles|practices|blend|combin|integrat|classroom", re.I)
APPROACH_NOT_RE = re.compile(r"\btime\b|diploma|degree|certif|trained|qualifi|toy|blog|many nurseries|other nurseries"
                             r"|some nurseries|unlike|compared|versus|\bvs\b", re.I)
FEE_RE = re.compile(r"\b(aed|dhs)\s*[\d,]{4,}|[\d,]{4,}\s*(aed|dhs)\b", re.I)

COLUMNS = ["listing_ids", "names", "areas", "website", "ages_from", "ages_to", "curriculum",
           "licensed_by", "fees_from_aed", "fees_to_aed", "fees_note", "source", "check", "fees_to_check",
           "evidence_ages", "evidence_curriculum", "evidence_licence"]


def to_months(number, unit):
    n, u = float(number), unit.lower()
    if u.startswith("day"):
        return round(n / 30, 2)
    if u.startswith("week"):
        return round(n / 4.3, 2)
    if u.startswith(("month", "mth")):
        return n
    return n * 12


def age_ranges(text):
    """(from_months, to_months) pairs in a passage: a whole range ("45 days
    to 5 years") or one class's ("Infants: 45 days - 17 months")."""
    if NOT_AGES_RE.search(text):
        return []
    pairs = [(to_months(a, ua), to_months(b, ub)) for a, ua, b, ub in RANGE_RE.findall(text)]
    if not pairs:
        for a, b, unit in SAME_UNIT_RE.findall(text):
            # "3 to 5 years" needs its unit, or "ages"/"years old" nearby
            if unit or AGES_WORD_RE.search(text) or re.search(r"years?\s*old", text, re.I):
                pairs.append((to_months(a, unit or "years"), to_months(b, unit or "years")))
    # Plausible nursery ages only: starting before 4, ending by 7
    return [(lo, hi) for lo, hi in pairs if 0 < lo <= 48 and 3 <= hi <= 84 and lo < hi]


def nursery_ages(snippets, home):
    """(from, to, evidence) for the nursery: from admission sentences if there
    are any, else the span of all its classes; the listing's own page (a
    chain's branch page) before the rest of the site, whose About page may
    describe the whole group. None if nothing plausible."""
    found = [(r, s) for s in snippets for r in age_ranges(s["text"])]
    for pool in ([f for f in found if f[1]["page"] == home], found):
        admission = [f for f in pool if ADMISSION_RE.search(f[1]["text"])]
        chosen = admission or pool
        if chosen:
            lo = min(r[0] for r, _ in chosen)
            hi = max(r[1] for r, _ in chosen)
            if hi >= 24:   # a nursery takes children to at least 2
                evidence = [s for r, s in chosen if r[0] == lo] + [s for r, s in chosen if r[1] == hi]
                return lo, hi, evidence
    return None


class Command(BaseCommand):
    help = "Draft nursery details (ages, curriculum, licence) from collected website passages"

    def add_arguments(self, parser):
        parser.add_argument("--city", required=True)
        parser.add_argument("--output", help="Default: scraper/details/<city>_details.csv")
        parser.add_argument("--force", action="store_true",
                            help="Overwrite an existing CSV (it may hold fees and corrections added by hand)")

    def handle(self, *args, **options):
        city_file = slugify(options["city"]).replace("-", "_")
        folder = Path(settings.BASE_DIR) / "scraper" / "details"
        out = Path(options["output"] or folder / f"{city_file}_details.csv")
        if out.exists() and not options["force"]:
            raise CommandError(f"{out} exists and may hold fees and corrections added by hand. "
                               "Write elsewhere with --output, or replace it with --force.")
        raw = json.loads((folder / f"{city_file}_details_raw.json").read_text(encoding="utf-8"))
        listings = {l.pk: l for l in DaycareListing.objects.select_related("area")}

        rows = []
        for rec in raw:
            row = {c: "" for c in COLUMNS}
            ls = [listings[i] for i in rec["listing_ids"] if i in listings]
            row.update(listing_ids=" ".join(map(str, rec["listing_ids"])), names=" | ".join(rec["names"]),
                       areas=", ".join(sorted({l.area.name for l in ls if l.area})), website=rec["url"])
            if rec["error"]:
                row["fees_note"] = f"website did not load: {rec['error'][:80]}"
            texts = rec["snippets"]

            ages = nursery_ages(texts, rec["pages"][0] if rec["pages"] else "")
            if ages:
                lo, hi, evidence = ages
                # 59 months is "5 years": round older ages to half years
                hi = round(hi / 6) * 6 if hi >= 24 else hi
                lo = round(lo / 6) * 6 if lo >= 24 else lo
                row["ages_from"], row["ages_to"] = months_label(lo), months_label(hi)
                if hi <= 36:
                    row["check"] = "ages may be one class only"
                elif hi > 72:
                    row["check"] = "oldest age over 6"
                row["evidence_ages"] = " // ".join(dict.fromkeys(s["text"][:150] for s in evidence[:2]))
                row["source"] = evidence[0]["page"]

            found = {}
            for key, pattern in CURRICULUM_RE.items():
                hit = next((s for s in texts if re.search(rf"\b(?:{pattern})\b", s["text"], re.I)
                            and not NOT_OWN_CURRICULUM_RE.search(s["text"])
                            and (key not in APPROACHES or (APPROACH_CONTEXT_RE.search(s["text"])
                                                           and not APPROACH_NOT_RE.search(s["text"])))), None)
                if hit:
                    found[key] = hit
            row["curriculum"] = "; ".join(CURRICULA[k] for k in found)
            if len(found) >= 4:
                row["check"] = "; ".join(filter(None, [row["check"], "many curricula: maybe a menu or blog"]))
            row["evidence_curriculum"] = " // ".join(f"{CURRICULA[k]}: {s['text'][:110]}" for k, s in found.items())
            if found and not row["source"]:
                row["source"] = next(iter(found.values()))["page"]

            khda = next((s for s in texts if re.search(r"\bKHDA\b", s["text"])), None)
            if khda:
                row["licensed_by"] = "KHDA"
                row["evidence_licence"] = khda["text"][:150]

            fee_pages = list(dict.fromkeys(s["page"] for s in texts if FEE_RE.search(s["text"])))
            row["fees_to_check"] = " ".join(fee_pages)
            rows.append(row)

        # utf-8-sig so Excel shows Arabic names correctly
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        n = len(rows)
        self.stdout.write(
            f"{n} websites -> {out}\n"
            f"  ages: {sum(bool(r['ages_from']) for r in rows)}, curriculum: {sum(bool(r['curriculum']) for r in rows)}, "
            f"KHDA: {sum(bool(r['licensed_by']) for r in rows)}, fees to check: {sum(bool(r['fees_to_check']) for r in rows)}, "
            f"did not load: {sum(r['fees_note'].startswith('website did not load') for r in rows)}")
