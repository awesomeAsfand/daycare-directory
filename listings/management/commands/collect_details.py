"""
python manage.py collect_details --city Dubai
python manage.py collect_details --city Dubai --resume      # continue an interrupted run
python manage.py collect_details --city Dubai --limit 10    # try a few sites first

Visit each active listing's website (once per website: chains share one)
and save the passages about ages, curriculum, licensing and fees, for review
before anything goes into the database (see apply_details).

For each website the home page is read, then up to MAX_EXTRA_PAGES pages on
the same site whose link looks like fees, admissions, curriculum or about.
Output: scraper/details/<city>_details_raw.json, saved after every site.
"""
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from listings.models import DaycareListing

MAX_EXTRA_PAGES = 4
MAX_SNIPPETS = 60
PAGE_TIMEOUT_MS = 30000
DELAY_S = 1.5

# Websites that aren't the nursery's own site
SKIP_DOMAINS = ("facebook.", "instagram.", "wa.me", "whatsapp.", "linktr.ee", "google.", "youtube.", "tiktok.")

LINK_WORDS = re.compile(
    r"fee|tuition|price|admission|enrol|enroll|apply|curricul|program|about|age|class|faq|timing|why",
    re.I,
)
SNIPPET_RE = re.compile(
    r"\d+\s*(?:days?|weeks?|months?|mths|years?|yrs)\b|\bages?\b"
    r"|eyfs|early years foundation|montessori|reggio|curricul|\bib\b|baccalaureate|american|canadian"
    r"|french|highscope|waldorf|steiner|forest school|bilingual|arabic"
    r"|khda|adek|spea|ministry of education|licen[cs]"
    r"|\baed\b|\bdhs\b|\bfees?\b|tuition|per term|per year|annual",
    re.I,
)


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Image names ("logo@2x.png"), site builders' tracking and template addresses
NOT_EMAIL_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg)$|@(?:sentry|wixpress|example)\.|@(?:domain|email|yourdomain)\.com$",
    re.I,
)
CONTACT_WORDS = re.compile(r"contact|get in touch|reach us|enquir|inquir", re.I)


def decode_cfemail(hex_text):
    """Cloudflare hides addresses as hex, XORed with the first byte."""
    key = int(hex_text[:2], 16)
    return "".join(chr(int(hex_text[i:i + 2], 16) ^ key) for i in range(2, len(hex_text), 2))


def find_emails(text, links, cf_codes=()):
    """Email addresses from mailto: links, Cloudflare-protected addresses and
    the page text, in that order, without repeats."""
    found = []
    for href, _ in links:
        href = (href or "").strip()
        if href.lower().startswith("mailto:"):
            found += unquote(href[7:].split("?")[0]).split(",")
        elif "/cdn-cgi/l/email-protection#" in href:
            cf_codes = [*cf_codes, href.rpartition("#")[2]]
    for code in cf_codes:
        try:
            found.append(decode_cfemail(code))
        except ValueError:
            pass
    found += EMAIL_RE.findall(text)
    out = []
    for email in found:
        email = email.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(email) and not NOT_EMAIL_RE.search(email) and email not in out:
            out.append(email)
    return out


def clean_url(url):
    """Drop tracking parameters and fragments: ?utm_source=gmb etc."""
    p = urlparse(url.strip())
    return urlunparse((p.scheme or "https", p.netloc.lower(), p.path or "/", "", "", ""))


def snippets(text, seen):
    """Lines (or sentences of long lines) that mention ages, curriculum,
    licensing or fees; each at most 300 characters, no repeats."""
    out = []
    for line in text.splitlines():
        for part in re.split(r"(?<=[.!?])\s+", line) if len(line) > 300 else [line]:
            part = re.sub(r"\s+", " ", part).strip()
            if 3 < len(part) <= 300 and SNIPPET_RE.search(part) and part.lower() not in seen:
                seen.add(part.lower())
                out.append(part)
    return out


