"""
python manage.py import_listings --city Dubai              # scraper/dubai_listings.json
python manage.py import_listings --city Dubai --dry-run    # report what would change, write nothing
python manage.py import_listings --city Dubai --file scraper/la_marelle_listings.json   # one place

Each scraped place is matched to an existing listing by, in order:
  1. Google place ID (0x...:0x...), also found inside older rows' maps_url
  2. the place_id an older scraper version stored (name or ChIJ... form)
  3. same name within SAME_PLACE_KM, or same name when only one exists
Unmatched places become new listings.

On existing listings the importer never changes slug, is_featured,
is_verified or is_active, never blanks a field because this scrape missed it,
and skips anything listed in the listing's locked_fields (set automatically
when the field is edited in the admin). Reviews and photos are only replaced
when the scrape found new ones.

Areas come only from the city's queries file (scraper/queries/<city>.txt,
[areas] section): see scraper/area_match.py for how a listing is placed.
Listings that can't be placed get no area and still show on the city page.
The report at the end lists listings per area and those left without one.
"""
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from listings.models import Area, City, Country, DaycareListing, ListingImage, Review

# The area rules live with the scraper's queries files
sys.path.insert(0, str(Path(settings.BASE_DIR) / "scraper"))
from area_match import AreaMatcher  # noqa: E402
from search_plan import classify, load_plan, pin_of, placeholder_pins  # noqa: E402

FEATURE_ID_RE = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+")
SAME_PLACE_KM = 0.3


def feature_id(item):
    """Google's stable place ID from the item, or from its maps URL."""
    pid = item.get("place_id", "")
    if FEATURE_ID_RE.fullmatch(pid):
        return pid
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", item.get("google_maps_url", ""))
    return m.group(1) if m else ""


def distance_km(lat1, lng1, lat2, lng2):
    # Equirectangular approximation; accurate to metres at city scale.
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


def is_empty(value):
    return value is None or value == "" or value == 0 or value == [] or value == {}


