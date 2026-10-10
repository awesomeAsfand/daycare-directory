from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from listings.models import coverage
from listings.sitemaps import CountrySitemap, CitySitemap, AreaSitemap, ListingSitemap, PageSitemap
from listings.views import robots_txt

sitemaps = {
    "pages": PageSitemap,
    "countries": CountrySitemap,
    "cities": CitySitemap,
    "areas": AreaSitemap,
    "listings": ListingSitemap,
}


class SitePage(TemplateView):
    """A static page; title and meta_desc are filled from settings.SITE_CONFIG
    on each request, e.g. "About {name}", plus {coverage} ("the UAE")."""
    title = meta_desc = ""

    def get_context_data(self, **kwargs):
        site = {**settings.SITE_CONFIG, "coverage": coverage(settings.SITE_CONFIG)}
        return super().get_context_data(
            title=self.title.format(**site), meta_desc=self.meta_desc.format(**site), **kwargs,
        )


def page(template, title, meta_desc):
    return SitePage.as_view(template_name=template, title=title, meta_desc=meta_desc)


urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path("robots.txt", robots_txt, name="robots_txt"),
    # Site pages come before the listings URLs, whose "<city>/" pattern
    # would otherwise catch them
    path("about/", page(
        "pages/about.html", "About {name}",
        "{name} is a free directory of {nouns} in {coverage}.",
    ), name="about"),
    path("privacy-policy/", page(
        "pages/privacy.html", "Privacy Policy — {name}",
        "How {name} handles information, cookies and advertising.",
    ), name="privacy"),
    path("contact/", page(
        "pages/contact.html", "Contact {name}",
        "Contact {name} to correct, claim or remove a {noun} listing, or to send feedback.",
    ), name="contact"),
    path("", include("listings.urls", namespace="listings")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
