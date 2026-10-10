import re

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils.functional import cached_property
from django.utils.text import slugify
from django.urls import reverse

from . import hours as opening_hours, phones

# Fields that import_listings fills from scraped data. Any of these listed in
# DaycareListing.locked_fields are left alone by the importer.
SCRAPED_FIELDS = [
    "name", "listing_type", "area", "sub_area", "address", "phone", "website", "description",
    "rating", "review_count", "latitude", "longitude",
    "categories", "hours", "maps_url", "reviews", "images",
]


# Curricula and approaches a nursery can follow (DaycareListing.curriculum)
CURRICULA = {
    "eyfs": "British (EYFS)",
    "montessori": "Montessori",
    "reggio": "Reggio Emilia",
    "ib": "IB",
    "american": "American",
    "canadian": "Canadian",
    "french": "French",
    "indian": "Indian",
    "highscope": "HighScope",
    "waldorf": "Waldorf / Steiner",
    "forest": "Forest school",
    "arabic": "Arabic / English bilingual",
    "scandinavian": "Scandinavian / Nordic",
    "australian": "Australian (EYLF)",
}

# What a nursery has (DaycareListing.facilities); the facility filter uses the keys
FACILITIES = {
    "outdoor_play": "Outdoor play area",
    "shaded_outdoor": "Shaded outdoor area",
    "indoor_play": "Indoor play area",
    "indoor_gym": "Indoor gym / soft play",
    "sand_water": "Sand & water play",
    "swimming_pool": "Swimming pool",
    "garden": "Garden & nature area",
    "bike_track": "Bike track",
    "library": "Library / reading corner",
    "art_room": "Art room",
    "music_room": "Music & dance room",
    "sensory_room": "Sensory room",
    "stem_room": "STEM / ICT room",
    "role_play": "Role-play area",
    "cooking": "Children's kitchen",
    "hall": "Hall / theatre",
    "sleep_room": "Baby sleep room",
    "clinic": "Nurse / clinic",
    "cctv": "CCTV",
}

# Extra-curricular activities (DaycareListing.activities)
ACTIVITIES = {
    "ballet_dance": "Ballet & dance",
    "gymnastics": "Gymnastics",
    "football": "Football",
    "martial_arts": "Martial arts",
    "sports": "Sports & tennis",
    "yoga": "Yoga",
    "swimming": "Swimming lessons",
    "music": "Music lessons",
    "arabic": "Arabic classes",
    "french": "French classes",
    "other_languages": "Other languages",
    "islamic": "Islamic studies & Quran",
    "art": "Art & craft",
    "drama": "Drama",
    "cooking": "Cooking",
    "stem": "STEM & robotics",
    "horse_riding": "Horse riding",
    "nature": "Nature & gardening",
    "holiday_camps": "Holiday camps",
    "after_school": "After-school club",
}

REGULATOR_CHOICES = [
    ("KHDA", "KHDA (Dubai)"),
    ("ADEK", "ADEK (Abu Dhabi)"),
    ("SPEA", "SPEA (Sharjah)"),
    ("MOE", "Ministry of Education"),
    ("other", "Other"),
]


def months_label(months) -> str:
    """1.5 -> "45 days", 6 -> "6 months", 18 -> "18 months", 60 -> "5 years"."""
    m = float(months)
    if m < 3 and m != int(m):
        return f"{round(m * 30)} days"
    if m < 24 and m % 12:
        return f"{m:g} months"
    years = m / 12
    return f"{years:g} year{'' if years == 1 else 's'}"


class CountryQuerySet(models.QuerySet):
    def live(self):
        """Countries with at least one active listing: the only ones shown on
        the site, so there are no empty country pages."""
        return self.filter(cities__listings__is_active=True).distinct()


