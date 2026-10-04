"""
Look up a map position for every area in a queries file, using OpenStreetMap's
Nominatim, and write them to <queries>_area_centres.csv for review.

    python scraper/area_centres.py                       # queries/islamabad.txt
    python scraper/area_centres.py queries/lahore.txt

import_listings uses the positions to place a listing whose address names no
area: it goes to the nearest area within [area_match] max_km. Results outside
the city boundary are left blank, so check the CSV and fill any gaps by hand
(lat,lng from Google Maps: right-click the area > copy coordinates).

Nominatim's usage policy allows at most one request per second.
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from search_plan import in_boundary, load_plan  # noqa: E402

USER_AGENT = "DaycaresPK-directory-scraper/1.0"


def lookup(query: str) -> list[dict]:
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 5, "countrycodes": "pk"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main(queries_file: Path):
    plan = load_plan(queries_file)
    out = queries_file.with_name(f"{queries_file.stem}_area_centres.csv")
    rows = []
    for area in plan.areas:
        lat = lng = ""
        found = "not found"
        for hit in lookup(f"{area}, {plan.city}"):
            la, ln = float(hit["lat"]), float(hit["lon"])
            if plan.boundary is None or in_boundary(la, ln, plan.boundary):
                lat, lng, found = f"{la:.5f}", f"{ln:.5f}", hit["display_name"][:120]
                break
            found = "only outside the boundary"
        print(f"{area:32} {lat:>9} {lng:>9}  {found[:70]}")
        rows.append({"area": area, "lat": lat, "lng": lng, "osm_match": found})
        time.sleep(1.1)

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["area", "lat", "lng", "osm_match"])
        writer.writeheader()
        writer.writerows(rows)
    missing = sum(1 for r in rows if not r["lat"])
    print(f"\nWrote {out.name}: {len(rows) - missing} found, {missing} to fill in by hand.")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "queries" / "islamabad.txt")
