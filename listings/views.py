import json
import math
from urllib.parse import quote, urlencode

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.db.models import Avg, Count, Q
from django.views.decorators.cache import cache_page
from .models import FACILITIES, DaycareListing, City, Area, Country, coverage

# "Top rated" lists only include listings with at least this many Google reviews
TOP_RATED_MIN_REVIEWS = 50
# Minimum rating for the "4.5★ and up" filter
GOOD_RATING = 4.5
# Facility filter chips: only facilities at least this many listings have, at most this many chips
FACILITY_CHIP_MIN = 3
FACILITY_CHIP_MAX = 6

ACTIVE = Q(listings__is_active=True)
# Area centres: the average position of their listings (0 = no position)
PLACED = ACTIVE & ~Q(listings__latitude=0)


def robots_txt(request):
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /search/",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


def listings():
    return (DaycareListing.objects.filter(is_active=True)
            .select_related("city__country", "area").prefetch_related("images"))


def top_rated(**filters):
    return listings().filter(review_count__gte=TOP_RATED_MIN_REVIEWS, **filters).order_by("-rating", "-review_count")


def tint(items, cols):
    """The design's tile colours: four pastels, shifted by one on each row."""
    for i, item in enumerate(items):
        item.tile = (i + i // cols) % 4
    return items


def plural(n, one, many):
    return f"{n:,} {one if n == 1 else many}"


def crumbs(*places):
    """[(label, url)] from the home page down to this page's place (a
    Country, City, Area or listing)."""
    home = settings.SITE_CONFIG.get("home_label", "Home")
    return [(home, "/")] + [(getattr(p, "short_name", None) or p.name, p.get_absolute_url()) for p in places]


def heading(in_name, short_name):
    """("the ", "UAE") for "Nurseries in the <mark>UAE</mark>"."""
    if in_name.endswith(short_name):
        return in_name[:-len(short_name)], short_name
    return "", in_name


def place_context(country, city=None, area=None):
    """What the header needs: the country menu and the search box's scope."""
    if area:
        return {"nav_country": country, "search_where": f"{city.slug}/{area.slug}", "search_label": area.name}
    if city:
        return {"nav_country": country, "search_where": city.slug, "search_label": city.name}
    return {"nav_country": country, "search_where": f"country:{country.slug}", "search_label": country.in_name}


def score(listing):
    """"Best match": the rating, pulled towards 4.0 when there are few reviews."""
    return (listing.rating * listing.review_count + 4.0 * 10) / (listing.review_count + 10)


@cache_page(60 * 30)  # 30 min cache
def index(request):
    site = settings.SITE_CONFIG
    where = coverage(site)
    cities = list(City.objects.select_related("country")
                  .annotate(n=Count("listings", filter=ACTIVE)).filter(n__gt=0).order_by("-n"))
    countries = list(Country.objects.annotate(n=Count("cities__listings", filter=Q(cities__listings__is_active=True)))
                     .filter(n__gt=0).order_by("-n"))
    for country in countries:
        country.top_cities = [c for c in cities if c.country_id == country.pk][:3]
    total = sum(c.n for c in countries)
    areas = Area.objects.annotate(n=Count("listings", filter=ACTIVE)).filter(n__gt=0).order_by("name")
    search_cities = sorted(cities, key=lambda c: c.name)
    for city in search_cities:
        city.areas_with_listings = [a for a in areas if a.city_id == city.pk]

    return render(request, "listings/index.html", {
        "countries": tint(countries, 3),
        "top_cities": cities[:8],
        "search_cities": search_cities,
        "total_label": f"{plural(total, site['noun'], site['nouns'])} in "
                       + (where if len(countries) == 1 else f"{len(countries)} countries"),
        "title": f"{site['title_nouns']} in {where} — Find the Best {site['nouns'].title()}",
        "meta_desc": (
            f"Find top-rated {site['nouns']} across {where}. "
            f"Compare ratings, opening hours and contact details."
        ),
    })


def country_listings(request, country_slug):
    site = settings.SITE_CONFIG
    country = get_object_or_404(Country, slug=country_slug)
    cities = list(country.cities.annotate(n=Count("listings", filter=ACTIVE, distinct=True),
                                          areas_n=Count("areas", filter=Q(areas__listings__is_active=True), distinct=True))
                  .filter(n__gt=0).order_by("-n"))
    if not cities:
        raise Http404("No listings in this country yet")
    total = sum(c.n for c in cities)
    label = country.city_label

    lead, mark = heading(country.in_name, country.short_name)
    return render(request, "listings/country.html", {
        **place_context(country),
        "breadcrumbs": crumbs(country),
        "heading_lead": lead,
        "heading_mark": mark,
        "country": country,
        "country_cities": tint(cities, 4),
        "choose": f"Choose {'an' if label[:1] in 'aeiou' else 'a'} {label}",
        "intro": f"{plural(total, site['noun'], site['nouns'])} with Google ratings, opening hours and phone numbers.",
        "top_rated": top_rated(city__country=country)[:10],
        "top_rated_min_reviews": TOP_RATED_MIN_REVIEWS,
        "title": f"{site['title_nouns']} in {country.in_name} | {site['name']}",
        "meta_desc": country.meta_description or (
            f"{total:,} {site['nouns']} in {country.in_name}. "
            f"Compare ratings, opening hours and contact details by city."
        ),
    })


def get_city(country_slug, city_slug):
    return get_object_or_404(City.objects.select_related("country"),
                             slug=city_slug, country__slug=country_slug)


def city_areas(city):
    """Areas with listings, with n (active listings) and lat/lng (their average position)."""
    return list(city.areas.annotate(
        n=Count("listings", filter=ACTIVE),
        lat=Avg("listings__latitude", filter=PLACED), lng=Avg("listings__longitude", filter=PLACED),
    ).filter(n__gt=0))


def group_by_region(areas):
    """[(region, [areas])]: biggest region first, areas by size. Without
    regions (a small city, or none loaded yet): one A–Z group named ""."""
    if not any(a.region for a in areas):
        return [("", sorted(areas, key=lambda a: a.name))]
    groups = {}
    for a in areas:
        groups.setdefault(a.region or "Other", []).append(a)
    for group in groups.values():
        group.sort(key=lambda a: (-a.n, a.name))
    return sorted(groups.items(), key=lambda kv: (kv[0] == "Other", -sum(a.n for a in kv[1])))


def city_listings(request, country_slug, city_slug):
    site = settings.SITE_CONFIG
    city = get_city(country_slug, city_slug)
    areas = city_areas(city)
    total = DaycareListing.objects.filter(city=city, is_active=True).count()
    search_url = reverse("listings:search")
    facilities = [
        {"label": FACILITIES[k], "n": n, "url": f"{search_url}?{urlencode({'where': city.slug, 'facility': k})}"}
        for k, n in facility_counts(DaycareListing.objects.filter(city=city, is_active=True)
                                    .exclude(facilities=[]).values_list("facilities", flat=True))
    ]

    return render(request, "listings/city.html", {
        **place_context(city.country, city),
        "breadcrumbs": crumbs(city.country, city),
        "city": city,
        "popular": tint(sorted(areas, key=lambda a: (-a.n, a.name))[:8], 4),
        "facilities": facilities,
        "regions": group_by_region(areas),
        "unplaced": listings().filter(city=city, area__isnull=True).order_by("name"),
        "top_rated": top_rated(city=city)[:10],
        "top_rated_min_reviews": TOP_RATED_MIN_REVIEWS,
        "intro": f"{plural(total, site['noun'], site['nouns'])} in {plural(len(areas), 'neighbourhood', 'neighbourhoods')}. "
                 f"Pick yours to compare ratings and call them directly.",
        "title": f"{site['title_nouns']} in {city.name} | {site['name']}",
        "meta_desc": city.meta_description or (
            f"Find the best {site['nouns']} in {city.name}. "
            f"Compare ratings, hours and contact info."
        ),
    })


def area_or_listing(request, country_slug, city_slug, slug):
    city = get_city(country_slug, city_slug)
    area = Area.objects.filter(city=city, slug=slug).first()
    if area:
        # A neighbourhood whose nurseries are all switched off has nothing to
        # show; it comes back once one is active again
        if not area.listings.filter(is_active=True).exists():
            raise Http404("No nurseries in this area")
        return area_listings(request, city, area)
    return listing_detail(request, city, slug)


SORTS = [("match", "Best match"), ("rating", "Top rated"), ("reviews", "Most reviewed"), ("name", "A–Z")]


def facility_param(request):
    """The ?facility= key, or "" when missing or unknown."""
    key = request.GET.get("facility", "")
    return key if key in FACILITIES else ""


def facility_counts(facility_lists):
    """[(key, n)] for facilities at least FACILITY_CHIP_MIN listings have, most common first."""
    counts = {}
    for keys in facility_lists:
        for key in keys or []:
            if key in FACILITIES:
                counts[key] = counts.get(key, 0) + 1
    common = [(k, n) for k, n in counts.items() if n >= FACILITY_CHIP_MIN]
    return sorted(common, key=lambda kn: (-kn[1], list(FACILITIES).index(kn[0])))


def filter_and_sort(request, qs):
    """The filter chips and sort links shared by area and search pages.
    Returns (results, filters, sorts) where filters/sorts are links to show."""
    params = request.GET
    results = list(qs)
    rating, kind, open_now = params.get("rating") == "4.5", params.get("type", ""), params.get("open") == "1"
    if rating:
        results = [r for r in results if r.rating >= GOOD_RATING]
    if kind in dict(DaycareListing.TYPE_CHOICES):
        results = [r for r in results if r.listing_type == kind]
    if open_now:
        results = [r for r in results if r.open_status and r.open_status["open"]]
    facility = facility_param(request)
    # chips come from the results before the facility filter, so the others stay offered
    facility_chips = [k for k, _ in facility_counts(r.facilities for r in results)][:FACILITY_CHIP_MAX]
    if facility:
        results = [r for r in results if facility in (r.facilities or [])]
        if facility not in facility_chips:
            facility_chips.append(facility)
    sort = params.get("sort", "match")
    keys = {
        "match": lambda r: (not r.is_featured, -score(r)),
        "rating": lambda r: (-r.rating, -r.review_count),
        "reviews": lambda r: (-r.review_count, -r.rating),
        "name": lambda r: r.name.lower(),
    }
    results.sort(key=keys.get(sort, keys["match"]))

    def link(**change):
        merged = {k: v for k, v in params.items()}
        merged.update(change)
        merged = {k: v for k, v in merged.items() if v and not (k == "sort" and v == "match")}
        return "?" + urlencode(merged) if merged else request.path

    site = settings.SITE_CONFIG
    types = site.get("type_labels", {})
    filters = [{"label": f"{GOOD_RATING}★ and up", "on": rating, "url": link(rating="" if rating else "4.5")}]
    choices = dict(DaycareListing.TYPE_CHOICES)
    order = [k for k in types if k in choices] + [k for k in choices if k not in types]
    filters += [{"label": types.get(k, choices[k]), "on": kind == k, "url": link(type="" if kind == k else k)}
                for k in order]
    filters.append({"label": "Open now", "on": open_now, "url": link(open="" if open_now else "1")})
    filters += [{"label": FACILITIES[k], "on": facility == k, "url": link(facility="" if facility == k else k)}
                for k in facility_chips]
    sorts = [{"label": label, "on": sort == key, "url": link(sort=key)} for key, label in SORTS]
    return results, filters, sorts


def map_pins(results):
    return [{"lat": r.latitude, "lng": r.longitude, "name": r.name, "url": r.get_absolute_url(),
             "rating": f"{r.rating:.1f}" if r.rating else ""}
            for r in results if r.latitude and r.longitude]


def distance(a, b):
    """Rough distance in km between two things with lat/lng, fine within a city."""
    x = math.radians(b.lng - a.lng) * math.cos(math.radians((a.lat + b.lat) / 2))
    return 6371 * math.hypot(x, math.radians(b.lat - a.lat))


def area_listings(request, city, area):
    site = settings.SITE_CONFIG
    results, filters, sorts = filter_and_sort(request, listings().filter(area=area))
    areas = city_areas(city)
    here = next((a for a in areas if a.pk == area.pk), None)
    others = [a for a in areas if a.pk != area.pk and a.lat is not None]
    nearby = sorted(others, key=lambda a: distance(here, a))[:6] if here and here.lat is not None else []
    total = area.listings.filter(is_active=True).count()

    return render(request, "listings/area.html", {
        **place_context(city.country, city, area),
        "breadcrumbs": crumbs(city.country, city, area),
        "city": city,
        "area": area,
        "results": results,
        "filtered": len(results) != total,
        "facility_note": bool(facility_param(request)),
        "heading": f"{plural(len(results), site['noun'], site['nouns'])} in {area.name}",
        "filters": filters,
        "sorts": sorts,
        "nearby": nearby,
        "pins": map_pins(results),
        "title": f"{site['title_nouns']} in {area.name}, {city.name}",
        "meta_desc": area.meta_description or (
            f"{total} {site['nouns'] if total != 1 else site['noun']} in {area.name}, {city.name}. "
            f"Compare ratings, opening hours and phone numbers."
        ),
    })


def map_embed_url(listing):
    """Google Maps for the nursery page: the Embed API when a key is set,
    otherwise the keyless embed of the listing's position."""
    key = settings.GOOGLE_MAPS_EMBED_KEY
    place = f"{listing.name}, {listing.address}" if listing.address else f"{listing.latitude},{listing.longitude}"
    if key:
        return f"https://www.google.com/maps/embed/v1/place?key={key}&q={quote(place)}"
    if listing.latitude and listing.longitude:
        return f"https://maps.google.com/maps?q={listing.latitude},{listing.longitude}&z=15&output=embed"
    return ""


def directions_url(listing):
    if listing.latitude and listing.longitude:
        return f"https://www.google.com/maps/dir/?api=1&destination={listing.latitude},{listing.longitude}"
    return listing.maps_url


def listing_detail(request, city, slug):
    site = settings.SITE_CONFIG
    listing = get_object_or_404(
        DaycareListing.objects.select_related("city__country", "area").prefetch_related("images"),
        city=city, slug=slug, is_active=True
    )
    others = listings().filter(area=listing.area).exclude(pk=listing.pk) if listing.area else DaycareListing.objects.none()
    similar = sorted(others, key=score, reverse=True)[:3]

    facts = [("Type", listing.type_label),
             ("Neighbourhood", f"{listing.location_label}, {city.name}" if listing.area else city.name)]
    if listing.phone:
        facts.append(("Phone", listing.phone_local))
    if listing.website:
        facts.append(("Website", listing.website.split("//")[-1].removeprefix("www.").rstrip("/")))
    if listing.email:
        facts.append(("Email", listing.email))
    if listing.has_details:
        facts += [(k, v) for k, v in [
            ("Ages", listing.age_range_label), ("Curriculum", ", ".join(listing.curriculum_labels)),
            ("Fees", listing.fees_label + (f" ({listing.fees_note})" if listing.fees_note and listing.fees_label else "")),
            ("Licensed by", listing.get_licensed_by_display()),
        ] if v]
    if listing.transport:
        facts.append(("Transport", "School bus available"))
    if listing.meals:
        facts.append(("Meals", "Included"))

    photos = listing.photos
    report = ""
    if settings.CONTACT_EMAIL:
        subject = f"Wrong info: {listing.name}"
        body = f"Listing: {request.build_absolute_uri()}\n\nWhat should change:\n"
        report = f"mailto:{settings.CONTACT_EMAIL}?subject={quote(subject)}&body={quote(body)}"

    places = [city.country, city] + ([listing.area] if listing.area else [])
    return render(request, "listings/detail.html", {
        **place_context(city.country, city),
        "breadcrumbs": crumbs(*places, listing),
        "listing": listing,
        "photos": photos,
        "gallery": photos[:5],
        "facts": facts,
        "similar": similar,
        "others_count": len(others),
        "map_url": map_embed_url(listing),
        "directions_url": directions_url(listing),
        "report_url": report,
        "title": f"{listing.name} — {site['noun'].capitalize()} in {listing.area or city.name}",
        "meta_desc": (
            f"{listing.name} is a {site['noun']} in {listing.location_label}, {city.name}. "
            + (f"Rated {listing.rating}/5 from {listing.review_count} Google reviews. " if listing.review_count else "")
            + "Opening hours, phone number and directions."
        ),
        "og_image": request.build_absolute_uri(photos[0].image.url) if photos else "",
    })


def search_queryset(q, where):
    qs = listings()
    if where.startswith("country:"):
        qs = qs.filter(city__country__slug=where.removeprefix("country:"))
    elif where:
        city_slug, _, area_slug = where.partition("/")
        qs = qs.filter(city__slug=city_slug)
        if area_slug:
            qs = qs.filter(area__slug=area_slug)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(address__icontains=q) | Q(area__name__icontains=q)
                       | Q(sub_area__icontains=q))
    return qs


