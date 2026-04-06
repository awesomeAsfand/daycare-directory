from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from listings.sitemaps import CitySitemap, AreaSitemap, ListingSitemap

sitemaps = {
    "cities": CitySitemap,
    "areas": AreaSitemap,
    "listings": ListingSitemap,
}

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path("", include("listings.urls", namespace="listings")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
