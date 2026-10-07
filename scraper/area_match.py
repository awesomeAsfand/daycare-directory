"""
Put a listing in one of the areas from a queries file ([areas] section).

match() tries, in order:
  1. with "sectors = cda" in [area_match] (Islamabad), a CDA sector in the
     address, in any of Google's spellings (F-7/4, F 7/4, F-7-1, F7), rolled
     up to the main sector (F-7); the sub-sector is kept separately so the
     listing can show "F-7/4, F-7". Off elsewhere: Dubai addresses are full
     of shop and block numbers like "Shop G-12" that look like sectors
  2. an area name or one of its [aliases] in the address, ignoring
     [not_areas] phrases ("Jumeirah Beach Road"). Several names can appear:
     in comma-style (Pakistan) addresses the longest name wins; in " - "
     style (UAE) addresses, where Google ends with the community people
     know ("Jabal Ali First - The Gardens - Dubai"), the last one does,
     unless an earlier mention is a longer spelling of it that is another
     area ("Al Barsha South Third - Al Barsha South": Arjan).
     With "numbered = yes" in [area_match] (Dubai), a number or ordinal
     after the name becomes the sub-area: "Al Barsha 1" and "Al Barsha
     First" both give area Al Barsha, sub-area "Al Barsha 1"
  3. the same two checks on the listing's name ("Daffodils School Tarnol")
  4. the nearest area position ([area_match] centres CSV) to the map pin,
     if within [area_match] max_km
Otherwise the listing gets no area.

A sector that the address names but that isn't in [areas] (e.g. F-5, removed
as mostly government land) gives no area rather than a guess from the map;
the import report lists these so the area can be added if needed.
"""
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from search_plan import QueryPlan, distance_km, load_area_centres

# Sector: one of B-I (there is no A series; "A-8" is a block in Arsalan
# Town), then the number, then optionally the sub-sector. Capital letter
# only, not preceded/followed by letters or digits.
SECTOR_RE = re.compile(r"(?<![A-Za-z0-9])([B-I])\s?-?\s?(\d{1,2})(?:\s?[/-]\s?([1-4]))?(?![0-9])")
# Real CDA sectors are numbered 5-18 (D-12, E-7 ... I-16, B-17)
SECTOR_NUMBERS = range(5, 19)
# Google plus codes ("G5C5+939", "MXCJ+4C6") contain letter-digit pairs that
# look like sectors, so they are removed before matching
PLUS_CODE_RE = re.compile(r"\b[2-9CFGHJMPQRVWX]{4,8}\+[2-9CFGHJMPQRVWX]{0,3}\b")
# Number right after an area name: "Al Barsha 1", "Umm Suqeim Second".
# Single digits 1-6 only, so "Dubai Marina 23" (a building) isn't one.
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}
NUMBER_AFTER_RE = re.compile(r"\s([1-6])(?![\d/])|\s(" + "|".join(ORDINALS) + r")(?![a-z])")


@dataclass
class AreaMatch:
    area: str = ""        # name exactly as in [areas], or "" for no area
    sub_area: str = ""    # e.g. "F-7/4"
    method: str = ""      # "sector", "name", "map pin" or ""
    unlisted_sector: str = ""   # sector named in the address but not in [areas]


def _name_pattern(name: str) -> re.Pattern:
    """Match a name ignoring case, spaces, hyphens and apostrophes, on word
    boundaries ("Al Fou'ah" = "Al Fouah").

    One stray comma between words is allowed too ("Diplomatic, Enclave").
    """
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    sep = r"[\s\-'’‘`]*,?[\s\-'’‘`]*"
    return re.compile(r"(?<![a-z0-9])" + sep.join(map(re.escape, tokens)) + r"(?![a-z0-9])")


