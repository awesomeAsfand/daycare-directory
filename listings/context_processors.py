from django.conf import settings
from .models import City


def global_context(request):
    site = settings.SITE_CONFIG
    return {
        "cities": City.objects.prefetch_related("areas").all(),
        "ADSENSE_PUBLISHER_ID": settings.ADSENSE_PUBLISHER_ID,
        "CONTACT_EMAIL": settings.CONTACT_EMAIL,
        "site": site,
        # For pages that don't set their own title / meta_desc
        "default_title": f"{site['title_nouns']} in {site['in_country']} | {site['name']}",
        "default_meta_desc": f"Find top-rated {site['nouns']} across {site['in_country']}.",
    }
