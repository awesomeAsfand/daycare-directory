"""
Search planning and listing filters for gmaps_scraper.py.

A queries file (e.g. queries/dubai.txt) has these sections:

    [country]           country settings: key = value lines
        name = United Arab Emirates
        code = ae                           (ISO code, for OpenStreetMap lookups)
        timezone = Asia/Dubai               (the scraper's browser time zone)
        phone_code = 971                    (to find phone numbers in page text)
    [keywords]          one search keyword per line
    [areas]             one area per line; "(...)" notes are ignored
    [grid]              map-grid sweep: key = value lines
        bbox = south, west, north, east     (default: the boundary's extent)
        step_km = 2.5
        zoom = 14
        keywords = daycare, montessori
        near_areas_km = 4                   (only cells this close to an [area_match] centre)
    [boundary]
        file = islamabad_boundary.geojson   (relative to the queries file)
    [exclude]           areas left out even where they cross the boundary;
                        a place whose address mentions one is rejected
    [aliases]           other spellings: <spelling> = <area as in [areas]>
    [not_areas]         phrases that contain an area name but aren't that
                        area ("Jumeirah Beach Road"): ignored by area matching
    [keep]              places the category and name rules would reject but
                        that belong: <Google place ID> = <name, as a note>
    [area_match]        how import_listings places listings in areas
        centres = islamabad_area_centres.csv
        max_km = 2.0
        sectors = cda   (Islamabad only: read CDA sectors such as F-7/4)
                        (see area_match.py)

Area searches are "<keyword> in <area> <city>". Grid searches run each grid
keyword on a map centred on every cell of the bounding box that touches the
city boundary, which catches places the area names miss.

classify() decides whether a scraped place belongs in the directory:
daycares and standalone preschools / montessori centres inside the city
boundary, but no schools, tuition centres, unrelated businesses or closed
places. Google's addresses are unreliable for this (many Islamabad addresses
never say "Islamabad", some in Bani Gala say "Rawalpindi"), so the boundary
check uses the place's map coordinates.
"""
import csv
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus


# ─── Search plan ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SearchTask:
    key: str                  # unique; recorded in the state file
    query: str                # text searched on Google Maps
    keyword: str = ""
    area: str = ""            # area from the queries file ("" for grid cells)
    center: tuple | None = None   # (lat, lng) for grid cells
    zoom: int = 0

    @property
    def url(self) -> str:
        url = f"https://www.google.com/maps/search/{quote_plus(self.query)}"
        if self.center:
            lat, lng = self.center
            url += f"/@{lat:.5f},{lng:.5f},{self.zoom}z"
        return url

    @property
    def is_grid(self) -> bool:
        return self.center is not None


@dataclass
class QueryPlan:
    city: str
    keywords: list = field(default_factory=list)
    areas: list = field(default_factory=list)
    grid: dict = field(default_factory=dict)
    boundary: list | None = None   # polygons, see load_boundary()
    exclude: list = field(default_factory=list)
    not_areas: list = field(default_factory=list)    # phrases that contain an area name but aren't it
    keep: set = field(default_factory=set)           # place IDs kept whatever the name/category rules say
    aliases: dict = field(default_factory=dict)      # spelling -> area
    area_match: dict = field(default_factory=dict)
    country: dict = field(default_factory=dict)      # [country]: name, code, timezone, phone_code
    path: Path | None = None                         # the queries file


# ─── City boundary ───────────────────────────────────────────────────────────

def load_boundary(path: Path) -> list:
    """GeoJSON Polygon / MultiPolygon -> list of polygons, each a list of rings
    of (lng, lat) points; the first ring is the outline, the rest are holes."""
    data = json.loads(path.read_text(encoding="utf-8"))
    geom = data.get("geometry", data)
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    raise ValueError(f"{path.name}: expected a Polygon or MultiPolygon, got {geom['type']}")


def _in_ring(lat: float, lng: float, ring: list) -> bool:
    # Ray casting
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_boundary(lat: float, lng: float, boundary: list) -> bool:
    return any(_in_ring(lat, lng, poly[0]) and not any(_in_ring(lat, lng, hole) for hole in poly[1:])
               for poly in boundary)