class Country(models.Model):
    name = models.CharField(max_length=100, help_text="Full name, e.g. United Arab Emirates")
    short_name = models.CharField(max_length=50, help_text='In headings and menus, e.g. "UAE"')
    in_name = models.CharField(max_length=60, help_text='As written after "in", e.g. "the UAE"')
    slug = models.SlugField(unique=True, help_text="First part of the URL: /uae/dubai/")
    code = models.CharField(max_length=2, unique=True, help_text="ISO 3166 code, e.g. AE")
    currency = models.CharField(max_length=3, blank=True, help_text="ISO 4217 code, e.g. AED")
    phone_code = models.CharField(max_length=4, blank=True, help_text="Without +, e.g. 971")
    time_zone = models.CharField(max_length=50, blank=True, help_text="e.g. Asia/Dubai")
    city_label = models.CharField(
        max_length=30, default="city", help_text='What its cities are called: "emirate or city" in the UAE',
    )
    meta_description = models.TextField(blank=True)

    objects = CountryQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "countries"
        ordering = ["name"]

    @classmethod
    def for_code(cls, code, name=""):
        """The country with this ISO code, created from COUNTRY_DEFAULTS
        (listings/countries.py) the first time it is needed."""
        from .countries import COUNTRY_DEFAULTS
        code = code.upper()
        defaults = COUNTRY_DEFAULTS.get(code) or {
            "name": name, "short_name": name, "in_name": name, "slug": slugify(name),
        }
        return cls.objects.get_or_create(code=code, defaults=defaults)[0]

    def get_absolute_url(self):
        return reverse("listings:country", kwargs={"country_slug": self.slug})

    def __str__(self):
        return self.name


def coverage(site):
    """What the site covers, as written after "in": the country while only
    one has listings ("the UAE"), then the site's region ("the Gulf")."""
    countries = list(Country.objects.live()[:2])
    return countries[0].in_name if len(countries) == 1 else site["in_region"]


class City(models.Model):
    country =models.ForeignKey(Country, on_delete=models.PROTECT, related_name="cities")
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    meta_description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "cities"
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse(
            "listings:city",
            kwargs={"country_slug": self.country.slug, "city_slug": self.slug},
        )

    def __str__(self):
        return self.name


class Area(models.Model):
    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name="areas")
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    region = models.CharField(
        max_length=60, blank=True,
        help_text='Group on the city page, e.g. "Jumeirah & Al Barsha". Leave all of a '
                  "city's areas blank for a plain A–Z list (load with apply_regions).",
    )
    meta_description = models.TextField(blank=True)

    class Meta:
        unique_together = [("city", "slug")]
        ordering = ["name"]

    def clean(self):
        if self.city_id and DaycareListing.objects.filter(city_id=self.city_id, slug=self.slug).exists():
            raise ValidationError({"slug": "A listing in this city already uses this slug, "
                                           "and areas and listings share one URL pattern."})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        # Areas and listings share /<country>/<city>/<slug>/ and the area wins,
        # so a listing with this slug would become unreachable: move it.
        # (The importer creates areas, so this can't stop with an error.)
        for listing in DaycareListing.objects.filter(city_id=self.city_id, slug=self.slug):
            listing.slug = DaycareListing.unique_slug(self.city, self.slug, exclude=listing.pk,
                                                      area_slugs={self.slug})
            listing.save(update_fields=["slug"])
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse(
            "listings:area",
            kwargs={"country_slug": self.city.country.slug, "city_slug": self.city.slug,
                    "slug": self.slug},
        )

    def __str__(self):
        return f"{self.name}, {self.city.name}"


