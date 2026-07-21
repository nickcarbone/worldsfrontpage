"""
World's Front Page — Local live test for front-page vision selection

Run this directly (your Mac, or any machine with the repo + API key) to see
the REAL pipeline in action on a small, deliberately diverse sample of
sources: real front-page images fetched from kiosko.net/frontpages.com,
real Claude vision calls, real matching against real scraped web candidates.

This does NOT touch Substack, does NOT update history.json, and does NOT
run the full 167-source pipeline — it's a fast, cheap sanity check (8
sources = 8 scrapes + up to 8 vision calls, a few cents at most) so you can
read the model's actual reasoning before trusting it inside the real
scheduled run.

WHAT TO LOOK FOR IN THE OUTPUT:
  - Does "matched headline" look like a real, sensible story for that
    outlet today, not something garbled or unrelated?
  - When "wire skipped" is > 0, does that seem right — was the banner
    story plausibly wire-led? (You'll have to eyeball this against the
    actual front page yourself the first few times; there's no ground
    truth to check it against automatically yet.)
  - Do any sources fail, and does the failure reason make sense (image
    unavailable / rollover mismatch / no scraped candidates) rather than
    something that looks like a bug?

Prerequisites:
  1. `pip install -r requirements.txt` has been run in this environment.
  2. ANTHROPIC_API_KEY is set in your environment (same as the real pipeline).

Usage:
    python test_frontpage_live.py
"""

import os
import sys
import logging
from datetime import date, datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.path.insert(0, "src")  # so `import sources` / `import scraper` etc. work run from repo root

if "ANTHROPIC_API_KEY" not in os.environ:
    print("ANTHROPIC_API_KEY is not set in this environment — set it the same")
    print("way the real pipeline expects it, then re-run.")
    sys.exit(1)

from sources import SOURCES  # noqa: E402
from scraper import scrape_all  # noqa: E402
from frontpage_selector import apply_frontpage_selection  # noqa: E402

TODAY_UTC = datetime.now(timezone.utc).date()
# Uses UTC, not date.today() (which reads your machine's LOCAL timezone) --
# the fetcher compares against each site's own roughly-UTC publish date, so
# a local-time date() here would cause false "rollover gap" drops that have
# nothing to do with the real cross-timezone edge case this is meant to
# surface. main.py's real pipeline already does this correctly
# (datetime.now(timezone.utc)); this test script just hadn't matched it.

# A deliberately small, deliberately diverse sample: different providers
# (kiosko-only, frontpages-only, both-with-fallback), different scripts,
# different regions — not just "whatever's first in the file."
TEST_SOURCE_IDS = [
    "el_universal",   # Mexico — kiosko only
    "daily_nation",    # Kenya — frontpages only
    "dawn",            # Pakistan — frontpages only, non-Latin-adjacent script context
    "irish_times",     # Ireland — has both providers (kiosko primary)
    "haaretz",         # Israel — frontpages only, Hebrew script
    "granma",          # Cuba — kiosko, state-controlled outlet (interesting selection case)
    "the_hindu",       # India — frontpages only, far-ahead timezone (rollover risk case)
    "publico",         # Portugal — has both providers (kiosko primary)
]
# Deliberately avoids NZZ/El País/etc. from scraper.py's PLAYWRIGHT_SITES set —
# all 8 above are requests-only, so this runs with nothing but
# `pip install -r requirements.txt`, no `playwright install chromium` needed
# for this first pass. Add Playwright-dependent sources back in later once
# you've confirmed the basics work.


def main():
    sample = [s for s in SOURCES if s["id"] in TEST_SOURCE_IDS]
    found_ids = {s["id"] for s in sample}
    missing = set(TEST_SOURCE_IDS) - found_ids
    if missing:
        print(f"NOTE: these test ids weren't found in sources.py (renamed/removed?): {missing}")

    print(f"\nScraping {len(sample)} sources for real web candidates...")
    stories = scrape_all(sample, use_playwright=True)
    scraped_ok = sum(1 for s in stories if s.headline and not s.scrape_error)
    print(f"  {scraped_ok}/{len(sample)} scraped successfully\n")

    sources_by_id = {s["id"]: s for s in sample}

    print("Running LIVE front-page vision selection (real images, real API calls)...\n")
    # `on=today` first; if that's an obvious rollover-timing miss for a
    # far-ahead-of-UTC source, this also tries yesterday so you're not
    # blocked from testing just because you ran this at the wrong hour.
    survivors, logs = apply_frontpage_selection(stories, sources_by_id, on=TODAY_UTC)

    matched_ids = {s.source_id for s in survivors}
    retry_ids = [l.source_id for l in logs if not l.matched and "not the requested" in l.reason]
    if retry_ids:
        print(f"Retrying {len(retry_ids)} rollover-mismatch source(s) against yesterday's date...\n")
        retry_sources = {sid: sources_by_id[sid] for sid in retry_ids}
        retry_stories = [s for s in stories if s.source_id in retry_ids]
        retry_survivors, retry_logs = apply_frontpage_selection(
            retry_stories, retry_sources, on=TODAY_UTC - timedelta(days=1)
        )
        survivors += retry_survivors
        logs = [l for l in logs if l.source_id not in retry_ids] + retry_logs

    print("=" * 70)
    for log in logs:
        story = next((s for s in survivors if s.source_id == log.source_id), None)
        print(f"\n[{log.source_id}] {'MATCHED' if log.matched else 'DROPPED'}")
        print(f"  reason: {log.reason}")
        if log.wire_elements_skipped:
            print(f"  wire elements skipped: {log.wire_elements_skipped}")
        if log.frontpage_image_url:
            print(f"  front-page image used: {log.frontpage_image_url}")
        if story:
            print(f"  --> matched headline: {story.headline}")
            print(f"  --> article URL:      {story.article_url}")
    print("\n" + "=" * 70)
    print(f"\n{len(survivors)}/{len(sample)} sources matched.")
    print("Go check each 'front-page image used' URL in a browser against the")
    print("matched headline above — that's the real sanity check here, there's")
    print("no automated ground truth for 'did it pick the right story.'")


if __name__ == "__main__":
    main()