class Command(BaseCommand):
    help = "Import scraped daycare listings from a JSON file"

    def add_arguments(self, parser):
        parser.add_argument("--file",
                            help="Path to JSON file produced by the scraper "
                                 "(default: scraper/<city>_listings.json)")
        parser.add_argument("--city", required=True,
                            help="City name to assign listings to, e.g. Dubai")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be created/updated without writing")
        parser.add_argument("--areas-file",
                            help="Queries file with the official areas "
                                 "(default: scraper/queries/<city>.txt)")

    def handle(self, *args, **options):
        city_file = slugify(options["city"]).replace("-", "_")   # "Abu Dhabi" -> abu_dhabi
        scraper_dir = Path(settings.BASE_DIR) / "scraper"
        path = Path(options["file"] or scraper_dir / f"{city_file}_listings.json")
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        self.base_dir = path.resolve().parent   # photo "file" paths are relative to this
        self.dry_run = options["dry_run"]

        areas_file = Path(options["areas_file"] or scraper_dir / "queries" / f"{city_file}.txt")
        if not areas_file.exists():
            raise CommandError(f"Areas file not found: {areas_file} (use --areas-file)")
        plan = load_plan(areas_file, options["city"])
        self.plan = plan
        self.matcher = AreaMatcher(plan)
        # Checked again here so areas added to [exclude] after a scrape still apply
        self.exclude = plan.exclude
        self.rule_skips = []
        self.area_counts = Counter()
        self.no_area = []

        raw = json.loads(path.read_text(encoding="utf-8"))
        self.stdout.write(f"Loaded {len(raw)} records from {path}")
        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes."))

        # The country comes from the queries file's [country] section (code = ae)
        code = plan.country.get("code", "").upper()
        if not code:
            raise CommandError(f"{areas_file} has no [country] code (e.g. code = ae)")
        city = City.objects.filter(slug=slugify(options["city"])).select_related("country").first()
        if city and city.country.code != code:
            raise CommandError(f"{city.name} is in {city.country.name}, but {areas_file.name} says country {code}")
        if not city:
            if self.dry_run:
                self.stdout.write(f"City {options['city']!r} would be created.")
                city = City(name=options["city"], slug=slugify(options["city"]))
            else:
                country = Country.for_code(code, plan.country.get("name", ""))
                city = City.objects.create(name=options["city"], slug=slugify(options["city"]), country=country)

        self.stats = Counter()
        self.now = timezone.now()
        seen_ids = set()
        pins = placeholder_pins(raw)

        for item in raw:
            name = item.get("name", "").strip()
            if not name:
                self.stats["skipped (no name)"] += 1
                continue
            excluded = next((a for a in self.exclude if a.lower() in item.get("address", "").lower()), None)
            if excluded:
                self.stats[f"skipped (excluded area: {excluded})"] += 1
                continue
            if pin_of(item) in pins:
                self.stats["skipped (placeholder map pin shared by many places)"] += 1
                continue
            reason = self.rules_reason(item, name)
            if reason:
                self.stats["skipped by the directory rules (listed below)"] += 1
                self.rule_skips.append((name, reason))
                continue
            try:
                with transaction.atomic():
                    obj = self.import_item(city, item, name)
                if obj and obj.pk:
                    seen_ids.add(obj.pk)
            except Exception as e:
                self.stats["failed"] += 1
                self.stderr.write(f"  [error] {name}: {e}")

        if city.pk:
            not_seen = DaycareListing.objects.filter(city=city).exclude(pk__in=seen_ids).count()
            self.stats["existing listings not in this file (left unchanged)"] = not_seen

        self.stdout.write("")
        for key, count in self.stats.items():
            self.stdout.write(f"  {key}: {count}")
        if self.rule_skips:
            self.stdout.write("\nSkipped by the directory rules:")
            for name, reason in sorted(self.rule_skips, key=lambda s: s[1]):
                self.stdout.write(f"  {name} ({reason})")
        self.area_report()
        self.stdout.write(self.style.SUCCESS("\nDone." + (" (dry run)" if self.dry_run else "")))

    def rules_reason(self, item, name):
        """Why the directory's current rules (search_plan.classify) leave this
        place out, or "". The scraper checked it already; checking again here
        means rules changed after a scrape still apply. Records without
        categories (older files) are not re-checked."""
        if not item.get("categories"):
            return ""
        lat, lng = float(item.get("latitude") or 0), float(item.get("longitude") or 0)
        _, reason = classify(name, item["categories"], item.get("address", ""), self.plan.city,
                             item.get("closed", False), lat, lng, self.plan.boundary, self.exclude,
                             keep=feature_id(item) in self.plan.keep)
        return reason

    # ── Matching ─────────────────────────────────────────────────────────────

    def find_existing(self, city, item, fid, name):
        if not city.pk:
            return None, None
        qs = DaycareListing.objects.filter(city=city).order_by("pk")

        if fid:
            hits = list(qs.filter(place_id=fid)) or list(qs.filter(maps_url__contains=f"!1s{fid}"))
            if hits:
                return self.pick(hits, name), "place ID"

        old_id = item.get("place_id", "")
        if old_id and old_id != fid:
            hits = list(qs.filter(place_id=old_id))
            if hits:
                return self.pick(hits, name), "old-style ID"

        candidates = list(qs.filter(name__iexact=name))
        lat, lng = float(item.get("latitude") or 0), float(item.get("longitude") or 0)
        if lat and lng:
            close = [o for o in candidates
                     if o.latitude and distance_km(lat, lng, o.latitude, o.longitude) <= SAME_PLACE_KM]
            if close:
                return self.pick(close, name), "name + location"
            # A same-named listing far away is another branch, not this place.
            candidates = [o for o in candidates if not o.latitude]
        if len(candidates) == 1:
            return candidates[0], "name only"
        return None, None

    def pick(self, hits, name):
        if len(hits) > 1:
            self.stats["matched more than one listing (run dedupe_listings)"] += 1
            self.stderr.write(f"  [duplicate] {name}: {len(hits)} listings match, updating the oldest")
        return hits[0]

    # ── Import ───────────────────────────────────────────────────────────────

    def record_area(self, name, item, place):
        if place.area:
            self.area_counts[place.area] += 1
            self.stats[f"area from {place.method}"] += 1
        else:
            self.stats["no area"] += 1
            self.no_area.append((name, item.get("address", "")))

    def area_report(self):
        out = self.stdout.write
        out("\nListings per area:")
        listed = [(a, self.area_counts.get(a, 0)) for a in self.matcher.areas]
        out("  " + ", ".join(f"{a} {n}" for a, n in listed if n))
        empty = [a for a, n in listed if not n]
        if empty:
            out(f"  Areas with no listings ({len(empty)}): " + ", ".join(empty))
        if self.no_area:
            out(f"\nNo area ({len(self.no_area)}) — shown on the city page only:")
            for name, address in self.no_area:
                out(f"  - {name[:45]:45} | {address[:70]}")

    def resolve_area(self, city, area_name):
        if not area_name:
            return None
        if self.dry_run or not city.pk:
            return Area.objects.filter(city=city, slug=slugify(area_name)).first() if city.pk else None
        area, _ = Area.objects.get_or_create(
            city=city, slug=slugify(area_name), defaults={"name": area_name},
        )
        return area

    def import_item(self, city, item, name):
        fid = feature_id(item)
        obj, how = self.find_existing(city, item, fid, name)

        listing_type = item.get("listing_type", "")
        lat, lng = float(item.get("latitude") or 0), float(item.get("longitude") or 0)
        place = self.matcher.match(item.get("address", ""), lat, lng, name)
        self.record_area(name, item, place)
        values = {
            "name":         name,
            "listing_type": listing_type if listing_type in dict(DaycareListing.TYPE_CHOICES) else "",
            "area":         self.resolve_area(city, place.area),
            "sub_area":     place.sub_area,
            "address":      item.get("address", ""),
            "phone":        item.get("phone", ""),
            "website":      item.get("website", ""),
            "description":  item.get("description", ""),
            "rating":       float(item.get("rating") or 0),
            "review_count": int(item.get("review_count") or 0),
            "latitude":     lat,
            "longitude":    lng,
            "categories":   item.get("categories") or [],
            "hours":        item.get("hours") or {},
            "maps_url":     item.get("google_maps_url", ""),
        }

        if obj is None:
            self.stats["created"] += 1
            if self.dry_run:
                self.stdout.write(f"  [new] {name}")
                return None
            obj = DaycareListing.objects.create(
                city=city,
                slug=self.unique_slug(city, name, values["area"] and values["area"].name),
                place_id=fid or item.get("place_id", ""),
                last_seen_at=self.now,
                **{k: v for k, v in values.items() if not (k == "listing_type" and not v)},
            )
        else:
            self.stats[f"updated (matched by {how})"] += 1
            if self.dry_run:
                if how != "place ID":
                    self.stdout.write(f"  [match by {how}] {name} -> {obj.slug}")
                return obj
            locked = set(obj.locked_fields or [])
            for field, value in values.items():
                if field not in locked and not is_empty(value):
                    setattr(obj, field, value)
            if fid:
                obj.place_id = fid
            obj.last_seen_at = self.now
            obj.save()

        self.import_reviews(obj, item)
        self.import_images(obj, item)
        return obj

    def unique_slug(self, city, name, area=None):
        # A name in Arabic script slugifies to nothing: fall back to
        # "nursery-al-barsha" (the site's noun and the area or city)
        base = slugify(name)[:290] or slugify(f"{settings.SITE_CONFIG['noun']} {area or city.name}")
        return DaycareListing.unique_slug(city, base)

    def import_reviews(self, obj, item):
        reviews = [r for r in item.get("reviews") or [] if r.get("author") or r.get("text")]
        if not reviews or "reviews" in (obj.locked_fields or []):
            return
        obj.reviews.all().delete()
        Review.objects.bulk_create([
            Review(
                listing=obj,
                author=r.get("author", "")[:255],
                rating=int(r.get("rating") or 0),
                text=r.get("text", ""),
                date=r.get("date", "")[:100],
            )
            for r in reviews
        ])

    def import_images(self, obj, item):
        images = item.get("images") or []
        files = [(img, self.base_dir / img["file"]) for img in images if img.get("file")]
        files = [(img, p) for img, p in files if p.is_file()]
        if len(files) < len(images):
            # Bare Google URLs expire within months, so they are never imported.
            self.stats["photos skipped (no downloaded file)"] += len(images) - len(files)
        if not files or "images" in (obj.locked_fields or []):
            return
        obj.images.all().delete()
        for i, (img, p) in enumerate(files):
            li = ListingImage(listing=obj, url=img.get("url", "")[:1000],
                              alt=img.get("alt", "")[:255], order=i)
            with p.open("rb") as fh:
                li.image.save(f"{i + 1}{p.suffix}", File(fh), save=True)
            self.stats["photos imported"] += 1