class DaycareListing(models.Model):
    DAYCARE = "daycare"
    PRESCHOOL = "preschool"
    TYPE_CHOICES = [
        (DAYCARE, "Daycare"),
        (PRESCHOOL, "Preschool / Montessori"),
    ]

    # Core info
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=300)
    listing_type = models.CharField(
        "type", max_length=20, choices=TYPE_CHOICES, default=DAYCARE, db_index=True,
    )
    city = models.ForeignKey(
        City, on_delete=models.SET_NULL, null=True, blank=True, related_name="listings"
    )
    area = models.ForeignKey(
        Area, on_delete=models.SET_NULL, null=True, blank=True, related_name="listings"
    )
    sub_area = models.CharField(
        max_length=50, blank=True,
        help_text='Part of the area, e.g. "Al Barsha 1". Shown on the listing only.',
    )
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.URLField(max_length=500, blank=True)
    # From the nursery's website (collect_details), checked in the review CSV
    email = models.EmailField(blank=True)
    description = models.TextField(blank=True)

    # Ratings from Google Maps
    rating = models.FloatField(default=0)
    review_count = models.IntegerField(default=0)

    # Location
    latitude = models.FloatField(default=0)
    longitude = models.FloatField(default=0)
    place_id = models.CharField(max_length=500, blank=True, db_index=True)
    maps_url = models.URLField(max_length=1000, blank=True)

    # Structured data
    categories = models.JSONField(default=list, blank=True)
    hours = models.JSONField(default=dict, blank=True)

    # Directory flags
    is_verified = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)  # paid placement
    is_active = models.BooleanField(default=True)

    # Nursery details: from the nursery's website, KHDA or the nursery itself,
    # never from Google, so import_listings doesn't touch them. Shown on the
    # site only once details_confirmed is ticked in the admin.
    age_from_months = models.DecimalField(
        "youngest age (months)", max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="45 days = 1.5",
    )
    age_to_months = models.DecimalField(
        "oldest age (months)", max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="5 years = 60",
    )
    curriculum = models.JSONField(
        default=list, blank=True, help_text="Keys from CURRICULA, e.g. [\"eyfs\", \"montessori\"]",
    )
    licensed_by = models.CharField(max_length=10, choices=REGULATOR_CHOICES, blank=True)
    fees_from_aed = models.PositiveIntegerField("fees from (AED a year)", null=True, blank=True)
    fees_to_aed = models.PositiveIntegerField("fees to (AED a year)", null=True, blank=True)
    fees_note = models.CharField(
        max_length=200, blank=True,
        help_text='What the fees cover, e.g. "3-5 days a week, 2026-27"',
    )
    details_source = models.URLField(
        max_length=500, blank=True, help_text="Page the details were taken from",
    )
    details_checked = models.DateField(null=True, blank=True, help_text="When the details were last checked")
    details_confirmed = models.BooleanField(
        default=False, help_text="Show the details on the site. Leave unticked until checked.",
    )
    # Services and facilities, shown whenever set. Empty = unknown, so a
    # nursery is never shown as "no transport" by mistake.
    transport = models.BooleanField(null=True, blank=True, help_text="Offers transport (school bus)")
    meals = models.BooleanField("meals included", null=True, blank=True)
    facilities = models.JSONField(
        default=list, blank=True, help_text="Keys from FACILITIES, e.g. [\"outdoor_play\", \"library\"]",
    )
    activities = models.JSONField(
        default=list, blank=True, help_text="Keys from ACTIVITIES, e.g. [\"ballet_dance\", \"football\"]",
    )

    # Import protection
    locked_fields = models.JSONField(
        default=list, blank=True,
        help_text="Fields the importer must not overwrite. Filled automatically "
                  "when you edit a field in the admin; remove a name to let "
                  "the next import update it again.",
    )
    last_seen_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When this listing last appeared in an imported scrape.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_featured", "-rating", "-review_count"]
        unique_together = [("slug", "city")]

    @classmethod
    def unique_slug(cls, city, base, exclude=None, area_slugs=None):
        """base, or base-1, base-2 ... : not used by another listing in the city
        or by one of its areas (both live at /<country>/<city>/<slug>/)."""
        if city is None or city.pk is None:
            return base
        taken = set(cls.objects.filter(city=city, slug__startswith=base)
                    .exclude(pk=exclude).values_list("slug", flat=True))
        taken |= set(Area.objects.filter(city=city, slug__startswith=base).values_list("slug", flat=True))
        taken |= area_slugs or set()
        slug, n = base, 1
        while slug in taken:
            slug = f"{base}-{n}"
            n += 1
        return slug

    def clean(self):
        if self.city_id and Area.objects.filter(city_id=self.city_id, slug=self.slug).exists():
            raise ValidationError({"slug": "An area in this city already uses this slug, "
                                           "and areas and listings share one URL pattern."})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.unique_slug(self.city, slugify(self.name), exclude=self.pk)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse(
            "listings:detail",
            kwargs={"country_slug": self.city.country.slug, "city_slug": self.city.slug,
                    "slug": self.slug},
        )

    @property
    def type_label(self):
        """"Nursery" / "Daycare" on the GulfNurseries site (config/sites.py type_labels)."""
        from django.conf import settings
        return settings.SITE_CONFIG.get("type_labels", {}).get(self.listing_type) \
            or self.get_listing_type_display()

    @property
    def country(self):
        return self.city.country if self.city else None

    @cached_property
    def local_now(self):
        return opening_hours.now_in(self.country.time_zone if self.country else "")

    @property
    def hours_week(self):
        return opening_hours.week(self.hours, self.local_now)

    @property
    def hours_summary(self):
        return opening_hours.summary(self.hours)

    @cached_property
    def open_status(self):
        return opening_hours.status(self.hours, self.local_now)

    @property
    def phone_local(self):
        return phones.local(self.phone, self.country) if self.country else self.phone

    @property
    def tel_url(self):
        return phones.tel_url(self.phone, self.country) if self.phone and self.country else ""

    @property
    def whatsapp_url(self):
        return phones.whatsapp_url(self.phone, self.country) if self.phone and self.country else ""

    @cached_property
    def photos(self):
        """Images with a file, in order (uses prefetch_related("images"))."""
        return [img for img in self.images.all() if img.image]

    @property
    def cover(self):
        return self.photos[0] if self.photos else None

    @property
    def rating_stars(self):
        """Returns rating as integer for star display."""
        return round(self.rating)

    @property
    def location_label(self):
        """"F-7/4, F-7", or just the area / city name."""
        if self.area and self.sub_area:
            return f"{self.sub_area}, {self.area.name}"
        return self.area.name if self.area else (self.city.name if self.city else "")

    @property
    def age_range_label(self):
        """"45 days – 5 years", "from 6 months", "up to 4 years" or ""."""
        lo, hi = self.age_from_months, self.age_to_months
        if lo is not None and hi is not None:
            return f"{months_label(lo)} – {months_label(hi)}"
        if lo is not None:
            return f"from {months_label(lo)}"
        if hi is not None:
            return f"up to {months_label(hi)}"
        return ""

    @property
    def curriculum_labels(self):
        return [CURRICULA[key] for key in self.curriculum or [] if key in CURRICULA]

    @property
    def facility_labels(self):
        return [FACILITIES[key] for key in self.facilities or [] if key in FACILITIES]

    @property
    def activity_labels(self):
        return [ACTIVITIES[key] for key in self.activities or [] if key in ACTIVITIES]

    @property
    def fees_approximate(self):
        """Fee ranges from a directory, not the nursery's own fee list."""
        return self.fees_note.lower().startswith("approximate")

    @property
    def fees_label(self):
        """"AED 40,755 – 52,800 a year", "from AED 30,000 a year" or ""."""
        lo, hi = self.fees_from_aed, self.fees_to_aed
        if lo and hi and lo != hi:
            return f"AED {lo:,} – {hi:,} a year"
        if lo or hi:
            return f"{'from ' if lo and not hi else ''}AED {(lo or hi):,} a year"
        return ""

    @property
    def has_details(self):
        return self.details_confirmed and bool(
            self.age_range_label or self.curriculum_labels or self.licensed_by or self.fees_label
        )

    @property
    def short_address(self):
        """First part of the address. Google separates parts with " - " in the
        UAE ("Al Wasl Rd - Umm Suqeim 2 - Dubai"), sometimes with commas."""
        return re.split(r",| - ", self.address)[0].strip() if self.address else ""

    def __str__(self):
        return f"{self.name} ({self.area or self.city})"


