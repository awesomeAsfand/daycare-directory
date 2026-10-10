from django import forms
from django.contrib import admin
from django.db.models import Q
from django.utils.html import format_html
from .models import ACTIVITIES, CURRICULA, FACILITIES, SCRAPED_FIELDS, DaycareListing, City, Country, Area, Review, ListingImage


class DetailsFilter(admin.SimpleListFilter):
    """Listings with nursery details still to check, for the review queue."""
    title = "nursery details"
    parameter_name = "details"

    def lookups(self, request, model_admin):
        return [("to_review", "Found, not confirmed yet"), ("none", "None found")]

    def queryset(self, request, queryset):
        found = (Q(age_from_months__isnull=False) | Q(age_to_months__isnull=False)
                 | ~Q(curriculum=[]) | ~Q(licensed_by="") | Q(fees_from_aed__isnull=False))
        if self.value() == "to_review":
            return queryset.filter(found, details_confirmed=False)
        if self.value() == "none":
            return queryset.exclude(found)
        return queryset


def image_preview(obj, height):
    if not obj.image:
        return "-"
    return format_html('<img src="{}" style="height:{}px;border-radius:4px" />', obj.image.url, height)


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ["name", "short_name", "slug", "code", "currency"]
    prepopulated_fields = {"slug": ["short_name"]}


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ["name", "country", "slug"]
    list_filter = ["country"]
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


class DaycareListingForm(forms.ModelForm):
    curriculum = forms.MultipleChoiceField(
        choices=list(CURRICULA.items()), required=False, widget=forms.CheckboxSelectMultiple,
    )
    facilities = forms.MultipleChoiceField(
        choices=list(FACILITIES.items()), required=False, widget=forms.CheckboxSelectMultiple,
    )
    activities = forms.MultipleChoiceField(
        choices=list(ACTIVITIES.items()), required=False, widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = DaycareListing
        fields = "__all__"


@admin.register(DaycareListing)
class DaycareAdmin(admin.ModelAdmin):
    form = DaycareListingForm
    inlines = [ListingImageInline, ReviewInline]
    list_display = [
        "name", "listing_type", "city", "area", "rating", "review_count",
        "is_featured", "is_verified", "is_active", "details_confirmed", "last_seen_at",
    ]
    list_filter = ["listing_type", "city", "area", "is_featured", "is_verified", "is_active",
                   "details_confirmed", DetailsFilter]
    actions = ["confirm_details"]
    search_fields = ["name", "address", "phone", "email"]
    list_editable = ["is_featured", "is_verified", "is_active"]
    prepopulated_fields = {"slug": ["name"]}
    readonly_fields = ["created_at", "updated_at", "last_seen_at", "maps_url", "place_id"]

    fieldsets = (
        ("Core", {
            "fields": ("name", "slug", "listing_type", "city", "area", "sub_area", "address", "phone",
                       "email", "website", "description")
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
        ("Nursery details (from the nursery's website, KHDA or the nursery)", {
            "fields": ("age_from_months", "age_to_months", "curriculum", "licensed_by",
                       ("fees_from_aed", "fees_to_aed"), "fees_note",
                       "details_source", "details_checked", "details_confirmed"),
        }),
        ("Services and facilities (shown whenever set)", {
            "fields": ("transport", "meals", "facilities", "activities"),
        }),
        ("Import protection", {
            "fields": ("locked_fields", "last_seen_at"),
        }),
        ("Metadata", {
            "fields": ("categories", "hours", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.action(description="Confirm details (show them on the site)")
    def confirm_details(self, request, queryset):
        n = queryset.update(details_confirmed=True)
        self.message_user(request, f"Details confirmed for {n} listing{'s' if n != 1 else ''}.")

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
