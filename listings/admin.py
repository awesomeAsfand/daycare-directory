from django.contrib import admin
from django.utils.html import format_html
from .models import SCRAPED_FIELDS, DaycareListing, City, Area, Review, ListingImage


def image_preview(obj, height):
    if not obj.image:
        return "-"
    return format_html('<img src="{}" style="height:{}px;border-radius:4px" />', obj.image.url, height)


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "slug"]
    list_filter = ["city"]
    prepopulated_fields = {"slug": ["name"]}


class ListingImageInline(admin.TabularInline):
    model = ListingImage
    extra = 0
    fields = ["preview", "image", "alt", "order", "url"]
    readonly_fields = ["preview", "url"]

    @admin.display(description="Preview")
    def preview(self, obj):
        return image_preview(obj, 60)


class ReviewInline(admin.TabularInline):
    model = Review
    extra = 0
    fields = ["author", "rating", "text", "date"]


@admin.register(DaycareListing)
class DaycareAdmin(admin.ModelAdmin):
    inlines = [ListingImageInline, ReviewInline]
    list_display = [
        "name", "listing_type", "city", "area", "rating", "review_count",
        "is_featured", "is_verified", "is_active", "last_seen_at",
    ]
    list_filter = ["listing_type", "city", "area", "is_featured", "is_verified", "is_active"]
    search_fields = ["name", "address", "phone"]
    list_editable = ["is_featured", "is_verified", "is_active"]
    prepopulated_fields = {"slug": ["name"]}
    readonly_fields = ["created_at", "updated_at", "last_seen_at", "maps_url", "place_id"]

    fieldsets = (
        ("Core", {
            "fields": ("name", "slug", "listing_type", "city", "area", "sub_area", "address", "phone",
                       "website", "description")
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
        ("Import protection", {
            "fields": ("locked_fields", "last_seen_at"),
        }),
        ("Metadata", {
            "fields": ("categories", "hours", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    # Anything edited by hand is locked so the next import_listings run keeps it.

    @staticmethod
    def lock(obj, fields):
        obj.locked_fields = sorted(set(obj.locked_fields or []) | set(fields))

    def save_model(self, request, obj, form, change):
        if change:
            self.lock(obj, [f for f in form.changed_data if f in SCRAPED_FIELDS])
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        key = {ListingImage: "images", Review: "reviews"}.get(formset.model)
        if change and key and formset.has_changed():
            self.lock(form.instance, [key])
            form.instance.save(update_fields=["locked_fields"])


@admin.register(ListingImage)
class ListingImageAdmin(admin.ModelAdmin):
    list_display = ["listing", "preview", "order"]
    search_fields = ["listing__name"]
    list_select_related = ["listing"]

    @admin.display(description="Preview")
    def preview(self, obj):
        return image_preview(obj, 40)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ["listing", "author", "rating", "date"]
    list_filter = ["rating"]
    search_fields = ["listing__name", "author", "text"]
    list_select_related = ["listing"]
