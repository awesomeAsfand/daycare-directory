from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from listings.sitemaps import CitySitemap, AreaSitemap, ListingSitemap, PageSitemap
from listings.views import robots_txt

sitemaps = {
    "pages": PageSitemap,
    "cities": CitySitemap,
    "areas": AreaSitemap,
    "listings": ListingSitemap,
}


def page(template, title, meta_desc):
    return TemplateView.as_view(
        template_name=template, extra_context={"title": title, "meta_desc": meta_desc},
    )


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
        "pages/about.html", "About DaycaresPK",
        "DaycaresPK is a free directory of daycares, preschools and Montessori centres in Islamabad.",
    ), name="about"),
    path("privacy-policy/", page(
        "pages/privacy.html", "Privacy Policy — DaycaresPK",
        "How DaycaresPK handles information, cookies and advertising.",
    ), name="privacy"),
    path("contact/", page(
        "pages/contact.html", "Contact DaycaresPK",
        "Contact DaycaresPK to correct, claim or remove a daycare listing, or to send feedback.",
    ), name="contact"),
    path("", include("listings.urls", namespace="listings")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
