import csv
import json
import shutil
import tempfile
from io import BytesIO, StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.contrib.redirects.models import Redirect
from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image

from listings.models import Area, City, Country, DaycareListing, ListingImage, Review

FID = "0x38dfe900449fe295:0x52f0dd4fa59ab339"
MAPS_URL = (
    "https://www.google.com/maps/place/Little+Kingdom+Childcare/data=!4m7!3m6"
    f"!1s{FID}!8m2!3d33.6917577!4d73.2199699!16s%2Fg%2F11vy6ny7bl"
)


def jpeg_bytes():
    buf = BytesIO()
    Image.new("RGB", (300, 200), "orange").save(buf, "JPEG")
    return buf.getvalue()


class ImportTestBase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=self.tmp / "media")
        media.enable()
        self.addCleanup(media.disable)
        self.city = City.objects.create(name="Islamabad", slug="islamabad", country=Country.for_code("PK"))

    def item(self, **overrides):
        data = {
            "name": "Little Kingdom Childcare",
            "address": "Street 5, F-7/4, Islamabad",
            "area": "F-7",
            "phone": "0300 1234567",
            "rating": 4.5,
            "review_count": 12,
            "latitude": 33.6917577,
            "longitude": 73.2199699,
            "place_id": FID,
            "google_maps_url": MAPS_URL,
            "reviews": [{"author": "Sara", "rating": 5, "text": "Great", "date": "1 month ago"}],
            "images": [],
        }
        data.update(overrides)
        return data

    def run_import(self, items, *extra):
        path = self.tmp / "listings.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        out = StringIO()
        call_command("import_listings", "--file", str(path), "--city", "Islamabad", *extra,
                     stdout=out, stderr=out)
        return out.getvalue()

    def legacy_listing(self, **kw):
        """A row as the April import left it: name as place_id, no coordinates."""
        fields = dict(
            name="Little Kingdom Childcare", slug="little-kingdom-childcare", city=self.city,
            place_id="Little+Kingdom+Childcare", maps_url=MAPS_URL, phone="051 111222",
        )
        fields.update(kw)
        return DaycareListing.objects.create(**fields)


class ImportMatchingTests(ImportTestBase):
    def test_matches_legacy_row_by_place_id_in_maps_url(self):
        old = self.legacy_listing(is_featured=True)
        self.run_import([self.item()])

        self.assertEqual(DaycareListing.objects.count(), 1)
        old.refresh_from_db()
        self.assertEqual(old.place_id, FID)
        self.assertEqual(old.slug, "little-kingdom-childcare")
        self.assertTrue(old.is_featured)
        self.assertEqual(old.latitude, 33.6917577)
        self.assertIsNotNone(old.last_seen_at)

    def test_reimport_is_idempotent(self):
        self.run_import([self.item()])
        self.run_import([self.item()])
        self.assertEqual(DaycareListing.objects.count(), 1)
        self.assertEqual(Review.objects.count(), 1)

    def test_same_name_far_away_is_a_new_branch(self):
        self.legacy_listing(place_id="x", maps_url="", latitude=33.52, longitude=73.09)
        self.run_import([self.item(place_id="", google_maps_url="")])
        slugs = sorted(DaycareListing.objects.values_list("slug", flat=True))
        self.assertEqual(slugs, ["little-kingdom-childcare", "little-kingdom-childcare-1"])

    def test_same_name_nearby_without_id_matches(self):
        self.legacy_listing(place_id="x", maps_url="", latitude=33.6918, longitude=73.2200)
        self.run_import([self.item(place_id="", google_maps_url="")])
        self.assertEqual(DaycareListing.objects.count(), 1)

    def test_listing_type_imported_and_shown(self):
        self.run_import([self.item(listing_type="preschool"),
                         self.item(name="Other", place_id="0x5:0x6", google_maps_url="",
                                   latitude=33.6, longitude=73.0)])
        pre, other = DaycareListing.objects.order_by("pk")
        self.assertEqual(pre.listing_type, "preschool")
        self.assertEqual(other.listing_type, "daycare")   # no type in the scrape: default
        with site_settings("pk"):
            self.assertContains(self.client.get(pre.get_absolute_url()), "Preschool / Montessori")
        with site_settings("gcc"):
            self.assertContains(self.client.get(pre.get_absolute_url()), "<span>Nursery</span>", html=True)

    def test_areas_from_areas_file(self):
        out = self.run_import([
            self.item(),                                           # "F-7/4" in the address
            self.item(name="Bani", place_id="0x7:0x8", google_maps_url="",
                      address="Plot 58, Bani Gala Greens, Bani Gala, 44000, Pakistan",
                      latitude=33.71, longitude=73.15),
            self.item(name="Nowhere", place_id="0x9:0xa", google_maps_url="",
                      address="Trail 5, Pakistan", latitude=33.76, longitude=73.03),
        ])
        f7, bani, nowhere = DaycareListing.objects.order_by("pk")
        self.assertEqual((f7.area.name, f7.sub_area), ("F-7", "F-7/4"))
        self.assertEqual((bani.area.name, bani.sub_area), ("Bani Gala", ""))
        self.assertIsNone(nowhere.area)
        # Only official areas are created, never "F-7/4"
        self.assertEqual(sorted(Area.objects.values_list("name", flat=True)), ["Bani Gala", "F-7"])
        self.assertIn("F-7 1, Bani Gala 1", out)
        self.assertIn("No area (1)", out)
        self.assertIn("Nowhere", out)

        resp = self.client.get(f7.get_absolute_url())
        self.assertContains(resp, "F-7/4, F-7")
        # A listing with no area still shows on the city page
        self.assertContains(self.client.get(self.city.get_absolute_url()), "Nowhere")

    def test_excluded_area_skipped_at_import(self):
        out = self.run_import([self.item(name="Rawat Kids", place_id="0xb:0xc", google_maps_url="",
                                         address="F5WV+F6G, Jawa Rd, Rawat, Rawalpindi, 45900, Pakistan")])
        self.assertEqual(DaycareListing.objects.count(), 0)
        self.assertIn("skipped (excluded area: Rawat): 1", out)

    def test_dry_run_writes_nothing(self):
        self.legacy_listing()
        out = self.run_import([self.item(), self.item(name="New Place", place_id="0x1:0x2",
                                                      google_maps_url="")], "--dry-run")
        self.assertIn("created: 1", out)
        self.assertIn("updated (matched by place ID): 1", out)
        self.assertEqual(DaycareListing.objects.count(), 1)
        self.assertEqual(DaycareListing.objects.get().place_id, "Little+Kingdom+Childcare")


