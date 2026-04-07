"""
Google Maps Daycare Scraper
Playwright + BeautifulSoup | Pakistan Directory Project

Usage:
    pip install playwright beautifulsoup4 lxml asyncio
    playwright install chromium
    python gmaps_scraper.py
"""

import asyncio
import csv
import json
import re
import time
import random
from dataclasses import dataclass, asdict, field
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout


# ─── Config ──────────────────────────────────────────────────────────────────

SEARCH_QUERIES = [
    "daycare in Islamabad",
    "daycare in F-7 Islamabad",
    "daycare in DHA Islamabad",
    "daycare in G-9 Islamabad",
    "daycare in Bahria Town Islamabad",
    "montessori school Islamabad",
    "preschool Islamabad",
    "nursery school Islamabad",
    "child care center Islamabad",
    "playschool Islamabad",
]

OUTPUT_FILE = Path(__file__).parent / "daycare_listings.json"
DELAY_MIN = 2.5   # seconds between actions (be polite, avoid bans)
DELAY_MAX = 5.0
MAX_RESULTS_PER_QUERY = 40


# ─── Data Model ──────────────────────────────────────────────────────────────

@dataclass
class DaycareCenter:
    name: str = ""
    address: str = ""
    city: str = "Islamabad"
    area: str = ""           # e.g. F-7, DHA, Bahria Town
    phone: str = ""
    website: str = ""
    rating: float = 0.0
    review_count: int = 0
    latitude: float = 0.0
    longitude: float = 0.0
    place_id: str = ""
    categories: list = field(default_factory=list)
    hours: dict = field(default_factory=dict)
    description: str = ""
    google_maps_url: str = ""
    source_query: str = ""
    reviews: list = field(default_factory=list)   # up to 5 Google reviews
    images: list = field(default_factory=list)    # up to 4 photo URLs


# ─── Helpers ─────────────────────────────────────────────────────────────────

def rand_delay():
    return random.uniform(DELAY_MIN, DELAY_MAX)


def extract_place_id(url: str) -> str:
    """Extract Google Place ID from maps URL."""
    match = re.search(r"place/([^/]+)/", url)
    if match:
        return match.group(1)
    match = re.search(r"!1s(0x[a-f0-9:]+)", url)
    if match:
        return match.group(1)
    return ""


def extract_coords(url: str) -> tuple[float, float]:
    """Extract lat/lng from Google Maps URL.
    Handles both full URLs (@lat,lng) and short URLs (!3d lat !4d lng).
    """
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if match:
        return float(match.group(1)), float(match.group(2))
    # Short-form URLs encode coords as !3d<lat>!4d<lng>
    match = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if match:
        return float(match.group(1)), float(match.group(2))
    return 0.0, 0.0


