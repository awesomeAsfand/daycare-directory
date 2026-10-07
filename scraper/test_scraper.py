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

BOUNDARY = load_boundary(Path(__file__).parent / "queries" / "islamabad_boundary.geojson")

ISB = "House 5, Street 9, F-7/3 F 7/3 F-7, Islamabad, 44000, Pakistan"
RWP = "Street 2, Bahria Town Phase 7, Rawalpindi, 46000, Pakistan"
DXB = "Shop G-12, Al Wasl Rd - Umm Suqeim - Umm Suqeim 2 - Dubai - United Arab Emirates"

QUERIES = """\
# comment
[keywords]
daycare
montessori

[areas]
F-7
E-18 (Gulshan-e-Sehat)

[grid]
bbox = 33.60, 73.00, 33.65, 73.06
step_km = 2.5
zoom = 14
keywords = daycare
"""


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "islamabad.txt"
        self.file.write_text(QUERIES, encoding="utf-8")

    def test_load_plan(self):
        plan = load_plan(self.file)
        self.assertEqual(plan.city, "Islamabad")
        self.assertEqual(plan.keywords, ["daycare", "montessori"])
        self.assertEqual(plan.areas, ["F-7", "E-18"])
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
        self.assertEqual(load_plan(Path(__file__).parent / "queries" / "islamabad.txt").country["timezone"],
                         "Asia/Karachi")

    def test_area_label_drops_notes(self):
        self.assertEqual(area_label("B-17 (Multi Gardens)"), "B-17")

    def test_area_tasks_then_grid(self):
        tasks = build_tasks(load_plan(self.file))
        self.assertEqual([t.query for t in tasks[:4]], [
            "daycare in F-7 Islamabad", "montessori in F-7 Islamabad",
            "daycare in E-18 Islamabad", "montessori in E-18 Islamabad",
        ])
        grid = [t for t in tasks if t.is_grid]
        self.assertTrue(grid and all(t.query == "daycare" for t in grid))
        self.assertTrue(grid[0].url.startswith("https://www.google.com/maps/search/daycare/@33.6"))
        self.assertTrue(grid[0].url.endswith(",14z"))
        self.assertEqual(len({t.key for t in tasks}), len(tasks))

    def test_selected_areas_skip_grid_and_reject_typos(self):
        tasks = build_tasks(load_plan(self.file), areas=["e-18"])
        self.assertEqual([t.area for t in tasks], ["E-18", "E-18"])
        with self.assertRaises(ValueError):
            build_tasks(load_plan(self.file), areas=["F-77"])

    def test_grid_cells_cover_box(self):
        cells = grid_cells((33.50, 72.80, 33.80, 73.30), 2.5)
        lats = sorted({c[0] for c in cells})
        self.assertTrue(33.50 < lats[0] < 33.53 and 33.77 < lats[-1] < 33.80)
        self.assertAlmostEqual(lats[1] - lats[0], 2.5 / 111, places=3)

    def test_url_encoding(self):
        t = SearchTask(key="k", query="day care center in F-7 Islamabad")
        self.assertEqual(t.url, "https://www.google.com/maps/search/day+care+center+in+F-7+Islamabad")


