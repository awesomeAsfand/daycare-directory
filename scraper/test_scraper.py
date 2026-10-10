"""
Offline tests for the scraper (no Google, no network).

    python -m unittest discover -s scraper -p "test_*.py"
"""
import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import gmaps_scraper as g  # noqa: E402
from search_plan import (  # noqa: E402
    QueryPlan, SearchTask, address_city, area_label, build_tasks, classify, grid_cells,
    in_boundary, load_boundary, load_plan,
)

BOUNDARY = load_boundary(Path(__file__).parent / "queries" / "dubai_boundary.geojson")

DXB = "Shop G-12, Al Wasl Rd - Umm Suqeim - Umm Suqeim 2 - Dubai - United Arab Emirates"
SHJ = "Villa 4 - Al Nahda - Sharjah - United Arab Emirates"

QUERIES = """\
# comment
[keywords]
daycare
montessori

[areas]
Al Barsha
Jumeirah Lake Towers (JLT)

[grid]
bbox = 25.05, 55.10, 25.10, 55.16
step_km = 2.5
zoom = 14
keywords = daycare
"""


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "dubai.txt"
        self.file.write_text(QUERIES, encoding="utf-8")

    def test_load_plan(self):
        plan = load_plan(self.file)
        self.assertEqual(plan.city, "Dubai")
        self.assertEqual(plan.keywords, ["daycare", "montessori"])
        self.assertEqual(plan.areas, ["Al Barsha", "Jumeirah Lake Towers"])
        self.assertEqual(plan.grid["step_km"], "2.5")

    def test_country_section(self):
        dubai = self.tmp / "dubai.txt"
        dubai.write_text("[country]\nname = United Arab Emirates\ncode = ae\ntimezone = Asia/Dubai\n"
                         "phone_code = 971\n" + QUERIES, encoding="utf-8")
        plan = load_plan(dubai)
        self.assertEqual(plan.city, "Dubai")
        self.assertEqual(plan.country, {"name": "United Arab Emirates", "code": "ae",
                                        "timezone": "Asia/Dubai", "phone_code": "971"})
        self.assertEqual(g.default_output(dubai), g.SCRAPER_DIR / "dubai_listings.json")
        self.assertEqual(load_plan(Path(__file__).parent / "queries" / "abu_dhabi.txt").city, "Abu Dhabi")

    def test_area_label_drops_notes(self):
        self.assertEqual(area_label("Jumeirah Lake Towers (JLT)"), "Jumeirah Lake Towers")

    def test_area_tasks_then_grid(self):
        tasks = build_tasks(load_plan(self.file))
        self.assertEqual([t.query for t in tasks[:4]], [
            "daycare in Al Barsha Dubai", "montessori in Al Barsha Dubai",
            "daycare in Jumeirah Lake Towers Dubai", "montessori in Jumeirah Lake Towers Dubai",
        ])
        grid = [t for t in tasks if t.is_grid]
        self.assertTrue(grid and all(t.query == "daycare" for t in grid))
        self.assertTrue(grid[0].url.startswith("https://www.google.com/maps/search/daycare/@25.0"))
        self.assertTrue(grid[0].url.endswith(",14z"))
        self.assertEqual(len({t.key for t in tasks}), len(tasks))

    def test_selected_areas_skip_grid_and_reject_typos(self):
        tasks = build_tasks(load_plan(self.file), areas=["al barsha"])
        self.assertEqual([t.area for t in tasks], ["Al Barsha", "Al Barsha"])
        with self.assertRaises(ValueError):
            build_tasks(load_plan(self.file), areas=["Al Barshaa"])

    def test_grid_cells_cover_box(self):
        cells = grid_cells((24.90, 55.00, 25.30, 55.60), 2.5)
        lats = sorted({c[0] for c in cells})
        self.assertTrue(24.90 < lats[0] < 24.93 and 25.27 < lats[-1] < 25.30)
        self.assertAlmostEqual(lats[1] - lats[0], 2.5 / 111, places=3)

    def test_url_encoding(self):
        t = SearchTask(key="k", query="day care center in Al Barsha Dubai")
        self.assertEqual(t.url, "https://www.google.com/maps/search/day+care+center+in+Al+Barsha+Dubai")


