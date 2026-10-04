"""
python manage.py dedupe_listings           # show what would be merged
python manage.py dedupe_listings --apply   # merge and delete duplicates

Groups listings that point at the same Google place (place ID, or the
0x...:0x... ID inside maps_url) and merges each group into one listing:
  - keeps the listing with the original slug (no "-1" suffix), so its URL stays
  - takes each scraped field from the most recently updated row that has it
  - keeps the largest set of reviews and of photos
  - ORs is_featured / is_verified / is_active and unions locked_fields
  - adds a 301 redirect from each deleted listing's URL to the kept one
"""
import re
from collections import defaultdict

from django.conf import settings
from django.contrib.redirects.models import Redirect
from django.core.management.base import BaseCommand
from django.db import transaction

from listings.models import DaycareListing

FEATURE_ID_RE = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+")
MERGE_FIELDS = [
    "name", "area_id", "address", "phone", "website", "description",
    "rating", "review_count", "latitude", "longitude",
    "categories", "hours", "maps_url",
]


def place_key(listing):
    if FEATURE_ID_RE.fullmatch(listing.place_id or ""):
        return listing.place_id
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", listing.maps_url or "")
    return m.group(1) if m else None


def is_empty(value):
    return value is None or value == "" or value == 0 or value == [] or value == {}


class Command(BaseCommand):
    help = "Merge duplicate listings that point at the same Google place"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes (default is a dry run)")

    def handle(self, *args, **options):
        groups = defaultdict(list)
        for listing in DaycareListing.objects.select_related("city").order_by("pk"):
            key = place_key(listing)
            if key:
                groups[(listing.city_id, key)].append(listing)
        dupes = [g for g in groups.values() if len(g) > 1]

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes. Use --apply to merge."))

        deleted = 0
        for group in dupes:
            # Prefer the original slug (no -N suffix), then the oldest row.
            group.sort(key=lambda o: (bool(re.search(r"-\d+$", o.slug)), o.pk))
            keeper, others = group[0], group[1:]
            self.stdout.write(f"  keep {keeper.slug}  <-  " + ", ".join(o.slug for o in others))
            deleted += len(others)
            if options["apply"]:
                with transaction.atomic():
                    self.merge(keeper, others)

        self.stdout.write(self.style.SUCCESS(
            f"\n{len(dupes)} duplicate groups, {deleted} listings "
            f"{'merged and deleted' if options['apply'] else 'would be merged'}."
        ))

    def merge(self, keeper, others):
        group = [keeper] + others
        by_recency = sorted(group, key=lambda o: o.updated_at)
        locked = set().union(*(set(o.locked_fields or []) for o in group))

        for field in MERGE_FIELDS:
            if field.removesuffix("_id") in locked:
                continue
            for src in by_recency:
                value = getattr(src, field)
                if not is_empty(value):
                    setattr(keeper, field, value)

        keeper.place_id = place_key(keeper)
        keeper.is_featured = any(o.is_featured for o in group)
        keeper.is_verified = any(o.is_verified for o in group)
        keeper.is_active = any(o.is_active for o in group)
        keeper.locked_fields = sorted(locked)
        seen = [o.last_seen_at for o in group if o.last_seen_at]
        keeper.last_seen_at = max(seen) if seen else None

        # Move the largest set of reviews / photos onto the keeper.
        for related in ("reviews", "images"):
            best = max(group, key=lambda o: getattr(o, related).count())
            if best is not keeper and getattr(best, related).exists():
                getattr(keeper, related).all().delete()
                getattr(best, related).update(listing=keeper)

        keeper.save()

        new_path = keeper.get_absolute_url()
        for other in others:
            old_path = other.get_absolute_url()
            other.delete()
            if old_path != new_path:
                Redirect.objects.update_or_create(
                    site_id=settings.SITE_ID, old_path=old_path,
                    defaults={"new_path": new_path},
                )
            # Anything that already redirected to the deleted page goes to the keeper.
            Redirect.objects.filter(new_path=old_path).update(new_path=new_path)