def boundary_bbox(boundary: list) -> tuple:
    points = [p for poly in boundary for p in poly[0]]
    lngs, lats = [p[0] for p in points], [p[1] for p in points]
    return min(lats), min(lngs), max(lats), max(lngs)


def area_label(line: str) -> str:
    """'E-18 (Gulshan-e-Sehat)' -> 'E-18'."""
    return re.sub(r"\s*\(.*?\)", "", line).strip()


def load_plan(path: Path, city: str | None = None) -> QueryPlan:
    """Parse a queries file. The city defaults to the file name (islamabad.txt -> Islamabad)."""
    plan = QueryPlan(city=city or path.stem.replace("_", " ").title(), path=path)
    section = None
    pending_aliases = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            section = m.group(1).lower()
        elif section == "keywords":
            plan.keywords.append(line)
        elif section == "areas":
            area = area_label(line)
            plan.areas.append(area)
            # Notes in brackets are other names: "B-17 (Multi Gardens)"
            for note in re.findall(r"\((.*?)\)", line):
                plan.aliases[note.strip()] = area
        elif section == "aliases":
            spelling, _, area = line.partition("=")
            pending_aliases.append((spelling.strip(), area.strip(), raw))
        elif section == "area_match":
            key, _, value = line.partition("=")
            plan.area_match[key.strip().lower()] = value.strip()
        elif section == "country":
            key, _, value = line.partition("=")
            plan.country[key.strip().lower()] = value.strip()
        elif section == "grid":
            key, _, value = line.partition("=")
            plan.grid[key.strip().lower()] = value.strip()
        elif section == "boundary":
            key, _, value = line.partition("=")
            if key.strip().lower() == "file":
                plan.boundary = load_boundary(path.parent / value.strip())
        elif section == "exclude":
            plan.exclude.append(line)
        elif section == "not_areas":
            plan.not_areas.append(line)
        elif section == "keep":
            # "<place ID> = <name>": the name is only a note
            plan.keep.add(line.partition("=")[0].strip())
        else:
            raise ValueError(f"{path.name}: line outside a [section]: {raw!r}")

    # Checked at the end so [aliases] may come before [areas]
    for spelling, area, raw in pending_aliases:
        if area not in plan.areas:
            raise ValueError(f"{path.name}: alias points to an area not in [areas]: {raw!r}")
        plan.aliases[spelling] = area
    return plan


