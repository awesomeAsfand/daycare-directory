from django.urls import path
from . import views

app_name = "listings"

urlpatterns = [
    path("", views.index, name="index"),
    path("search/", views.search, name="search"),
    path("<slug:city_slug>/", views.city_listings, name="city"),
    path("<slug:city_slug>/<slug:area_slug>/", views.area_listings, name="area"),
    path("<slug:city_slug>/<slug:slug>/detail/", views.listing_detail, name="detail"),
]
