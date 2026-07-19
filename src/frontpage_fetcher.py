"""
frontpage_fetcher.py

Acquires today's front-page cover image for a source, given the "frontpage"
config block added to that source's entry in sources.py, e.g.:

    {"provider": "frontpages", "slug": "the-new-york-times"}
    {"provider": "kiosko", "country_code": "mx", "slug": "mx_universal"}
    {"provider": "frontpages", "slug": "...", "fallback": {"provider": "kiosko", ...}}

This module ONLY fetches the image bytes. It does not select a story, write
a brief, or publish anything -- per the architecture decision that the front
page is an internal signal (fed to a vision-capable model in curator.py to
rank prominent stories and flag wire credit), never reproduced to subscribers.

Two providers, two different acquisition mechanics:

  - kiosko.net: the cover image URL is fully predictable from
    (country_code, slug, date) -- no page fetch needed.

  - frontpages.com: the cover image is injected client-side; the real image
    URL has to be scraped out of that day's newspaper page HTML first.
    Verified pattern (confirmed live, 2026-07-17):
        page:      https://www.frontpages.com/{slug}/
        image:     https://www.frontpages.com/t/{YYYY}/{MM}/{DD}/{slug}-{hash}.webp
    The {hash} suffix is not derivable in advance -- it must be scraped fresh
    per source per day.

    IMPORTANT RESOLUTION CAVEAT: the page markup references a larger cover
    render (~1200x1466) under a "/g/..." path, but that path 404s on direct
    request -- it isn't actually a working public URL, just something that
    looked plausible in the markup. The only path confirmed to actually
    resolve (tested against multiple sources, 2026-07-17) is "/t/...", which
    is a **300x400px thumbnail**. That's probably enough to read a banner
    headline but is genuinely too small to reliably read deck text or a
    wire-credit byline -- which matters a lot here, since wire-credit
    detection is exactly what this pipeline leans on the front page for.
    This is why kiosko.net (confirmed 750x1343) is treated as the *primary*
    provider where both exist, with frontpages.com as a lower-resolution
    fallback, not the reverse.

Neither provider has an explicit "yes, automated fetching is fine" policy.
Both have a standard, non-bot-gated robots.txt and returned clean responses
to a plain HTTP request when this was tested directly (2026-07-17) -- unlike
Freedom Forum, which sits behind active bot-detection and was deliberately
excluded from this design for that reason. Kept as a comment here rather
than just in chat history, since it's load-bearing for why this module is
written the way it is.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import date as date_cls, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
REQUEST_TIMEOUT = 15

# frontpages.com's own image path, e.g.
#   https://www.frontpages.com/t/2026/07/17/daily-nation-kenya-001906gju.webp
# ("/t/" = thumbnail, 300x400px -- see resolution caveat in module docstring)
FRONTPAGES_IMG_PATH_RE = re.compile(
    r'/t/(\d{4})/(\d{2})/(\d{2})/[a-z0-9\-]+-[a-z0-9]+\.webp'
)


@dataclass
class FrontPageResult:
    source_id: str
    provider: str          # "kiosko" | "frontpages"
    image_bytes: bytes
    image_url: str
    content_type: str
    used_date: date_cls = None  # the edition's ACTUAL date, which may be
                                 # up to STALENESS_TOLERANCE_DAYS earlier
                                 # than the date requested -- see below


# How many days old an edition is allowed to be before it's treated as a
# real failure rather than expected rollover noise. Confirmed live
# (2026-07-19, run at ~01:00 UTC): at that hour, ~40 of 55 sources were
# still showing their prior day's edition -- not because anything was
# broken, but because most of the world hadn't published "today" yet in
# UTC terms. Requiring an exact date match treated all of that ordinary
# publish-timing lag as failure. 1 day tolerates that; a source showing up
# MORE than 1 day stale (a few did: reforma was 4 days behind, adevarul 3)
# is a genuinely different problem -- frontpages.com/kiosko.net not
# refreshing that particular outlet -- and should still fail loudly rather
# than being silently absorbed by a wider tolerance window.
STALENESS_TOLERANCE_DAYS = 1


class FrontPageUnavailable(Exception):
    """Raised when a front page couldn't be fetched for any reason worth
    distinguishing from a hard error -- e.g. today's edition isn't up yet,
    the source has no frontpage config, or both primary and fallback
    providers failed. Callers should treat this as "this source drops out
    of today's front-page pipeline," not a crash."""


