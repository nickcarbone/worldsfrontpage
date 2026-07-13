"""
World's Front Page — Homepage Scraper
Extracts headline + deckline + visible lede grafs from each source homepage.
Uses requests+BeautifulSoup for static sites, Playwright for JS-heavy ones.

Extraction strategy, in priority order:
  1. Known "lead story" container selectors
  2. First <h1> and its parent container
  3. First <article> tag that contains a heading
  4. First h1/h2 that sits inside a linked <a> tag (card-style homepages
     that don't wrap headlines in <article>/<h1> containers)
  5. First substantial <h2>/<h3> anywhere on the page
  6. Broadest: first substantial linked headline anywhere on the page,
     skipping obvious nav/tag/category/author links
  7. LAST RESORT: og:title / og:description — on a homepage these are
     usually the SITE's title, not the top article's headline, so this
     only fires when nothing else was found, and it's logged as such.

At every tier, candidate headlines are checked against _is_site_name() to
reject generic nav/masthead text ("Latest News", "Breaking News", the
publication's own name, etc.) rather than accepting the first tag found.
"""

import time
import logging
import random
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Sites that require a headless browser (JS-rendered content)
PLAYWRIGHT_SITES = {
    "wsj", "nyt", "ft", "le_monde", "le_figaro", "faz", "sz",
    "nzz", "nrc", "volkskrant", "el_pais", "corriere", "repubblica",
    "dn_sweden", "gazeta", "straits_t", "scmp", "malaysiakini",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT = 20
RATE_LIMIT_DELAY = (1.5, 3.0)  # Random seconds between requests

LEAD_SELECTORS = [
    "article.lead", "article.featured", "article.top-story",
    "[class*='lead-story']", "[class*='top-story']",
    "[class*='featured-story']", "[class*='headline--primary']",
    "[data-testid='lead']", "[data-testid='top-story']",
    ".story--featured", ".article--lead", ".main-story",
]

DECKLINE_SELECTORS = [
    "p.summary", "p.deck", ".standfirst", ".summary",
    ".description", "p.lead", "[class*='summary']",
    "[class*='standfirst']", "[class*='deck']",
]

# Generic nav/masthead phrases that show up where a real headline should be
SITE_NAME_SIGNALS = [
    "latest news", "breaking news", "top headlines", "news from",
    "world news", "national news", "news today", "newspaper",
    "official website", "home page", "homepage", "front page",
    "all the news", "your source for", "stay informed",
]

# Link patterns that indicate navigation, not an article
NAV_HREF_SIGNALS = ["#", "mailto:", "javascript:", "/tag/", "/category/", "/author/"]


@dataclass
class ScrapedStory:
    source_id: str
    country: str
    publication: str
    url: str
    headline: str
    deckline: str = ""
    lede: str = ""
    article_url: str = ""
    language_hint: str = "en"
    scrape_error: Optional[str] = None


def _is_site_name(text: str, publication_name: str) -> bool:
    """Reject candidate headlines that are actually masthead/nav text."""
    if not text:
        return True
    t = text.lower().strip()
    if len(t) < 15:
        return True
    if publication_name and publication_name.lower().split()[0] in t[:50]:
        return True
    return any(signal in t for signal in SITE_NAME_SIGNALS)


def scrape_all(sources: list[dict], use_playwright: bool = True) -> list[ScrapedStory]:
    """Scrape all sources, return list of ScrapedStory objects."""
    results = []
    for source in sources:
        logger.info(f"Scraping {source['name']} ({source['country']})")
        try:
            if use_playwright and source["id"] in PLAYWRIGHT_SITES:
                story = _scrape_playwright(source)
            else:
                story = _scrape_requests(source)
            logger.info(f"  → '{story.headline[:70]}'" if story.headline else "  → (no headline)")
        except Exception as e:
            logger.warning(f"Failed to scrape {source['name']}: {e}")
            story = ScrapedStory(
                source_id=source["id"],
                country=source["country"],
                publication=source["name"],
                url=source["url"],
                headline="",
                scrape_error=str(e),
            )
        results.append(story)
        time.sleep(random.uniform(*RATE_LIMIT_DELAY))
    return results


def _scrape_requests(source: dict) -> ScrapedStory:
    """Static site scraping with requests + BeautifulSoup."""
    resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_story(soup, source)


def _scrape_playwright(source: dict) -> ScrapedStory:
    """JS-rendered site scraping with Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed, falling back to requests")
        return _scrape_requests(source)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers=HEADERS)
        page.goto(source["url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    return _extract_story(soup, source)


def _extract_story(soup: BeautifulSoup, source: dict) -> ScrapedStory:
    """Extract the top story from a parsed homepage. See module docstring
    for the priority order — OG meta tags are a last resort, not the first
    check, because on a homepage they describe the SITE, not the top story."""
    headline, deckline, lede, article_url = "", "", "", ""
    lead_container = None
    base_url = source["url"]
    pub_name = source["name"]

    # ── 1. Known lead-story container selectors ───────────────────────────
    for sel in LEAD_SELECTORS:
        candidate = soup.select_one(sel)
        if candidate:
            h = ""
            for tag in ["h1", "h2", "h3"]:
                el = candidate.find(tag)
                if el:
                    h = el.get_text(strip=True)
                    break
            if h and not _is_site_name(h, pub_name):
                lead_container = candidate
                headline = h
                break

    # ── 2. First <h1> and its parent container ─────────────────────────────
    if not lead_container:
        for h1 in soup.find_all("h1"):
            text = h1.get_text(strip=True)
            if text and not _is_site_name(text, pub_name):
                headline = text
                lead_container = h1.find_parent(["article", "div", "section"])
                break

    # ── 3. First <article> tag that contains a heading ─────────────────────
    if not lead_container and not headline:
        for art in soup.find_all("article"):
            heading = art.find(["h1", "h2", "h3"])
            if heading:
                text = heading.get_text(strip=True)
                if len(text) > 15 and not _is_site_name(text, pub_name):
                    lead_container = art
                    headline = text
                    break

    # ── 4. First h1/h2 sitting inside a linked <a> tag ──────────────────────
    if not headline:
        for tag in ["h1", "h2"]:
            for el in soup.find_all(tag)[:15]:
                text = el.get_text(strip=True)
                if not text or len(text) < 20 or _is_site_name(text, pub_name):
                    continue
                a = el.find("a") or el.find_parent("a")
                if a and a.get("href"):
                    headline = text
                    href = a["href"]
                    article_url = href if href.startswith("http") else urljoin(base_url, href)
                    lead_container = el.find_parent(["article", "div", "section", "li"])
                    break
            if headline:
                break

    # ── 5. Broadest: first substantial h2/h3 anywhere on the page ──────────
    if not lead_container and not headline:
        for tag in soup.find_all(["h2", "h3"]):
            text = tag.get_text(strip=True)
            if len(text) > 20 and not _is_site_name(text, pub_name):
                headline = text
                lead_container = tag.find_parent(["article", "div", "section"])
                break

    # ── 6. First substantial linked headline anywhere on the page ──────────
    if not headline:
        for a in soup.find_all("a", href=True)[:50]:
            href = a.get("href", "")
            if any(x in href for x in NAV_HREF_SIGNALS):
                continue
            text = a.get_text(strip=True)
            if len(text) > 30 and not _is_site_name(text, pub_name):
                headline = text
                article_url = href if href.startswith("http") else urljoin(base_url, href)
                break

    # ── Pull deckline / lede / article_url from whatever container we found ─
    if lead_container:
        for sel in DECKLINE_SELECTORS:
            el = lead_container.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if len(text) > 30:
                    deckline = text
                    break

        if not deckline:
            paras = lead_container.find_all("p")
            visible = [p.get_text(strip=True) for p in paras
                       if len(p.get_text(strip=True)) > 60]
            if visible:
                deckline = visible[0]
                if len(visible) > 1:
                    lede = visible[1]

        if not article_url:
            a = lead_container.find("a", href=True)
            if a:
                href = a["href"]
                article_url = href if href.startswith("http") else urljoin(base_url, href)

    # ── 7. LAST RESORT: og:title / og:description ───────────────────────────
    if not headline:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            headline = og_title.get("content", "").strip()
            logger.warning(
                f"{source['name']}: no article headline found by structural "
                f"selectors — falling back to og:title (likely the site name, "
                f"not the top article)"
            )
    if not deckline:
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            deckline = og_desc.get("content", "").strip()
    if not article_url:
        og_url = soup.find("meta", property="og:url")
        if og_url:
            article_url = og_url.get("content", "").strip()

    # ── Detect likely non-English content ───────────────────────────────────
    lang_el = soup.find("html")
    lang = lang_el.get("lang", "en") if lang_el else "en"
    language_hint = lang[:2].lower() if lang else "en"

    return ScrapedStory(
        source_id=source["id"],
        country=source["country"],
        publication=source["name"],
        url=source["url"],
        headline=headline[:500] if headline else "",
        deckline=deckline[:800] if deckline else "",
        lede=lede[:800] if lede else "",
        article_url=article_url,
        language_hint=language_hint,
    )


def scrape_baselines(baseline_sources: list[dict]) -> list[ScrapedStory]:
    """
    Scrape the baseline sources (NYT, WSJ, WaPo, FT, Guardian).
    Used to calibrate global news saturation — not included in output.
    Returns top headlines per source for LLM comparison.
    """
    results = []
    for source in baseline_sources:
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            headlines = []
            for tag in soup.find_all(["h1", "h2", "h3"])[:25]:
                text = tag.get_text(strip=True)
                if len(text) > 20 and not _is_site_name(text, source["name"]):
                    headlines.append(text)
            results.append(ScrapedStory(
                source_id=source["id"],
                country=source["country"],
                publication=source["name"],
                url=source["url"],
                headline=" | ".join(headlines[:10]),
            ))
            logger.info(f"Baseline {source['name']}: {len(headlines)} headlines")
        except Exception as e:
            logger.warning(f"Baseline scrape failed for {source['name']}: {e}")
        time.sleep(random.uniform(1.0, 2.0))
    return results
