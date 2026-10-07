import re

from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils.text import slugify
from django.urls import reverse

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


class City(models.Model):
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
        return reverse("listings:city", kwargs={"city_slug": self.slug})

    def __str__(self):
        return self.name


class Area(models.Model):
    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name="areas")
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    meta_description = models.TextField(blank=True)

    class Meta:
        unique_together = [("city", "slug")]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse(
            "listings:area",
            kwargs={"city_slug": self.city.slug, "area_slug": self.slug},
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
        help_text='Sub-sector within the area, e.g. "F-7/4". Shown on the listing only.',
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

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse(
            "listings:detail",
            kwargs={"city_slug": self.city.slug, "slug": self.slug},
        )

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
        """First part of the address. Google separates parts with commas in
        Pakistan and with " - " in the UAE ("Al Wasl Rd - Umm Suqeim 2 - Dubai")."""
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
