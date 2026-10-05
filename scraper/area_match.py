"""
Put a listing in one of the areas from a queries file ([areas] section).

match() tries, in order:
  1. with "sectors = cda" in [area_match] (Islamabad), a CDA sector in the
     address, in any of Google's spellings (F-7/4, F 7/4, F-7-1, F7), rolled
     up to the main sector (F-7); the sub-sector is kept separately so the
     listing can show "F-7/4, F-7". Off elsewhere: Dubai addresses are full
     of shop and block numbers like "Shop G-12" that look like sectors
  2. an area name or one of its [aliases] in the address
  3. the same two checks on the listing's name ("Daffodils School Tarnol")
  4. the nearest area position ([area_match] centres CSV) to the map pin,
     if within [area_match] max_km
Otherwise the listing gets no area.

A sector that the address names but that isn't in [areas] (e.g. F-5, removed
as mostly government land) gives no area rather than a guess from the map;
the import report lists these so the area can be added if needed.
"""
import csv
import math
import re
from dataclasses import dataclass

from search_plan import QueryPlan

# Sector: one of B-I (there is no A series; "A-8" is a block in Arsalan
# Town), then the number, then optionally the sub-sector. Capital letter
# only, not preceded/followed by letters or digits.
SECTOR_RE = re.compile(r"(?<![A-Za-z0-9])([B-I])\s?-?\s?(\d{1,2})(?:\s?[/-]\s?([1-4]))?(?![0-9])")
# Real CDA sectors are numbered 5-18 (D-12, E-7 ... I-16, B-17)
SECTOR_NUMBERS = range(5, 19)
# Google plus codes ("G5C5+939", "MXCJ+4C6") contain letter-digit pairs that
# look like sectors, so they are removed before matching
PLUS_CODE_RE = re.compile(r"\b[2-9CFGHJMPQRVWX]{4,8}\+[2-9CFGHJMPQRVWX]{0,3}\b")


@dataclass
class AreaMatch:
    area: str = ""        # name exactly as in [areas], or "" for no area
    sub_area: str = ""    # e.g. "F-7/4"
    method: str = ""      # "sector", "name", "map pin" or ""
    unlisted_sector: str = ""   # sector named in the address but not in [areas]


def _name_pattern(name: str) -> re.Pattern:
    """Match a name ignoring case, spaces and hyphens, on word boundaries.

    One stray comma between words is allowed too ("Diplomatic, Enclave").
    """
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-]*,?[\s\-]*".join(map(re.escape, tokens)) + r"(?![a-z0-9])")


def distance_km(lat1, lng1, lat2, lng2):
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


class AreaMatcher:
    def __init__(self, plan: QueryPlan):
        self.areas = list(plan.areas)
        self.match_sectors = plan.area_match.get("sectors", "").lower() == "cda"
        self.sectors = ({a for a in self.areas if re.fullmatch(r"[B-I]-\d{1,2}", a)}
                        if self.match_sectors else set())

        # Names and aliases, longest first, so "National Police Foundation O-9"
        # wins over "Police Foundation" and "Gulberg Residencia" over "Gulberg"
        names = {a: a for a in self.areas if a not in self.sectors}
        names.update(plan.aliases)
        # An alias spelled like a sector sends that sector to a named area:
        # "G-5 = Diplomatic Enclave"
        self.sector_aliases = {s: a for s, a in plan.aliases.items() if re.fullmatch(r"[B-I]-\d{1,2}", s)}
        self.names = sorted(((_name_pattern(spelling), area) for spelling, area in names.items()),
                            key=lambda p: -len(p[0].pattern))

        self.centres = {}
        self.max_km = float(plan.area_match.get("max_km", 2.0))
        centres = plan.area_match.get("centres")
        if centres and plan.path:
            with (plan.path.parent / centres).open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["lat"] and row["lng"] and row["area"] in self.areas:
                        self.centres[row["area"]] = (float(row["lat"]), float(row["lng"]))

    def find_sector(self, address: str) -> tuple[str, str]:
        """(main sector, sub-sector) from the address, e.g. ("F-7", "F-7/4").

        Google often repeats the sector ("F-7/4 F 7/4 F-7"); the mention with
        a sub-sector wins, otherwise the last one.
        """
        found = []
        for letter, number, sub in SECTOR_RE.findall(PLUS_CODE_RE.sub(" ", address)):
            if int(number) in SECTOR_NUMBERS:
                found.append((f"{letter}-{int(number)}", f"{letter}-{int(number)}/{sub}" if sub else ""))
        if not found:
            return "", ""
        with_sub = [f for f in found if f[1]]
        return with_sub[0] if with_sub else found[-1]

    def match_text(self, text: str) -> AreaMatch | None:
        """Sector or area name in a piece of text, or None."""
        sector, sub = self.find_sector(text) if self.match_sectors else ("", "")
        if sector in self.sectors:
            return AreaMatch(sector, sub, "sector")
        if sector in self.sector_aliases:
            return AreaMatch(self.sector_aliases[sector], sub, "sector")
        if sector:
            return AreaMatch("", sub, "", unlisted_sector=sector)
        lowered = text.lower()
        for pattern, area in self.names:
            if pattern.search(lowered):
                return AreaMatch(area, "", "name")
        return None

    def match(self, address: str, lat: float = 0, lng: float = 0, name: str = "") -> AreaMatch:
        found = self.match_text(address or "")
        if found:
            return found
        found = self.match_text(name or "")
        if found and found.area:
            found.method = f"listing name ({found.method})"
            return found

        if lat and lng and self.centres:
            area, (clat, clng) = min(self.centres.items(),
                                     key=lambda kv: distance_km(lat, lng, *kv[1]))
            if distance_km(lat, lng, clat, clng) <= self.max_km:
                return AreaMatch(area, "", "map pin")
        return AreaMatch()
