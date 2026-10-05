# DaycaresPK — Pakistan Daycare Directory

A Django-powered directory of daycare centres, nurseries and Montessori schools across Pakistan, monetised with Google AdSense.

## Tech stack

- **Backend** — Django 5, PostgreSQL
- **Frontend** — Django templates, Tailwind CSS (CDN), HTMX (live search)
- **Scraper** — Playwright + BeautifulSoup4
- **Server** — Gunicorn + Nginx, DigitalOcean $6/mo droplet
- **CDN** — Cloudflare (free tier)

## Quick start

```bash
# 1. Clone & create virtual env
git clone https://github.com/YOUR_USERNAME/daycare-directory-pakistan.git
cd daycare-directory-pakistan
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Environment
cp .env.example .env
# Edit .env with your DB credentials and secret key

# 3. Database
createdb daycare_db
python manage.py migrate
python manage.py createsuperuser

# 4. Run
python manage.py runserver
```

## Scraper

```bash
cd scraper
pip install playwright beautifulsoup4 lxml
playwright install chromium

# Run scraper (saves to scraper/daycare_listings.json, photos to scraper/images/)
# Searches come from scraper/queries/islamabad.txt: every keyword in every
# area, then a map-grid sweep. Places that aren't daycares / standalone
# preschools in Islamabad go to daycare_listings.rejected.json with the reason.
python gmaps_scraper.py --list                   # show the searches that would run
python gmaps_scraper.py --areas "F-7,G-13"       # trial on a few areas (no grid)
python gmaps_scraper.py --max-hours 2            # fresh run, stop cleanly after 2 hours
python gmaps_scraper.py --resume --max-hours 2   # continue (also after a CAPTCHA block)
python gmaps_scraper.py --report                 # what each keyword / area / the grid found
python gmaps_scraper.py --fill-missing           # retry listings missing photos/reviews
python -m unittest discover -s . -p "test_*.py"  # offline tests

# Import into Django (preview first, then for real)
python manage.py import_listings --file scraper/daycare_listings.json --city Islamabad --dry-run
python manage.py import_listings --file scraper/daycare_listings.json --city Islamabad

# Merge listings that point at the same Google place (adds 301s for removed URLs)
python manage.py dedupe_listings            # preview
python manage.py dedupe_listings --apply
```

Areas come only from `scraper/queries/islamabad.txt`: the importer places each
listing by the sector in its address (F-7/4 → F-7, sub-sector kept for the
listing page), then an area name or spelling from `[aliases]`, then its
listing name, then the nearest area position within 1.5 km
(`islamabad_area_centres.csv`, regenerate with `python scraper/area_centres.py`).
Listings that can't be placed show on the city page only; the import report
lists them so you can add a spelling or an area.

The importer matches places by Google place ID and never changes a listing's
slug, featured/verified/active flags, or any field you have edited in the admin
(those are recorded in the listing's "Import protection → locked fields").
Photos are copied into `media/`; Google's photo URLs expire, so they are never
hotlinked.

## URL structure (SEO)

| URL | Target keyword |
|-----|---------------|
| `/` | Pakistan daycare directory |
| `/islamabad/` | Daycare centers in Islamabad |
| `/islamabad/f-7/` | Daycare in F-7 Islamabad |
| `/islamabad/dha/` | Daycare in DHA Islamabad |
| `/islamabad/best-stars-daycare/detail/` | Individual listing |
| `/sitemap.xml` | Auto-generated sitemap |

## Monetisation

1. **Google AdSense** — set `ADSENSE_PUBLISHER_ID` in `.env`; ad slots are pre-wired in templates
2. **Featured listings** — set `is_featured=True` in Django admin; charge daycares PKR 2,000–5,000/mo
3. **Verified badge** — `is_verified=True`; upsell to listing owners

## Deployment (Docker, e.g. a DigitalOcean droplet)

Production runs from `docker-compose.prod.yml` on its own (it is not layered on
`docker-compose.yml`): PostgreSQL, Redis, Django under gunicorn, and nginx with
HTTPS from Let's Encrypt. The database and Redis are not reachable from outside.
In the commands below, `$P` stands for:

```bash
P="docker compose -f docker-compose.prod.yml --env-file .env.prod"
```

### 1. Server and settings

```bash
# Ubuntu droplet with Docker installed; point the domain's A records
# (example.pk and www.example.pk) at the droplet's IP first
git clone <repo> daycare-directory && cd daycare-directory
cp .env.prod.example .env.prod
nano .env.prod      # SECRET_KEY, DB_PASSWORD, DOMAIN, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, CONTACT_EMAIL
```

### 2. First HTTPS certificate

nginx won't start without a certificate, so get the first one with certbot's own
web server, before starting nginx (port 80 must be free):

```bash
source .env.prod
$P run --rm -p 80:80 certbot certonly --standalone \
  -d $DOMAIN -d www.$DOMAIN --email $SSL_EMAIL --agree-tos --no-eff-email
```

### 3. Start the site

```bash
$P up -d --build
$P exec web python manage.py createsuperuser
```

On every start the web container waits for the database, applies migrations,
sets the sitemap's domain from `DOMAIN` (`manage.py sync_site`) and collects
static files.

### 4. Copy the data from your computer

The scrape and import run on your computer; copy the result to the server rather
than scraping there.

```bash
# On your computer (Git Bash: prefix with MSYS_NO_PATHCONV=1)
docker compose exec db pg_dump -U daycare_user -d daycare_db -Fc -f /tmp/site.dump
docker compose cp db:/tmp/site.dump site.dump
tar czf media.tgz media
scp site.dump media.tgz root@<server>:daycare-directory/

# On the server
$P cp site.dump db:/tmp/site.dump
$P exec db pg_restore -U daycare_user -d daycare_db --clean --if-exists --no-owner /tmp/site.dump
tar xzf media.tgz && $P cp media/. web:/app/media/
$P exec web python manage.py sync_site     # the restore brought your local domain
```

The restore replaces the server's database, including admin users, with yours.

### 5. Certificate renewal

Let's Encrypt certificates last 90 days. Renew from cron, e.g. weekly:

```bash
0 3 * * 1  cd /root/daycare-directory && docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm certbot && docker compose -f docker-compose.prod.yml --env-file .env.prod exec nginx nginx -s reload
```

Once HTTPS works, set `SECURE_HSTS_SECONDS=31536000` in `.env.prod` and restart
(`$P up -d`). Set `ADSENSE_PUBLISHER_ID` once AdSense approves the site.