class Review(models.Model):
    listing = models.ForeignKey(
        DaycareListing, on_delete=models.CASCADE, related_name="reviews"
    )
    author = models.CharField(max_length=255, blank=True)
    rating = models.PositiveSmallIntegerField(default=0)  # 1–5
    text = models.TextField(blank=True)
    date = models.CharField(max_length=100, blank=True)   # e.g. "2 months ago"

    class Meta:
        ordering = ["-rating"]

    def __str__(self):
        return f"{self.author} — {self.listing.name}"


def listing_image_path(instance, filename):
    return f"listings/{instance.listing_id}/{filename}"


class ListingImage(models.Model):
    listing = models.ForeignKey(
        DaycareListing, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(upload_to=listing_image_path, blank=True)
    # Where the photo was downloaded from. Google photo URLs expire, so this is
    # kept for reference only and never shown on the site.
    url = models.URLField("source URL", max_length=1000, blank=True)
    alt = models.CharField(max_length=255, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"Image {self.order} — {self.listing.name}"


@receiver(post_delete, sender=ListingImage)
def delete_listing_image_file(sender, instance, **kwargs):
    # Wait for the commit so a rolled-back delete doesn't lose the file.
    if instance.image:
        storage, name = instance.image.storage, instance.image.name
        transaction.on_commit(lambda: storage.delete(name))
