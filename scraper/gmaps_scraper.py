"""
Google Maps Daycare Scraper
Playwright + BeautifulSoup

Usage:
    pip install playwright beautifulsoup4 lxml asyncio
    playwright install chromium
    python gmaps_scraper.py -q queries/dubai.txt                    # all searches, output dubai_listings.json
    python gmaps_scraper.py -q queries/dubai.txt --areas "JLT,Al Barsha"   # trial: only these areas, no grid
    python gmaps_scraper.py -q queries/dubai.txt --resume --max-hours 2    # continue, stop after 2 hours
    python gmaps_scraper.py -q queries/dubai.txt --report           # what each keyword/area found
    python gmaps_scraper.py -q queries/dubai.txt --fill-missing     # re-visit listings missing data

Searches come from a queries file (see search_plan.py): every keyword in
every area, then a map-grid sweep. The output defaults to
<queries file name>_listings.json next to this script. Each place is checked against the
directory's rules (search_plan.classify); places that don't belong are
written to <output>.rejected.json with the reason instead of being scraped
in full.

Photos are downloaded while their Google URLs are still valid and saved under
scraper/images/<place id>/. The JSON records each photo's local file, which
import_listings copies into Django's media storage.
"""

import asyncio
import csv
import hashlib
import json
import os
import re
import time
import random
from collections import Counter
from dataclasses import dataclass, asdict, field
from pathlib import Path
from urllib.parse import quote_plus, unquote_plus
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from area_match import AreaMatcher
from search_plan import QueryPlan, SearchTask, build_tasks, classify, load_plan


# ─── Config ──────────────────────────────────────────────────────────────────

SCRAPER_DIR = Path(__file__).parent
IMAGES_DIR = SCRAPER_DIR / "images"
SCREENSHOTS_DIR = SCRAPER_DIR / "screenshots"   # pages where nothing was found
STOP_FILE = SCRAPER_DIR / "STOP"   # create this file to stop a run cleanly
DELAY_MIN = 2.5   # seconds between actions (be polite, avoid bans)
DELAY_MAX = 5.0
MAX_RESULTS_PER_QUERY = 120   # Google Maps lists at most ~120 per search
MAX_SCROLLS = 40
MAX_IMAGES = 4
MIN_IMAGE_BYTES = 5_000   # anything smaller is an icon or placeholder

# Place photos are served from googleusercontent.com under paths that vary
# between page loads (/p/, /gps-cs-s/, /grass-cs/, ...). Reviewers' profile
# pictures always use /a/ or /a-/, so those are the ones to exclude.
PHOTO_URL_RE = re.compile(r"https://[\w.-]*googleusercontent\.com/(?!a-?/)[\w-]+/[^\s\"')]+")
MIN_PHOTO_PX = 100   # size suffixes below this (=w32-h32, =s40) are icons
# h1 headings of Google's panels, never a place name
PANEL_HEADINGS = {"", "results", "hours", "sponsored"}


class ScraperBlocked(Exception):
    """Google served a CAPTCHA / 'unusual traffic' page."""


# ─── Data Model ──────────────────────────────────────────────────────────────

@dataclass
class DaycareCenter:
    name: str = ""
    address: str = ""
    city: str = ""
    area: str = ""           # from the queries file's areas (area_match.py); import_listings re-checks it
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
    listing_type: str = ""   # "daycare" or "preschool" (search_plan.classify)
    search_area: str = ""    # area from the queries file that first found it
    found_by: list = field(default_factory=list)   # keys of every search that listed it
    closed: bool = False     # Google says "Permanently closed"
    reviews: list = field(default_factory=list)   # up to 5 Google reviews
    images: list = field(default_factory=list)    # up to 4 {"url", "alt", "file"}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def rand_delay():
    return random.uniform(DELAY_MIN, DELAY_MAX)


def extract_place_id(url: str) -> str:
    """Extract Google's stable place (feature) ID, e.g. 0x38dfe9...:0x52f0dd...

    Every place URL carries it as !1s<id> in the data= segment. The name in
    the /place/<name>/ path is not an ID: it changes when the business is
    renamed and is shared by branches with the same name.
    """
    match = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url)
    return match.group(1) if match else ""


def place_name_from_url(url: str) -> str:
    """'.../maps/place/Gardenia+Nursery+-+The+Greens/...' -> 'Gardenia Nursery - The Greens'"""
    match = re.search(r"/maps/place/([^/@]+)/", url)
    return unquote_plus(match.group(1)) if match else ""