class ClassifyTests(unittest.TestCase):
    def check(self, name, categories, address=DXB, closed=False):
        return classify(name, categories, address, "Dubai", closed)

    def test_kept(self):
        self.assertEqual(self.check("Little Kingdom Childcare", ["Day care center"]), ("daycare", ""))
        self.assertEqual(self.check("Interactive Learning & ChildCare", ["Preschool"]), ("daycare", ""))
        self.assertEqual(self.check("SuperNova Pre School", ["Kindergarten"]), ("preschool", ""))
        self.assertEqual(self.check("Tiny Tots Montessori", ["Educational institution"]), ("preschool", ""))
        self.assertEqual(self.check("Kids Corner", ["Montessori preschool"]), ("preschool", ""))
        self.assertEqual(self.check("Red Maple Montessori | Best Montessori School in Dubai",
                                    ["General education school"]), ("preschool", ""))
        self.assertEqual(self.check("Small Steps Everyday, Toddlers Day Care, Academy, Montessori",
                                    ["Education center"]), ("daycare", ""))
        self.assertEqual(self.check("Treehouse School", ["Educational institution"]),
                         ("", "category: Educational institution"))

    def test_rejected(self):
        cases = [
            ("Friends School Dubai", ["School house"], "category: School house"),
            ("The Lighthouse School System", ["Montessori school"], "part of a school (name)"),
            ("Bright Future Academy", ["Preschool"], "part of a school (name)"),
            ("Mothercare", ["Baby store"], "category: Baby store"),
            ("No Category Daycare", [], "category: none"),
        ]
        for name, cats, reason in cases:
            with self.subTest(name=name):
                self.assertEqual(self.check(name, cats), ("", reason))

    def test_location_and_status(self):
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], SHJ), ("", "outside Dubai (Sharjah)"))
        # Comma-separated address: "Dubai" in a road name doesn't count
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], "Plot 4, Dubai Al Ain Road, Al Ain"),
                         ("", "outside Dubai (Al Ain)"))
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], "Villa 12, Al Wasl Rd, Dubai"),
                         ("daycare", ""))
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], ""), ("", "no address or location"))
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], closed=True), ("", "permanently closed"))

    def test_boundary_overrides_address(self):
        def check(address, lat, lng, exclude=()):
            return classify("Tiny Tots", ["Day care center"], address, "Dubai",
                            lat=lat, lng=lng, boundary=BOUNDARY, exclude=exclude)
        # Pin in Al Barsha, address wrongly says Sharjah
        self.assertEqual(check(SHJ, 25.10, 55.20), ("daycare", ""))
        # Sharjah city
        self.assertEqual(check("Al Majaz - Sharjah - United Arab Emirates", 25.33, 55.39),
                         ("", "outside Dubai boundary"))
        # Inside the boundary but excluded by name
        self.assertEqual(check("Jebel Ali Free Zone - Dubai - United Arab Emirates", 25.01, 55.08, ["Free Zone"]),
                         ("", "excluded area (Free Zone)"))
        self.assertEqual(check("Jebel Ali Village - Dubai - United Arab Emirates", 25.03, 55.12, ["Free Zone"]),
                         ("daycare", ""))

    def test_boundary_points(self):
        for name, lat, lng, inside in [
            ("Al Barsha", 25.10, 55.20, True), ("Dubai Marina", 25.08, 55.14, True),
            ("Hatta", 24.80, 56.12, True), ("Jebel Ali", 25.01, 55.08, True),
            ("Sharjah Al Majaz", 25.33, 55.39, False), ("Ajman", 25.40, 55.45, False),
            ("Abu Dhabi", 24.45, 54.38, False),
        ]:
            with self.subTest(name=name):
                self.assertEqual(in_boundary(lat, lng, BOUNDARY), inside)

    def test_grid_skips_cells_outside_boundary(self):
        bbox = (24.6, 54.9, 25.4, 56.2)
        cells = grid_cells(bbox, 2.5, BOUNDARY)
        self.assertLess(len(cells), len(grid_cells(bbox, 2.5)))
        self.assertNotIn(True, [abs(la - 25.346) < 0.02 and abs(ln - 55.421) < 0.02
                                for la, ln in cells])   # not centred on Sharjah city

    def test_address_city(self):
        self.assertEqual(address_city(DXB), "Dubai")
        self.assertEqual(address_city("Al Barsha, Dubai 12345"), "Dubai")
        self.assertEqual(address_city("Villa 5, Street 12 - Al Khalidiyah - W 10 - Abu Dhabi - "
                                      "United Arab Emirates"), "Abu Dhabi")

    def test_location_from_uae_address(self):
        # No coordinates: the city must be one of the address parts
        self.assertEqual(classify("Tiny Tots Nursery", ["Nursery school"], DXB, "Dubai"), ("preschool", ""))
        self.assertEqual(classify("Tiny Tots Nursery", ["Nursery school"], "Al Nahda - Sharjah - "
                                  "United Arab Emirates", "Dubai"), ("", "outside Dubai (Sharjah)"))
        # "Dubai" in a road name doesn't count
        self.assertEqual(classify("Tiny Tots Nursery", ["Nursery school"], "Dubai - Al Ain Rd - Al Ain - "
                                  "United Arab Emirates", "Dubai"), ("", "outside Dubai (Al Ain)"))


