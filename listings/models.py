from django.db import models
from django.utils.text import slugify
from django.urls import reverse


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
    # Core info
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=300)
    city = models.ForeignKey(
        City, on_delete=models.SET_NULL, null=True, blank=True, related_name="listings"
    )
    area = models.ForeignKey(
        Area, on_delete=models.SET_NULL, null=True, blank=True, related_name="listings"
    )
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.URLField(blank=True)
    description = models.TextField(blank=True)

    # Ratings from Google Maps
    rating = models.FloatField(default=0)
    review_count = models.IntegerField(default=0)

    # Location
    latitude = models.FloatField(default=0)
    longitude = models.FloatField(default=0)
    place_id = models.CharField(max_length=200, blank=True, db_index=True)
    maps_url = models.URLField(blank=True)

    # Structured data
    categories = models.JSONField(default=list)
    hours = models.JSONField(default=dict)

    # Directory flags
    is_verified = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)  # paid placement
    is_active = models.BooleanField(default=True)

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
    def short_address(self):
        """First line of address."""
        return self.address.split(",")[0] if self.address else ""

    def __str__(self):
        return f"{self.name} ({self.area or self.city})"