def extract_coords(url: str) -> tuple[float, float]:
    """Extract lat/lng from Google Maps URL.

    !3d<lat>!4d<lng> is the place's own pin; @lat,lng is only where the map
    viewport is centred, so it is the fallback.
    """
    match = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if match:
        return float(match.group(1)), float(match.group(2))
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if match:
        return float(match.group(1)), float(match.group(2))
    return 0.0, 0.0


def listing_key(dc: "DaycareCenter") -> str:
    """Dedup key: the place ID, or name+address when Google gave none."""
    return dc.place_id or f"{dc.name}|{dc.address}"


def image_dir_name(dc: "DaycareCenter") -> str:
    """Filesystem-safe folder name for a listing's photos."""
    if dc.place_id:
        return dc.place_id.replace(":", "_")
    return "noid_" + hashlib.sha1(listing_key(dc).encode("utf-8")).hexdigest()[:16]


def default_output(queries_file: Path) -> Path:
    """queries/dubai.txt -> scraper/dubai_listings.json"""
    return SCRAPER_DIR / f"{queries_file.stem}_listings.json"


def find_phone(text: str, phone_code: str = "") -> str:
    """A phone number in page text: international (+971 4 123 4567, +92 300 ...)
    or local (04 123 4567, 050 123 4567, 051 2345678)."""
    intl = rf"\+{phone_code}" if phone_code else r"\+\d{1,3}"
    m = re.search(rf"({intl}[\s\-]?\d[\d\s\-]{{7,}}\d|\b0\d{{1,3}}[\s\-]?\d{{3}}[\s\-]?\d{{3,5}}\b)", text)
    return m.group(1).strip() if m else ""


