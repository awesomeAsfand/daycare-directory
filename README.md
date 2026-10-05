# Childcare Directory (UAE / Pakistan)

A Django-powered directory of nurseries and daycare centres, monetised with Google AdSense.
One codebase runs a separate site per country, each with its own database, domain and `.env`:

| `SITE` | Site | Status |
|--------|------|--------|
| `uae`  | Nurseries in the UAE, starting with Dubai (brand name is a placeholder) | In progress |
| `pk`   | DaycaresPK: daycares and preschools in Islamabad | Parked 2026-10-05 (git tag `pakistan-parked`) |

`SITE` in `.env` picks the site's name, wording, time zone and country
(`config/sites.py`; `SITE_NAME` overrides the brand name). Templates in
`templates/sites/<site>/` replace the shared ones for that site (e.g. the About page).

## Tech stack

- **Backend** — Django 5, PostgreSQL
- **Frontend** — Django templates, Tailwind CSS (CDN), HTMX (live search)
- **Scraper** — Playwright + BeautifulSoup4
- **Server** — Gunicorn + Nginx, DigitalOcean $6/mo droplet
- **CDN** — Cloudflare (free tier)

## Quick start

```bash
# 1. Clone & create virtual env
git clone https://github.com/YOUR_USERNAME/daycare-directory.git
cd daycare-directory
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

# One queries file per city: scraper/queries/<city>.txt holds the country
# settings, keywords, areas, boundary and grid (see search_plan.py). Output
# goes to scraper/<city>_listings.json, photos to scraper/images/. Places that
# don't belong in the directory go to <city>_listings.rejected.json with the reason.
python gmaps_scraper.py -q queries/dubai.txt --list                  # show the searches that would run
python gmaps_scraper.py -q queries/dubai.txt --areas "JLT,Al Barsha" # trial on a few areas (no grid)
python gmaps_scraper.py -q queries/dubai.txt --max-hours 2           # fresh run, stop cleanly after 2 hours
python gmaps_scraper.py -q queries/dubai.txt --resume --max-hours 2  # continue (also after a CAPTCHA block)
python gmaps_scraper.py -q queries/dubai.txt --report                # what each keyword / area / the grid found
python gmaps_scraper.py -q queries/dubai.txt --fill-missing          # retry listings missing photos/reviews
python -m unittest discover -s . -p "test_*.py"  # offline tests

# Import into Django (preview first, then for real); reads scraper/dubai_listings.json
python manage.py import_listings --city Dubai --dry-run
python manage.py import_listings --city Dubai
# The parked Islamabad scrape kept its old file name:
python manage.py import_listings --city Islamabad --file scraper/daycare_listings.json

# Merge listings that point at the same Google place (adds 301s for removed URLs)
python manage.py dedupe_listings            # preview
python manage.py dedupe_listings --apply
```

Areas come only from the city's queries file: the importer places each
listing by an area name or spelling from `[aliases]` in its address, then its
listing name, then the nearest area position within `[area_match] max_km`
(`<city>_area_centres.csv`, generate with `python scraper/area_centres.py queries/<city>.txt`).
Islamabad's file also sets `sectors = cda`, which reads CDA sectors first
(F-7/4 → F-7, sub-sector kept for the listing page).
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
| `/` | Nurseries in the UAE |
| `/dubai/` | Nurseries in Dubai |
| `/dubai/al-barsha/` | Nursery in Al Barsha, Dubai |
| `/dubai/tiny-tots-nursery/detail/` | Individual listing |
| `/sitemap.xml` | Auto-generated sitemap |

## Monetisation

1. **Google AdSense** — set `ADSENSE_PUBLISHER_ID` in `.env`; ad slots are pre-wired in templates
2. **Featured listings** — set `is_featured=True` in Django admin; charge a monthly fee
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
# (example.ae and www.example.ae) at the droplet's IP first
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
