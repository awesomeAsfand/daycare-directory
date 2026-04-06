from django.contrib import admin
from .models import DaycareListing, City, Area


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "slug"]
    list_filter = ["city"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(DaycareListing)
class DaycareAdmin(admin.ModelAdmin):
    list_display = [
        "name", "city", "area", "rating", "review_count",
        "is_featured", "is_verified", "is_active",
    ]
    list_filter = ["city", "area", "is_featured", "is_verified", "is_active"]
    search_fields = ["name", "address", "phone"]
    list_editable = ["is_featured", "is_verified", "is_active"]
    prepopulated_fields = {"slug": ["name"]}
    readonly_fields = ["created_at", "updated_at", "maps_url", "place_id"]

    fieldsets = (
        ("Core", {
            "fields": ("name", "slug", "city", "area", "address", "phone", "website", "description")
        }),
        ("Ratings", {
            "fields": ("rating", "review_count")
        }),
        ("Location", {
            "fields": ("latitude", "longitude", "place_id", "maps_url")
        }),
        ("Status", {
            "fields": ("is_active", "is_verified", "is_featured")
        }),
        ("Metadata", {
            "fields": ("categories", "hours", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )
