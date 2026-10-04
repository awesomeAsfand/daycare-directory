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

## Deployment (DigitalOcean)

```bash
# On your droplet
sudo apt install postgresql nginx python3-venv
git clone <repo> && cd daycare-directory-pakistan
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3 --daemon
# Point Nginx to 127.0.0.1:8000
```
