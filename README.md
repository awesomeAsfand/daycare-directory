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

# Run scraper (saves to scraper/daycare_listings.json)
python gmaps_scraper.py

# Import into Django
python manage.py import_listings --file scraper/daycare_listings.json --city Islamabad
```

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
