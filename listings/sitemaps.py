from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from .models import DaycareListing, City, Area, Country


class PageSitemap(Sitemap):
    changefreq = "yearly"
    priority = 0.3

    def items(self):
        return ["about", "privacy", "contact"]

    def location(self, name):
        return reverse(name)


class CountrySitemap(Sitemap):
    changefreq = "weekly"
    priority = 1.0

    def items(self):
        return Country.objects.live()


class CitySitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.9

    def items(self):
        return City.objects.filter(listings__is_active=True).distinct().select_related("country")


class AreaSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        # Only neighbourhoods with an active nursery (empty ones are 404s)
        return Area.objects.filter(listings__is_active=True).distinct().select_related("city__country")


class ListingSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.7

    def items(self):
        # Only nurseries Google is asked to index (DaycareListing.objects.indexable)
        return DaycareListing.objects.indexable().select_related("city__country")

    def lastmod(self, obj):
        return obj.updated_at
