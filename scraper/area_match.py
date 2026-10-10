"""
Put a listing in one of the areas from a queries file ([areas] section).

match() tries, in order:
  1. an area name or one of its [aliases] in the address, ignoring
     [not_areas] phrases ("Jumeirah Beach Road"). Several names can appear:
     in text without " - " (such as a listing name) the longest name wins; in
     Google's " - " style addresses, which end with the community people
     know ("Jabal Ali First - The Gardens - Dubai"), the last one does,
     unless an earlier mention is a longer spelling of it that is another
     area ("Al Barsha South Third - Al Barsha South": Arjan).
     With "numbered = yes" in [area_match] (Dubai), a number or ordinal
     after the name becomes the sub-area: "Al Barsha 1" and "Al Barsha
     First" both give area Al Barsha, sub-area "Al Barsha 1"
  2. the same check on the listing's name ("Magic Kids Nursery, Al Taawun")
  3. the nearest area position ([area_match] centres CSV) to the map pin,
     if within [area_match] max_km
Otherwise the listing gets no area.
"""
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from search_plan import QueryPlan, distance_km, load_area_centres

# Number right after an area name: "Al Barsha 1", "Umm Suqeim Second".
# Single digits 1-6 only, so "Dubai Marina 23" (a building) isn't one.
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}
NUMBER_AFTER_RE = re.compile(r"\s([1-6])(?![\d/])|\s(" + "|".join(ORDINALS) + r")(?![a-z])")


@dataclass
class AreaMatch:
    area: str = ""        # name exactly as in [areas], or "" for no area
    sub_area: str = ""    # e.g. "Al Barsha 1"
    method: str = ""      # "name", "map pin" or ""


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
        self.numbered = plan.area_match.get("numbered", "").lower() in ("yes", "on", "true")

        # Names and aliases, longest first, so "Jumeirah Village Circle" wins
        # over "Jumeirah"
        names = {a: a for a in self.areas}
        names.update(plan.aliases)
        self.names = sorted(((_name_pattern(spelling), area, spelling) for spelling, area in names.items()),
                            key=lambda p: -len(p[0].pattern))

        self.not_areas = [_name_pattern(phrase) for phrase in plan.not_areas]
        # Big sectors Google names after the district ("Al Ramla - Halwan"):
        # a district in the same address, or in the listing's name, wins
        self.parents = {p.strip() for p in plan.area_match.get("parents", "").split(",") if p.strip()}

        self.max_km = float(plan.area_match.get("max_km", 2.0))
        self.centres = load_area_centres(plan)

    def match_text(self, text: str) -> AreaMatch | None:
        """Area name in a piece of text, or None."""
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
            # Otherwise (a listing name, or a comma-separated address) the
            # longest name wins, e.g. "Jumeirah Village Circle" over "Jumeirah"
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