class ImportProtectionTests(ImportTestBase):
    def test_missing_scraped_value_does_not_blank_field(self):
        old = self.legacy_listing()
        self.run_import([self.item(phone="")])
        old.refresh_from_db()
        self.assertEqual(old.phone, "051 111222")

    def test_locked_fields_are_kept(self):
        old = self.legacy_listing(description="Written by hand", locked_fields=["description", "phone"])
        self.run_import([self.item(description="Scraped text")])
        old.refresh_from_db()
        self.assertEqual(old.description, "Written by hand")
        self.assertEqual(old.phone, "051 111222")
        self.assertEqual(old.rating, 4.5)   # unlocked field still updates

    def test_reviews_kept_when_scrape_found_none(self):
        old = self.legacy_listing()
        Review.objects.create(listing=old, author="Ali", rating=4, text="Nice")
        self.run_import([self.item(reviews=[])])
        self.assertEqual(list(old.reviews.values_list("author", flat=True)), ["Ali"])


class ImportPhotoTests(ImportTestBase):
    def write_photo(self, rel):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg_bytes())

    def test_downloaded_photos_are_copied_to_media(self):
        self.write_photo("images/a/1.jpg")
        out = self.run_import([self.item(images=[
            {"url": "https://lh3.googleusercontent.com/p/x=w1200-h800", "alt": "Front", "file": "images/a/1.jpg"},
            {"url": "https://lh3.googleusercontent.com/gps-cs-s/y=w1200-h800", "alt": ""},   # no file
        ])])
        img = ListingImage.objects.get()
        self.assertTrue(Path(img.image.path).is_file())
        self.assertEqual(img.alt, "Front")
        self.assertIn("photos skipped (no downloaded file): 1", out)

    def test_existing_photos_kept_when_scrape_found_none(self):
        self.write_photo("images/a/1.jpg")
        self.run_import([self.item(images=[{"url": "", "file": "images/a/1.jpg"}])])
        self.run_import([self.item(images=[])])
        self.assertEqual(ListingImage.objects.count(), 1)

    def test_replaced_photo_file_is_deleted(self):
        self.write_photo("images/a/1.jpg")
        self.run_import([self.item(images=[{"url": "", "file": "images/a/1.jpg"}])])
        old_path = Path(ListingImage.objects.get().image.path)
        with self.captureOnCommitCallbacks(execute=True):
            self.run_import([self.item(images=[{"url": "", "file": "images/a/1.jpg"}])])
        self.assertFalse(old_path.exists())
        self.assertTrue(Path(ListingImage.objects.get().image.path).is_file())

    def test_detail_page_shows_local_photo(self):
        self.write_photo("images/a/1.jpg")
        self.run_import([self.item(images=[{"url": "", "file": "images/a/1.jpg"}])])
        listing = DaycareListing.objects.get()
        resp = self.client.get(listing.get_absolute_url())
        self.assertContains(resp, ListingImage.objects.get().image.url)
        self.assertNotContains(resp, "googleusercontent")


class DedupeTests(ImportTestBase):
    def test_merges_duplicates_and_redirects_old_url(self):
        keeper = self.legacy_listing()
        dup = DaycareListing.objects.create(
            name="Little Kingdom Childcare", slug="little-kingdom-childcare-1", city=self.city,
            place_id="ChIJleKfRADp3zgRObOapU_d8FI", maps_url=MAPS_URL,
            latitude=33.69, longitude=73.21, is_verified=True,
        )
        Review.objects.create(listing=dup, author="Sara", rating=5, text="Great")
        dup_url = dup.get_absolute_url()

        call_command("dedupe_listings", stdout=StringIO())
        self.assertEqual(DaycareListing.objects.count(), 2)   # dry run

        call_command("dedupe_listings", "--apply", stdout=StringIO())
        self.assertEqual(list(DaycareListing.objects.values_list("pk", flat=True)), [keeper.pk])
        keeper.refresh_from_db()
        self.assertEqual(keeper.place_id, FID)
        self.assertEqual(keeper.latitude, 33.69)
        self.assertTrue(keeper.is_verified)
        self.assertEqual(keeper.reviews.count(), 1)
        self.assertTrue(Redirect.objects.filter(old_path=dup_url).exists())

        resp = self.client.get(dup_url)
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], keeper.get_absolute_url())


class AdminLockingTests(ImportTestBase):
    def test_editing_a_field_in_admin_locks_it(self):
        listing = self.legacy_listing()
        User.objects.create_superuser("admin", "", "pw")
        self.client.login(username="admin", password="pw")
        url = f"/admin/listings/daycarelisting/{listing.pk}/change/"
        form = self.client.get(url).context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None}
        data.update({
            "name": listing.name, "slug": listing.slug, "city": self.city.pk,
            "phone": "0333 7654321", "rating": 0, "review_count": 0,
            "latitude": 0, "longitude": 0, "categories": "[]", "hours": "{}",
            "locked_fields": "[]", "is_active": "on",
            # Hidden inputs the admin renders for fields with callable defaults
            "initial-categories": "[]", "initial-hours": "{}", "initial-locked_fields": "[]",
            "images-TOTAL_FORMS": 0, "images-INITIAL_FORMS": 0,
            "reviews-TOTAL_FORMS": 0, "reviews-INITIAL_FORMS": 0,
        })
        data.pop("area", None)
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None)
                         and resp.context["adminform"].form.errors)
        listing.refresh_from_db()
        self.assertEqual(listing.locked_fields, ["phone"])


