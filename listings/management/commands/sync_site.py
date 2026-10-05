"""
python manage.py sync_site

Set the Site record (django.contrib.sites) to settings.SITE_DOMAIN, i.e. the
DOMAIN in .env / .env.prod. The sitemap builds its absolute URLs from it, so
without this they point at the default "example.com". entrypoint.sh runs it
on every start.
"""
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Set the Site domain from the DOMAIN setting"

    def handle(self, *args, **options):
        domain = settings.SITE_DOMAIN
        site, _ = Site.objects.update_or_create(
            pk=settings.SITE_ID, defaults={"domain": domain, "name": "DaycaresPK"},
        )
        self.stdout.write(f"Site domain: {site.domain}")