class Command(BaseCommand):
    help = "Collect ages, curriculum, licensing and fees passages from nursery websites"

    def add_arguments(self, parser):
        parser.add_argument("--city", required=True)
        parser.add_argument("--output", help="Default: scraper/details/<city>_details_raw.json")
        parser.add_argument("--resume", action="store_true", help="Skip websites already collected")
        parser.add_argument("--limit", type=int, help="Only this many websites")

    def handle(self, *args, **options):
        city_file = slugify(options["city"]).replace("-", "_")
        out = Path(options["output"] or Path(settings.BASE_DIR) / "scraper" / "details"
                   / f"{city_file}_details_raw.json")
        out.parent.mkdir(parents=True, exist_ok=True)

        sites = defaultdict(list)   # website -> listings
        qs = DaycareListing.objects.filter(city__slug=slugify(options["city"]), is_active=True).exclude(website="")
        for listing in qs.order_by("pk"):
            url = clean_url(listing.website)
            if not any(d in urlparse(url).netloc for d in SKIP_DOMAINS):
                sites[url].append(listing)

        results = json.loads(out.read_text(encoding="utf-8")) if options["resume"] and out.exists() else []
        done = {r["url"] for r in results}
        todo = [u for u in sites if u not in done][:options["limit"]]
        self.stdout.write(f"{len(sites)} websites for {qs.count()} listings; {len(done)} done, {len(todo)} to visit")

        asyncio.run(self.collect(todo, sites, results, out))
        found = sum(1 for r in results if r["snippets"])
        self.stdout.write(self.style.SUCCESS(f"Done: {len(results)} websites, {found} with passages -> {out}"))

    async def collect(self, todo, sites, results, out):
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            # ignore_https_errors: several nursery sites have expired certificates
            context = await browser.new_context(ignore_https_errors=True, locale="en-US",
                                                viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            try:
                for n, url in enumerate(todo, 1):
                    listings = sites[url]
                    self.stdout.write(f"[{n}/{len(todo)}] {url}  ({listings[0].name[:40]}"
                                      f"{f' +{len(listings) - 1}' if len(listings) > 1 else ''})")
                    results.append(await self.visit(page, url, listings))
                    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            finally:
                await browser.close()

    async def visit(self, page, url, listings):
        record = {"url": url, "listing_ids": [l.pk for l in listings],
                  "names": sorted({l.name for l in listings}), "pages": [], "snippets": [],
                  "emails": [], "error": ""}
        seen = set()
        try:
            text, links, emails = await self.read(page, url)
        except Exception as e:
            record["error"] = str(e).splitlines()[0][:200]
            self.stdout.write(f"  [error] {record['error']}")
            return record
        home = page.url
        record["pages"].append(home)
        record["snippets"] += [{"page": home, "text": s} for s in snippets(text, seen)]
        record["emails"] += [e for e in emails if e not in record["emails"]]

        # Same-site pages whose link looks useful, fees and admissions first
        domain = urlparse(home).netloc.removeprefix("www.")
        ranked = []
        for href, label in links:
            target = clean_url(urljoin(home, href))
            if (urlparse(target).netloc.removeprefix("www.") == domain and target not in ranked
                    and target.rstrip("/") != clean_url(home).rstrip("/")
                    and not re.search(r"\.(pdf|jpe?g|png|docx?)$", target, re.I)
                    and LINK_WORDS.search(f"{href} {label}")):
                ranked.append(target)
        ranked.sort(key=lambda u: 0 if re.search(r"fee|tuition|price|admission|enrol", u, re.I) else 1)
        # No email on those pages: try the contact page
        contact = [] if record["emails"] else [
            clean_url(urljoin(home, href)) for href, label in links
            if CONTACT_WORDS.search(f"{href} {label}")
            and urlparse(clean_url(urljoin(home, href))).netloc.removeprefix("www.") == domain]
        contact = [u for u in contact if u not in ranked[:MAX_EXTRA_PAGES]][:1]
        for target in ranked[:MAX_EXTRA_PAGES] + contact:
            await asyncio.sleep(DELAY_S)
            try:
                text, _, emails = await self.read(page, target)
            except Exception as e:
                self.stdout.write(f"  [page error] {target}: {str(e).splitlines()[0][:100]}")
                continue
            record["pages"].append(target)
            record["snippets"] += [{"page": target, "text": s} for s in snippets(text, seen)]
            record["emails"] += [e for e in emails if e not in record["emails"]]
        record["snippets"] = record["snippets"][:MAX_SNIPPETS]
        self.stdout.write(f"  {len(record['pages'])} pages, {len(record['snippets'])} passages, "
                          f"emails: {', '.join(record['emails']) or 'none'}")
        return record

    async def read(self, page, url):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception as e:
            # Sites that redirect themselves while loading (www, language)
            if "interrupted by another navigation" not in str(e):
                raise
            await page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await asyncio.sleep(2.5)   # let JavaScript-built pages render
        text = await page.inner_text("body")
        links = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.getAttribute('href'), (e.innerText || '').trim()])")
        cf_codes = await page.eval_on_selector_all(
            "[data-cfemail]", "els => els.map(e => e.getAttribute('data-cfemail'))")
        return text, links, find_emails(text, links, cf_codes)