@override_settings(SITE_DOMAIN="daycares.example", CONTACT_EMAIL="hello@daycares.example")
class SitePagesTests(TestCase):
    def test_pages_render_and_are_linked(self):
        for url, text in [("/about/", "What we list"), ("/privacy-policy/", "Google Ads Settings"),
                          ("/contact/", "hello@daycares.example")]:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertContains(resp, text)
                # Footer links on every page
                for link in ("/about/", "/privacy-policy/", "/contact/"):
                    self.assertContains(resp, f'href="{link}"')

    def test_robots_txt(self):
        resp = self.client.get("/robots.txt")
        self.assertEqual(resp["Content-Type"], "text/plain")
        self.assertContains(resp, "Disallow: /admin/")
        self.assertContains(resp, "Sitemap: http://testserver/sitemap.xml")

    def test_sitemap_uses_site_domain(self):
        call_command("sync_site", stdout=StringIO())
        city = City.objects.create(name="Islamabad", slug="islamabad", country=Country.for_code("PK"))
        DaycareListing.objects.create(name="Little Kingdom", city=city)
        resp = self.client.get("/sitemap.xml")
        self.assertContains(resp, "<loc>http://daycares.example/pakistan/</loc>")
        self.assertContains(resp, "<loc>http://daycares.example/pakistan/islamabad/</loc>")
        self.assertContains(resp, "<loc>http://daycares.example/pakistan/islamabad/little-kingdom/</loc>")
        self.assertContains(resp, "<loc>http://daycares.example/privacy-policy/</loc>")
        self.assertNotContains(resp, "example.com")


def site_settings(site):
    """override_settings for running the tests as another site (config/sites.py)."""
    from django.conf import settings
    from config.sites import SITES
    templates = [{**settings.TEMPLATES[0],
                  "DIRS": [settings.BASE_DIR / "templates" / "sites" / site, settings.BASE_DIR / "templates"]}]
    return override_settings(SITE=site, SITE_CONFIG=SITES[site], TEMPLATES=templates)


# The home page is cached (cache_page): a page cached as one site must not leak into another
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
class SiteConfigTests(TestCase):
    def setUp(self):
        self.city = City.objects.create(name="Dubai", slug="dubai", country=Country.for_code("AE"))
        area = Area.objects.create(city=self.city, name="Umm Suqeim", slug="umm-suqeim")
        self.listing = DaycareListing.objects.create(
            name="Tiny Tots Nursery", slug="tiny-tots-nursery", city=self.city, area=area,
            address="Villa 12, Al Wasl Rd - Umm Suqeim 2 - Dubai - United Arab Emirates",
        )

    def test_uae_site(self):
        with site_settings("gcc"):
            home = self.client.get("/")
            self.assertContains(home, "<title>Nurseries in the UAE — Find the Best Nurseries</title>", html=True)
            self.assertContains(home, "GulfNurseries")
            self.assertNotContains(home, "Pakistan")
            self.assertContains(self.client.get("/uae/dubai/"), "Nurseries in Dubai")
            self.assertContains(self.client.get("/uae/dubai/umm-suqeim/"), "Nurseries in Umm Suqeim, Dubai")
            detail = self.client.get(self.listing.get_absolute_url())
            self.assertContains(detail, '"addressCountry": "AE"')
            self.assertContains(detail, "Dubai, United Arab Emirates")
            self.assertContains(detail, "Tiny Tots Nursery — Nursery in Umm Suqeim")
            about = self.client.get("/about/")
            self.assertContains(about, "FS1, FS2 or KG")
            self.assertNotContains(about, "Islamabad")

    def test_pakistan_site(self):
        with site_settings("pk"):
            self.assertContains(self.client.get("/uae/dubai/"), "Daycare Centers in Dubai")
            city = City.objects.create(name="Islamabad", slug="islamabad", country=Country.for_code("PK"))
            listing = DaycareListing.objects.create(name="Little Kingdom", city=city)
            self.assertContains(self.client.get(listing.get_absolute_url()), '"addressCountry": "PK"')
            self.assertContains(self.client.get("/about/"), "Islamabad")

    def test_site_name_override(self):
        from config.sites import SITES
        with override_settings(SITE_CONFIG={**SITES["gcc"], "name": "Little Steps"}):
            self.assertContains(self.client.get("/contact/"), "<title>Contact Little Steps</title>", html=True)

    def test_short_address_uae_format(self):
        self.assertEqual(self.listing.short_address, "Villa 12")
        self.listing.address = "Al Wasl Rd - Umm Suqeim 2 - Dubai - United Arab Emirates"
        self.assertEqual(self.listing.short_address, "Al Wasl Rd")


class ArabicNameImportTests(ImportTestBase):
    def test_arabic_only_name_gets_readable_slug(self):
        with site_settings("gcc"):
            self.run_import([self.item(name="حضانة الأطفال", place_id="0xd:0xe", google_maps_url="")])
        self.assertEqual(DaycareListing.objects.get().slug, "nursery-f-7")


class ImportRulesTests(ImportTestBase):
    def test_placeholder_pin_and_rules_skipped(self):
        pin = dict(latitude=33.70, longitude=73.05, address="Islamabad", categories=["Nursery school"])
        items = [self.item(name=f"Ghost {i}", place_id=f"0x{i}:0x1", google_maps_url="", **pin) for i in range(5)]
        items.append(self.item(name="Fun Kids Amusement Arcade", place_id="0x9:0x9", google_maps_url="",
                               categories=["Preschool"]))
        items.append(self.item(categories=["Nursery school"]))
        out = self.run_import(items)
        self.assertEqual(list(DaycareListing.objects.values_list("name", flat=True)), ["Little Kingdom Childcare"])
        self.assertIn("skipped (placeholder map pin shared by many places): 5", out)
        self.assertIn("Fun Kids Amusement Arcade (not a nursery (name))", out)


