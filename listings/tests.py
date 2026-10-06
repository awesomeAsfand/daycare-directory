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

from listings.models import Area, City, DaycareListing, ListingImage, Review

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
        self.city = City.objects.create(name="Islamabad", slug="islamabad")

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
        self.assertContains(self.client.get(pre.get_absolute_url()), "Preschool / Montessori")

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
        City.objects.create(name="Islamabad", slug="islamabad")
        resp = self.client.get("/sitemap.xml")
        self.assertContains(resp, "<loc>http://daycares.example/islamabad/</loc>")
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
        self.city = City.objects.create(name="Dubai", slug="dubai")
        area = Area.objects.create(city=self.city, name="Umm Suqeim", slug="umm-suqeim")
        self.listing = DaycareListing.objects.create(
            name="Tiny Tots Nursery", slug="tiny-tots-nursery", city=self.city, area=area,
            address="Villa 12, Al Wasl Rd - Umm Suqeim 2 - Dubai - United Arab Emirates",
        )

    def test_uae_site(self):
        with site_settings("uae"):
            home = self.client.get("/")
            self.assertContains(home, "<title>Nurseries in the UAE — Find the Best Nurseries</title>", html=True)
            self.assertContains(home, "NurseriesUAE")
            self.assertNotContains(home, "Pakistan")
            self.assertContains(self.client.get("/dubai/"), "Nurseries in Dubai")
            self.assertContains(self.client.get("/dubai/umm-suqeim/"), "Nurseries in Umm Suqeim, Dubai")
            detail = self.client.get(self.listing.get_absolute_url())
            self.assertContains(detail, '"addressCountry": "AE"')
            self.assertContains(detail, "Dubai, United Arab Emirates")
            self.assertContains(detail, "Tiny Tots Nursery — Nursery in Umm Suqeim")
            about = self.client.get("/about/")
            self.assertContains(about, "FS1, FS2 or KG")
            self.assertNotContains(about, "Islamabad")

    def test_pakistan_site(self):
        with site_settings("pk"):
            self.assertContains(self.client.get("/dubai/"), "Daycare Centers in Dubai")
            self.assertContains(self.client.get(self.listing.get_absolute_url()), '"addressCountry": "PK"')
            self.assertContains(self.client.get("/about/"), "Islamabad")

    def test_site_name_override(self):
        from config.sites import SITES
        with override_settings(SITE_CONFIG={**SITES["uae"], "name": "Little Steps"}):
            self.assertContains(self.client.get("/contact/"), "<title>Contact Little Steps</title>", html=True)

    def test_short_address_uae_format(self):
        self.assertEqual(self.listing.short_address, "Villa 12")
        self.listing.address = "Al Wasl Rd - Umm Suqeim 2 - Dubai - United Arab Emirates"
        self.assertEqual(self.listing.short_address, "Al Wasl Rd")


class ArabicNameImportTests(ImportTestBase):
    def test_arabic_only_name_gets_readable_slug(self):
        with site_settings("uae"):
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