def extract_area(address: str) -> str:
    """Try to extract the area/sector from a Pakistani address."""
    patterns = [
        r"\b(F-\d+(?:/\d+)?)\b",
        r"\b(G-\d+(?:/\d+)?)\b",
        r"\b(I-\d+(?:/\d+)?)\b",
        r"\b(E-\d+(?:/\d+)?)\b",
        r"\b(DHA(?:\s+Phase\s+\d+)?)\b",
        r"\b(Bahria Town(?:\s+Phase\s+\d+)?)\b",
        r"\b(PWD(?:\s+Housing)?)\b",
        r"\b(Bani Gala)\b",
        r"\b(Margalla Hills)\b",
    ]
    for p in patterns:
        m = re.search(p, address, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


# ─── Parser ──────────────────────────────────────────────────────────────────

def parse_listing_page(html: str, url: str, query: str) -> DaycareCenter:
    """Parse a single Google Maps place page into a DaycareCenter."""
    soup = BeautifulSoup(html, "lxml")
    dc = DaycareCenter(google_maps_url=url, source_query=query)

    # Name — h1 or the main title element
    name_tag = soup.find("h1")
    if name_tag:
        dc.name = name_tag.get_text(strip=True)

    # All visible text sections for targeted extraction
    all_text = soup.get_text(" ", strip=True)

    # Rating — 3-tier approach for robustness
    # Tier 1: aria-label on rating button e.g. aria-label="4.3 stars"
    rating_btn = soup.find(attrs={"aria-label": re.compile(r"\d+\.?\d*\s*stars?", re.I)})
    if rating_btn:
        m = re.search(r"(\d+\.?\d*)", rating_btn.get("aria-label", ""))
        if m:
            dc.rating = float(m.group(1))
    # Tier 2: aria-hidden span containing bare numeric rating e.g. "4.3"
    if not dc.rating:
        for span in soup.find_all("span", {"aria-hidden": "true"}):
            t = span.get_text(strip=True)
            if re.match(r"^\d\.\d$", t):
                dc.rating = float(t)
                break
    # Tier 3: JSON-LD or structured data embedded in the page source
    if not dc.rating:
        m = re.search(r'"averageRating":\s*(\d+\.?\d*)', html)
        if m:
            dc.rating = float(m.group(1))

    # Review count — handles "(123)" and "123 reviews" formats
    review_match = re.search(r"\(([\d,]+)\s*(?:reviews?)?\)", all_text)
    if not review_match:
        review_match = re.search(r"([\d,]+)\s+(?:Google\s+)?reviews?", all_text, re.IGNORECASE)
    if review_match:
        dc.review_count = int(review_match.group(1).replace(",", ""))

    # Address — look for aria-label patterns in the page
    addr_tag = soup.find("button", {"data-item-id": "address"})
    if addr_tag:
        dc.address = addr_tag.get_text(strip=True)
    else:
        # Fallback: find text next to map pin icon
        for tag in soup.find_all(["div", "span"]):
            text = tag.get_text(strip=True)
            if re.search(r"\d+.+Islamabad|Lahore", text) and len(text) < 150:
                dc.address = text
                break

    dc.area = extract_area(dc.address)

    # Phone number
    phone_match = re.search(r"(\+92[\s\-]?\d[\d\s\-]{8,}|\b0\d{2,3}[\s\-]?\d{6,8}\b)", all_text)
    if phone_match:
        dc.phone = phone_match.group(1).strip()

    # Website
    website_tag = soup.find("a", href=re.compile(r"^https?://(?!maps\.google|google)"))
    if website_tag:
        dc.website = website_tag.get("href", "")

    # Categories (type tags)
    cat_tags = soup.find_all("button", {"jsaction": re.compile("category|type", re.I)})
    dc.categories = [t.get_text(strip=True) for t in cat_tags if t.get_text(strip=True)]

    # Coordinates from URL
    dc.latitude, dc.longitude = extract_coords(url)
    dc.place_id = extract_place_id(url)

    return dc


# ─── Review & Image Scrapers ─────────────────────────────────────────────────

async def scrape_reviews(page: Page, max_reviews: int = 5) -> list[dict]:
    """Scrape up to max_reviews reviews from the currently loaded place page."""
    reviews = []
    try:
        # Click the reviews tab/button to open the reviews panel
        reviews_btn = await page.query_selector(
            'button[aria-label*="reviews" i], button[jsaction*="reviews" i]'
        )
        if reviews_btn:
            await reviews_btn.click()
            await asyncio.sleep(rand_delay())

        # Wait for review cards to appear
        await page.wait_for_selector(
            'div[data-review-id], div[class*="jftiEf"]',
            timeout=8000,
        )

        # Expand any truncated review text by clicking "More" buttons
        more_btns = await page.query_selector_all('button[aria-label="See more"], button.w8nwRe')
        for btn in more_btns[:max_reviews]:
            try:
                await btn.click()
                await asyncio.sleep(0.5)
            except Exception:
                pass

        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        # Each review card
        cards = soup.find_all("div", attrs={"data-review-id": True})
        if not cards:
            # Fallback selector used by some Maps layouts
            cards = [d for d in soup.find_all("div") if "jftiEf" in d.get("class", [])]

        for card in cards[:max_reviews]:
            # Author name
            author = ""
            author_tag = card.find(class_=re.compile(r"d4r55|reviewer|author", re.I))
            if author_tag:
                author = author_tag.get_text(strip=True)

            # Star rating from aria-label e.g. "5 stars"
            rating = 0
            star_tag = card.find(attrs={"aria-label": re.compile(r"\d+\s*stars?", re.I)})
            if star_tag:
                m = re.search(r"(\d+)", star_tag.get("aria-label", ""))
                if m:
                    rating = int(m.group(1))

            # Review text
            text = ""
            text_tag = card.find(class_=re.compile(r"wiI7pd|review-full-text|MyEned", re.I))
            if text_tag:
                text = text_tag.get_text(strip=True)

            # Relative date e.g. "2 months ago"
            date = ""
            date_tag = card.find(class_=re.compile(r"rsqaWe|dehysf|review-snippet", re.I))
            if date_tag:
                date = date_tag.get_text(strip=True)

            if author or text:
                reviews.append({
                    "author": author,
                    "rating": rating,
                    "text": text,
                    "date": date,
                })

    except (PWTimeout, Exception):
        pass

    return reviews


def _upgrade_image_url(url: str) -> str:
    """Replace Google's thumbnail size parameters with high-quality dimensions."""
    # Pattern: =w[N]-h[N]-... or =s[N] at end of URL
    url = re.sub(r"=w\d+-h\d+.*$", "=w1200-h800", url)
    url = re.sub(r"=s\d+$", "=w1200-h800", url)
    return url


async def scrape_images(page: Page, max_images: int = 4) -> list[dict]:
    """Scrape up to max_images high-quality photos from the currently loaded place page."""
    images = []
    seen_urls: set[str] = set()

    try:
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        # Primary: img tags served from Google's content CDN
        for img in soup.find_all("img", src=re.compile(r"googleusercontent\.com")):
            src = img.get("src", "")
            if not src or "maps_api_static" in src:
                continue
            # Skip tiny icons — quality URLs contain width/height params
            if not re.search(r"=w\d+|=s\d+", src):
                continue
            hq = _upgrade_image_url(src)
            if hq not in seen_urls:
                seen_urls.add(hq)
                images.append({"url": hq, "alt": img.get("alt", "")})
            if len(images) >= max_images:
                break

        # Fallback: try clicking the photos section to open the gallery
        if len(images) < 2:
            photos_btn = await page.query_selector(
                'button[aria-label*="photo" i], div[data-photo-index], [jsaction*="photo" i]'
            )
            if photos_btn:
                await photos_btn.click()
                await asyncio.sleep(rand_delay())
                html = await page.content()
                soup = BeautifulSoup(html, "lxml")
                for img in soup.find_all("img", src=re.compile(r"googleusercontent\.com")):
                    src = img.get("src", "")
                    if not src or "maps_api_static" in src:
                        continue
                    hq = _upgrade_image_url(src)
                    if hq not in seen_urls:
                        seen_urls.add(hq)
                        images.append({"url": hq, "alt": img.get("alt", "")})
                    if len(images) >= max_images:
                        break

    except Exception:
        pass

    return images[:max_images]


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def scroll_results_panel(page: Page, times: int = 8):
    """Scroll the search results panel to load more listings."""
    try:
        # Google Maps results panel selector
        panel = await page.wait_for_selector(
            'div[role="feed"], div.m6QErb[aria-label]',
            timeout=8000
        )
        for _ in range(times):
            await panel.evaluate("el => el.scrollBy(0, 800)")
            await asyncio.sleep(rand_delay() * 0.6)
    except PWTimeout:
        # Try scrolling the page directly as fallback
        for _ in range(times):
            await page.keyboard.press("PageDown")
            await asyncio.sleep(1.2)


async def get_listing_urls(page: Page, query: str) -> list[str]:
    """Search Google Maps and collect all listing URLs."""
    search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
    print(f"\n[search] {query}")
    await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
    # Wait for results panel or the map to appear (Maps never reaches networkidle)
    try:
        await page.wait_for_selector('div[role="feed"], div.m6QErb', timeout=15000)
    except PWTimeout:
        pass
    await asyncio.sleep(rand_delay())

    # Handle cookie/consent popup
    try:
        consent = await page.wait_for_selector(
            'button:has-text("Accept"), button:has-text("I agree"), button[aria-label*="Accept"]',
            timeout=4000
        )
        if consent:
            await consent.click()
            await asyncio.sleep(1.5)
    except PWTimeout:
        pass

    # Scroll to load all results
    await scroll_results_panel(page, times=10)

    # Collect all place URLs from the results panel
    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/maps/place/" in href:
            # Normalise: strip trailing params after the place path
            clean = re.sub(r"(\/maps\/place\/[^@]*).*", r"\1", href)
            if clean.startswith("/"):
                clean = "https://www.google.com" + clean
            urls.add(clean)

    print(f"  found {len(urls)} listings")
    return list(urls)[:MAX_RESULTS_PER_QUERY]


async def scrape_listing(page: Page, url: str, query: str) -> DaycareCenter | None:
    """Navigate to a place page and extract details."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(rand_delay())

        # Wait for the main content to render
        await page.wait_for_selector("h1", timeout=10000)

        # Expand "See more" if present
        try:
            see_more = await page.query_selector('button:has-text("See more")')
            if see_more:
                await see_more.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        html = await page.content()
        final_url = page.url
        dc = parse_listing_page(html, final_url, query)

        # Scrape reviews and images while still on the page
        dc.images = await scrape_images(page)
        dc.reviews = await scrape_reviews(page)

        return dc

    except PWTimeout:
        print(f"  [timeout] {url}")
        return None
    except Exception as e:
        print(f"  [error] {url}: {e}")
        return None


async def run_scraper():
    """Main scraper entrypoint."""
    all_results: list[DaycareCenter] = []
    seen_place_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Karachi",
        )

        # Stealth: remove webdriver flag
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        for query in SEARCH_QUERIES:
            try:
                listing_urls = await get_listing_urls(page, query)
            except Exception as e:
                print(f"  [search error] {query}: {e}")
                continue

            for url in listing_urls:
                await asyncio.sleep(rand_delay())
                dc = await scrape_listing(page, url, query)

                if dc and dc.name:
                    # Deduplicate by place_id or name+address
                    key = dc.place_id or f"{dc.name}|{dc.address}"
                    if key not in seen_place_ids:
                        seen_place_ids.add(key)
                        all_results.append(dc)
                        print(f"  + [{len(all_results):03d}] {dc.name} | {dc.area or dc.address[:40]}")

            # Save incrementally after each query
            save_results(all_results)
            print(f"  Saved {len(all_results)} total. Sleeping...")
            await asyncio.sleep(random.uniform(8, 15))  # longer pause between queries

        await browser.close()

    print(f"\nDone. {len(all_results)} unique daycares scraped → {OUTPUT_FILE}")
    return all_results


def save_results(results: list[DaycareCenter]):
    """Save results to JSON and CSV, overwriting each time."""
    data = [asdict(r) for r in results]
    OUTPUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_file = OUTPUT_FILE.with_suffix(".csv")
    if data:
        # Flatten list/dict fields to strings for CSV
        flat = []
        for row in data:
            flat.append({
                **{k: v for k, v in row.items() if not isinstance(v, (list, dict))},
                "categories": ", ".join(row.get("categories") or []),
                "hours": json.dumps(row.get("hours") or {}, ensure_ascii=False),
                "reviews": json.dumps(row.get("reviews") or [], ensure_ascii=False),
                "images": "|".join(img["url"] for img in (row.get("images") or []) if img.get("url")),
            })
        with csv_file.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=flat[0].keys())
            writer.writeheader()
            writer.writerows(flat)


# ─── Django Import Utility ────────────────────────────────────────────────────

def import_to_django(json_path: str = "daycare_listings.json"):
    """
    Run this AFTER setting up Django models.
    Call from: python manage.py shell < import_daycares.py
    Or import this function in a management command.

    Example Django model (models.py):
    ─────────────────────────────────
    from django.db import models
    from django.contrib.gis.db import models as gis_models  # if using GeoDjango

    class DaycareListing(models.Model):
        name         = models.CharField(max_length=255)
        address      = models.TextField(blank=True)
        city         = models.CharField(max_length=100, default="Islamabad")
        area         = models.CharField(max_length=100, blank=True)
        phone        = models.CharField(max_length=50, blank=True)
        website      = models.URLField(blank=True)
        rating       = models.FloatField(default=0)
        review_count = models.IntegerField(default=0)
        latitude     = models.FloatField(default=0)
        longitude    = models.FloatField(default=0)
        place_id     = models.CharField(max_length=200, unique=True, blank=True)
        categories   = models.JSONField(default=list)
        hours        = models.JSONField(default=dict)
        description  = models.TextField(blank=True)
        maps_url     = models.URLField(blank=True)
        is_verified  = models.BooleanField(default=False)
        is_featured  = models.BooleanField(default=False)
        created_at   = models.DateTimeField(auto_now_add=True)

        class Meta:
            ordering = ["-rating", "-review_count"]

        def __str__(self):
            return f"{self.name} ({self.area or self.city})"
    """
    import django, os, json
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from listings.models import DaycareListing  # adjust to your app name

    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    created = 0
    updated = 0

    for item in data:
        obj, is_new = DaycareListing.objects.update_or_create(
            place_id=item.get("place_id") or item["name"],
            defaults={
                "name":         item.get("name", ""),
                "address":      item.get("address", ""),
                "city":         item.get("city", "Islamabad"),
                "area":         item.get("area", ""),
                "phone":        item.get("phone", ""),
                "website":      item.get("website", ""),
                "rating":       item.get("rating", 0),
                "review_count": item.get("review_count", 0),
                "latitude":     item.get("latitude", 0),
                "longitude":    item.get("longitude", 0),
                "categories":   item.get("categories", []),
                "hours":        item.get("hours", {}),
                "description":  item.get("description", ""),
                "maps_url":     item.get("google_maps_url", ""),
            }
        )
        if is_new:
            created += 1
        else:
            updated += 1

    print(f"Import complete: {created} created, {updated} updated.")


# ─── Fill-Missing Mode ───────────────────────────────────────────────────────

def _needs_update(item: dict) -> bool:
    """Return True if the listing is missing any of the key fields."""
    return (
        not item.get("rating")
        or not item.get("reviews")
        or not item.get("images")
        or not item.get("latitude")
    )


async def run_fill_missing():
    """Re-scrape only listings that are missing rating, coords, reviews, or images."""
    if not OUTPUT_FILE.exists():
        print(f"No existing data file found at {OUTPUT_FILE}. Run a full scrape first.")
        return

    data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    to_update = [item for item in data if _needs_update(item)]

    print(f"Total listings : {len(data)}")
    print(f"Need updating  : {len(to_update)}")

    if not to_update:
        print("Nothing to update — all listings are complete.")
        return

    # Index existing data by place_id (or name+address as fallback) for fast merging
    index = {}
    for item in data:
        key = item.get("place_id") or f"{item['name']}|{item.get('address','')}"
        index[key] = item

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Karachi",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()

        updated = 0
        for i, item in enumerate(to_update, 1):
            url = item.get("google_maps_url", "")
            if not url:
                print(f"  [{i:03d}] Skipping (no URL): {item.get('name')}")
                continue

            print(f"  [{i:03d}/{len(to_update)}] {item.get('name', '')[:50]}")
            await asyncio.sleep(rand_delay())

            dc = await scrape_listing(page, url, item.get("source_query", ""))
            if not dc:
                continue

            # Merge: only overwrite fields that were missing/zero
            key = item.get("place_id") or f"{item['name']}|{item.get('address','')}"
            existing = index.get(key, item)

            if not existing.get("rating") and dc.rating:
                existing["rating"] = dc.rating
            if not existing.get("review_count") and dc.review_count:
                existing["review_count"] = dc.review_count
            if not existing.get("latitude") and dc.latitude:
                existing["latitude"] = dc.latitude
                existing["longitude"] = dc.longitude
            if not existing.get("reviews") and dc.reviews:
                existing["reviews"] = dc.reviews
            if not existing.get("images") and dc.images:
                existing["images"] = dc.images

            updated += 1

            # Save after every 10 updates
            if updated % 10 == 0:
                save_results([DaycareCenter(**{k: v for k, v in d.items()
                                              if k in DaycareCenter.__dataclass_fields__})
                              for d in data])
                print(f"  Checkpoint saved ({updated} updated so far)...")

        await browser.close()

    # Final save using raw dicts (avoids dataclass field mismatch issues)
    OUTPUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Rebuild CSV
    results_dc = []
    for d in data:
        dc = DaycareCenter(**{k: v for k, v in d.items()
                              if k in DaycareCenter.__dataclass_fields__})
        results_dc.append(dc)
    save_results(results_dc)

    print(f"\nDone. {updated}/{len(to_update)} listings updated → {OUTPUT_FILE}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Google Maps Daycare Scraper")
    parser.add_argument(
        "--fill-missing",
        action="store_true",
        help="Re-scrape only listings missing rating, coordinates, reviews, or images",
    )
    args = parser.parse_args()

    if args.fill_missing:
        asyncio.run(run_fill_missing())
    else:
        asyncio.run(run_scraper())
