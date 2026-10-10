from django.urls import path
from . import views

app_name = "listings"

urlpatterns = [
    path("", views.index, name="index"),
    path("search/", views.search, name="search"),
    path("search/suggest/", views.suggest, name="suggest"),
    path("<slug:country_slug>/", views.country_listings, name="country"),
    path("<slug:country_slug>/<slug:city_slug>/", views.city_listings, name="city"),
    # Areas and listings share one pattern: the view looks for an area first.
    # Slugs can't clash (DaycareListing.unique_slug, Area.save). "detail" is
    # for reverse() only; requests always resolve to "area".
    path("<slug:country_slug>/<slug:city_slug>/<slug:slug>/", views.area_or_listing, name="area"),
    path("<slug:country_slug>/<slug:city_slug>/<slug:slug>/", views.area_or_listing, name="detail"),
]
