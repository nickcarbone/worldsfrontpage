"""
World's Front Page — Homepage Scraper
Extracts candidate front-page stories from each source homepage.

Architecture (v2):
  - MULTI-CANDIDATE: each source now yields up to 5 candidate headlines in
    page order, not just one. The curation LLM decides which candidate is
    the real lead — turning fragile CSS heuristics into a soft signal
    instead of a single point of failure.
  - PARALLEL: requests-based sources are scraped concurrently
    (ThreadPoolExecutor). Playwright sources share ONE browser instance
    instead of launching a fresh browser per site.
  - ESCALATION LADDER: rotating user-agent pool → retry with backoff →
    403/429/503 responses escalate to Playwright (a real browser
    fingerprint clears most bot walls).
  - CJK-AWARE LENGTH FILTERS: Japanese/Chinese/Korean headlines carry ~2x
    information per character, so CJK characters count double toward
    length thresholds. Latin-script thresholds were silently rejecting
    substantive Yomiuri/Chosun Ilbo headlines.
  - ARTICLE FETCHER: fetch_article_text() pulls the opening paragraphs of
    a selected story so briefs are grounded in real source text, not just
    a headline.

Primary-extraction strategy (candidate 0), in priority order:
  1. Known "lead story" container selectors
  2. First <h1> and its parent container
  3. First <article> tag that contains a heading
  4. First h1/h2 that sits inside a linked <a> tag
  5. First substantial <h2>/<h3> anywhere on the page
  6. Broadest: first substantial linked headline anywhere on the page
  7. LAST RESORT: og:title / og:description (usually the SITE name on a
     homepage — logged as such)

At every tier, candidates are checked against _is_site_name() to reject
generic nav/masthead text.
"""

import re
import time
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    # NOTE (2026-07-28): "reuters_baseline" / "bloomberg_baseline" were
    # removed from this set. They had been pre-declared here, but the
    # matching entries were never added to sources.py, so the IDs referred
    # to nothing. Baseline calibration is now feed-first (see
    # scrape_baselines) and does not route through this set at all.
}