class NurseryDetailsTests(ImportTestBase):
    def listing(self, **kw):
        fields = dict(name="Tiny Tots", slug="tiny-tots", city=self.city, age_from_months=1.5,
                      age_to_months=60, curriculum=["eyfs", "montessori"], licensed_by="KHDA",
                      fees_from_aed=40755, fees_to_aed=52800, fees_note="3-5 days a week, 2026-27",
                      details_source="https://tinytots.example/fees")
        fields.update(kw)
        return DaycareListing.objects.create(**fields)

    def test_labels(self):
        from listings.models import months_label
        self.assertEqual([months_label(m) for m in (1.5, 3, 6, 12, 18, 24, 60)],
                         ["45 days", "3 months", "6 months", "1 year", "18 months", "2 years", "5 years"])
        l = self.listing()
        self.assertEqual(l.age_range_label, "45 days – 5 years")
        self.assertEqual(l.curriculum_labels, ["British (EYFS)", "Montessori"])
        self.assertEqual(l.fees_label, "AED 40,755 – 52,800 a year")
        self.assertEqual(self.listing(slug="b", fees_from_aed=30000, fees_to_aed=None).fees_label,
                         "from AED 30,000 a year")

    def test_details_shown_only_when_confirmed(self):
        l = self.listing()
        self.assertNotContains(self.client.get(l.get_absolute_url()), "Curriculum")
        l.details_confirmed = True
        l.save()
        resp = self.client.get(l.get_absolute_url())
        for text in ("Curriculum", "45 days – 5 years", "British (EYFS), Montessori",
                     "KHDA (Dubai)", "AED 40,755 – 52,800 a year", "https://tinytots.example/fees"):
            self.assertContains(resp, text)

    def test_apply_details_keeps_confirmed(self):
        new = DaycareListing.objects.create(name="New", slug="new", city=self.city)
        done = self.listing(details_confirmed=True)
        path = self.tmp / "details.json"
        path.write_text(json.dumps([{"url": "https://x.example/", "listing_ids": [new.pk, done.pk],
                                     "source": "https://x.example/about", "age_from_months": 3,
                                     "age_to_months": 48, "curriculum": ["reggio"], "licensed_by": "KHDA",
                                     "fees_from_aed": None}]), encoding="utf-8")
        out = StringIO()
        call_command("apply_details", "--file", str(path), stdout=out)
        self.assertIn("Listings updated: 1; skipped (details already confirmed): 1", out.getvalue())
        new.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual((new.age_range_label, new.curriculum, new.details_confirmed), ("3 months – 4 years", ["reggio"], False))
        self.assertEqual(new.details_source, "https://x.example/about")
        self.assertEqual(done.curriculum, ["eyfs", "montessori"])   # untouched

    def test_apply_details_rejects_unknown_curriculum(self):
        from django.core.management.base import CommandError
        path = self.tmp / "details.json"
        path.write_text(json.dumps([{"url": "u", "listing_ids": [], "curriculum": ["astrology"]}]), encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command("apply_details", "--file", str(path), stdout=StringIO())

    def test_apply_details_from_csv(self):
        l = DaycareListing.objects.create(name="Csv", slug="csv", city=self.city)
        path = self.tmp / "details.csv"
        path.write_text(
            "listing_ids,names,areas,website,ages_from,ages_to,curriculum,licensed_by,fees_from_aed,"
            "fees_to_aed,fees_note,source,check,evidence_ages,evidence_curriculum,evidence_licence\n"
            f"{l.pk},Csv,,https://csv.example/,45 days,5 years,British (EYFS); montessori,KHDA,27200,54000,"
            "\"3-5 days a week, 2026-27\",https://csv.example/fees,,,,\n", encoding="utf-8-sig")
        call_command("apply_details", "--file", str(path), "--confirm", stdout=StringIO())
        l.refresh_from_db()
        self.assertEqual((l.age_range_label, l.curriculum, l.licensed_by, l.fees_label, l.fees_note, l.details_confirmed),
                         ("45 days – 5 years", ["eyfs", "montessori"], "KHDA", "AED 27,200 – 54,000 a year",
                          "3-5 days a week, 2026-27", True))

    def test_correct_details(self):
        from django.core.management.base import CommandError
        a = DaycareListing.objects.create(name="Branch A", slug="a", city=self.city)
        b = DaycareListing.objects.create(name="Branch B", slug="b", city=self.city)
        header = ("listing_ids,names,areas,website,ages_from,ages_to,curriculum,licensed_by,fees_from_aed,"
                  "fees_to_aed,fees_note,source,check,evidence_ages,evidence_curriculum,evidence_licence\n")
        details = self.tmp / "details.csv"
        details.write_text(header + f'"{a.pk} {b.pk}",A | B,,https://chain.example/,18 months,2 years,'
                           "British (EYFS); Waldorf / Steiner,,,,,,,,,\n", encoding="utf-8-sig")
        fixes = self.tmp / "fixes.csv"
        cols = "listing_ids,name,areas,field,current_value,correct_value,what_the_site_says,page_checked,note\n"
        fixes.write_text(cols + f"{a.pk},Branch A,,ages_from,18 months,birth (0),,https://chain.example/a,\n"
                         f"{a.pk},Branch A,,curriculum,British (EYFS); Waldorf / Steiner,British (EYFS),,,\n",
                         encoding="utf-8-sig")
        call_command("correct_details", "--details", str(details), "--corrections", str(fixes), stdout=StringIO())
        rows = list(csv.DictReader(details.open(encoding="utf-8-sig")))
        by_id = {r["listing_ids"]: r for r in rows}
        self.assertEqual(len(rows), 2)   # the chain row was split per branch
        self.assertEqual((by_id[str(a.pk)]["ages_from"], by_id[str(a.pk)]["curriculum"]), ("45 days", "British (EYFS)"))
        self.assertEqual(by_id[str(b.pk)]["ages_from"], "18 months")   # the other branch untouched
        # A correction that doesn't match the CSV stops the run
        fixes.write_text(cols + f"{b.pk},Branch B,,ages_to,9 years,5 years,,,\n", encoding="utf-8-sig")
        with self.assertRaises(CommandError):
            call_command("correct_details", "--details", str(details), "--corrections", str(fixes), stdout=StringIO())


class CollectEmailTests(TestCase):
    def test_find_emails(self):
        from listings.management.commands.collect_details import decode_cfemail, find_emails

        # Cloudflare encoding of "hi@nursery.ae" with key 0x42
        code = "42" + "".join(f"{ord(c) ^ 0x42:02x}" for c in "hi@nursery.ae")
        self.assertEqual(decode_cfemail(code), "hi@nursery.ae")
        links = [("mailto:Admissions@Nursery.ae?subject=Hello", "Email us"),
                 (f"/cdn-cgi/l/email-protection#{code}", "[email protected]"), ("/about", "About")]
        text = "Call us or write to info@nursery.ae. Logo: logo@2x.png, admissions@nursery.ae"
        self.assertEqual(find_emails(text, links),
                         ["admissions@nursery.ae", "hi@nursery.ae", "info@nursery.ae"])


class ApplyReviewTests(ImportTestBase):
    FIELDS = ["id", "decision", "name", "email", "ages_from", "ages_to", "curriculum", "licensed_by", "details_source"]

    def write(self, rows):
        path = self.tmp / "review.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, self.FIELDS)
            w.writeheader()
            w.writerows([{k: r.get(k, "") for k in self.FIELDS} for r in rows])
        return path

    def test_apply_review(self):
        keep = DaycareListing.objects.create(name="Keep", slug="keep", city=self.city)
        junk = DaycareListing.objects.create(name="Junk", slug="junk", city=self.city)
        done = DaycareListing.objects.create(name="Done", slug="done", city=self.city, curriculum=["eyfs"],
                                             details_confirmed=True)
        path = self.write([
            {"id": keep.pk, "decision": "keep", "name": "Keep", "email": "Info@Keep.ae", "ages_from": "45 days",
             "ages_to": "4 years", "curriculum": "British (EYFS); Reggio Emilia", "licensed_by": "ADEK",
             "details_source": "https://keep.ae/"},
            {"id": junk.pk, "decision": "remove", "name": "Junk", "email": "x@junk.ae"},
            {"id": done.pk, "decision": "keep", "name": "Done", "email": "hi@done.ae", "curriculum": "Montessori"},
        ])
        out = StringIO()
        call_command("apply_review", "--file", str(path), "--dry-run", stdout=out)
        self.assertIn("(dry run", out.getvalue())
        self.assertTrue(DaycareListing.objects.get(pk=junk.pk).is_active)

        out = StringIO()
        call_command("apply_review", "--file", str(path), stdout=out)
        self.assertIn("Switched off: 1; renamed: 0; emails set: 2; details loaded (confirmed): 1; "
                      "details already confirmed, left as they were: 1", out.getvalue())
        keep.refresh_from_db(); junk.refresh_from_db(); done.refresh_from_db()
        self.assertEqual((keep.email, keep.age_range_label, keep.curriculum, keep.licensed_by, keep.details_confirmed),
                         ("info@keep.ae", "45 days – 4 years", ["eyfs", "reggio"], "ADEK", True))
        self.assertFalse(junk.is_active)
        self.assertEqual(junk.email, "")
        self.assertEqual((done.email, done.curriculum), ("hi@done.ae", ["eyfs"]))
        self.assertContains(self.client.get(keep.get_absolute_url()), "mailto:info@keep.ae")

    def test_rename(self):
        from django.contrib.redirects.models import Redirect
        self.FIELDS = self.FIELDS + ["new_name"]
        house = DaycareListing.objects.create(name="Centre house", slug="centre-house", city=self.city)
        old_url = house.get_absolute_url()
        call_command("apply_review", "--file", str(self.write([
            {"id": house.pk, "decision": "keep", "name": "Centre house", "new_name": "Alma's Day Care"}])),
            stdout=StringIO())
        house.refresh_from_db()
        self.assertEqual((house.name, house.slug), ("Alma's Day Care", "almas-day-care"))
        self.assertIn("name", house.locked_fields)
        self.assertEqual(Redirect.objects.get(old_path=old_url).new_path, house.get_absolute_url())

    def test_undecided_row_stops(self):
        from django.core.management.base import CommandError
        path = self.write([{"id": 1, "decision": "check", "name": "X"}])
        with self.assertRaises(CommandError):
            call_command("apply_review", "--file", str(path), stdout=StringIO())


class RematchAreasTests(ImportTestBase):
    def test_rematch_areas(self):
        city = City.objects.create(name="Al Ain", slug="al-ain", country=Country.for_code("AE"))
        fouah = DaycareListing.objects.create(name="Sugar Bits Nursery", slug="sugar", city=city,
                                              address="Al Fou'ah - Al Sajaa - Abu Dhabi - United Arab Emirates")
        locked = DaycareListing.objects.create(name="Locked Nursery", slug="locked", city=city, locked_fields=["area"],
                                               address="Villa 5 - Al Jimi - Al Ain - Abu Dhabi - United Arab Emirates")
        out = StringIO()
        call_command("rematch_areas", "--city", "Al Ain", "--dry-run", stdout=out)
        self.assertIn("Changed: 1; locked, left alone: 1", out.getvalue())
        self.assertIsNone(DaycareListing.objects.get(pk=fouah.pk).area)
        call_command("rematch_areas", "--city", "Al Ain", stdout=StringIO())
        fouah.refresh_from_db(); locked.refresh_from_db()
        self.assertEqual(fouah.area.name, "Al Foah")
        self.assertIsNone(locked.area)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
class CountryUrlTests(TestCase):
    def setUp(self):
        self.uae = Country.for_code("AE")
        self.dubai = City.objects.create(name="Dubai", slug="dubai", country=self.uae)
        self.area = Area.objects.create(city=self.dubai, name="Al Barsha", slug="al-barsha")
        self.listing = DaycareListing.objects.create(
            name="Little Acorns Nursery", city=self.dubai, area=self.area, rating=4.9, review_count=312)

    def test_urls(self):
        self.assertEqual(self.uae.get_absolute_url(), "/uae/")
        self.assertEqual(self.dubai.get_absolute_url(), "/uae/dubai/")
        self.assertEqual(self.area.get_absolute_url(), "/uae/dubai/al-barsha/")
        self.assertEqual(self.listing.get_absolute_url(), "/uae/dubai/little-acorns-nursery/")
        for url, text in [("/", 'href="/uae/"'), ("/uae/", "Nurseries in the UAE"),
                          ("/uae/dubai/", "Nurseries in Dubai"),
                          ("/uae/dubai/al-barsha/", "Nurseries in Al Barsha, Dubai"),
                          ("/uae/dubai/little-acorns-nursery/", "Little Acorns Nursery")]:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), text)

    def test_not_found(self):
        Country.for_code("QA")   # no listings: no country page
        for url in ["/qatar/", "/qatar/dubai/", "/dubai/", "/uae/abu-dhabi/", "/uae/dubai/nowhere/",
                    "/dubai/little-acorns-nursery/detail/", "/uae/dubai/little-acorns-nursery/detail/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_switched_off_listing_not_found(self):
        self.listing.is_active = False
        self.listing.save()
        self.assertEqual(self.client.get("/uae/dubai/little-acorns-nursery/").status_code, 404)

    def test_country_page_top_rated_needs_50_reviews(self):
        DaycareListing.objects.create(name="Few Reviews Nursery", city=self.dubai, rating=5, review_count=3)
        resp = self.client.get("/uae/")
        self.assertContains(resp, 'href="/uae/dubai/"')
        self.assertContains(resp, "Little Acorns Nursery")
        self.assertNotContains(resp, "Few Reviews Nursery")

    def test_coverage_follows_live_countries(self):
        home = self.client.get("/")
        self.assertContains(home, "<title>Nurseries in the UAE — Find the Best Nurseries</title>", html=True)
        doha = City.objects.create(name="Doha", slug="doha", country=Country.for_code("QA"))
        DaycareListing.objects.create(name="Pearl Nursery", city=doha)
        home = self.client.get("/")
        self.assertContains(home, "<title>Nurseries in the Gulf — Find the Best Nurseries</title>", html=True)
        self.assertContains(home, 'href="/qatar/"')
        self.assertContains(self.client.get("/qatar/doha/pearl-nursery/"), '"addressCountry": "QA"')

    def test_listing_slug_avoids_area_slug(self):
        listing = DaycareListing.objects.create(name="Al Barsha", city=self.dubai)
        self.assertEqual(listing.slug, "al-barsha-1")
        self.assertContains(self.client.get(listing.get_absolute_url()), "Al Barsha")

    def test_new_area_moves_clashing_listing(self):
        listing = DaycareListing.objects.create(name="Jumeirah", city=self.dubai)
        self.assertEqual(listing.slug, "jumeirah")
        Area.objects.create(city=self.dubai, name="Jumeirah")
        listing.refresh_from_db()
        self.assertEqual(listing.slug, "jumeirah-1")
        self.assertContains(self.client.get("/uae/dubai/jumeirah/"), "Nurseries in Jumeirah, Dubai")
        self.assertEqual(self.client.get("/uae/dubai/jumeirah-1/").status_code, 200)

    def test_admin_validation_stops_clash(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            DaycareListing(name="X", slug="al-barsha", city=self.dubai).clean()
        with self.assertRaises(ValidationError):
            Area(name="Little Acorns", slug="little-acorns-nursery", city=self.dubai).clean()


class ImportCountryTests(ImportTestBase):
    def test_new_city_gets_country_from_queries_file(self):
        self.city.delete()
        Country.objects.all().delete()
        self.run_import([self.item()])
        city = City.objects.get(slug="islamabad")
        self.assertEqual((city.country.code, city.country.slug), ("PK", "pakistan"))
        self.assertEqual(DaycareListing.objects.get().get_absolute_url(),
                         "/pakistan/islamabad/little-kingdom-childcare/")

    def test_city_in_another_country_stops(self):
        from django.core.management.base import CommandError
        self.city.country = Country.for_code("AE")
        self.city.save()
        with self.assertRaises(CommandError):
            self.run_import([self.item()])


class RedirectMigrationTests(TestCase):
    def test_new_path(self):
        import importlib
        new_path = importlib.import_module("listings.migrations.0013_cities_to_country").new_path
        cities = {"dubai", "sharjah"}
        for old, new in [("/dubai/gardenia-nursery/detail/", "/uae/dubai/gardenia-nursery/"),
                         ("/sharjah/almas-day-care/detail/", "/uae/sharjah/almas-day-care/"),
                         ("/dubai/al-barsha/", "/uae/dubai/al-barsha/"),
                         ("/dubai/", "/uae/dubai/"),
                         ("/dubai/kids-detail/", "/uae/dubai/kids-detail/"),
                         ("/about/", "/about/")]:
            with self.subTest(old=old):
                self.assertEqual(new_path(old, cities, "uae"), new)


class HoursTests(TestCase):
    def test_parse_and_label(self):
        from listings import hours as H
        nbsp = " "
        for text, label in [(f"7:30{nbsp}AM–6{nbsp}PM", "7:30–18:00"), ("7–11 AM", "7:00–11:00"),
                            ("11–2 PM", "11:00–14:00"), ("8:30 AM–1:30 PM 4–7 PM", "8:30–13:30, 16:00–19:00"),
                            ("6 PM–2 AM", "18:00–2:00"), ("Open 24 hours", "Open 24 hours"), ("Closed", "Closed")]:
            with self.subTest(text=text):
                self.assertEqual(H.label(text), label)

    def test_summary_and_status(self):
        from datetime import datetime
        from listings import hours as H
        week = {d: "7:30 AM–6 PM" for d in H.DAYS[:5]} | {"Saturday": "Closed", "Sunday": "Closed"}
        self.assertEqual(H.summary(week), "Mon–Fri · 7:30–18:00")
        self.assertEqual(H.summary({**week, "Wednesday": "Closed"}), "Mon, Tue, Thu, Fri · 7:30–18:00")
        thursday = datetime(2026, 10, 8, 10, 0)
        self.assertEqual(H.status(week, thursday), {"open": True, "until": "18:00"})
        self.assertEqual(H.status(week, thursday.replace(hour=19)), {"open": False})
        self.assertEqual(H.status(week, datetime(2026, 10, 10, 10, 0)), {"open": False})   # Saturday
        self.assertTrue(H.status({"Friday": "6 PM–2 AM"}, datetime(2026, 10, 10, 1, 0))["open"])
        self.assertIsNone(H.status({}, thursday))
        self.assertEqual([d["today"] for d in H.week(week, thursday)].index(True), 3)


class PhoneTests(TestCase):
    def test_uae_numbers(self):
        from listings import phones as P
        uae = Country.for_code("AE")
        self.assertEqual(P.local("+971 4 345 1200", uae), "04 345 1200")
        self.assertEqual(P.local("+971 800 4321", uae), "800 4321")
        self.assertEqual(P.tel_url("+971 4 345 1200", uae), "tel:+97143451200")
        self.assertEqual(P.whatsapp_url("+971 50 123 4567", uae), "https://wa.me/971501234567")
        self.assertEqual(P.whatsapp_url("050 123 4567", uae), "https://wa.me/971501234567")
        self.assertEqual(P.whatsapp_url("+971 4 345 1200", uae), "")
        self.assertEqual(P.whatsapp_url("+971 600 522225", uae), "")


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
class AreaPageTests(TestCase):
    def setUp(self):
        self.dubai = City.objects.create(name="Dubai", slug="dubai", country=Country.for_code("AE"))
        self.barsha = Area.objects.create(city=self.dubai, name="Al Barsha", region="Jumeirah & Al Barsha")
        self.jlt = Area.objects.create(city=self.dubai, name="JLT", region="Marina, JLT & Palm")
        open_all_week = {d: "Open 24 hours" for d in ["Monday", "Tuesday", "Wednesday", "Thursday",
                                                       "Friday", "Saturday", "Sunday"]}
        make = DaycareListing.objects.create
        self.a = make(name="Acorns", city=self.dubai, area=self.barsha, rating=4.9, review_count=300,
                      listing_type="preschool", phone="+971 50 123 4567", hours=open_all_week,
                      latitude=25.10, longitude=55.20)
        self.b = make(name="Bumblebee", city=self.dubai, area=self.barsha, rating=4.2, review_count=900,
                      listing_type="daycare", phone="+971 4 345 1200", latitude=25.11, longitude=55.21)
        self.c = make(name="Crayons", city=self.dubai, area=self.jlt, rating=5.0, review_count=2,
                      listing_type="preschool", latitude=25.07, longitude=55.14)

    def names(self, url):
        return [l.name for l in self.client.get(url).context["results"]]

    def test_filters_and_sorts(self):
        url = self.barsha.get_absolute_url()
        self.assertEqual(self.names(url), ["Acorns", "Bumblebee"])
        self.assertEqual(self.names(url + "?sort=reviews"), ["Bumblebee", "Acorns"])
        self.assertEqual(self.names(url + "?rating=4.5"), ["Acorns"])
        self.assertEqual(self.names(url + "?type=daycare"), ["Bumblebee"])
        self.assertEqual(self.names(url + "?open=1"), ["Acorns"])
        resp = self.client.get(url + "?type=daycare&rating=4.5")
        self.assertContains(resp, "0 nurseries in Al Barsha")
        self.assertContains(resp, "Clear filters")

    def test_area_page_parts(self):
        resp = self.client.get(self.barsha.get_absolute_url())
        self.assertContains(resp, 'href="https://wa.me/971501234567"', count=0)   # WhatsApp is on the nursery page
        self.assertContains(resp, 'href="tel:+971501234567"')
        self.assertContains(resp, "Nearby neighbourhoods")
        self.assertContains(resp, self.jlt.get_absolute_url())
        self.assertContains(resp, 'id="map-data"')
        self.assertContains(resp, '"@type": "BreadcrumbList"')

    def test_detail_contact(self):
        resp = self.client.get(self.a.get_absolute_url())
        self.assertContains(resp, "https://wa.me/971501234567")
        self.assertContains(resp, "Open now")
        self.assertContains(resp, "050 123 4567")
        self.assertNotContains(self.client.get(self.b.get_absolute_url()), "wa.me")

    def test_city_regions_and_az(self):
        resp = self.client.get(self.dubai.get_absolute_url())
        self.assertEqual([r for r, _ in resp.context["regions"]], ["Jumeirah & Al Barsha", "Marina, JLT & Palm"])
        Area.objects.update(region="")
        resp = self.client.get(self.dubai.get_absolute_url())
        self.assertEqual([(r, [a.name for a in areas]) for r, areas in resp.context["regions"]],
                         [("", ["Al Barsha", "JLT"])])
        self.assertContains(resp, "regions--az")

    def test_search_and_suggest(self):
        resp = self.client.get("/search/?q=acorn")
        self.assertEqual([l.name for l in resp.context["results"]], ["Acorns"])
        self.assertContains(resp, 'content="noindex, follow"')
        self.assertEqual([l.name for l in self.client.get("/search/?where=dubai/jlt").context["results"]], ["Crayons"])
        self.assertEqual(len(self.client.get("/search/?where=country:uae").context["results"]), 3)
        self.assertContains(self.client.get("/search/suggest/?q=bumble"), self.b.get_absolute_url())
        self.assertNotContains(self.client.get("/search/suggest/?q=b"), "Bumblebee")

    def test_apply_regions(self):
        path = Path(tempfile.mkdtemp()) / "regions.csv"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text("city,area,listings,region\nDubai,Al Barsha,2,Barsha side\nDubai,Nowhere,0,X\n",
                        encoding="utf-8-sig")
        out = StringIO()
        call_command("apply_regions", "--file", str(path), "--dry-run", stdout=out)
        self.assertEqual(Area.objects.get(pk=self.barsha.pk).region, "Jumeirah & Al Barsha")
        call_command("apply_regions", "--file", str(path), stdout=out)
        self.assertEqual(Area.objects.get(pk=self.barsha.pk).region, "Barsha side")
        self.assertIn("not in the file: Dubai / JLT", out.getvalue())
        self.assertIn("no such area: Dubai / Nowhere", out.getvalue())


class ApplyExtraDetailsTests(ImportTestBase):
    FIELDS = ["listing_id", "our_name", "decision", "curriculum", "email", "email_replace", "transport", "meals",
              "facilities", "activities", "fees_from_aed", "fees_to_aed"]

    def write(self, rows):
        path = self.tmp / "extra.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, self.FIELDS)
            w.writeheader()
            w.writerows([{k: r.get(k, "") for k in self.FIELDS} for r in rows])
        return path

    def test_fills_gaps_only(self):
        new = DaycareListing.objects.create(name="New", slug="new", city=self.city)
        ours = DaycareListing.objects.create(
            name="Ours", slug="ours", city=self.city, email="branch@ours.ae", transport=False,
            facilities=["library"], fees_from_aed=30000, fees_to_aed=40000, details_confirmed=True,
            details_source="https://ours.ae/fees")
        moved = DaycareListing.objects.create(name="Moved", slug="moved", city=self.city, email="old@moved.ae")
        closed = DaycareListing.objects.create(name="Closed", slug="closed", city=self.city)
        path = self.write([
            {"listing_id": new.pk, "decision": "keep", "curriculum": "British (EYFS); Scandinavian / Nordic",
             "email": "Info@New.ae", "transport": "yes", "meals": "yes",
             "facilities": "Outdoor play area; Library / reading corner", "activities": "Ballet & dance",
             "fees_from_aed": "36000", "fees_to_aed": "45000"},
            {"listing_id": ours.pk, "decision": "keep", "email": "info@ours.ae", "transport": "yes",
             "facilities": "Swimming pool", "fees_from_aed": "10000", "fees_to_aed": "20000"},
            {"listing_id": moved.pk, "decision": "keep", "email": "new@moved.ae", "email_replace": "yes"},
            {"listing_id": closed.pk, "decision": "remove", "email": "x@closed.ae"},
            {"listing_id": "", "decision": "skip"},
        ])
        out = StringIO()
        call_command("apply_extra_details", "--file", str(path), "--dry-run", stdout=out)
        self.assertIn("(dry run", out.getvalue())
        self.assertEqual(DaycareListing.objects.get(pk=new.pk).facilities, [])

        call_command("apply_extra_details", "--file", str(path), stdout=StringIO())
        for obj in (new, ours, moved, closed):
            obj.refresh_from_db()
        self.assertEqual((new.curriculum, new.email, new.transport, new.meals, new.facilities, new.activities),
                         (["eyfs", "scandinavian"], "info@new.ae", True, True, ["outdoor_play", "library"],
                          ["ballet_dance"]))
        self.assertEqual((new.fees_from_aed, new.fees_to_aed, new.fees_note, new.details_confirmed),
                         (36000, 45000, "approximate", True))
        self.assertTrue(new.fees_approximate)
        # ours wins: email, a known "no" for transport and our own fees stay; facilities are added
        self.assertEqual((ours.email, ours.transport, ours.fees_from_aed, ours.facilities),
                         ("branch@ours.ae", False, 30000, ["swimming_pool", "library"]))
        self.assertEqual(moved.email, "new@moved.ae")
        self.assertEqual((closed.is_active, closed.email), (False, ""))

        page = self.client.get(new.get_absolute_url())
        for text in ["Facilities", "Outdoor play area", "Ballet &amp; dance", "School bus available",
                     "AED 36,000 – 45,000 a year (approximate)", "Scandinavian / Nordic"]:
            self.assertContains(page, text)

    def test_unknown_tag_stops(self):
        from django.core.management.base import CommandError
        listing = DaycareListing.objects.create(name="X", slug="x", city=self.city)
        path = self.write([{"listing_id": listing.pk, "decision": "keep", "facilities": "Helipad"}])
        with self.assertRaises(CommandError):
            call_command("apply_extra_details", "--file", str(path), stdout=StringIO())


class FacilityFilterTests(TestCase):
    def setUp(self):
        self.dubai = City.objects.create(name="Dubai", slug="dubai", country=Country.for_code("AE"))
        self.barsha = Area.objects.create(city=self.dubai, name="Al Barsha", slug="al-barsha")
        make = DaycareListing.objects.create
        for name, facilities in [("Acorns", ["swimming_pool", "library"]), ("Bumblebee", ["swimming_pool"]),
                                 ("Crayons", ["swimming_pool", "art_room"]), ("Daisies", [])]:
            make(name=name, city=self.dubai, area=self.barsha, facilities=facilities, review_count=10)

    def names(self, url):
        return sorted(l.name for l in self.client.get(url).context["results"])

    def test_area_chips_and_filter(self):
        url = self.barsha.get_absolute_url()
        resp = self.client.get(url)
        labels = [f["label"] for f in resp.context["filters"]]
        self.assertIn("Swimming pool", labels)        # 3 listings have it
        self.assertNotIn("Art room", labels)          # only 1: no chip
        resp = self.client.get(url + "?facility=swimming_pool")
        self.assertEqual(sorted(l.name for l in resp.context["results"]), ["Acorns", "Bumblebee", "Crayons"])
        self.assertContains(resp, "whose facilities we've listed")
        self.assertEqual(self.names(url + "?facility=nonsense"), ["Acorns", "Bumblebee", "Crayons", "Daisies"])

    def test_city_chips_link_to_search(self):
        resp = self.client.get(self.dubai.get_absolute_url())
        self.assertContains(resp, "Find by facility")
        self.assertContains(resp, 'href="/search/?where=dubai&amp;facility=swimming_pool"')
        self.assertNotContains(resp, "facility=art_room")
        resp = self.client.get("/search/?where=dubai&facility=swimming_pool")
        self.assertEqual(sorted(l.name for l in resp.context["results"]), ["Acorns", "Bumblebee", "Crayons"])
        self.assertContains(resp, "with: Swimming pool")
        self.assertContains(resp, 'content="noindex, follow"')

    def test_no_chips_without_data(self):
        DaycareListing.objects.update(facilities=[])
        self.assertNotContains(self.client.get(self.dubai.get_absolute_url()), "Find by facility")