class AreaMatcher:
    def __init__(self, plan: QueryPlan):
        self.areas = list(plan.areas)
        self.match_sectors = plan.area_match.get("sectors", "").lower() == "cda"
        self.numbered = plan.area_match.get("numbered", "").lower() in ("yes", "on", "true")
        self.sectors = ({a for a in self.areas if re.fullmatch(r"[B-I]-\d{1,2}", a)}
                        if self.match_sectors else set())

        # Names and aliases, longest first, so "National Police Foundation O-9"
        # wins over "Police Foundation" and "Gulberg Residencia" over "Gulberg"
        names = {a: a for a in self.areas if a not in self.sectors}
        names.update(plan.aliases)
        # An alias spelled like a sector sends that sector to a named area:
        # "G-5 = Diplomatic Enclave"
        self.sector_aliases = {s: a for s, a in plan.aliases.items() if re.fullmatch(r"[B-I]-\d{1,2}", s)}
        self.names = sorted(((_name_pattern(spelling), area, spelling) for spelling, area in names.items()),
                            key=lambda p: -len(p[0].pattern))

        self.not_areas = [_name_pattern(phrase) for phrase in plan.not_areas]
        # Big sectors Google names after the district ("Al Ramla - Halwan"):
        # a district in the same address, or in the listing's name, wins
        self.parents = {p.strip() for p in plan.area_match.get("parents", "").split(",") if p.strip()}

        self.max_km = float(plan.area_match.get("max_km", 2.0))
        self.centres = load_area_centres(plan)

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
        # [not_areas] phrases ("Jumeirah Beach Road") are blanked out, keeping
        # the positions of everything else
        for pattern in self.not_areas:
            lowered = pattern.sub(lambda m: " " * len(m.group()), lowered)
        found = []   # (end, length, area, text) of every name mention
        for pattern, area, _ in self.names:
            for m in pattern.finditer(lowered):
                found.append((m.end(), m.end() - m.start(), area, m.group()))
        if not found:
            return None
        if " - " in text:
            # UAE style: the last mention wins; of names ending at the same
            # place ("Jumeirah Village Circle" / "Village Circle"), the longest.
            # A parent sector loses to any other area named.
            districts = [f for f in found if f[2] not in self.parents]
            last = max(districts or found)
            area = last[2]
            # ...unless an earlier mention is a more specific spelling of it
            # that is another area: Google writes "Al Barsha South Third -
            # Al Barsha South" for Arjan (Al Barsha South Third = Arjan)
            specific = [f for f in found if f[2] != area and len(f[3]) > len(last[3])
                        and f[3].startswith(last[3])]
            if specific:
                area = max(specific)[2]
        else:
            # Comma style: the longest name wins, e.g. "National Police
            # Foundation O-9" over "Police Foundation"
            area = max(found, key=lambda f: f[1])[2]
        return AreaMatch(area, self.number_after(lowered, area) if self.numbered else "", "name")

    def number_after(self, lowered: str, area: str) -> str:
        """Sub-area from a number after any spelling of the area, or "".

        A spelling variant takes the area's name ("Al Qouz 3" -> "Al Quoz 3");
        an alias that is another name keeps its own ("Wadi Al Safa 3", in
        Dubailand, rather than "Dubailand 3").
        """
        for pattern, name_area, spelling in self.names:
            if name_area != area:
                continue
            for m in pattern.finditer(lowered):
                n = NUMBER_AFTER_RE.match(lowered, m.end())
                if n:
                    variant = SequenceMatcher(None, spelling.lower(), area.lower()).ratio() >= 0.75
                    return f"{area if variant else spelling} {n.group(1) or ORDINALS[n.group(2)]}"
        return ""

    def match(self, address: str, lat: float = 0, lng: float = 0, name: str = "") -> AreaMatch:
        found = self.match_text(address or "")
        if found and found.area in self.parents:
            # "Magic Kids Nursery, Al Taawun" at "... - Al Khalidiya District"
            in_name = self.match_text(name or "")
            if in_name and in_name.area and in_name.area not in self.parents:
                in_name.method = f"listing name ({in_name.method})"
                return in_name
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
