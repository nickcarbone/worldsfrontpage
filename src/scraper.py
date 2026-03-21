"""
World's Front Page — Homepage Scraper
Extracts headline + deckline + visible lede grafs from each source homepage.
Uses requests+BeautifulSoup for static sites, Playwright for JS-heavy ones.
"""

import time
import logging
import random
from dataclasses import dataclass, field
from typing import Optional
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

REQUEST_TIMEOUT = 15
RATE_LIMIT_DELAY = (1.5, 3.5)  # Random seconds between requests


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
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    return _extract_story(soup, source)


def _extract_story(soup: BeautifulSoup, source: dict) -> ScrapedStory:
    """
    Extract the top story from a parsed homepage.
    Strategy: find the most prominent headline on the page.
    Prominence is determined by: h1 > h2 with largest font/first position >
    article with 'lead'/'featured'/'top' class > first article link.
    """
    headline, deckline, lede, article_url = "", "", "", ""

    # ── 1. Try Open Graph meta tags first (most reliable for top story) ──────
    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    og_url   = soup.find("meta", property="og:url")

    if og_title:
        headline = og_title.get("content", "").strip()
    if og_desc:
        deckline = og_desc.get("content", "").strip()
    if og_url:
        article_url = og_url.get("content", "").strip()

    # ── 2. Look for prominent lead article containers ─────────────────────────
    LEAD_SELECTORS = [
        "article.lead", "article.featured", "article.top-story",
        "[class*='lead-story']", "[class*='top-story']",
        "[class*='featured-story']", "[class*='headline--primary']",
        "[data-testid='lead']", "[data-testid='top-story']",
        ".story--featured", ".article--lead", ".main-story",
    ]
    lead_container = None
    for sel in LEAD_SELECTORS:
        lead_container = soup.select_one(sel)
        if lead_container:
            break

    # ── 3. If no lead container, fall back to first h1 or prominent h2 ───────
    if not lead_container:
        h1 = soup.find("h1")
        if h1:
            headline = headline or h1.get_text(strip=True)
            parent = h1.find_parent(["article", "div", "section"])
            lead_container = parent

    # ── 4. Extract from lead container if found ───────────────────────────────
    if lead_container:
        # Headline
        if not headline:
            for tag in ["h1", "h2", "h3"]:
                el = lead_container.find(tag)
                if el:
                    headline = el.get_text(strip=True)
                    break

        # Deckline / summary
        if not deckline:
            for sel in ["p.summary", "p.deck", ".standfirst", ".summary",
                        ".description", "p.lead", "[class*='summary']",
                        "[class*='standfirst']"]:
                el = lead_container.select_one(sel)
                if el:
                    deckline = el.get_text(strip=True)
                    break

        # Lede paragraph (first visible <p> after headline)
        if not deckline:
            paras = lead_container.find_all("p")
            visible = [p.get_text(strip=True) for p in paras
                       if len(p.get_text(strip=True)) > 60]
            if visible:
                deckline = visible[0]
                if len(visible) > 1:
                    lede = visible[1]

        # Article URL
        if not article_url:
            a = lead_container.find("a", href=True)
            if a:
                href = a["href"]
                if href.startswith("http"):
                    article_url = href
                else:
                    from urllib.parse import urljoin
                    article_url = urljoin(source["url"], href)

    # ── 5. Detect likely non-English content ─────────────────────────────────
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
    Returns top 5 headlines per source for LLM comparison.
    """
    results = []
    for source in baseline_sources:
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.text, "html.parser")
            # Grab multiple headlines for a richer baseline
            headlines = []
            for tag in soup.find_all(["h1", "h2", "h3"])[:20]:
                text = tag.get_text(strip=True)
                if len(text) > 20:
                    headlines.append(text)
            # Pack them as a single story object for simplicity
            results.append(ScrapedStory(
                source_id=source["id"],
                country=source["country"],
                publication=source["name"],
                url=source["url"],
                headline=" | ".join(headlines[:8]),
            ))
        except Exception as e:
            logger.warning(f"Baseline scrape failed for {source['name']}: {e}")
        time.sleep(random.uniform(1.0, 2.0))
    return results
