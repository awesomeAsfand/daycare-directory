"""
Google Maps Daycare Scraper
Playwright + BeautifulSoup | Pakistan Directory Project

Usage:
    pip install playwright beautifulsoup4 lxml asyncio
    playwright install chromium
    python gmaps_scraper.py
"""

import asyncio
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

OUTPUT_FILE = Path("daycare_listings.json")
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
    """Extract lat/lng from Google Maps URL."""
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
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

    # Rating — pattern: "4.3 stars" or "4.3"
    rating_match = re.search(r"(\d\.\d)\s*(?:stars?|★)", all_text)
    if rating_match:
        dc.rating = float(rating_match.group(1))

    # Review count
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
    await page.goto(search_url, wait_until="networkidle", timeout=30000)
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
        await page.goto(url, wait_until="networkidle", timeout=25000)
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
        return parse_listing_page(html, final_url, query)

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
    """Save results to JSON, overwriting each time."""
    data = [asdict(r) for r in results]
    OUTPUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run_scraper())