def fetch_frontpage(source: dict, on: Optional[date_cls] = None) -> FrontPageResult:
    """
    Fetch today's (or `on`'s) front-page cover image for a source dict from
    sources.py that has a "frontpage" key. Tries the primary provider, then
    the "fallback" provider if present and the primary fails.

    Raises FrontPageUnavailable if no image could be retrieved.
    """
    cfg = source.get("frontpage")
    if not cfg:
        raise FrontPageUnavailable(f"{source.get('id')}: no frontpage config")

    on = on or date_cls.today()

    try:
        return _fetch_by_provider(source["id"], cfg, on)
    except FrontPageUnavailable as e:
        fallback = cfg.get("fallback")
        if not fallback:
            raise
        logger.warning(
            "Primary provider failed for %s (%s); trying fallback %s",
            source["id"], e, fallback.get("provider"),
        )
        return _fetch_by_provider(source["id"], fallback, on)


def _fetch_by_provider(source_id: str, cfg: dict, on: date_cls) -> FrontPageResult:
    provider = cfg["provider"]
    if provider == "kiosko":
        return _fetch_kiosko(source_id, cfg, on)
    elif provider == "frontpages":
        return _fetch_frontpages(source_id, cfg, on)
    raise FrontPageUnavailable(f"{source_id}: unknown provider '{provider}'")


def _fetch_kiosko(source_id: str, cfg: dict, on: date_cls) -> FrontPageResult:
    """kiosko.net: URL is fully predictable and date-specific, so unlike
    frontpages.com, retrying with an earlier date is a genuinely different
    request (not just re-reading the same live page). Tries `on`, then up
    to STALENESS_TOLERANCE_DAYS earlier, before giving up."""
    country_code = cfg["country_code"]
    slug = cfg["slug"]
    last_err = None
    for days_back in range(0, STALENESS_TOLERANCE_DAYS + 1):
        try_date = on - timedelta(days=days_back)
        url = (
            f"https://img.kiosko.net/{try_date.year}/{try_date.month:02d}/{try_date.day:02d}"
            f"/{country_code}/{slug}.750.jpg"
        )
        resp = _get(url)
        if resp is not None and resp.status_code == 200 and resp.content:
            if days_back:
                logger.info(f"{source_id}: kiosko edition is {days_back} day(s) stale ({try_date}) — within tolerance")
            return FrontPageResult(
                source_id=source_id, provider="kiosko",
                image_bytes=resp.content, image_url=url,
                content_type=resp.headers.get("Content-Type", "image/jpeg"),
                used_date=try_date,
            )
        last_err = f"kiosko image fetch failed for {url}"
    raise FrontPageUnavailable(f"{source_id}: {last_err} (tried today and {STALENESS_TOLERANCE_DAYS} day(s) back)")