class AreaMatchTests(unittest.TestCase):
    """Area matching against the real Dubai areas file."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        cls.m = AreaMatcher(load_plan(Path(__file__).parent / "queries" / "dubai.txt"))

    def check(self, address, lat=0, lng=0):
        r = self.m.match(address, lat, lng)
        return r.area, r.sub_area, r.method

    def test_names_and_spellings(self):
        cases = [
            ("Villa 9 - Mirdiff - Dubai - United Arab Emirates", "Mirdif"),
            ("Tower 3 - Jumeirah Lakes Towers - Dubai - United Arab Emirates", "Jumeirah Lake Towers"),
            ("Building 7 - Garhoud - Dubai - United Arab Emirates", "Al Garhoud"),
            ("Villa 2 - The Palm - Dubai - United Arab Emirates", "Palm Jumeirah"),
            ("Villa 5, Al Wasl Rd, Dubai", "Al Wasl"),
        ]
        for address, area in cases:
            with self.subTest(address=address):
                self.assertEqual(self.check(address), (area, "", "name"))

    def test_shop_numbers_are_not_areas(self):
        # "Shop G-12" is a shop number; without "numbered = yes" no sub-area
        from area_match import AreaMatcher
        plain = AreaMatcher(QueryPlan(city="Dubai", areas=["Umm Suqeim"]))
        r = plain.match(DXB)
        self.assertEqual((r.area, r.sub_area, r.method), ("Umm Suqeim", "", "name"))

    def test_no_false_matches(self):
        self.assertEqual(self.check("Warehouse 4, Some Road - United Arab Emirates"), ("", "", ""))

    def test_map_pin_fallback(self):
        # On Al Barsha's area position, address names no area
        self.assertEqual(self.check("Service Road - Dubai - United Arab Emirates", 25.09949, 55.20173),
                         ("Al Barsha", "", "map pin"))
        # In the desert, far from every area position
        self.assertEqual(self.check("Desert Rd - Dubai - United Arab Emirates", 24.80, 55.70), ("", "", ""))

    def test_area_in_listing_name(self):
        r = self.m.match("Shop 3, Some Rd - Dubai - United Arab Emirates", 0, 0, name="Ladybird Nursery Al Barsha")
        self.assertEqual((r.area, r.method), ("Al Barsha", "listing name (name)"))

    def test_alias_to_unknown_area_is_an_error(self):
        tmp = Path(tempfile.mkdtemp()) / "x.txt"
        tmp.write_text("[areas]\nAl Barsha\n[aliases]\nBarsha = Al Barshaa\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_plan(tmp)


class ParserTests(unittest.TestCase):
    def test_place_id_and_coords(self):
        url = ("https://www.google.com/maps/place/X/@25.11,55.20,17z/data=!3m1!4b1!4m6!3m5"
               "!1s0x3e5f6b77230d5925:0x641c4a3c502519de!8m2!3d25.1111!4d55.2222")
        self.assertEqual(g.extract_place_id(url), "0x3e5f6b77230d5925:0x641c4a3c502519de")
        self.assertEqual(g.extract_coords(url), (25.1111, 55.2222))

    def test_photo_filter(self):
        html = """
        <img src="https://lh3.googleusercontent.com/gps-cs-s/AAA=w408-h306-k-no">
        <img src="https://lh3.googleusercontent.com/grass-cs/BBB=w408-h374-k-no">
        <img src="https://lh3.googleusercontent.com/grass-cs/BBB=w32-h32-p-k-no">
        <img src="https://lh3.googleusercontent.com/a-/ALV=w36-h36-p">
        <img src="https://lh3.googleusercontent.com/a/ACg=s120-c">
        <div style='background-image: url("https://lh5.googleusercontent.com/p/CCC=w203-h152-k-no");'></div>
        """
        imgs = []
        g._collect_photo_urls(html, imgs, set(), 4)
        self.assertEqual([i["url"].split(".com/")[1] for i in imgs],
                         ["gps-cs-s/AAA=w1200-h800", "grass-cs/BBB=w1200-h800", "p/CCC=w1200-h800"])

    def test_hours_and_text_cleanup(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<div aria-label="Saturday, Closed; Sunday, Closed; Monday, 8 AM to 2 PM. '
                             'Hide open hours for the week"></div>', "lxml")
        self.assertEqual(g.parse_hours(soup), {"Saturday": "Closed", "Sunday": "Closed", "Monday": "8 AM–2 PM"})
        self.assertEqual(g.clean_text("Villa 1,  Al Barsha"), "Villa 1, Al Barsha")

    def test_closed_detection(self):
        dc = g.parse_listing_page("<h1>Old Place</h1><span>Permanently closed</span>", "https://x", "q")
        self.assertTrue(dc.closed)

    def test_find_phone(self):
        cases = [
            ("Call +971 4 123 4567 today", "971", "+971 4 123 4567"),
            ("Mobile +971 50 123 4567", "971", "+971 50 123 4567"),
            ("Tel 04 123 4567", "971", "04 123 4567"),
            ("Tel 050-1234567", "971", "050-1234567"),
            ("Phone +966 50 123 4567", "966", "+966 50 123 4567"),
            ("Phone 011 234 5678", "966", "011 234 5678"),
            ("Phone +966 50 123 4567", "", "+966 50 123 4567"),   # no phone code: any country
            ("Rated 4.5 by 120 parents", "971", ""),
        ]
        for text, code, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(g.find_phone(text, code), expected)

    def test_address_fallback_uses_city(self):
        html = "<h1>Tiny Tots</h1><div>Villa 12, Al Wasl Rd, Dubai</div>"
        self.assertEqual(g.parse_listing_page(html, "https://x", "q", city="Dubai").address,
                         "Villa 12, Al Wasl Rd, Dubai")
        self.assertEqual(g.parse_listing_page(html, "https://x", "q", city="Sharjah").address, "")


class FakeBrowser:
    async def close(self):
        pass


class FakePlaywright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RunScraperTests(unittest.TestCase):
    """The main loop with Google replaced by fakes."""

    PLACES = {
        "0x1:0x1": ("Little Kingdom Childcare", ["Day care center"], DXB),
        "0x2:0x2": ("SuperNova Pre School", ["Kindergarten"], DXB),
        "0x3:0x3": ("The Lighthouse School System", ["School"], DXB),
        "0x4:0x4": ("Tiny Tots", ["Day care center"], SHJ),
    }
    SEARCHES = {
        "area:Al Barsha|daycare": ["0x1:0x1", "0x3:0x3"],
        "area:Al Barsha|montessori": ["0x1:0x1", "0x2:0x2"],
        "area:Mirdif|daycare": ["0x4:0x4", "0x2:0x2"],
        "area:Mirdif|montessori": [],
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "out.json"
        self.tasks = [SearchTask(key=k, query=k, keyword=k.split("|")[1], area=k[5:].split("|")[0])
                      for k in self.SEARCHES]
        self.visits = []

    def url(self, pid):
        return f"https://www.google.com/maps/place/X/data=!1s{pid}!3d25.1!4d55.2"

    async def fake_search(self, page, task):
        return [self.url(pid) for pid in self.SEARCHES[task.key]]

    async def fake_scrape(self, page, url, query, plan=None):
        pid = g.extract_place_id(url)
        self.visits.append(pid)
        name, cats, address = self.PLACES[pid]
        dc = g.DaycareCenter(name=name, categories=cats, address=address, place_id=pid,
                             google_maps_url=url)
        dc.listing_type, reason = classify(name, cats, address, plan.city)
        return dc, reason

    def run_scraper(self, **kw):
        async def no_sleep(*a):
            pass

        async def fake_launch(pw, timezone_id="UTC"):
            return FakeBrowser(), None

        with mock.patch.object(g, "async_playwright", FakePlaywright), \
             mock.patch.object(g, "launch_browser", fake_launch), \
             mock.patch.object(g, "get_listing_urls", self.fake_search), \
             mock.patch.object(g, "scrape_listing", self.fake_scrape), \
             mock.patch.object(g.asyncio, "sleep", no_sleep), \
             redirect_stdout(StringIO()) as out:
            asyncio.run(g.run_scraper(self.tasks, QueryPlan(city="Dubai"), self.output, **kw))
        return out.getvalue()

    def test_full_run(self):
        out = self.run_scraper()
        kept = json.loads(self.output.read_text(encoding="utf-8"))
        rejected = json.loads(self.output.with_suffix(".rejected.json").read_text(encoding="utf-8"))
        state = json.loads(self.output.with_suffix(".state.json").read_text(encoding="utf-8"))

        self.assertEqual([k["name"] for k in kept], ["Little Kingdom Childcare", "SuperNova Pre School"])
        self.assertEqual([k["listing_type"] for k in kept], ["daycare", "preschool"])
        self.assertEqual(kept[0]["found_by"], ["area:Al Barsha|daycare", "area:Al Barsha|montessori"])
        self.assertEqual(kept[1]["found_by"], ["area:Al Barsha|montessori", "area:Mirdif|daycare"])
        self.assertEqual(kept[0]["search_area"], "Al Barsha")
        self.assertEqual({r["name"]: r["reason"] for r in rejected}, {
            "The Lighthouse School System": "category: School",
            "Tiny Tots": "outside Dubai (Sharjah)",
        })
        # Each place is visited once, however many searches list it
        self.assertEqual(sorted(self.visits), ["0x1:0x1", "0x2:0x2", "0x3:0x3", "0x4:0x4"])
        self.assertEqual(state["completed"], list(self.SEARCHES))
        self.assertEqual(state["stats"]["area:Al Barsha|montessori"], {"found": 2, "new": 1, "kept": 1, "rejected": 0})
        self.assertIn("area: daycare", out)
        self.assertIn("Rejected: category: School 1, outside Dubai 1", out)

    def test_resume_skips_finished_searches(self):
        # deadline already passed, so it stops before the first search (a tiny
        # positive value didn't work on Windows: monotonic() ticks every ~15 ms)
        self.run_scraper(max_hours=-1)
        self.assertEqual(self.visits, [])
        self.run_scraper(resume=True)
        self.assertEqual(len(json.loads(self.output.read_text(encoding="utf-8"))), 2)
        visits = list(self.visits)
        self.run_scraper(resume=True)      # everything done: nothing re-visited
        self.assertEqual(self.visits, visits)

    def test_stop_file_stops_cleanly(self):
        stop = self.tmp / "STOP"
        stop.touch()
        with mock.patch.object(g, "STOP_FILE", stop):
            out = self.run_scraper()
        self.assertEqual(self.visits, [])
        self.assertFalse(stop.exists())
        self.assertIn("Run again with --resume", out)
        with mock.patch.object(g, "STOP_FILE", stop):
            self.run_scraper(resume=True)
        self.assertEqual(len(json.loads(self.output.read_text(encoding="utf-8"))), 2)
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_fresh_run_backs_up_previous_output(self):
        self.run_scraper()
        self.run_scraper()
        self.assertTrue((self.tmp / "out.bak.json").exists())
        self.assertTrue((self.tmp / "out.rejected.bak.json").exists())


class DubaiAreaMatchTests(unittest.TestCase):
    """Area matching against the real Dubai areas file."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        cls.m = AreaMatcher(load_plan(Path(__file__).parent / "queries" / "dubai.txt"))

    def check(self, address):
        r = self.m.match(address)
        return r.area, r.sub_area

    def test_numbered_communities_roll_up(self):
        cases = [
            ("Villa 12 - Al Barsha - Al Barsha 1 - Dubai - United Arab Emirates", ("Al Barsha", "Al Barsha 1")),
            ("Street 5 - Al Barsha First - Dubai - United Arab Emirates", ("Al Barsha", "Al Barsha 1")),
            ("Al Wasl Rd - Umm Suqeim - Umm Suqeim 2 - Dubai - United Arab Emirates", ("Umm Suqeim", "Umm Suqeim 2")),
            # Official spellings take the area's name
            ("Warehouse 4 - Al Qouz 3 - Dubai - United Arab Emirates", ("Al Quoz", "Al Quoz 3")),
            ("Villa 7 - Jumeira 1 - Dubai - United Arab Emirates", ("Jumeirah", "Jumeirah 1")),
            ("Nadd Al Shiba 4 - Dubai - United Arab Emirates", ("Nad Al Sheba", "Nad Al Sheba 4")),
            # Another name keeps its own: Wadi Al Safa is part of Dubailand
            ("Villa 3 - Wadi Al Safa 3 - Dubai - United Arab Emirates", ("Dubailand", "Wadi Al Safa 3")),
            ("Arabian Ranches 2 - Dubai - United Arab Emirates", ("Arabian Ranches", "Arabian Ranches 2")),
            # Not numbered
            ("Cluster Y - Jumeirah Lake Towers - Dubai - United Arab Emirates", ("Jumeirah Lake Towers", "")),
            ("Mirdif - Dubai - United Arab Emirates", ("Mirdif", "")),
        ]
        for address, expected in cases:
            with self.subTest(address=address):
                self.assertEqual(self.check(address), expected)

    def test_last_name_in_address_wins(self):
        cases = [
            # A road named after another area comes first
            ("Shop 5, Jumeirah Beach Rd - Al Safa 1 - Dubai - United Arab Emirates", ("Al Safa", "Al Safa 1")),
            ("District 12 - Jumeirah Village Circle - Dubai - United Arab Emirates", ("Jumeirah Village Circle", "")),
            ("Al Barsha South Fourth - Dubai - United Arab Emirates", ("Jumeirah Village Circle", "")),
            ("Al Barsha South 1 - Dubai - United Arab Emirates", ("Al Barsha South", "Al Barsha South 1")),
            ("Frond K - Palm Jumeirah - Dubai - United Arab Emirates", ("Palm Jumeirah", "")),
            ("Tower 2 - Barsha Heights - Dubai - United Arab Emirates", ("Barsha Heights", "")),
            ("Damac Hills 2 - Dubai - United Arab Emirates", ("Damac Hills 2", "")),
            ("Oud Al Muteena 3 - Dubai - United Arab Emirates", ("Oud Al Muteena", "Oud Al Muteena 3")),
            # Shop numbers aren't areas, and a building number isn't a sub-area
            ("Shop G-12, Marina Gate - Dubai Marina 23 - Dubai - United Arab Emirates", ("Dubai Marina", "")),
        ]
        for address, expected in cases:
            with self.subTest(address=address):
                self.assertEqual(self.check(address), expected)

    def test_search_list(self):
        plan = load_plan(Path(__file__).parent / "queries" / "dubai.txt")
        tasks = build_tasks(plan)
        self.assertIn("nursery in Jumeirah Lake Towers Dubai", [t.query for t in tasks])
        grid = [t.center for t in tasks if t.is_grid]
        self.assertEqual(len(tasks) - len(grid), len(plan.areas) * len(plan.keywords))

        # Grid only near listed areas: no cells far out at sea or deep in
        # the desert, but Hatta (far to the east) is covered
        from search_plan import distance_km, load_area_centres
        centres = load_area_centres(plan)
        self.assertEqual(len(centres), len(plan.areas))
        self.assertLess(len(grid), 250)
        self.assertTrue(all(min(distance_km(*c, *x) for x in centres.values()) <= 4 for c in grid))
        self.assertNotIn(True, [abs(la - 25.40) < 0.02 and abs(ln - 55.10) < 0.02 for la, ln in grid])   # sea
        self.assertTrue(any(ln > 56 for la, ln in grid))   # Hatta


