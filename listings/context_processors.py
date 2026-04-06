from django.conf import settings
from .models import City


def global_context(request):
    return {
        "cities": City.objects.prefetch_related("areas").all(),
        "ADSENSE_PUBLISHER_ID": settings.ADSENSE_PUBLISHER_ID,
    }