def _fetch_frontpages(source_id: str, cfg: dict, on: date_cls) -> FrontPageResult:
    """frontpages.com: the outlet's own dedicated page (/{slug}/) does NOT
    reliably expose its own cover thumbnail in static HTML -- confirmed by
    testing (2026-07-17): that page only contains OTHER outlets' thumbnails
    (a cross-promotion sidebar), so naively taking "the first /t/ match on
    the page" silently returns the wrong newspaper's cover. (Caught this by
    testing against multiple sources -- it returned USA Today for a Kenyan
    outlet and Daily Jang for a Pakistani one.)

    The reliable source is the outlet's *country listing* page
    (/{country_slug}-newspapers/), which lists every outlet for that
    country as an <a href="/{slug}/"><img src=".../t/...webp"></a> pair --
    same structure kiosko.net uses. This fetches that page and extracts the
    image from inside the anchor matching this outlet's exact slug, not
    just any /t/ path on the page.
    """
    slug = cfg["slug"]
    country_slug = cfg.get("country_slug")
    if not country_slug:
        raise FrontPageUnavailable(
            f"{source_id}: frontpages.com config missing 'country_slug' "
            f"(required to find {slug}'s own thumbnail, not someone else's)"
        )

    country_url = f"https://www.frontpages.com/{country_slug}-newspapers/"
    resp = _get(country_url)
    if resp is None or resp.status_code != 200:
        raise FrontPageUnavailable(
            f"{source_id}: frontpages.com country page fetch failed for {country_url}"
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    anchor = soup.select_one(f'a[href="/{slug}/"]')
    if anchor is None:
        raise FrontPageUnavailable(
            f"{source_id}: no anchor for /{slug}/ found on {country_url} "
            f"(slug may have changed, or outlet dropped from that country page)"
        )
    img = anchor.find("img")
    src = img.get("src") if img else None
    if not src:
        raise FrontPageUnavailable(
            f"{source_id}: anchor for /{slug}/ on {country_url} has no image"
        )

    m = FRONTPAGES_IMG_PATH_RE.search(src)
    if not m:
        raise FrontPageUnavailable(
            f"{source_id}: unexpected image path format for {slug}: {src}"
        )
    yyyy, mm, dd = m.groups()
    found_date = date_cls(int(yyyy), int(mm), int(dd))
    staleness = (on - found_date).days
    if staleness < 0 or staleness > STALENESS_TOLERANCE_DAYS:
        # Note: re-fetching wouldn't change this -- frontpages.com always
        # serves whatever it currently has live for this outlet, there's no
        # way to request a specific past date (confirmed: its own "?d="
        # param breaks the image injection entirely). So this is a genuine
        # staleness problem on their end for this outlet, not a timing gap
        # we can retry past.
        raise FrontPageUnavailable(
            f"{source_id}: frontpages.com's live edition for {slug} is dated "
            f"{found_date}, {staleness} day(s) from the requested {on} "
            f"(beyond the {STALENESS_TOLERANCE_DAYS}-day tolerance -- this "
            f"looks like a genuinely stale/unrefreshed outlet, not ordinary "
            f"publish-timing lag)"
        )
    if staleness:
        logger.info(f"{source_id}: frontpages.com edition is {staleness} day(s) stale ({found_date}) — within tolerance")

    image_url = f"https://www.frontpages.com{src}"
    img_resp = _get(image_url)
    if img_resp is None or img_resp.status_code != 200 or not img_resp.content:
        raise FrontPageUnavailable(
            f"{source_id}: frontpages.com image fetch failed for {image_url}"
        )
    return FrontPageResult(
        source_id=source_id,
        provider="frontpages",
        image_bytes=img_resp.content,
        image_url=image_url,
        content_type=img_resp.headers.get("Content-Type", "image/webp"),
        used_date=found_date,
    )


def _get(url: str) -> Optional[requests.Response]:
    try:
        return requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        logger.warning("Request failed for %s: %s", url, e)
        return None


if __name__ == "__main__":
    # Quick manual smoke test against a couple of real sources.
    logging.basicConfig(level=logging.INFO)
    test_sources = [
        {"id": "el_universal", "frontpage": {"provider": "kiosko", "country_code": "mx", "slug": "mx_universal"}},
        {"id": "daily_nation", "frontpage": {"provider": "frontpages", "slug": "daily-nation-kenya"}},
    ]
    for s in test_sources:
        try:
            result = fetch_frontpage(s)
            print(f"{s['id']}: OK via {result.provider}, {len(result.image_bytes)} bytes, {result.image_url}")
        except FrontPageUnavailable as e:
            print(f"{s['id']}: FAILED -- {e}")