def grid_cells(bbox: tuple, step_km: float, boundary: list | None = None) -> list[tuple]:
    """Centres of step_km squares covering bbox (south, west, north, east).

    With a boundary, squares that don't touch it (centre, corners and edge
    midpoints all outside) are left out.
    """
    south, west, north, east = bbox
    lat_step = step_km / 111.0
    lng_step = step_km / (111.32 * math.cos(math.radians((south + north) / 2)))
    cells = []
    lat = south + lat_step / 2
    while lat < north:
        lng = west + lng_step / 2
        while lng < east:
            probes = [(lat + dy * lat_step / 2, lng + dx * lng_step / 2)
                      for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
            if boundary is None or any(in_boundary(y, x, boundary) for y, x in probes):
                cells.append((round(lat, 5), round(lng, 5)))
            lng += lng_step
        lat += lat_step
    return cells


def distance_km(lat1, lng1, lat2, lng2):
    # Equirectangular approximation; accurate to metres at city scale
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


def load_area_centres(plan: QueryPlan) -> dict:
    """{area: (lat, lng)} from the [area_match] centres CSV; areas without a
    position, or no longer in [areas], are left out."""
    centres = {}
    name = plan.area_match.get("centres")
    if name and plan.path:
        with (plan.path.parent / name).open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["lat"] and row["lng"] and row["area"] in plan.areas:
                    centres[row["area"]] = (float(row["lat"]), float(row["lng"]))
    return centres


def build_tasks(plan: QueryPlan, areas: list | None = None,
                include_areas: bool = True, include_grid: bool = True) -> list[SearchTask]:
    """Area searches (area by area, every keyword), then the grid sweep.

    `areas` limits the area searches to those names (case-insensitive) and
    skips the grid; unknown names raise ValueError so typos don't go unnoticed.
    """
    selected = plan.areas
    if areas:
        known = {a.lower(): a for a in plan.areas}
        unknown = [a for a in areas if a.lower() not in known]
        if unknown:
            raise ValueError(f"Not in the queries file: {', '.join(unknown)}")
        selected = [known[a.lower()] for a in areas]
        include_grid = False

    tasks = []
    if include_areas:
        for area in selected:
            for kw in plan.keywords:
                tasks.append(SearchTask(
                    key=f"area:{area}|{kw}", query=f"{kw} in {area} {plan.city}",
                    keyword=kw, area=area,
                ))
    if include_grid and plan.grid:
        if "bbox" in plan.grid:
            bbox = tuple(float(x) for x in plan.grid["bbox"].split(","))
        elif plan.boundary:
            bbox = boundary_bbox(plan.boundary)
        else:
            raise ValueError("[grid] needs a bbox or a [boundary] file")
        step = float(plan.grid.get("step_km", 2.5))
        zoom = int(plan.grid.get("zoom", 14))
        keywords = [k.strip() for k in plan.grid.get("keywords", "daycare").split(",") if k.strip()]
        cells = grid_cells(bbox, step, plan.boundary)
        if "near_areas_km" in plan.grid:
            # Only cells near a listed area: skips sea and empty desert that
            # lie inside the boundary (most of Dubai emirate)
            near = float(plan.grid["near_areas_km"])
            centres = list(load_area_centres(plan).values())
            if not centres:
                raise ValueError("[grid] near_areas_km needs [area_match] centres")
            cells = [c for c in cells if min(distance_km(*c, *x) for x in centres) <= near]
        for lat, lng in cells:
            for kw in keywords:
                tasks.append(SearchTask(
                    key=f"grid:{kw}@{lat:.4f},{lng:.4f}", query=kw,
                    keyword=kw, center=(lat, lng), zoom=zoom,
                ))
    return tasks


# ─── Listing filter ──────────────────────────────────────────────────────────

# Google categories (lower-cased) that are in scope on their own
DAYCARE_CATEGORIES = {"day care center", "childcare", "creche"}
PRESCHOOL_CATEGORIES = {
    "preschool", "montessori preschool", "montessori school", "kindergarten",
    "nursery school", "playgroup",
}
# Generic education categories: in scope only when the name says early years
GENERIC_CATEGORIES = {
    "school", "educational institution", "general education school", "education center",
    "school house", "learning center", "private educational institution",
    # In Dubai mostly nanny, maid and babysitting agencies
    "child care agency",
}

EARLY_YEARS_NAME_RE = re.compile(
    r"day\s?care|child\s?care|cr[eè]che|montessori|pre[\s-]?school|nursery|kindergarten"
    r"|play\s?group|toddler|early\s+(?:years|learning|childhood)",
    re.I,
)
DAYCARE_NAME_RE = re.compile(r"day\s?care|child\s?care|cr[eè]che", re.I)
# Names that mark part of a school, whatever Google's category says
# (unless the name also says daycare)
SCHOOL_CHAIN_RE = re.compile(
    r"\b(?:school\s+systems?|systems?|campus|academy|college|grammar|high\s+school|secondary"
    r"|primary\s+school|junior\s+school|public\s+school|private\s+school|international\s+school"
    r"|foundation\s+stage|cadet|university)\b",
    re.I,
)
# Places for children that aren't nurseries (seen in the Dubai scrape), unless
# the name also says nursery, daycare, preschool and so on
NOT_NURSERY_NAME_RE = re.compile(
    r"play\s?(?:ground|area|land|zone)|entertainment|amusement|arcade|activity\s+cent"
    r"|kids\s+(?:zone|club)|gymboree|tuition",
    re.I,
)
# Google's separate entries for a building's entrance ("... Nursery Entrance")
ENTRANCE_RE = re.compile(r"\bentrance\b", re.I)


def pin_of(item: dict) -> tuple | None:
    """A scraped place's map pin, rounded to about a metre, or None."""
    lat, lng = float(item.get("latitude") or 0), float(item.get("longitude") or 0)
    return (round(lat, 5), round(lng, 5)) if lat and lng else None


def placeholder_pins(items: list, min_places: int = 5) -> set:
    """Map pins shared exactly by min_places or more places. Google puts
    listings with no real location on the city's default point (in the
    Dubai scrape, 43 'nurseries' with no reviews or street address)."""
    counts = Counter(pin_of(i) for i in items)
    return {pin for pin, n in counts.items() if pin and n >= min_places}


# Last part of a Google address, dropped before reading the city
COUNTRY_NAMES = {"pakistan", "united arab emirates", "uae"}


def address_parts(address: str) -> list[str]:
    """Google separates address parts with commas in Pakistan and with " - "
    in the UAE ("Al Wasl Rd - Umm Suqeim 2 - Dubai - United Arab Emirates")."""
    return [p.strip() for p in re.split(r",| - ", address) if p.strip()]


def address_city(address: str) -> str:
    """City from a Google address: '..., F-7/4, Islamabad, 44000, Pakistan' -> 'Islamabad',
    'Al Barsha 1 - Dubai - United Arab Emirates' -> 'Dubai'."""
    parts = address_parts(address)
    while parts and (parts[-1].lower() in COUNTRY_NAMES or re.fullmatch(r"\d{4,6}", parts[-1])):
        parts.pop()
    if not parts:
        return ""
    # "Islamabad 44000" or "Islamabad Capital Territory"
    return re.sub(r"\s*\d{4,6}$", "", parts[-1]).removesuffix(" Capital Territory").strip()


def location_problem(address: str, city: str, lat: float = 0, lng: float = 0,
                     boundary: list | None = None) -> str:
    """"" if the place is in the city, else the reason it isn't (or can't be told)."""
    if boundary is not None and lat and lng:
        return "" if in_boundary(lat, lng, boundary) else f"outside {city} boundary"
    # No coordinates: fall back to the address
    if not address:
        return "no address or location"
    if " - " in address:
        # UAE style: the city is always the last part before the country
        # ("... - Al Ain - United Arab Emirates"); a road such as
        # "Dubai - Al Ain Rd" would otherwise look like a "Dubai" part
        if address_city(address).lower() == city.lower():
            return ""
        return f"outside {city} ({address_city(address) or 'unknown city'})"
    # Pakistan style: the city must end one of the comma-separated parts
    # ("G-13/2 Islamabad", "Islamabad 44000"), so that a road like
    # "Islamabad Highway, Rawalpindi" doesn't count
    city_part = re.compile(rf"(?:^|\s){re.escape(city)}(?:\s+Capital Territory)?(?:\s+\d{{4,6}})?$", re.I)
    if any(city_part.search(part.strip()) for part in address.split(",")):
        return ""
    return f"outside {city} ({address_city(address) or 'unknown city'})"


def classify(name: str, categories: list, address: str, city: str, closed: bool = False,
             lat: float = 0, lng: float = 0, boundary: list | None = None,
             exclude: list = (), keep: bool = False) -> tuple[str, str]:
    """Return (listing_type, "") for places that belong in the directory,
    or ("", reason) for places that don't.

    keep=True (the place is in the queries file's [keep] list) skips the
    category and name rules; closed places and the location still count."""
    if closed:
        return "", "permanently closed"
    for area in exclude:
        if area.lower() in address.lower():
            return "", f"excluded area ({area})"
    problem = location_problem(address, city, lat, lng, boundary)
    if problem:
        return "", problem

    category = (categories[0] if categories else "").strip()
    cat = category.lower()
    daycare_name = bool(DAYCARE_NAME_RE.search(name))

    if keep:
        pass
    elif cat in DAYCARE_CATEGORIES or cat in PRESCHOOL_CATEGORIES:
        pass
    elif cat in GENERIC_CATEGORIES and EARLY_YEARS_NAME_RE.search(name):
        pass
    else:
        return "", f"category: {category or 'none'}"

    early_years_name = bool(EARLY_YEARS_NAME_RE.search(name))
    if keep:
        pass
    elif SCHOOL_CHAIN_RE.search(name) and not daycare_name:
        return "", "part of a school (name)"
    elif re.search(r"\bschool\b", name, re.I) and not early_years_name:
        return "", "school (name)"
    elif NOT_NURSERY_NAME_RE.search(name) and not early_years_name:
        return "", "not a nursery (name)"
    elif ENTRANCE_RE.search(name):
        return "", "building entrance (name)"

    if cat in DAYCARE_CATEGORIES or daycare_name:
        return "daycare", ""
    return "preschool", ""