# Rotating pool of realistic browser fingerprints (first rung of the
# anti-bot escalation ladder — a single static UA is an easy block).
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def _headers() -> dict:
    """Fresh headers per request with a randomly rotated user-agent."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }


REQUEST_TIMEOUT = 20
MAX_WORKERS = 8            # concurrent requests-based scrapes
MAX_RETRIES = 2            # attempts before giving up (non-bot-wall errors)
# Candidate headlines collected per source.
#
# Raised 5 -> 18 (2026-07-27). The 2026-07-27 run matched only 20 of 55
# frontpage-configured sources, and ~20 of the 35 drops were the vision
# call correctly reporting that NONE of the scraped candidates described
# any story on the print front page (Straits Times, Haaretz, SCMP,
# Kathimerini, Inquirer, Punch, Liberty Times, Hurriyet, ThisDay...). That
# is not a matching failure, it's a coverage failure: a homepage and a
# print front page diverge constantly, and five candidates is far too
# narrow a net to reliably contain whatever the print editors led with.
# The cost of a wider net is a longer vision prompt, not a worse decision --
# the vision call is choosing among candidates, so extra wrong ones are
# inert, while a missing right one drops the whole country for the day.
MAX_CANDIDATES = 18
BLOCK_STATUS_CODES = {403, 429, 503}  # bot-wall signals → escalate to Playwright

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

# Promotional/boilerplate phrases — newsletter signups, app-download
# prompts, social-follow CTAs. These are real homepage content, but never
# the actual lead story, so they get the same treatment as site-name
# rejection: reject this candidate, let extraction try the next tier.
PROMO_SIGNALS = [
    "newsletter", "subscribe", "sign up", "sign-up", "daily digest",
    "morning briefing", "download our app", "download the app",
    "get the app", "app store", "google play", "follow us on",
    "delivers the latest", "delivered to your inbox",
    # Non-English promo boilerplate seen in the 2026-07-27 run
    "recibe nuestro", "reciba nuestro", "suscríbete", "suscribete",
    "abonnez-vous", "abonnieren", "assine", "boletín", "boletin",
    "su opinión importa", "su opinion importa", "encontrá a tu",
    "encontra a tu", "let us curate", "contact us today",
]

# Interstitials, bot walls, consent notices, legal furniture and bare
# domain strings. These are not "a headline the heuristic guessed wrong" --
# they are pages or fragments with no news content at all, and they poison
# everything downstream: the front-page vision call cannot match a real
# print lead against "JavaScript is disabled", so the source drops, and
# any that survive burn a sufficiency-screen slot for nothing.
#
# The 2026-07-27 run put at least a dozen of these into the candidate
# pool -- El País (Uruguay), La Prensa Gráfica, Público, Times of Malta,
# Trend News, Arab News, Sudan Tribune, Solomon Star ("Performing security
# verification"), The Australian ("You might have been detected and
# blocked as a crawler bot!"), SCMP ("Opt out of the sale or sharing of
# personal information"), Der Standard (a copyright line), El Nacional
# ("paywall · ads · cookies"), plus four bare domains reaching the
# candidate list via the unguarded og:title fallback.
JUNK_SIGNALS = [
    # Bot walls / JS interstitials
    "javascript is disabled", "enable javascript", "javascript required",
    "performing security verification", "security verification",
    "checking your browser", "verify you are human", "are you a robot",
    "detected and blocked", "crawler bot", "access denied",
    "unusual traffic", "please wait while", "redirecting",
    "transferring to the website", "one moment", "403 forbidden",
    # Consent / legal / account furniture
    "cookie", "consent", "privacy policy", "terms of service",
    "terms of use", "all rights reserved", "opt out of the sale",
    "personal information", "verlagsgesellschaft",
    "log in to continue", "create an account", "already a subscriber",
    "paywall · ads", "paywall - ads",
    # Nav / section furniture that reads like a headline
    "advertise with us", "about us", "contact us", "help centre",
    "help center", "most read", "más leídas", "mais lidas",
]

# Bare-domain junk: "www.standaard.be", "www.heraldonline.co.zw",
# "www.monitor.co.ug", "www.irrawaddy.com" all reached the candidate list
# on 2026-07-27 via og:title. A "headline" that is only a hostname is
# never a story.
_BARE_DOMAIN_RE = re.compile(r"^\s*(https?://)?(www\.)?[a-z0-9\-]+(\.[a-z]{2,}){1,3}/?\s*$", re.I)

# All-caps section/label prefixes glued to the front of a real headline by
# sloppy markup -- e.g. Daily Nation's "PREMIUMAdongo dared to dream, made
# Kenya proud, met harsh end" or Dagens Nyheter's "Just nu:Knivdåd i
# Paris". The headline underneath is genuine, so these are STRIPPED rather
# than rejected; rejecting them would throw away real leads.
_LABEL_PREFIX_RE = re.compile(
    r"^(PREMIUM|EXCLUSIVE|BREAKING|UPDATE[D]?|LIVE|VIDEO|PHOTOS?|GALLERY|OPINION|"
    r"ANALYSIS|EDITORIAL|COMMENT|INTERVIEW|EXKLUSIV|EILMELDUNG|DIRECTO|EN VIVO|"
    r"ÚLTIMA HORA|ULTIMA HORA|EXCLUSIVO|ANÁLISE|ANALISE|DIRECT|ÉDITO|EDITO|"
    r"Just nu|Nyhet|Siste nytt|Seneste nyt)\s*[:\-–—]?\s*"
)


def _strip_label_prefix(text: str) -> str:
    """Remove a glued section/label prefix, leaving the real headline.
    Applied repeatedly since some sites stack two ('PREMIUMLIVE...')."""
    prev = None
    out = (text or "").strip()
    while out != prev:
        prev = out
        out = _LABEL_PREFIX_RE.sub("", out).strip()
    return out


def _decode(resp) -> str:
    """
    Return correctly-decoded response text.

    requests falls back to ISO-8859-1 for any text/* response whose HTTP
    Content-Type header omits a charset, regardless of what the document
    itself declares in a <meta> tag. Most non-Anglophone outlets in this
    source list declare UTF-8 only in markup, so the 2026-07-27 run carried
    mojibake through the entire pipeline: Folha's "tarifaÃ§o"/"FlÃ¡vio",
    Asahi's "ããã¼ã«ãã", Adevărul's "cumpÄrÄturi". Translation
    happened to repair the Brazilian story before publication, but the
    corrupted text was what the sufficiency screen, the localization
    scorer, the syndication clustering and the front-page vision matcher
    all actually saw.
    """
    ctype = resp.headers.get("Content-Type", "") or ""
    if "charset=" not in ctype.lower():
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

# Wire-service attribution markers. World's Front Page exists to surface
# what a country's OWN newsroom chose to invest reporting resources in — a
# story that's really AP/Reuters/AFP/Bloomberg copy republished under a
# local masthead defeats that premise even though it appears on a genuine
# front page. Checked twice downstream in curator.py: once cheaply against
# headline/deckline teaser text pre-selection, and again post-selection
# against the fetched article body, since dateline attribution often only
# shows up in full article text, not the homepage teaser.
# Known limitation: this is a Latin-script regex, so it will under-detect
# wire content on non-Latin-script front pages where the agency name is
# transliterated rather than kept in Roman characters (curator.py runs a
# second pass after translation to partially cover that gap).
WIRE_SERVICE_PATTERNS = [
    r"\(reuters\)", r"\breuters\b",
    r"\(ap\)", r"\bassociated press\b",
    r"\(afp\)", r"\bagence france-presse\b", r"\bafp\b",
    r"\(bloomberg\)", r"\bbloomberg\b",
    r"\bdpa\b", r"\bdeutsche presse-agentur\b",
    r"\befe\b",
]
_WIRE_RE = re.compile("|".join(WIRE_SERVICE_PATTERNS), re.IGNORECASE)


def detect_wire_service(*texts: str) -> bool:
    """True if any wire-service attribution marker appears in the given
    text(s). Known false-positive risk: a story genuinely ABOUT one of
    these agencies as a subject (not sourced from it) would also match —
    rare enough in front-page news to accept the tradeoff."""
    combined = " ".join(t for t in texts if t)
    return bool(_WIRE_RE.search(combined))

# Link patterns that indicate navigation, not an article
NAV_HREF_SIGNALS = ["#", "mailto:", "javascript:", "/tag/", "/category/", "/author/"]

# CJK ranges: kana, CJK ext-A, CJK unified, hangul
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _effective_len(text: str) -> int:
    """
    Length for threshold checks, counting CJK characters double.
    A 12-character Japanese headline carries roughly as much information as
    a 24-character English one — raw len() systematically rejected exactly
    the domestic-language papers we deliberately chose over English editions.
    """
    return len(text) + len(_CJK_RE.findall(text))


@dataclass
class Candidate:
    """One candidate front-page headline from a source homepage."""
    headline: str
    deckline: str = ""
    article_url: str = ""


@dataclass
class ScrapedStory:
    source_id: str
    country: str
    publication: str
    url: str
    headline: str                 # primary candidate (scraper's best guess at the lead)
    deckline: str = ""
    lede: str = ""
    article_url: str = ""
    language_hint: str = "en"
    wire_service: bool = False    # best-effort flag set at scrape time; curator.py
                                   # re-checks this at multiple later stages since
                                   # translation can surface attribution this
                                   # regex couldn't see in the original script
    localization_score: int = 3   # 1-5, set by curator._screen_stories(); default
                                   # is a neutral middle value used only if
                                   # screening fails and stories pass through unscored
    scrape_error: Optional[str] = None
    candidates: list = field(default_factory=list)  # list[Candidate], primary first


# Leading articles and other non-distinctive first words in masthead names.
# These must never be used on their own to identify masthead text.
_MASTHEAD_STOPWORDS = {
    "the", "le", "la", "les", "el", "los", "las", "o", "a", "os", "as",
    "il", "lo", "de", "der", "die", "das", "het", "al", "an", "l", "un",
    "una", "new", "daily", "news", "times", "post", "star", "sun", "mail",
    "herald", "tribune", "journal", "press", "observer", "national",
}


def _masthead_tokens(publication_name: str) -> list[str]:
    """Distinctive tokens from a publication's name, dropping leading
    articles and generic newspaper words."""
    if not publication_name:
        return []
    words = re.findall(r"[^\W\d_]+", publication_name.lower(), re.UNICODE)
    return [w for w in words if w not in _MASTHEAD_STOPWORDS and len(w) > 2]


def _matches_publication_name(t: str, publication_name: str) -> bool:
    """
    True if `t` looks like the outlet's own masthead rather than a headline.

    Replaces a first-word substring test that was catastrophically broad:
    it took publication_name.lower().split()[0] and asked whether that
    appeared anywhere in the first 50 characters. For every outlet whose
    name begins with an article -- The Punch, The Globe and Mail, Le Monde,
    La Nación, El Universal, O Globo, Il Fatto, De Standaard, Der Standard,
    Al-Ahram, and dozens more across this source list -- that reduced to
    asking whether the headline contained "the"/"le"/"la"/"el"/"o"/"al"
    early on. Which most of their headlines do, in their own language.

    Verified against real headlines from the 2026-07-27 run: O Globo's
    "Bolsonaro deve depor na próxima semana", The Punch's NSCDC petrol
    story, Le Monde's "Emmanuel Macron convoque le conseil de défense" and
    La Nación's "El Gobierno no hará un pedido de disculpas a Lula" were
    ALL being rejected as masthead text. This silently suppressed real
    leads at every extraction tier and in the alternate sweep, and is very
    likely a larger contributor to thin candidate lists than any of the
    junk-text problems this batch was written to fix.

    The replacement requires either the full masthead as a phrase, or a
    distinctive (non-article, non-generic) token from it.
    """
    if not publication_name:
        return False
    head = t[:80]
    if publication_name.lower().strip() in head:
        return True
    return any(tok in head for tok in _masthead_tokens(publication_name))


def _is_site_name(text: str, publication_name: str) -> bool:
    """Reject candidate headlines that are actually masthead/nav text, or
    promotional boilerplate (newsletter signups, app-download prompts,
    etc.) — neither is ever the actual lead story, and both get the same
    treatment: reject this candidate, let extraction try the next tier."""
    if not text:
        return True
    t = text.lower().strip()
    if _BARE_DOMAIN_RE.match(t):
        return True
    if t.startswith("©") or t.startswith("(c)"):
        return True
    if _effective_len(t) < 15:
        return True
    if _matches_publication_name(t, publication_name):
        return True
    if any(signal in t for signal in SITE_NAME_SIGNALS):
        return True
    if any(signal in t for signal in JUNK_SIGNALS):
        return True
    return any(signal in t for signal in PROMO_SIGNALS)


def _error_story(source: dict, error: str) -> ScrapedStory:
    return ScrapedStory(
        source_id=source["id"],
        country=source["country"],
        publication=source["name"],
        url=source["url"],
        headline="",
        scrape_error=error,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def scrape_all(sources: list[dict], use_playwright: bool = True) -> list[ScrapedStory]:
    """
    Scrape all sources, return list of ScrapedStory objects in source order.
    Requests-based sources run in parallel; Playwright sources (declared +
    escalated bot-wall 403s) run sequentially through one shared browser.
    """
    results: dict[str, ScrapedStory] = {}
    escalated: list[dict] = []

    pw_declared = [s for s in sources if use_playwright and s["id"] in PLAYWRIGHT_SITES]
    req_sources = [s for s in sources if s not in pw_declared]

    logger.info(
        f"Scraping {len(req_sources)} sources via requests "
        f"({MAX_WORKERS} workers), {len(pw_declared)} declared Playwright sites"
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_scrape_requests_safe, s): s for s in req_sources}
        for fut in as_completed(futures):
            source = futures[fut]
            story, needs_browser = fut.result()
            if needs_browser and use_playwright:
                escalated.append(source)
            else:
                results[source["id"]] = story

    if escalated:
        logger.info(f"Escalating {len(escalated)} bot-walled sources to Playwright: "
                    f"{[s['id'] for s in escalated]}")

    pw_batch = pw_declared + escalated
    if pw_batch:
        results.update(_scrape_playwright_batch(pw_batch))

    # Any escalated source we couldn't reach playwright for (use_playwright=False)
    for s in escalated:
        if s["id"] not in results:
            results[s["id"]] = _error_story(s, "HTTP 403 (blocked, no browser fallback)")

    ordered = [results[s["id"]] for s in sources if s["id"] in results]
    cand_counts = []
    for story in ordered:
        if story.headline:
            logger.info(f"  {story.publication}: '{story.headline[:70]}' "
                        f"(+{max(0, len(story.candidates) - 1)} alt candidates)")
        elif story.candidates:
            # No primary survived the site-name/junk guards, but the
            # alternate sweep found real stories -- worth logging distinctly
            # from a hard scrape failure, since the front-page vision call
            # can still match against these.
            logger.info(f"  {story.publication}: no primary headline, "
                        f"{len(story.candidates)} alternate candidate(s)")
        if not story.scrape_error:
            cand_counts.append(len(story.candidates))
    if cand_counts:
        cand_counts.sort()
        logger.info(
            f"Candidate depth across {len(cand_counts)} scraped sources: "
            f"median {cand_counts[len(cand_counts)//2]}, "
            f"min {cand_counts[0]}, max {cand_counts[-1]}, "
            f"{sum(1 for c in cand_counts if c == 0)} with none"
        )
    return ordered


def _scrape_requests_safe(source: dict) -> tuple[ScrapedStory, bool]:
    """
    Requests-based scrape with jitter, retry/backoff, and bot-wall detection.
    Returns (story, needs_playwright_escalation).
    """
    last_err = "unknown error"
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(random.uniform(0.2, 1.2))  # jitter — avoid thundering herd
            resp = requests.get(source["url"], headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code in BLOCK_STATUS_CODES:
                # Bot wall. Retrying with another UA rarely helps; a real
                # browser fingerprint usually does — escalate.
                return _error_story(source, f"HTTP {resp.status_code}"), True
            resp.raise_for_status()
            soup = BeautifulSoup(_decode(resp), "html.parser")
            return _extract_story(soup, source), False
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))
    logger.warning(f"Failed to scrape {source['name']}: {last_err}")
    return _error_story(source, last_err), False


def _fetch_html_playwright(sources: list[dict]) -> dict[str, str]:
    """
    Fetch rendered HTML for a batch of sources through ONE shared Chromium
    instance (launching a fresh browser per site was the single biggest
    time cost in the old pipeline).
    Returns {source_id: html} for successful fetches only.
    """
    out: dict[str, str] = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed — browser batch skipped")
        return out

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        for source in sources:
            try:
                page.goto(source["url"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                out[source["id"]] = page.content()
            except Exception as e:
                logger.warning(f"Playwright failed for {source['name']}: {e}")
                # Page may be wedged — replace it and continue the batch
                try:
                    page.close()
                except Exception:
                    pass
                page = context.new_page()
        browser.close()
    return out


def _scrape_playwright_batch(sources: list[dict]) -> dict[str, ScrapedStory]:
    html_by_id = _fetch_html_playwright(sources)
    results = {}
    for source in sources:
        html = html_by_id.get(source["id"])
        if html:
            soup = BeautifulSoup(html, "html.parser")
            results[source["id"]] = _extract_story(soup, source)
        else:
            results[source["id"]] = _error_story(source, "playwright fetch failed")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_story(soup: BeautifulSoup, source: dict) -> ScrapedStory:
    """Extract the primary story plus up to MAX_CANDIDATES-1 alternate
    candidates from a parsed homepage. See module docstring for the tiered
    priority order — OG meta tags are a last resort, not the first check,
    because on a homepage they describe the SITE, not the top story."""
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
                if _effective_len(text) > 15 and not _is_site_name(text, pub_name):
                    lead_container = art
                    headline = text
                    break

    # ── 4. First h1/h2 sitting inside a linked <a> tag ──────────────────────
    if not headline:
        for tag in ["h1", "h2"]:
            for el in soup.find_all(tag)[:15]:
                text = el.get_text(strip=True)
                if not text or _effective_len(text) < 20 or _is_site_name(text, pub_name):
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
            if _effective_len(text) > 20 and not _is_site_name(text, pub_name):
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
            if _effective_len(text) > 30 and not _is_site_name(text, pub_name):
                headline = text
                article_url = href if href.startswith("http") else urljoin(base_url, href)
                break

    # ── Pull deckline / lede / article_url from whatever container we found ─
    if lead_container:
        for sel in DECKLINE_SELECTORS:
            el = lead_container.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if _effective_len(text) > 30:
                    deckline = text
                    break

        if not deckline:
            paras = lead_container.find_all("p")
            visible = [p.get_text(strip=True) for p in paras
                       if _effective_len(p.get_text(strip=True)) > 60]
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
    #
    # 2026-07-27: this tier was previously UNGUARDED -- whatever og:title
    # contained became the primary candidate with no _is_site_name() check,
    # which is how "O GLOBO | Confira as principais notícias", "Le Monde -
    # World news, culture and opinion", "The New Times", "www.standaard.be"
    # and "www.heraldonline.co.zw" all reached the front-page vision call as
    # this outlet's supposed lead story. Every one of those sources then
    # dropped, because no print front page can be matched against a
    # masthead. An empty headline is strictly better than a fake one: the
    # alternate-candidate sweep below still runs, so the source keeps a
    # real chance instead of being poisoned by its own site name.
    if not headline:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            og_text = _strip_label_prefix(og_title.get("content", "").strip())
            if og_text and not _is_site_name(og_text, pub_name):
                headline = og_text
                logger.warning(
                    f"{source['name']}: no article headline found by structural "
                    f"selectors — falling back to og:title"
                )
            else:
                logger.info(
                    f"{source['name']}: og:title rejected as site name/boilerplate "
                    f"('{og_text[:60]}') — relying on alternate candidates"
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

    # ── Build candidate list: primary first, then alternates in page order ──
    headline = _strip_label_prefix(headline) if headline else ""

    candidates = []
    if headline:
        candidates.append(Candidate(
            headline=headline[:500],
            deckline=deckline[:800] if deckline else "",
            article_url=article_url,
        ))
    candidates.extend(
        _collect_alt_candidates(soup, source, exclude=headline,
                                limit=MAX_CANDIDATES - len(candidates))
    )

    # If every tier's primary was rejected (usually: og:title was the site
    # name and structural selectors found nothing), fall back to the first
    # alternate. It has already passed the same junk/site-name guards, and
    # document order makes it the best available guess at the lead. Keeps
    # main.py's "successful scrapes" count meaningful now that the og:title
    # tier can legitimately return nothing.
    if not headline and candidates:
        headline = candidates[0].headline
        deckline = deckline or candidates[0].deckline
        article_url = article_url or candidates[0].article_url

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
        wire_service=detect_wire_service(headline, deckline, lede),
        candidates=candidates,
    )


def _collect_alt_candidates(soup: BeautifulSoup, source: dict,
                            exclude: str = "", limit: int = 17) -> list[Candidate]:
    """
    Collect additional candidate headlines in document order. The second or
    third front-page story is often the more underreported one — and when
    the primary heuristic grabs the wrong element, these give the curator a
    recovery path instead of a dead slot for that country.

    Two sweeps as of 2026-07-27. The heading sweep is the original and
    still runs first, so document order is preserved for sites with sane
    markup. The anchor sweep is new: a large minority of this source list
    (particularly Asian and African outlets, and anything built on a
    card/grid template) puts its front-page leads in <a> text or <span>s
    inside links, with headings reserved for section labels. Those sites
    were previously contributing at most one candidate — usually the wrong
    one — which is a direct cause of the print/web mismatch drops.
    """
    if limit <= 0:
        return []
    base_url = source["url"]
    pub_name = source["name"]
    seen = {exclude.lower().strip()} if exclude else set()
    out: list[Candidate] = []

    def _consider(text: str, href: str) -> bool:
        """Returns True if the limit is now reached."""
        text = _strip_label_prefix(text)
        if not text or _effective_len(text) < 20 or _is_site_name(text, pub_name):
            return False
        key = text.lower().strip()
        if key in seen:
            return False
        if href and any(x in href for x in NAV_HREF_SIGNALS):
            return False
        url = ""
        if href:
            url = href if href.startswith("http") else urljoin(base_url, href)
        seen.add(key)
        out.append(Candidate(headline=text[:500], article_url=url))
        return len(out) >= limit

    # ── Sweep 1: headings (original behaviour) ──────────────────────────
    for el in soup.find_all(["h1", "h2", "h3"]):
        a = el.find("a") or el.find_parent("a")
        href = a.get("href", "") if a else ""
        if _consider(el.get_text(strip=True), href):
            return out

    # ── Sweep 2: substantial linked text not already captured ───────────
    # Only fires if the heading sweep left room, so heading-driven sites
    # are unaffected. Capped at 400 anchors so a link-farm homepage can't
    # dominate the pass.
    for a in soup.find_all("a", href=True)[:400]:
        if _consider(a.get_text(strip=True), a.get("href", "")):
            return out

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Article-text fetcher (brief grounding)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_article_text(url: str, max_chars: int = 2500) -> str:
    """
    Fetch the opening paragraphs of an article so briefs are written from
    actual source text instead of a headline + truncated deckline. Only
    called for SELECTED stories (~8–15 fetches/run), so the cheap scrape
    path stays cheap. Returns "" on any failure — the brief prompt handles
    the ungrounded case explicitly.
    """
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(_decode(resp), "html.parser")
        container = (
            soup.find("article")
            or soup.find(attrs={"class": re.compile(r"article|story[-_]body|content", re.I)})
            or soup
        )
        paras = []
        for p in container.find_all("p"):
            t = p.get_text(strip=True)
            if _effective_len(t) > 60:
                paras.append(t)
            if len(paras) >= 8:
                break
        return "\n".join(paras)[:max_chars]
    except Exception as e:
        logger.info(f"Article fetch failed ({url}): {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Baselines
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_feed_headlines(feed_url: str, pub_name: str, limit: int = 40) -> list[str]:
    """
    Pull headlines from an RSS/Atom feed.

    Baselines answer exactly one question — "what is the world already
    covering?" — and that needs no front-page layout fidelity, so scraping
    a rendered homepage was always more machinery than the job required.
    Feeds are published for machine consumption: no JS render, no consent
    interstitial, no bot wall, and titles arrive clean enough that the junk
    filters rarely fire.

    Titles still pass through _is_site_name/_effective_len, because a few
    feeds carry section-label or promo items alongside real stories.
    """
    resp = requests.get(feed_url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(_decode(resp), "xml")
    nodes = soup.find_all("item") or soup.find_all("entry")   # RSS, then Atom
    out: list[str] = []
    for node in nodes[:limit]:
        title = node.find("title")
        if not title:
            continue
        text = title.get_text(strip=True)
        if _effective_len(text) > 20 and not _is_site_name(text, pub_name):
            out.append(text)
    return out


def scrape_baselines(baseline_sources: list[dict]) -> list[ScrapedStory]:
    """
    Scrape the baseline sources — the calibration for what is already
    globally known. The uniqueness filter IS the product, so this tries
    feeds first and falls back to the original requests -> Playwright
    ladder for any source without a working feed.

    Why feed-first (2026-07-28): that day's run aborted with 1/5 baselines,
    and every one of the four failures had gone through Playwright, while
    the single success (Guardian) was the only one that never touched it.
    Headless Chromium from a datacenter IP is trivially fingerprinted;
    NYT/WSJ/FT returned gate pages whose headings _is_site_name correctly
    stripped to zero via JUNK_SIGNALS ("cookie", "consent", "log in to
    continue"). That is indistinguishable in the logs from a page that
    genuinely had no headlines on it — which is why the same NYT/WSJ
    symptom had been misread as transient since 2026-07-13.
    """
    headlines_by_id: dict[str, list[str]] = {}
    needs_html: list[dict] = []

    # ── Pass 1: feeds ────────────────────────────────────────────────────
    for source in baseline_sources:
        feed = source.get("baseline_feed")
        if not feed:
            needs_html.append(source)
            continue
        try:
            hl = _fetch_feed_headlines(feed, source["name"])
            if hl:
                headlines_by_id[source["id"]] = hl
                logger.info(f"Baseline {source['name']}: {len(hl)} headlines (feed)")
            else:
                logger.warning(
                    f"Baseline feed for {source['name']} returned no usable "
                    "titles — falling back to homepage"
                )
                needs_html.append(source)
        except Exception as e:
            logger.warning(
                f"Baseline feed failed for {source['name']}: {e} — falling back to homepage"
            )
            needs_html.append(source)
        time.sleep(random.uniform(0.3, 0.8))

    # ── Pass 2: original homepage ladder for whatever the feeds missed ───
    html_by_id: dict[str, str] = {}
    escalate: list[dict] = []

    for source in needs_html:
        if source["id"] in PLAYWRIGHT_SITES:
            escalate.append(source)
            continue
        try:
            resp = requests.get(source["url"], headers=_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code in BLOCK_STATUS_CODES:
                escalate.append(source)
                continue
            resp.raise_for_status()
            html_by_id[source["id"]] = _decode(resp)
        except requests.RequestException as e:
            logger.warning(
                f"Baseline requests fetch failed for {source['name']}: {e} — escalating"
            )
            escalate.append(source)
        time.sleep(random.uniform(0.5, 1.5))

    if escalate:
        html_by_id.update(_fetch_html_playwright(escalate))

    for source in needs_html:
        html = html_by_id.get(source["id"])
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        hl = []
        for tag in soup.find_all(["h1", "h2", "h3"])[:40]:
            text = tag.get_text(strip=True)
            if _effective_len(text) > 20 and not _is_site_name(text, source["name"]):
                hl.append(text)
        if hl:
            headlines_by_id[source["id"]] = hl
            logger.info(f"Baseline {source['name']}: {len(hl)} headlines (homepage)")

    # ── Assemble in source order ─────────────────────────────────────────
    results = []
    for source in baseline_sources:
        hl = headlines_by_id.get(source["id"])
        if not hl:
            logger.warning(f"Baseline scrape failed for {source['name']} (all methods)")
            continue
        results.append(ScrapedStory(
            source_id=source["id"],
            country=source["country"],
            publication=source["name"],
            url=source["url"],
            headline=" | ".join(hl[:10]),
        ))
    return results