# ─── Parser ──────────────────────────────────────────────────────────────────

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def clean_text(text: str) -> str:
    """Drop Google's icon glyphs (Unicode private-use area) and squeeze spaces."""
    text = re.sub(r"[-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_hours(soup: BeautifulSoup) -> dict:
    """Opening hours as {"Monday": "8 AM–2 PM", ...}, or {} if not listed.

    Google renders the week either as an aria-label like
    "Monday, 8 AM to 2 PM; Tuesday, ...; Hide open hours for the week" or as a
    table with one row per day.
    """
    hours = {}
    for tag in soup.find_all(attrs={"aria-label": re.compile(r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,")}):
        label = tag["aria-label"]
        if sum(day in label for day in DAYS) < 3:
            continue
        for part in label.split(";"):
            day, _, time = part.partition(",")
            day = day.strip()
            if day in DAYS:
                hours[day] = clean_text(time.split(". Hide")[0].replace(" to ", "–"))
        if hours:
            return hours

    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            day = clean_text(cells[0].get_text(" ", strip=True))
            if day in DAYS:
                hours[day] = clean_text(cells[1].get_text(" ", strip=True))
    return hours

def parse_listing_page(html: str, url: str, query: str,
                       city: str = "", phone_code: str = "") -> DaycareCenter:
    """Parse a single Google Maps place page into a DaycareCenter.

    `city` and `phone_code` (from the queries file) help the fallbacks used
    when the address or phone button is missing."""
    soup = BeautifulSoup(html, "lxml")
    dc = DaycareCenter(google_maps_url=url, source_query=query)

    # Name — the place title (h1.DUwDvf); otherwise the first h1 that isn't
    # a panel heading such as "Results" or "Hours"
    title = soup.select_one("h1.DUwDvf")
    if title and title.get_text(strip=True):
        dc.name = title.get_text(strip=True)
    else:
        dc.name = next((h.get_text(strip=True) for h in soup.find_all("h1")
                        if h.get_text(strip=True).lower() not in PANEL_HEADINGS), "")

    # All visible text sections for targeted extraction
    all_text = soup.get_text(" ", strip=True)
    dc.closed = "Permanently closed" in all_text

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

    # Address — the address button's aria-label is "Address: <address>"
    addr_tag = soup.find("button", {"data-item-id": "address"})
    if addr_tag:
        dc.address = clean_text(
            re.sub(r"^Address:\s*", "", addr_tag.get("aria-label", "")) or addr_tag.get_text(" ", strip=True)
        )
    elif city:
        # Fallback: a short text with a number followed by the city name
        city_re = re.compile(rf"\d+.+\b{re.escape(city)}\b", re.I)
        for tag in soup.find_all(["div", "span"]):
            text = tag.get_text(strip=True)
            if city_re.search(text) and len(text) < 150:
                dc.address = text
                break

    # Phone number — from the phone button ("Phone: +971 ..."); scanning the
    # whole page as a fallback can pick up numbers quoted in reviews
    phone_tag = soup.find("button", {"data-item-id": re.compile(r"^phone:")})
    if phone_tag:
        dc.phone = clean_text(re.sub(r"^Phone:\s*", "", phone_tag.get("aria-label", "")))
    if not dc.phone:
        dc.phone = find_phone(all_text, phone_code)

    # Website — the "authority" link; otherwise the first non-Google link
    website_tag = soup.find("a", {"data-item-id": "authority"}, href=True)
    if not website_tag:
        website_tag = soup.find(
            "a", href=re.compile(r"^https?://(?![^/]*(?:google|gstatic|ggpht)\.)")
        )
    if website_tag:
        dc.website = website_tag.get("href", "")

    dc.hours = parse_hours(soup)

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

        # Each review card. Cards are nested (an inner div repeats the
        # data-review-id), so keep only the first, outermost div per review.
        cards, seen_ids = [], set()
        for div in soup.find_all("div", attrs={"data-review-id": True}):
            if div["data-review-id"] not in seen_ids:
                seen_ids.add(div["data-review-id"])
                cards.append(div)
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


def _collect_photo_urls(html: str, images: list[dict], seen_urls: set[str], max_images: int):
    """Append place-photo URLs found in html, skipping profile pictures and icons.

    Photos appear as <img src> on the place page and as CSS background-image
    in the photo gallery.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates = [(img["src"], img.get("alt", "")) for img in soup.find_all("img", src=True)]
    for tag in soup.find_all(style=re.compile("background-image")):
        candidates += [(u, "") for u in re.findall(r'url\(["\']?([^"\')]+)', tag["style"])]

    for src, alt in candidates:
        if len(images) >= max_images:
            return
        if not PHOTO_URL_RE.fullmatch(src):
            continue
        # Quality URLs carry a size suffix; tiny ones are icons
        size = re.search(r"=(?:w|s)(\d+)", src)
        if not size or int(size.group(1)) < MIN_PHOTO_PX:
            continue
        hq = _upgrade_image_url(src)
        if hq not in seen_urls:
            seen_urls.add(hq)
            images.append({"url": hq, "alt": alt})


async def scrape_images(page: Page, max_images: int = MAX_IMAGES) -> list[dict]:
    """Collect up to max_images place-photo URLs from the currently loaded place page."""
    images: list[dict] = []
    seen_urls: set[str] = set()

    try:
        _collect_photo_urls(await page.content(), images, seen_urls, max_images)

        # Fallback: try clicking the photos section to open the gallery
        if len(images) < 2:
            photos_btn = await page.query_selector(
                'button[aria-label*="photo" i], div[data-photo-index], [jsaction*="photo" i]'
            )
            if photos_btn:
                await photos_btn.click()
                await asyncio.sleep(rand_delay())
                _collect_photo_urls(await page.content(), images, seen_urls, max_images)
                # Close the gallery so the reviews step sees the place panel
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)

    except Exception:
        pass

    return images[:max_images]


_IMAGE_EXTS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


async def download_images(page: Page, dc: "DaycareCenter") -> list[dict]:
    """Download dc.images to IMAGES_DIR now, while Google's URLs still work.

    Returns only the photos that downloaded, each with a "file" path relative
    to the scraper folder.
    """
    folder = IMAGES_DIR / image_dir_name(dc)
    saved = []
    for img in dc.images:
        try:
            resp = await page.context.request.get(img["url"], timeout=30000)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
            body = await resp.body()
        except Exception as e:
            print(f"    [photo error] {e}")
            continue
        if not resp.ok or ctype not in _IMAGE_EXTS or len(body) < MIN_IMAGE_BYTES:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{len(saved) + 1}.{_IMAGE_EXTS[ctype]}"
        path.write_bytes(body)
        saved.append({**img, "file": path.relative_to(SCRAPER_DIR).as_posix()})
    return saved


async def dismiss_sign_in_prompt(page: Page):
    """Close Google's "Sign-in to get the best of Google Maps" popup if shown."""
    try:
        btn = await page.query_selector('button:has-text("Dismiss")')
        if btn and await btn.is_visible():
            await btn.click()
            await asyncio.sleep(1)
    except Exception:
        pass


async def check_not_blocked(page: Page):
    if "/sorry/" in page.url or "detected unusual traffic" in (await page.content()):
        raise ScraperBlocked(page.url)


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def scroll_results_panel(page: Page, max_scrolls: int = MAX_SCROLLS):
    """Scroll the results list until Google says it has reached the end, or
    three scrolls in a row load nothing new."""
    try:
        panel = await page.wait_for_selector('div[role="feed"]', timeout=8000)
    except PWTimeout:
        return   # no results list (single result or no results)

    last_count, stalls = -1, 0
    for _ in range(max_scrolls):
        await panel.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        await asyncio.sleep(rand_delay() * 0.6)
        if await page.get_by_text("reached the end of the list").count():
            return
        count = await page.locator('div[role="feed"] a[href*="/maps/place/"]').count()
        if count >= MAX_RESULTS_PER_QUERY:
            return
        stalls = stalls + 1 if count == last_count else 0
        if stalls >= 3:
            return
        last_count = count


async def get_listing_urls(page: Page, task: SearchTask) -> list[str]:
    """Search Google Maps and collect all listing URLs."""
    print(f"\n[search] {task.query}" + (f" @ {task.center[0]:.4f},{task.center[1]:.4f}" if task.is_grid else ""))
    await page.goto(task.url, wait_until="domcontentloaded", timeout=60000)
    await check_not_blocked(page)
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
    await scroll_results_panel(page)

    # Collect all place URLs from the results panel
    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    # Keep the full href: its data= segment holds the place ID and pin
    # coordinates, and without it Google may open a search list instead of
    # the place when several share a name.
    urls: dict[str, str] = {}   # place ID (or URL) -> URL, in result order
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/maps/place/" in href:
            if href.startswith("/"):
                href = "https://www.google.com" + href
            urls.setdefault(extract_place_id(href) or href, href)

    # A search with exactly one match opens that place instead of a list
    if not urls and "/maps/place/" in page.url:
        urls[extract_place_id(page.url) or page.url] = page.url

    print(f"  found {len(urls)} listings")
    return list(urls.values())[:MAX_RESULTS_PER_QUERY]


async def open_by_name_search(page: Page, url: str, city: str) -> bool:
    """Load a place through a search for its name, for places whose own link
    gives an empty panel. Google either opens the place straight away or
    lists results; then the result with the same place ID is clicked
    (opening its link directly would give the empty panel again).
    Returns whether the place's title loaded."""
    name, fid = place_name_from_url(url), extract_place_id(url)
    if not name or not fid:
        return False
    await asyncio.sleep(rand_delay())
    await page.goto(f"https://www.google.com/maps/search/{quote_plus(f'{name} {city}'.strip())}",
                    wait_until="domcontentloaded", timeout=60000)
    await check_not_blocked(page)
    await asyncio.sleep(rand_delay())
    # The place's own URL may write the ID's ":" as "%3A"
    if fid not in unquote_plus(page.url):
        link = (await page.query_selector(f'a[href*="{fid}"]')
                or await page.query_selector(f'a[href*="{fid.replace(":", "%3A")}"]'))
        if not link:
            return False
        await link.click()
        await asyncio.sleep(rand_delay())
    try:
        await page.wait_for_function(
            "() => { const h = document.querySelector('h1.DUwDvf'); return h && h.innerText.trim(); }",
            timeout=10000)
    except PWTimeout:
        return False
    await dismiss_sign_in_prompt(page)
    return True


async def scrape_listing(page: Page, url: str, query: str,
                         plan: QueryPlan | None = None) -> tuple[DaycareCenter | None, str]:
    """Navigate to a place page and extract details.

    With `plan`, the place is first checked against the directory's rules;
    a place that doesn't belong is returned straight away with the reason,
    without fetching its photos and reviews. Returns (place, reject reason).
    """
    city = plan.city if plan else ""
    phone_code = plan.country.get("phone_code", "") if plan else ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await check_not_blocked(page)
        await asyncio.sleep(rand_delay())

        # Wait for the main content to render. "attached", not visible: on
        # an empty panel (below) the title is there but hidden
        await page.wait_for_selector("h1", state="attached", timeout=10000)

        # Expand "See more" if present
        try:
            see_more = await page.query_selector('button:has-text("See more")')
            if see_more:
                await see_more.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        await dismiss_sign_in_prompt(page)

        dc = parse_listing_page(await page.content(), page.url, query, city, phone_code)
        if not dc.name or not (dc.categories or dc.address):
            # For some places Google serves an empty panel (no name, category
            # or address) when the place link is opened directly, every
            # time; found through a search, the same place loads in full
            print(f"    [empty page] trying a name search: {place_name_from_url(url)}")
            if not await open_by_name_search(page, url, city):
                print(f"  [load failed] {url}")
                return None, ""   # not recorded, so a later search can retry it
            dc = parse_listing_page(await page.content(), page.url, query, city, phone_code)
        final_url = page.url
        # The final URL sometimes loses the data= segment; the search-result
        # link always has it.
        if not dc.place_id:
            dc.place_id = extract_place_id(url)
        if not dc.latitude:
            dc.latitude, dc.longitude = extract_coords(url)

        if plan:
            dc.listing_type, reason = classify(
                dc.name, dc.categories, dc.address, plan.city, dc.closed,
                dc.latitude, dc.longitude, plan.boundary, plan.exclude,
                keep=dc.place_id in plan.keep,
            )
            if reason:
                return dc, reason

        # Scrape reviews and images while still on the page
        dc.images = await scrape_images(page)
        dc.reviews = await scrape_reviews(page)
        dc.images = await download_images(page, dc)

        if not dc.images and not dc.reviews:
            # Keep a picture of the page to see why nothing was found
            SCREENSHOTS_DIR.mkdir(exist_ok=True)
            shot = SCREENSHOTS_DIR / f"{image_dir_name(dc)}.png"
            await page.screenshot(path=str(shot))
            print(f"    [no photos or reviews] screenshot: {shot.relative_to(SCRAPER_DIR).as_posix()}")

        return dc, ""

    except ScraperBlocked:
        raise
    except PWTimeout:
        print(f"  [timeout] {url}")
        return None, ""
    except Exception as e:
        print(f"  [error] {url}: {e}")
        return None, ""


def _state_file(output: Path) -> Path:
    return output.with_suffix(".state.json")


def _rejected_file(output: Path) -> Path:
    return output.with_suffix(".rejected.json")


def _load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, data):
    """Write via a temp file so a run stopped mid-save never leaves a broken file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_results(output: Path) -> list[DaycareCenter]:
    data = json.loads(output.read_text(encoding="utf-8"))
    return [DaycareCenter(**{k: v for k, v in d.items()
                             if k in DaycareCenter.__dataclass_fields__})
            for d in data]


async def launch_browser(pw, timezone_id: str = "UTC"):
    """Headless Chromium set up to get Google Maps' English desktop layout,
    in the time zone of the country being scraped ([country] timezone)."""
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
        timezone_id=timezone_id,
    )
    # Stealth: remove webdriver flag
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    """)
    return browser, await context.new_page()


def _add_found_by(found_by: list, key: str):
    if key not in found_by:
        found_by.append(key)


async def run_scraper(tasks: list[SearchTask], plan: QueryPlan, output: Path,
                      resume: bool = False, max_hours: float | None = None):
    """Main scraper entrypoint.

    Progress is saved after every place. Searches that finished are recorded
    in <output>.state.json, with how many places each found, so --resume
    skips them. Places that don't belong in the directory go to
    <output>.rejected.json.
    """
    state_file, rejected_file = _state_file(output), _rejected_file(output)
    results: list[DaycareCenter] = []
    rejected: list[dict] = []
    state = {"completed": [], "stats": {}}

    if resume:
        if output.exists():
            results = _load_results(output)
        rejected = _load_json(rejected_file, [])
        state = _load_json(state_file, state)
        state.setdefault("completed", state.pop("completed_queries", []))
        state.setdefault("stats", {})
        done = len(set(state["completed"]) & {t.key for t in tasks})
        print(f"Resuming: {len(results)} kept, {len(rejected)} rejected, "
              f"{done}/{len(tasks)} searches already done.")
    else:
        for path in (output, rejected_file):
            if path.exists():
                backup = path.with_name(path.stem + ".bak.json")
                path.replace(backup)
                print(f"Previous {path.name} moved to {backup.name}")
        state_file.unlink(missing_ok=True)

    # Every place seen so far, kept or rejected, by place ID (or name|address)
    kept = {listing_key(dc): dc for dc in results}
    rejected_by_key = {r["key"]: r for r in rejected}
    completed = set(state["completed"])
    todo = [t for t in tasks if t.key not in completed]
    deadline = time.monotonic() + max_hours * 3600 if max_hours else None
    matcher = AreaMatcher(plan)

    def save():
        save_results(results, output)
        write_json(rejected_file, rejected)
        write_json(state_file, state)

    def should_stop() -> str:
        if STOP_FILE.exists():
            return "stop file"
        if deadline is not None and time.monotonic() > deadline:
            return "time"
        return ""

    def already_seen(key: str, task: SearchTask) -> bool:
        if key in kept:
            _add_found_by(kept[key].found_by, task.key)
            return True
        if key in rejected_by_key:
            _add_found_by(rejected_by_key[key]["found_by"], task.key)
            return True
        return False

    stop_reason = ""
    async with async_playwright() as pw:
        browser, page = await launch_browser(pw, plan.country.get("timezone", "UTC"))
        try:
            for n, task in enumerate(todo, len(tasks) - len(todo) + 1):
                stop_reason = should_stop()
                if stop_reason:
                    break
                print(f"\n[{n}/{len(tasks)}]", end="")
                try:
                    listing_urls = await get_listing_urls(page, task)
                except ScraperBlocked:
                    raise
                except Exception as e:
                    print(f"  [search error] {e}")
                    continue   # not marked done, so --resume retries it

                stats = {"found": len(listing_urls), "new": 0, "kept": 0, "rejected": 0}
                for url in listing_urls:
                    stop_reason = should_stop()
                    if stop_reason:
                        break
                    # Skip places already scraped by an earlier search
                    pid = extract_place_id(url)
                    if pid and already_seen(pid, task):
                        continue

                    await asyncio.sleep(rand_delay())
                    dc, reason = await scrape_listing(page, url, task.query, plan)
                    if not dc or not dc.name or already_seen(listing_key(dc), task):
                        continue

                    key = listing_key(dc)
                    stats["new"] += 1
                    if reason:
                        entry = {
                            "key": key, "name": dc.name, "reason": reason,
                            "categories": dc.categories, "address": dc.address,
                            "google_maps_url": dc.google_maps_url, "found_by": [task.key],
                        }
                        rejected.append(entry)
                        rejected_by_key[key] = entry
                        stats["rejected"] += 1
                        print(f"  - {dc.name[:50]} | rejected: {reason}")
                    else:
                        dc.search_area = task.area
                        dc.city = plan.city
                        dc.area = matcher.match(dc.address, dc.latitude, dc.longitude, dc.name).area
                        dc.found_by = [task.key]
                        results.append(dc)
                        kept[key] = dc
                        stats["kept"] += 1
                        print(f"  + [{len(results):03d}] {dc.name} | {dc.listing_type} | "
                              f"{dc.area or dc.address[:40]} | {len(dc.images)} photos")
                    save()

                if stop_reason:
                    break   # search only half done: leave it for --resume
                state["completed"].append(task.key)
                state["stats"][task.key] = stats
                save()
                print(f"  found {stats['found']}, new {stats['new']} "
                      f"(kept {stats['kept']}, rejected {stats['rejected']})")
                await asyncio.sleep(random.uniform(8, 15))  # longer pause between searches

        except ScraperBlocked as e:
            stop_reason = f"blocked ({e})"

        save()
        await browser.close()

    if stop_reason == "time":
        print(f"\nStopped after {max_hours} h. Run again with --resume to continue.")
    elif stop_reason == "stop file":
        STOP_FILE.unlink(missing_ok=True)
        print(f"\nStopped because {STOP_FILE.name} was found (now removed). "
              "Run again with --resume to continue.")
    elif stop_reason:
        print(f"\n[blocked] Google is showing a CAPTCHA / unusual-traffic page ({stop_reason}).")
        print("Progress saved. Wait a few hours, then run again with --resume.")
    else:
        print(f"\nDone. {len(results)} kept, {len(rejected)} rejected → {output}")
    print_report(output)
    return results


def _key_parts(key: str) -> tuple[str, str, str]:
    """Search key -> (method, keyword, area): 'area:F-7|daycare' -> ('area', 'daycare', 'F-7')."""
    method, _, rest = key.partition(":")
    if method == "area":
        area, _, keyword = rest.partition("|")
        return method, keyword, area
    if method == "grid":
        return method, rest.partition("@")[0], ""
    return method, rest, ""


def print_report(output: Path):
    """Summarise what each keyword, area and the grid sweep contributed."""
    state = _load_json(_state_file(output), None)
    if state is None:
        print(f"No progress file for {output.name}.")
        return
    results = _load_json(output, [])
    rejected = _load_json(_rejected_file(output), [])
    stats = state.get("stats", {})

    print(f"\n── Report: {output.name} ──")
    print(f"Searches done: {len(state.get('completed', []))} | kept: {len(results)} | rejected: {len(rejected)}")
    types = Counter(r.get("listing_type") or "?" for r in results)
    print("Kept by type: " + ", ".join(f"{t} {c}" for t, c in sorted(types.items())))

    # Per keyword: searches run, results listed, kept places it found,
    # and kept places no other keyword found
    rows = {}

    def row(mk):
        return rows.setdefault(mk, {"searches": 0, "listed": 0, "kept": 0, "only": 0})

    for key, s in stats.items():
        r = row(_key_parts(key)[:2])
        r["searches"] += 1
        r["listed"] += s.get("found", 0)
    for res in results:
        found = {_key_parts(k)[:2] for k in res.get("found_by", [])}
        for mk in found:
            row(mk)["kept"] += 1
        if len(found) == 1:
            row(next(iter(found)))["only"] += 1
    print(f"\n{'search':30} {'searches':>8} {'results':>8} {'kept':>6} {'only this':>9}")
    for (method, kw), r in sorted(rows.items()):
        label = f"{method}: {kw}"
        print(f"{label[:30]:30} {r['searches']:>8} {r['listed']:>8} {r['kept']:>6} {r['only']:>9}")

    if any(k.startswith("grid:") for k in stats):
        only_grid = sum(1 for res in results
                        if res.get("found_by") and all(k.startswith("grid:") for k in res["found_by"]))
        print(f"\nFound only by the grid sweep (missed by every area search): {only_grid}")

    # Per area: kept places listed by that area's searches
    areas = {_key_parts(k)[2]: set() for k in stats if k.startswith("area:")}
    for res in results:
        for k in res.get("found_by", []):
            method, _, area = _key_parts(k)
            if method == "area":
                areas.setdefault(area, set()).add(res.get("place_id") or res.get("name"))
    if areas:
        print("\nKept places per area: " + ", ".join(f"{a} {len(p)}" for a, p in areas.items()))
        empty = [a for a, p in areas.items() if not p]
        if empty:
            print("Areas with nothing kept: " + ", ".join(empty))

    reasons = Counter(r["reason"].split(" (")[0] for r in rejected)
    if reasons:
        print("\nRejected: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()))


def save_results(results: list[DaycareCenter], output: Path):
    """Save results to JSON and CSV, overwriting each time."""
    data = [asdict(r) for r in results]
    write_json(output, data)

    csv_file = output.with_suffix(".csv")
    if data:
        # Flatten list/dict fields to strings for CSV
        flat = []
        for row in data:
            flat.append({
                **{k: v for k, v in row.items() if not isinstance(v, (list, dict))},
                "categories": ", ".join(row.get("categories") or []),
                "hours": json.dumps(row.get("hours") or {}, ensure_ascii=False),
                "reviews": json.dumps(row.get("reviews") or [], ensure_ascii=False),
                "images": "|".join(img.get("file") or img.get("url", "")
                                   for img in (row.get("images") or [])),
            })
        tmp = csv_file.with_name(csv_file.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=flat[0].keys())
            writer.writeheader()
            writer.writerows(flat)
        os.replace(tmp, csv_file)


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

def _has_photos(item: dict) -> bool:
    """True if at least one photo was downloaded (bare Google URLs expire)."""
    return any(img.get("file") for img in item.get("images") or [])


def _needs_update(item: dict) -> bool:
    """Return True if the listing is missing any of the key fields."""
    return (
        not item.get("rating")
        or not item.get("reviews")
        or not _has_photos(item)
        or not item.get("hours")
        or not item.get("latitude")
        or not item.get("place_id", "").startswith("0x")
    )


async def run_fill_missing(output: Path, timezone_id: str = "UTC"):
    """Re-scrape only listings that are missing rating, coords, reviews, or images."""
    if not output.exists():
        print(f"No existing data file found at {output}. Run a full scrape first.")
        return

    data = json.loads(output.read_text(encoding="utf-8"))
    to_update = [item for item in data if _needs_update(item)]

    print(f"Total listings : {len(data)}")
    print(f"Need updating  : {len(to_update)}")

    if not to_update:
        print("Nothing to update — all listings are complete.")
        return

    async with async_playwright() as pw:
        browser, page = await launch_browser(pw, timezone_id)

        def checkpoint():
            write_json(output, data)

        updated = 0
        try:
            for i, item in enumerate(to_update, 1):
                url = item.get("google_maps_url", "")
                if not url:
                    print(f"  [{i:03d}] Skipping (no URL): {item.get('name')}")
                    continue

                print(f"  [{i:03d}/{len(to_update)}] {item.get('name', '')[:50]}")
                await asyncio.sleep(rand_delay())

                dc, _ = await scrape_listing(page, url, item.get("source_query", ""))
                if not dc:
                    continue

                # Merge: only overwrite fields that were missing/zero
                if not item.get("place_id", "").startswith("0x") and dc.place_id:
                    item["place_id"] = dc.place_id
                if not item.get("rating") and dc.rating:
                    item["rating"] = dc.rating
                if not item.get("review_count") and dc.review_count:
                    item["review_count"] = dc.review_count
                if not item.get("latitude") and dc.latitude:
                    item["latitude"] = dc.latitude
                    item["longitude"] = dc.longitude
                if not item.get("reviews") and dc.reviews:
                    item["reviews"] = dc.reviews
                if not item.get("hours") and dc.hours:
                    item["hours"] = dc.hours
                if not _has_photos(item) and dc.images:
                    item["images"] = dc.images

                updated += 1

                # Save after every 10 updates
                if updated % 10 == 0:
                    checkpoint()
                    print(f"  Checkpoint saved ({updated} updated so far)...")

        except ScraperBlocked as e:
            print(f"\n[blocked] Google is showing a CAPTCHA / unusual-traffic page ({e}).")
            print("Progress saved. Wait a few hours and run --fill-missing again.")

        await browser.close()

    # Final save using raw dicts (avoids dataclass field mismatch issues),
    # then rebuild the CSV
    checkpoint()
    save_results(_load_results(output), output)

    print(f"\nDone. {updated}/{len(to_update)} listings updated → {output}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Google Maps Daycare Scraper")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fill-missing",
        action="store_true",
        help="Re-scrape only listings missing rating, coordinates, reviews, or images",
    )
    mode.add_argument(
        "--resume",
        action="store_true",
        help="Continue an interrupted run, skipping searches that already finished",
    )
    mode.add_argument(
        "--report",
        action="store_true",
        help="Print what each keyword, area and the grid sweep found, then exit",
    )
    parser.add_argument(
        "-q", "--queries-file", type=Path, required=True,
        help="Country, keywords, areas and grid to search, e.g. queries/dubai.txt",
    )
    parser.add_argument(
        "--output", type=Path,
        help="JSON output file (default: <queries file name>_listings.json, e.g. dubai_listings.json)",
    )
    parser.add_argument(
        "--city", help="City that addresses must be in (default: from the queries file name)",
    )
    parser.add_argument(
        "--areas", metavar="A,B",
        help='Only search these areas from the queries file, e.g. "F-7,G-9" (skips the grid)',
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--no-grid", action="store_true", help="Skip the map-grid sweep")
    which.add_argument("--grid-only", action="store_true", help="Only run the map-grid sweep")
    parser.add_argument(
        "--query", action="append", dest="queries", metavar="TEXT",
        help="Search this text instead of the queries file (repeatable)",
    )
    parser.add_argument(
        "--max-per-query", type=int, default=MAX_RESULTS_PER_QUERY,
        help=f"Listings to scrape per search (default: {MAX_RESULTS_PER_QUERY})",
    )
    parser.add_argument(
        "--max-hours", type=float,
        help="Stop cleanly after this many hours; continue later with --resume",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print the searches that would run, then exit",
    )
    args = parser.parse_args()
    MAX_RESULTS_PER_QUERY = args.max_per_query

    # "queries/dubai.txt" works from the project root as well as from scraper/
    if not args.queries_file.exists() and (SCRAPER_DIR / args.queries_file).exists():
        args.queries_file = SCRAPER_DIR / args.queries_file
    if not args.queries_file.exists():
        parser.error(f"queries file not found: {args.queries_file}")
    plan = load_plan(args.queries_file, args.city)
    args.output = args.output or default_output(args.queries_file)

    if args.report:
        print_report(args.output)
    elif args.fill_missing:
        asyncio.run(run_fill_missing(args.output, plan.country.get("timezone", "UTC")))
    else:
        if args.queries:
            tasks = [SearchTask(key=f"query:{q}", query=q, keyword=q) for q in args.queries]
        else:
            areas = [a.strip() for a in args.areas.split(",")] if args.areas else None
            try:
                tasks = build_tasks(plan, areas=areas, include_areas=not args.grid_only,
                                    include_grid=not args.no_grid)
            except ValueError as e:
                parser.error(str(e))
        if args.list:
            for t in tasks:
                print(t.query + (f"  @ {t.center[0]:.4f},{t.center[1]:.4f}" if t.is_grid else ""))
            grid = sum(t.is_grid for t in tasks)
            print(f"\n{len(tasks)} searches ({len(tasks) - grid} area, {grid} grid), city: {plan.city}")
        else:
            asyncio.run(run_scraper(tasks, plan, args.output,
                                    resume=args.resume, max_hours=args.max_hours))
