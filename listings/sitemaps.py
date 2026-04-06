from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from .models import DaycareListing, City, Area


class CitySitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.9

    def items(self):
        return City.objects.all()

    def location(self, obj):
        return reverse("listings:city", kwargs={"city_slug": obj.slug})


class AreaSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Area.objects.select_related("city").all()

    def location(self, obj):
        return reverse(
            "listings:area",
            kwargs={"city_slug": obj.city.slug, "area_slug": obj.slug},
        )


class ListingSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.7

    def items(self):
        return DaycareListing.objects.filter(is_active=True).select_related("city")

    def location(self, obj):
        return obj.get_absolute_url()

    def lastmod(self, obj):
        return obj.updated_at
