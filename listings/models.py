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
