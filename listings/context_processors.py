from django.conf import settings
from .models import Country, coverage


def global_context(request):
    site = settings.SITE_CONFIG
    where = coverage(site)
    return {
        # Countries with listings: header menu and footer
        "countries": Country.objects.live(),
        "ADSENSE_PUBLISHER_ID": settings.ADSENSE_PUBLISHER_ID,
        "SHOW_AD_PLACEHOLDERS": settings.SHOW_AD_PLACEHOLDERS,
        "CONTACT_EMAIL": settings.CONTACT_EMAIL,
        "site": site,
        "coverage": where,
        # For pages that don't set their own title / meta_desc
        "default_title": f"{site['title_nouns']} in {where} | {site['name']}",
        "default_meta_desc": f"Find top-rated {site['nouns']} across {where}.",
    }