class DubaiTrialTests(unittest.TestCase):
    """Cases from the Dubai trial (2026-10-05), with Google's real addresses."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        cls.m = AreaMatcher(load_plan(Path(__file__).parent / "queries" / "dubai.txt"))

    def test_areas_from_trial_addresses(self):
        cases = [
            ("Flamingo Tower, Z - Al Barsha South Third - Al Barsha South - Dubai - United Arab Emirates",
             ("Arjan", "")),
            ("59 - 5 Street 5 - Jabal Ali First - The Gardens - Dubai - United Arab Emirates",
             ("Discovery Gardens", "")),
            ("Wadi Al Safa 6 - Saheel - Dubai - United Arab Emirates", ("Arabian Ranches", "")),
            ("9A St - Al Barsha Second - Al Barsha - Dubai - United Arab Emirates", ("Al Barsha", "Al Barsha 2")),
            ("Emirates Living Community - First Al Khail St - Al Thanyah Third - The Greens - Dubai - "
             "United Arab Emirates", ("The Greens", "")),
            ("AMSA Building - Kaheel Blvd - Al Barsha South Fourth - Jumeirah Village Circle - Dubai - "
             "United Arab Emirates", ("Jumeirah Village Circle", "")),
            ("Al Thamam 41, Remraam Community - Dubai - United Arab Emirates", ("Remraam", "")),
        ]
        for address, expected in cases:
            with self.subTest(address=address):
                r = self.m.match(address)
                self.assertEqual((r.area, r.sub_area), expected)

    def test_not_areas_are_ignored(self):
        # A road named after an area, with no community in the address: the
        # listing name decides (here a branch name)
        r = self.m.match("Al Bahar Tower 1 Plaza Level Jumeirah Beach Road - Dubai - United Arab Emirates",
                         name="Jebel Ali Village Early Childhood Centre - Jumeirah Beach Residence Br.")
        self.assertEqual((r.area, r.method), ("Jumeirah Beach Residence", "listing name (name)"))
        self.assertEqual(self.m.match("Jumeirah International Nurseries").area, "")

    def test_nanny_agencies_rejected(self):
        self.assertEqual(classify("Yaya Middle East | Trusted Nannies & Maids", ["Child care agency"], DXB, "Dubai"),
                         ("", "category: Child care agency"))
        self.assertEqual(classify("Kids Kingdom - Nursery & Daycare in JLT", ["Child care agency"], DXB, "Dubai"),
                         ("daycare", ""))

    def test_name_ignores_panel_headings(self):
        html = ('<h1 class="fontTitleLarge">Results</h1><h1></h1>'
                '<h1 class="DUwDvf lfPIob">Paddington Park Early Childhood Center</h1>')
        self.assertEqual(g.parse_listing_page(html, "https://x", "q").name, "Paddington Park Early Childhood Center")
        self.assertEqual(g.parse_listing_page("<h1>Hours</h1>", "https://x", "q").name, "")
        self.assertEqual(g.place_name_from_url(
            "https://www.google.com/maps/place/Gardenia+Nursery+-+The+Greens,+Dubai/@25.09,55.16,17z/data=!4m7"),
            "Gardenia Nursery - The Greens, Dubai")


class DubaiFullRunTests(unittest.TestCase):
    """Cases from the full Dubai run (2026-10-06)."""

    def check(self, name, category="Nursery school"):
        return classify(name, [category], DXB, "Dubai")

    def test_not_nurseries_rejected(self):
        for name in ["BUSTAN AL FARAH KIDS ENTERTAINMENT HALL", "JAW Kids Amusement Arcade",
                     "Kiddie Clouds Kids Activity Center", "Little Kids Playground", "Kids Zone",
                     "Fun First Kids club Kidzania", "Gymboree Play & Music | Ripe Market",
                     "Manoj tuition center", "Little fingerz playarea and kids care"]:
            with self.subTest(name=name):
                self.assertEqual(self.check(name, "Preschool")[1], "not a nursery (name)")

    def test_schools_rejected(self):
        self.assertEqual(self.check("Al Fanar School", "Preschool")[1], "school (name)")
        self.assertEqual(self.check("Dubai British School Jumeirah Park Foundation", "Preschool")[1], "school (name)")
        self.assertEqual(self.check("Al Salam Private School & Nursery", "Educational institution")[1],
                         "part of a school (name)")
        self.assertEqual(self.check("Victory Heights Foundation Stage", "Kindergarten")[1], "part of a school (name)")
        self.assertEqual(self.check("British Orchard Nursery Entrance", "Educational institution")[1],
                         "building entrance (name)")

    def test_nurseries_kept(self):
        for name in ["Stepping Stones Kids Arcade Nursery", "Kids Zone Nursery", "Little Hands Nursery School",
                     "Shinning Star Nursery and Day care", "Al Rashidiya Nursery"]:
            with self.subTest(name=name):
                self.assertEqual(self.check(name, "Kindergarten")[1], "")

    def test_placeholder_pins(self):
        from search_plan import placeholder_pins
        items = [{"latitude": 25.204849, "longitude": 55.270783}] * 5 + [{"latitude": 25.1, "longitude": 55.2}]
        self.assertEqual(placeholder_pins(items), {(25.20485, 55.27078)})
        self.assertEqual(placeholder_pins(items[1:]), set())

    def test_warsan_is_international_city(self):
        from area_match import AreaMatcher
        m = AreaMatcher(load_plan(Path(__file__).parent / "queries" / "dubai.txt"))
        r = m.match("shop 9&10 - Warsan Fourth - Warsan 4 - Dubai - United Arab Emirates")
        self.assertEqual((r.area, r.sub_area), ("International City", "Warsan 4"))
        r = m.match("Wasl Green Park (R1081-A1 - Ras Al Khor Ind. Third - Ras Al Khor Industrial Area 3 - "
                    "Dubai - United Arab Emirates")
        self.assertEqual(r.area, "Ras Al Khor")

    def test_keep_list(self):
        plan = load_plan(Path(__file__).parent / "queries" / "dubai.txt")
        self.assertIn("0x3e5f5d326152a035:0x2dd3b34f1d7319f1", plan.keep)   # Dubai Infants School
        self.assertEqual(classify("Dubai Infants School", ["Day care center"], DXB, "Dubai", keep=True),
                         ("daycare", ""))
        # The location still counts
        self.assertEqual(classify("Dubai Infants School", ["Day care center"], "Al Nahda - Sharjah - "
                                  "United Arab Emirates", "Dubai", keep=True)[1], "outside Dubai (Sharjah)")


class AbuDhabiAlAinTests(unittest.TestCase):
    """The Abu Dhabi and Al Ain queries files (2026-10-07)."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        q = Path(__file__).parent / "queries"
        cls.ad, cls.aa = load_plan(q / "abu_dhabi.txt"), load_plan(q / "al_ain.txt")
        cls.mad, cls.maa = AreaMatcher(cls.ad), AreaMatcher(cls.aa)

    def test_cities_and_boundaries(self):
        self.assertEqual((self.ad.city, self.aa.city), ("Abu Dhabi", "Al Ain"))
        # Al Dhafra (Ruwais) is in neither; Al Ain and Abu Dhabi don't overlap
        self.assertEqual(classify("Tiny Tots Nursery", ["Nursery school"], "", "Abu Dhabi", False,
                                  24.11, 52.73, self.ad.boundary)[1], "outside Abu Dhabi boundary")
        self.assertEqual(classify("Tiny Tots Nursery", ["Nursery school"], "", "Abu Dhabi", False,
                                  24.25, 55.73, self.ad.boundary)[1], "outside Abu Dhabi boundary")
        self.assertEqual(classify("Tiny Tots Nursery", ["Nursery school"], "", "Al Ain", False,
                                  24.25, 55.73, self.aa.boundary), ("preschool", ""))

    def test_areas(self):
        cases = [
            (self.mad, "Villa 12 - Khalifa City B - SE-45 - Abu Dhabi - United Arab Emirates", "Shakhbout City"),
            (self.mad, "Street 9 - Mohamed Bin Zayed City - ME-9 - Abu Dhabi - United Arab Emirates", "Mohammed Bin Zayed City"),
            (self.mad, "Corniche Rd - Al Khalidiya - W10-02 - Abu Dhabi - United Arab Emirates", "Al Khalidiyah"),
            (self.mad, "Zayed Sports City - Abu Dhabi - United Arab Emirates", ""),
            (self.maa, "Villa 5 - Al Jimi - Al Ain - Abu Dhabi - United Arab Emirates", "Al Jimi"),
            (self.maa, "Shop 3, Hili Mall - Al Ain - Abu Dhabi - United Arab Emirates", ""),
            (self.maa, "Al Fou'ah - Al Sajaa - Abu Dhabi - United Arab Emirates", "Al Foah"),
            (self.maa, "23rd St - Al Aamerah - Al Rifaa - Abu Dhabi - United Arab Emirates", "Al Yahar"),
        ]
        for m, address, area in cases:
            with self.subTest(address=address):
                self.assertEqual(m.match(address).area, area)

    def test_skills_centres_left_out(self):
        reason = "skills / talent centre (name)"
        for name in ["Yuaanz Mind Abilities Development Center", "Papus Mind Abilites Development & Childcare",
                     "Play House Daycare and Skill Development Center", "Cozy Cubs Talented Children Development Center",
                     "lulys children talents center", "Dam skill devolopment center ME -10"]:
            with self.subTest(name=name):
                self.assertEqual(classify(name, ["Day care center"], "", "Abu Dhabi", False,
                                          24.45, 54.38, self.ad.boundary)[1], reason)
        # Other "development" names go to the review CSV instead
        self.assertEqual(classify("Dino Kids Early Learning & Development Centre", ["Preschool"], "",
                                  "Abu Dhabi", False, 24.45, 54.38, self.ad.boundary), ("preschool", ""))

    def test_search_lists(self):
        for plan in (self.ad, self.aa):
            tasks = build_tasks(plan)
            self.assertEqual(len([t for t in tasks if not t.is_grid]), len(plan.areas) * 3)
            self.assertNotIn("early childhood centre", plan.keywords)
            self.assertLess(len([t for t in tasks if t.is_grid]), 200)


