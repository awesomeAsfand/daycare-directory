"""Reading the nursery details written in review CSVs (apply_review, apply_extra_details)."""
import re

from .models import CURRICULA


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