def search(request):
    """Search results page (not indexed: robots.txt and a noindex tag)."""
    site = settings.SITE_CONFIG
    q, where = request.GET.get("q", "").strip(), request.GET.get("where", "").strip()
    qs = search_queryset(q, where) if (q or where) else DaycareListing.objects.none()
    facility = facility_param(request)
    if facility:
        qs = qs.filter(facilities__contains=[facility])
    results, filters, sorts = filter_and_sort(request, qs[:200])
    place, scope = "", {}
    if where.startswith("country:"):
        country = Country.objects.filter(slug=where.removeprefix("country:")).first()
        place = country.in_name if country else ""
        scope = place_context(country) if country else {}
    elif where:
        city_slug, _, area_slug = where.partition("/")
        area = Area.objects.filter(city__slug=city_slug, slug=area_slug).first() if area_slug else None
        city = City.objects.filter(slug=city_slug).first()
        place = area.name if area else (city.name if city else "")
        if city:
            scope = place_context(city.country, city, area)
    heading = plural(len(results), site["noun"], site["nouns"])
    heading += f" matching “{q}”" if q else ""
    heading += f" in {place}" if place else ""
    heading += f" with: {FACILITIES[facility]}" if facility else ""

    return render(request, "listings/search.html", {
        **scope,
        "q": q,
        "where": where,
        "results": results,
        "heading": heading,
        "facility_note": bool(facility),
        "filters": filters,
        "sorts": sorts,
        "pins": map_pins(results),
        "title": f"Search {site['nouns']} | {site['name']}",
    })


def suggest(request):
    """Live suggestions under the search box: a fragment of links."""
    q, where = request.GET.get("q", "").strip(), request.GET.get("where", "").strip()
    results = []
    if len(q) >= 2:
        results = search_queryset(q, where).order_by("-review_count")[:8]
    return render(request, "components/search_results.html", {"results": results, "q": q, "where": where})
