"""
World's Front Page — Homepage Scraper
Extracts the top story headline + summary from each source homepage.
Strategy: find the first real article link, not the site's OG meta tags.
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

# Sites that require a headless browser
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
RATE_LIMIT_DELAY = (1.5, 3.0)

SITE_NAME_SIGNALS = [
    "latest news", "breaking news", "top headlines", "news from",
    "world news", "national news", "news today", "newspaper",
    "official website", "home page", "homepage", "front page",
    "all the news", "your source for", "stay informed",
]


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
            logger.warning(f"  Failed: {e}")
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
    resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_story(soup, source)


def _scrape_playwright(source: dict) -> ScrapedStory:
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


def _is_site_name(text: str, publication_name: str) -> bool:
    if not text:
        return True
    t = text.lower().strip()
    if len(t) < 15:
        return True
    if publication_name.lower().split()[0] in t[:50].lower():
        return True
    for signal in SITE_NAME_SIGNALS:
        if signal in t:
            return True
    return False


def _extract_story(soup: BeautifulSoup, source: dict) -> ScrapedStory:
    base_url = source["url"]
    pub_name = source["name"]

    lang_el = soup.find("html")
    lang = lang_el.get("lang", "en") if lang_el else "en"
    language_hint = lang[:2].lower() if lang else "en"

    headline, deckline, lede, article_url = "", "", "", ""

    # Strategy 1: Named lead/featured article containers
    LEAD_SELECTORS = [
        "article.lead", "article.featured", "article.top-story",
        "[class*='lead-story']", "[class*='top-story']", "[class*='featured-story']",
        "[class*='headline--primary']", "[class*='main-story']",
        "[data-testid='lead']", "[data-testid='top-story']",
        ".story--featured", ".article--lead",
    ]
    for sel in LEAD_SELECTORS:
        container = soup.select_one(sel)
        if container:
            h = _extract_headline_from_container(container)
            if h and not _is_site_name(h, pub_name):
                headline = h
                deckline = _extract_deckline_from_container(container)
                article_url = _extract_url_from_container(container, base_url)
                break

    # Strategy 2: First article tag with a substantial headline
    if not headline:
        for article in soup.find_all("article")[:10]:
            h = _extract_headline_from_container(article)
            if h and not _is_site_name(h, pub_name) and len(h) > 20:
                headline = h
                deckline = _extract_deckline_from_container(article)
                article_url = _extract_url_from_container(article, base_url)
                break

    # Strategy 3: First h1/h2 inside an anchor
    if not headline:
        for tag in ["h1", "h2"]:
            for el in soup.find_all(tag)[:15]:
                text = el.get_text(strip=True)
                if not text or _is_site_name(text, pub_name) or len(text) < 20:
                    continue
                a = el.find("a") or el.find_parent("a")
                if a and a.get("href"):
                    headline = text
                    href = a["href"]
                    article_url = href if href.startswith("http") else urljoin(base_url, href)
                    parent = el.find_parent(["article", "div", "section", "li"])
                    if parent:
                        deckline = _extract_deckline_from_container(parent)
                    break
            if headline:
                break

    # Strategy 4: First substantial linked headline anywhere on page
    if not headline:
        for a in soup.find_all("a", href=True)[:50]:
            href = a.get("href", "")
            if any(x in href for x in ["#", "mailto:", "javascript:", "/tag/", "/category/", "/author/"]):
                continue
            text = a.get_text(strip=True)
            if len(text) > 30 and not _is_site_name(text, pub_name):
                headline = text
                article_url = href if href.startswith("http") else urljoin(base_url, href)
                break

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


def _extract_headline_from_container(container) -> str:
    for tag in ["h1", "h2", "h3"]:
        el = container.find(tag)
        if el:
            return el.get_text(strip=True)
    return ""


def _extract_deckline_from_container(container) -> str:
    for sel in ["p.summary", "p.deck", ".standfirst", ".summary",
                ".description", "p.lead", "[class*='summary']",
                "[class*='standfirst']", "[class*='deck']"]:
        el = container.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if len(text) > 30:
                return text
    for p in container.find_all("p"):
        text = p.get_text(strip=True)
        if len(text) > 60:
            return text
    return ""


def _extract_url_from_container(container, base_url: str) -> str:
    a = container.find("a", href=True)
    if a:
        href = a["href"]
        return href if href.startswith("http") else urljoin(base_url, href)
    return ""


def scrape_baselines(baseline_sources: list[dict]) -> list[ScrapedStory]:
    results = []
    for source in baseline_sources:
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.text, "html.parser")
            headlines = []
            for tag in soup.find_all(["h1", "h2", "h3"])[:25]:
                text = tag.get_text(strip=True)
                if len(text) > 25 and not _is_site_name(text, source["name"]):
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
