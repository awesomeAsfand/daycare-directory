from pathlib import Path
from decouple import config

from config.sites import SITE_ALIASES, SITES

BASE_DIR = Path(__file__).resolve().parent.parent

# Which directory this deployment runs: "gcc" or "pk" (see config/sites.py)
SITE = config("SITE", default="gcc")
SITE = SITE_ALIASES.get(SITE, SITE)
if SITE not in SITES:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(f"SITE must be one of {', '.join(SITES)}, not {SITE!r}")
SITE_CONFIG = {**SITES[SITE], "name": config("SITE_NAME", default=SITES[SITE]["name"])}

SECRET_KEY = config("SECRET_KEY", default="django-insecure-change-me-in-production")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    "django.contrib.sites",
    "django.contrib.redirects",
    "listings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # 301s for old listing URLs (e.g. duplicates merged by dedupe_listings)
    "django.contrib.redirects.middleware.RedirectFallbackMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # The site's own templates first, then the shared ones
        "DIRS": [BASE_DIR / "templates" / "sites" / SITE, BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "listings.context_processors.global_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="daycare_db"),
        "USER": config("DB_USER", default="daycare_user"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = SITE_CONFIG["time_zone"]
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Hashed + compressed files in production; plain files in development
    # and tests, which run without collectstatic
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
# Locally, keep each site's photos apart (MEDIA_DIR=media_uae): listing IDs
# restart in each site's database and photos are stored by listing ID
MEDIA_ROOT = BASE_DIR / config("MEDIA_DIR", default="media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SITE_ID = 1
# Domain used for absolute links in the sitemap; synced into the Site record
# by `manage.py sync_site` (run on every container start)
SITE_DOMAIN = config("DOMAIN", default="localhost:8000")

# Trust X-Forwarded-Proto from Nginx reverse proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Required for CSRF when behind Nginx in Docker
CSRF_TRUSTED_ORIGINS = config(
    "CSRF_TRUSTED_ORIGINS",
    default="http://localhost:8000,http://127.0.0.1:8000",
).split(",")

# Redis / Celery
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# In development nothing is cached, so pages show imports and template
# changes straight away (the home page is otherwise cached for 30 minutes)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.dummy.DummyCache",
    } if DEBUG else {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# AdSense
ADSENSE_PUBLISHER_ID = config("ADSENSE_PUBLISHER_ID", default="")

# Shown on the Contact and Privacy pages
CONTACT_EMAIL = config("CONTACT_EMAIL", default="")

# Google Maps Embed API key for the map on nursery pages (free; restrict it to
# the site's domain). Without one, a keyless embed of the position is used.
GOOGLE_MAPS_EMBED_KEY = config("GOOGLE_MAPS_EMBED_KEY", default="")

# Grey boxes where ads will go, while AdSense isn't set up (on by default in development)
SHOW_AD_PLACEHOLDERS = config("SHOW_AD_PLACEHOLDERS", default=DEBUG, cast=bool)

# ── Production hardening (DEBUG=False) ───────────────────────────────────────
if not DEBUG:
    if SECRET_KEY.startswith("django-insecure"):
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("Set a real SECRET_KEY in .env.prod")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # Tell browsers to only use HTTPS. Start at 0, raise to e.g. 31536000
    # (a year) once HTTPS is confirmed working: browsers remember it.
    SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=0, cast=int)