class ClassifyTests(unittest.TestCase):
    def check(self, name, categories, address=ISB, closed=False):
        return classify(name, categories, address, "Islamabad", closed)

    def test_kept(self):
        self.assertEqual(self.check("Little Kingdom Childcare", ["Day care center"]), ("daycare", ""))
        self.assertEqual(self.check("Interactive Learning & ChildCare", ["Preschool"]), ("daycare", ""))
        self.assertEqual(self.check("SuperNova Pre School", ["Kindergarten"]), ("preschool", ""))
        self.assertEqual(self.check("Tiny Tots Montessori", ["Educational institution"]), ("preschool", ""))
        self.assertEqual(self.check("Kids Corner", ["Montessori preschool"]), ("preschool", ""))
        # From the step 2 trial
        self.assertEqual(self.check("Red Maple Montessori | Best Montessori School in Islamabad",
                                    ["General education school"]), ("preschool", ""))
        self.assertEqual(self.check("Small Steps Everyday, Toddlers Day Care, Academy, Montessori",
                                    ["Education center"]), ("daycare", ""))
        self.assertEqual(self.check("Treehouse School", ["Educational institution"]),
                         ("", "category: Educational institution"))

    def test_rejected(self):
        cases = [
            ("Friends School Islamabad", ["School house"], "category: School house"),
            ("The Lighthouse School System", ["Montessori school"], "part of a school (name)"),
            ("Bright Future Academy", ["Preschool"], "part of a school (name)"),
            ("Mothercare", ["Baby store"], "category: Baby store"),
            ("No Category Daycare", [], "category: none"),
        ]
        for name, cats, reason in cases:
            with self.subTest(name=name):
                self.assertEqual(self.check(name, cats), ("", reason))

    def test_location_and_status(self):
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], RWP), ("", "outside Islamabad (Rawalpindi)"))
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], "Plot 4, Islamabad Highway, Rawalpindi, Pakistan"),
                         ("", "outside Islamabad (Rawalpindi)"))
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], ""), ("", "no address or location"))
        self.assertEqual(self.check("Tiny Tots", ["Day care center"], closed=True), ("", "permanently closed"))

    def test_boundary_overrides_address(self):
        def check(address, lat, lng, exclude=()):
            return classify("Tiny Tots", ["Day care center"], address, "Islamabad",
                            lat=lat, lng=lng, boundary=BOUNDARY, exclude=exclude)
        # Bani Gala pin, address wrongly says Rawalpindi (seen in the trial)
        self.assertEqual(check("Main Bani Gala, Bani Gala, Rawalpindi, Pakistan", 33.7180, 73.1520), ("daycare", ""))
        # Diplomatic Enclave: address never says Islamabad
        self.assertEqual(check("Faheem Heights, Diplomatic, Enclave, 44000, Pakistan", 33.7300, 73.1100),
                         ("daycare", ""))
        # Saddar, Rawalpindi
        self.assertEqual(check("Saddar, Rawalpindi, Pakistan", 33.5970, 73.0480),
                         ("", "outside Islamabad boundary"))
        # Bahria Town Phase 4 is inside the boundary but excluded by name
        self.assertEqual(check("Bahria Town Phase 4, Islamabad, Pakistan", 33.5517, 73.1249, ["Bahria Town"]),
                         ("", "excluded area (Bahria Town)"))
        self.assertEqual(check("Bahria Enclave, Islamabad", 33.6877, 73.2140, ["Bahria Town"]), ("daycare", ""))

    def test_boundary_points(self):
        for name, lat, lng, inside in [
            ("F-7", 33.7215, 73.0560, True), ("G-13", 33.6500, 72.9600, True),
            ("DHA Phase 2", 33.5273, 73.1609, True), ("PWD", 33.5710, 73.1440, True),
            ("Saddar RWP", 33.5970, 73.0480, False), ("DHA Phase 1 RWP", 33.5590, 73.1080, False),
            ("Bahria Phase 7 RWP", 33.5701, 73.1163, False), ("Bahria Phase 8 RWP", 33.4877, 73.0727, False),
        ]:
            with self.subTest(name=name):
                self.assertEqual(in_boundary(lat, lng, BOUNDARY), inside)

    def test_grid_skips_cells_outside_boundary(self):
        bbox = (33.48, 72.81, 33.81, 73.38)
        self.assertLess(len(grid_cells(bbox, 2.5, BOUNDARY)), len(grid_cells(bbox, 2.5)))
        self.assertNotIn(True, [abs(la - 33.597) < 0.005 and abs(ln - 73.048) < 0.005
                                for la, ln in grid_cells(bbox, 2.5, BOUNDARY)])   # not centred on Saddar

    def test_address_city(self):
        self.assertEqual(address_city(ISB), "Islamabad")
        self.assertEqual(address_city("Sector G-9, Islamabad 44000, Pakistan"), "Islamabad")
        self.assertEqual(address_city("Bani Gala, Islamabad Capital Territory, Pakistan"), "Islamabad")
        # UAE addresses use " - " between parts
        self.assertEqual(address_city(DXB), "Dubai")
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
    """Area matching against the real Islamabad areas file."""

    @classmethod
    def setUpClass(cls):
        from area_match import AreaMatcher
        cls.m = AreaMatcher(load_plan(Path(__file__).parent / "queries" / "islamabad.txt"))

    def check(self, address, lat=0, lng=0):
        r = self.m.match(address, lat, lng)
        return r.area, r.sub_area, r.method

    def test_sector_spellings(self):
        # Real addresses from the step 2 trial
        cases = [
            ("House # 1-A, Main Parbat Rd, F-7/4 F 7/4 F-7, Islamabad, 44000, Pakistan", ("F-7", "F-7/4")),
            ("24-A, Nazim-ud-Din Rd, F-7-1, F-7, Islamabad, ICT, F 7/1 F-7, Pakistan", ("F-7", "F-7/1")),
            ("589 Service Rd W, G-13/2 Islamabad, 44000, Pakistan", ("G-13", "G-13/2")),
            ("Street 12, F7 Markaz, Islamabad", ("F-7", "")),
            ("Plot 3, I-8/4, Islamabad", ("I-8", "I-8/4")),
            ("House 9, Street 2, F-10/2, Islamabad", ("F-10", "F-10/2")),
            ("Multi Gardens, B-17, Islamabad", ("B-17", "")),
        ]
        for address, expected in cases:
            with self.subTest(address=address):
                self.assertEqual(self.check(address), (*expected, "sector"))

    def test_names_and_spellings(self):
        cases = [
            ("Plot 58, Street 12, Bani Gala Greens, Near Imran Khan Chowk Korang Road, Bani Gala, 44000", "Bani Gala"),
            ("Main Banigala Road, Islamabad", "Bani Gala"),
            ("House 3, Street 7, National Police Foundation O-9, Islamabad", "National Police Foundation O-9"),
            ("Street 4, Police Foundation, Islamabad", "Police Foundation"),
            ("House 12, DHA Phase II, Islamabad", "DHA Phase 2"),
            ("Block C, Gulshan-e-Sehat, Islamabad", "E-18"),
            ("Street 5, Multi Gardens, Islamabad", "B-17"),
            ("Top City 1, Block A, Islamabad", "Top City-1"),
            ("CBR Town Phase 1, Islamabad", "CBR Town"),
        ]
        for address, area in cases:
            with self.subTest(address=address):
                self.assertEqual(self.check(address), (area, "", "name"))

    def test_sectors_only_with_cda_setting(self):
        # Islamabad's file turns sector matching on; without it (Dubai), shop
        # and block numbers like "Shop G-12" must not be read as sectors
        from area_match import AreaMatcher
        self.assertTrue(self.m.match_sectors)
        dubai = AreaMatcher(QueryPlan(city="Dubai", areas=["Umm Suqeim"]))
        r = dubai.match(DXB)
        self.assertEqual((r.area, r.sub_area, r.method, r.unlisted_sector), ("Umm Suqeim", "", "name", ""))

    def test_no_false_matches(self):
        for address in ["Mohallah No 9, Some Road, Pakistan", "House 1-A, Block C, Street 5, Pakistan"]:
            with self.subTest(address=address):
                self.assertEqual(self.check(address), ("", "", ""))

    def test_map_pin_fallback(self):
        # Middle of G-13, address names no area
        self.assertEqual(self.check("Service Road, Pakistan", 33.6515, 72.9620), ("G-13", "", "map pin"))
        # Margalla Hills, far from every area position
        self.assertEqual(self.check("Trail 5, Pakistan", 33.7600, 73.0300), ("", "", ""))

    def test_found_in_dry_run(self):
        # "A-8" is a block, not a sector: the pin decides (0.5 km from Shaheen Town)
        self.assertEqual(self.check("A-8, 2 Phase, Arsalan Town, Islamabad, Pakistan", 33.6370, 73.2193),
                         ("Shaheen Town", "", "map pin"))
        self.assertEqual(self.check("Irfan Shaheed Road, Jhangi Sayedan, Islamabad"),
                         ("Jhangi Syedan", "", "name"))
        # Area only in the listing's name
        r = self.m.match("MWC6+4M4, Sharif Gull Khan Rd, Islamabad, Pakistan", 0, 0,
                         name="Daffodils School Tarnol Islamabad")
        self.assertEqual((r.area, r.method), ("Tarnol", "listing name (name)"))

    def test_plus_codes_are_not_sectors(self):
        # Seen in the full scrape: "G5C5" was read as G-5, "H5CP" as H-5
        self.assertEqual(self.check("H5CP+G83, Anchorage, Jinnah Garden, Islamabad, Pakistan")[0], "Jinnah Garden")
        self.assertEqual(self.m.find_sector("G5C5+939, Giga Mall, Main GT Road, Sector F"), ("", ""))
        self.assertEqual(self.m.find_sector("M747+C5G, Nilore, Pakistan"), ("", ""))
        self.assertEqual(self.m.find_sector("JWFP+848 Kazmin Arcade, Rehman Town H-15, Islamabad"), ("H-15", ""))

    def test_areas_added_after_full_scrape(self):
        self.assertEqual(self.check("Serena Business Complex, G-5/1 G-5, Islamabad, 44000"),
                         ("Diplomatic Enclave", "G-5/1", "sector"))
        self.assertEqual(self.check("Faheem Heights, Sector, Diplomatic, Enclave, 44000, Pakistan")[0],
                         "Diplomatic Enclave")
        self.assertEqual(self.check("House # 2 Main St, G-12/1 G-12, Islamabad")[:2], ("G-12", "G-12/1"))
        self.assertEqual(self.check("G5JX+QWQ, Sector C DHA Phase 5, Islamabad")[0], "DHA Phase 5")
        self.assertEqual(self.check("M747+C5G, Nilore, Pakistan")[0], "Nilore")

    def test_unlisted_sector_is_not_guessed(self):
        r = self.m.match("Ministers Enclave, F-5/2, Islamabad", 33.7270, 73.0930)   # F-5 was removed
        self.assertEqual((r.area, r.sub_area, r.unlisted_sector), ("", "F-5/2", "F-5"))
        r = self.m.match("Plot 4, H-15, Islamabad")
        self.assertEqual((r.area, r.unlisted_sector), ("", "H-15"))

    def test_alias_to_unknown_area_is_an_error(self):
        tmp = Path(tempfile.mkdtemp()) / "x.txt"
        tmp.write_text("[areas]\nF-7\n[aliases]\nF7 Markaz = F-77\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_plan(tmp)


class ParserTests(unittest.TestCase):
    def test_place_id_and_coords(self):
        url = ("https://www.google.com/maps/place/X/@33.70,73.00,17z/data=!3m1!4b1!4m6!3m5"
               "!1s0x38dfed4fe9af5665:0xc8c4a6c176c9d26e!8m2!3d33.6111!4d73.1222")
        self.assertEqual(g.extract_place_id(url), "0x38dfed4fe9af5665:0xc8c4a6c176c9d26e")
        self.assertEqual(g.extract_coords(url), (33.6111, 73.1222))

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
        self.assertEqual(g.clean_text("House 1,  F-7"), "House 1, F-7")

    def test_closed_detection(self):
        dc = g.parse_listing_page("<h1>Old Place</h1><span>Permanently closed</span>", "https://x", "q")
        self.assertTrue(dc.closed)

    def test_find_phone(self):
        cases = [
            ("Call +971 4 123 4567 today", "971", "+971 4 123 4567"),
            ("Mobile +971 50 123 4567", "971", "+971 50 123 4567"),
            ("Tel 04 123 4567", "971", "04 123 4567"),
            ("Tel 050-1234567", "971", "050-1234567"),
            ("Phone +92 300 1234567", "92", "+92 300 1234567"),
            ("Phone 051 2345678", "92", "051 2345678"),
            ("Phone +92 300 1234567", "", "+92 300 1234567"),   # no phone code: any country
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
        "0x1:0x1": ("Little Kingdom Childcare", ["Day care center"], ISB),
        "0x2:0x2": ("SuperNova Pre School", ["Kindergarten"], ISB),
        "0x3:0x3": ("The Lighthouse School System", ["School"], ISB),
        "0x4:0x4": ("Tiny Tots", ["Day care center"], RWP),
    }
    SEARCHES = {
        "area:F-7|daycare": ["0x1:0x1", "0x3:0x3"],
        "area:F-7|montessori": ["0x1:0x1", "0x2:0x2"],
        "area:G-9|daycare": ["0x4:0x4", "0x2:0x2"],
        "area:G-9|montessori": [],
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "out.json"
        self.tasks = [SearchTask(key=k, query=k, keyword=k.split("|")[1], area=k[5:].split("|")[0])
                      for k in self.SEARCHES]
        self.visits = []

    def url(self, pid):
        return f"https://www.google.com/maps/place/X/data=!1s{pid}!3d33.7!4d73.0"

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
            asyncio.run(g.run_scraper(self.tasks, QueryPlan(city="Islamabad"), self.output, **kw))
        return out.getvalue()

    def test_full_run(self):
        out = self.run_scraper()
        kept = json.loads(self.output.read_text(encoding="utf-8"))
        rejected = json.loads(self.output.with_suffix(".rejected.json").read_text(encoding="utf-8"))
        state = json.loads(self.output.with_suffix(".state.json").read_text(encoding="utf-8"))

        self.assertEqual([k["name"] for k in kept], ["Little Kingdom Childcare", "SuperNova Pre School"])
        self.assertEqual([k["listing_type"] for k in kept], ["daycare", "preschool"])
        self.assertEqual(kept[0]["found_by"], ["area:F-7|daycare", "area:F-7|montessori"])
        self.assertEqual(kept[1]["found_by"], ["area:F-7|montessori", "area:G-9|daycare"])
        self.assertEqual(kept[0]["search_area"], "F-7")
        self.assertEqual({r["name"]: r["reason"] for r in rejected}, {
            "The Lighthouse School System": "category: School",
            "Tiny Tots": "outside Islamabad (Rawalpindi)",
        })
        # Each place is visited once, however many searches list it
        self.assertEqual(sorted(self.visits), ["0x1:0x1", "0x2:0x2", "0x3:0x3", "0x4:0x4"])
        self.assertEqual(state["completed"], list(self.SEARCHES))
        self.assertEqual(state["stats"]["area:F-7|montessori"], {"found": 2, "new": 1, "kept": 1, "rejected": 0})
        self.assertIn("area: daycare", out)
        self.assertIn("Rejected: category: School 1, outside Islamabad 1", out)

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
            # Shop numbers aren't sectors, and a building number isn't a sub-area
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
        ]
        for address, area, sub_area in cases:
            with self.subTest(address=address):
                m = self.m.match(address)
                self.assertEqual(m.area, area)
                if sub_area:
                    self.assertEqual(m.sub_area, sub_area)


if __name__ == "__main__":
    unittest.main()