class SharjahTests(unittest.TestCase):
    """The Sharjah queries file (2026-10-07): Sharjah city only."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        cls.plan = load_plan(Path(__file__).parent / "queries" / "sharjah.txt")
        cls.m = AreaMatcher(cls.plan)

    def test_boundary(self):
        def reason(lat, lng):
            return classify("Tiny Tots Nursery", ["Nursery school"], "", "Sharjah", False, lat, lng,
                            self.plan.boundary)[1]
        self.assertEqual(reason(25.3020, 55.3720), "")                          # Al Nahda, Sharjah
        for lat, lng in [(25.2890, 55.3680), (25.4050, 55.4450), (25.3390, 56.3560)]:  # Dubai, Ajman, Khor Fakkan
            self.assertEqual(reason(lat, lng), "outside Sharjah boundary")

    def test_areas(self):
        cases = [
            ("Al Majaz 2 - Al Majaz - Sharjah - United Arab Emirates", "Al Majaz", "Al Majaz 2"),
            ("University City Road - Al Shahba - Sharjah - United Arab Emirates", "Al Shahba", ""),
            ("Muwailih Commercial - Sharjah - United Arab Emirates", "Muwaileh", ""),
            ("Al Taawun Street - Al Khan - Sharjah - United Arab Emirates", "Al Khan", ""),
            # Parent sectors lose to the district named before them
            ("Al Faiha - Halwan - Sharjah - United Arab Emirates", "Al Fayha", ""),
            ("Al Jazzat - Al Riqa Suburb - Sharjah - United Arab Emirates", "Al Jazzat", ""),
            ("89VF+HJV - Al Khalidiya District - Sharjah - United Arab Emirates", "Al Khalidiya", ""),
        ]
        for address, area, sub_area in cases:
            with self.subTest(address=address):
                m = self.m.match(address)
                self.assertEqual(m.area, area)
                if sub_area:
                    self.assertEqual(m.sub_area, sub_area)
        # ...and to a district in the listing's name
        self.assertEqual(self.m.match("Al Taawun St - Al Khalidiya District - Sharjah - United Arab Emirates",
                                      name="Magic Kids Nursery, Al Taawun").area, "Al Taawun")
        # Dubai addresses are rejected even with a pin just inside the boundary
        self.assertEqual(classify("Winners World", ["Nursery school"],
                                  "Al Nahda First - Dubai - United Arab Emirates", "Sharjah", False,
                                  25.3020, 55.3720, self.plan.boundary, self.plan.exclude)[1],
                         "excluded area (- Dubai - United Arab Emirates)")


class AjmanTests(unittest.TestCase):
    """The Ajman queries file (2026-10-08): Ajman city only."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        cls.plan = load_plan(Path(__file__).parent / "queries" / "ajman.txt")
        cls.m = AreaMatcher(cls.plan)

    def test_boundary(self):
        def reason(lat, lng, address=""):
            return classify("Tiny Tots Nursery", ["Nursery school"], address, "Ajman", False, lat, lng,
                            self.plan.boundary, self.plan.exclude)[1]
        self.assertEqual(reason(25.392, 55.455), "")                                   # Al Nuaimiya
        for lat, lng in [(25.329, 55.383), (25.564, 55.555), (24.818, 56.105)]:    # Sharjah, UAQ, Masfout
            self.assertEqual(reason(lat, lng), "outside Ajman boundary")
        self.assertEqual(reason(25.392, 55.455, "Al Khan - Sharjah - United Arab Emirates"),
                         "excluded area (- Sharjah - United Arab Emirates)")

    def test_areas(self):
        m = self.m.match("Al Nuaimiya 2 - Al Nuaimiya - Ajman - United Arab Emirates")
        self.assertEqual((m.area, m.sub_area), ("Al Nuaimiya", "Al Nuaimiya 2"))
        self.assertEqual(self.m.match("Al Jerf 1 - Ajman - United Arab Emirates").area, "Al Jurf")


