from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.db.models import Q
from django.views.decorators.cache import cache_page
from .models import DaycareListing, City, Area


def robots_txt(request):
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /search/",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


@cache_page(60 * 30)  # 30 min cache
def index(request):
    site = settings.SITE_CONFIG
    cities = City.objects.prefetch_related("areas").all()
    featured = DaycareListing.objects.filter(
        is_featured=True, is_active=True
    ).select_related("city", "area")[:6]

    return render(request, "listings/index.html", {
        "cities": cities,
        "featured": featured,
        "title": f"{site['title_nouns']} in {site['in_country']} — Find the Best {site['nouns'].title()}",
        "meta_desc": (
            f"Find top-rated {site['nouns']} across {site['in_country']}. "
            f"Compare ratings, opening hours and contact details."
        ),
    })


def city_listings(request, city_slug):
    site = settings.SITE_CONFIG
    city = get_object_or_404(City, slug=city_slug)
    qs = DaycareListing.objects.filter(city=city, is_active=True).select_related("area")

    area_slug = request.GET.get("area")
    if area_slug:
        qs = qs.filter(area__slug=area_slug)

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(description__icontains=q)
            | Q(address__icontains=q)
        )

    sort = request.GET.get("sort", "featured")
    sort_map = {
        "rating": "-rating",
        "reviews": "-review_count",
        "name": "name",
        "featured": "-is_featured",
    }
    qs = qs.order_by(sort_map.get(sort, "-is_featured"), "-rating")

    return render(request, "listings/city.html", {
        "city": city,
        "listings": qs,
        "areas": Area.objects.filter(city=city).order_by("name"),
        "active_area": area_slug,
        "q": q,
        "sort": sort,
        "title": f"{site['title_nouns']} in {city.name} | {site['name']}",
        "meta_desc": (
            f"Find the best {site['nouns']} in {city.name}. "
            f"Compare ratings, hours and contact info."
        ),
    })


def area_listings(request, city_slug, area_slug):
    site = settings.SITE_CONFIG
    city = get_object_or_404(City, slug=city_slug)
    area = get_object_or_404(Area, city=city, slug=area_slug)
    qs = DaycareListing.objects.filter(
        area=area, is_active=True
    ).order_by("-is_featured", "-rating")

    return render(request, "listings/area.html", {
        "city": city,
        "area": area,
        "listings": qs,
        "title": f"{site['title_nouns']} in {area.name}, {city.name}",
        "meta_desc": area.meta_description or (
            f"{site['nouns'].capitalize()} in {area.name}, {city.name}. "
            f"Find ratings, contact info and more."
        ),
    })


def listing_detail(request, city_slug, slug):
    site = settings.SITE_CONFIG
    city = get_object_or_404(City, slug=city_slug)
    listing = get_object_or_404(
        DaycareListing.objects.prefetch_related("reviews", "images"),
        city=city, slug=slug, is_active=True
    )
    similar = DaycareListing.objects.filter(
        area=listing.area, is_active=True
    ).exclude(pk=listing.pk).order_by("-rating")[:4]

    images = [img for img in listing.images.all() if img.image]
    return render(request, "listings/detail.html", {
        "listing": listing,
        "images": images,
        "similar": similar,
        "title": f"{listing.name} — {site['noun'].capitalize()} in {listing.area or city.name}",
        "meta_desc": (
            listing.description[:155]
            if listing.description
            else f"{listing.name} is a {site['noun']} in {listing.address}. "
                 f"Rating: {listing.rating}/5 from {listing.review_count} reviews."
        ),
        "og_image": request.build_absolute_uri(images[0].image.url) if images else "",
    })


def search(request):
    """HTMX live search — returns partial HTML fragment."""
    q = request.GET.get("q", "").strip()
    results = []
    if len(q) >= 2:
        results = DaycareListing.objects.filter(is_active=True).filter(
            Q(name__icontains=q)
            | Q(address__icontains=q)
            | Q(area__name__icontains=q)
        ).select_related("city", "area")[:10]

    return render(request, "components/search_results.html", {
        "results": results,
        "q": q,
    })