class NorthernEmiratesTests(unittest.TestCase):
    """RAK, Fujairah and UAQ queries files (2026-10-08): city parts only."""

    def check(self, city_file, city, inside, outside):
        plan = load_plan(Path(__file__).parent / "queries" / f"{city_file}.txt")
        self.assertEqual(plan.city, city)

        def reason(lat, lng):
            return classify("Tiny Tots Nursery", ["Nursery school"], "", city, False, lat, lng,
                            plan.boundary, plan.exclude)[1]
        self.assertEqual(reason(*inside), "")
        for point in outside:
            self.assertEqual(reason(*point), f"outside {city} boundary")
        self.assertTrue(classify("Tiny Tots Nursery", ["Nursery school"],
                                 "Al Khan - Sharjah - United Arab Emirates", city, False, *inside,
                                 plan.boundary, plan.exclude)[1].startswith("excluded area"))

    def test_boundaries(self):
        self.check("ras_al_khaimah", "Ras Al Khaimah", (25.790, 55.950), [(25.564, 55.555), (25.000, 56.200)])
        self.check("fujairah", "Fujairah", (25.125, 56.330), [(25.080, 56.355), (25.620, 56.270)])  # Kalba, Dibba
        self.check("umm_al_quwain", "Umm Al Quwain", (25.565, 55.555), [(25.405, 55.445), (25.790, 55.950)])  # Ajman, RAK


if __name__ == "__main__":
    unittest.main()
