"""
World's Front Page — Main Pipeline Orchestrator

Modes:
  python main.py                      Full run: scrape, curate, post to Substack
  python main.py --dry-run            Full run, skip posting (for local/CI testing)
  python main.py --build              Scrape/curate/assemble only; save the result
                                       to logs/pending_post.json for a separate
                                       publish step. Does NOT post or update history.
  python main.py --publish PATH       Load a pending_post.json and post it via the
                                       browser-session publisher (self-hosted runner
                                       only — see publish_to_substack.py). Updates
                                       history on success.

--build / --publish exist because posting now has to happen from a residential
IP with a real logged-in browser session (see publish_to_substack.py's
docstring for why) — that means it has to run on a self-hosted runner, while
scraping/curating has no such constraint and should stay on GitHub's free,
disposable runners. Splitting the run in two lets each half live on the
infrastructure suited to it.
"""

from __future__ import annotations  # lets `str | None` etc. run on Python < 3.10

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# logs/ must exist BEFORE logging.basicConfig runs — the FileHandler opens
# its file at import time, and a fresh checkout has no logs/ directory.
Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/pipeline_{datetime.now().strftime('%Y-%m-%d')}.log"),
    ],
)
logger = logging.getLogger("main")

# The uniqueness filter is the product. Below this many working baselines,
# "not already globally known" is being judged against a hollowed-out
# reference set — better to fail loudly than publish a miscalibrated issue.
#
# Set to 2 (not the original 3) as of 2026-07-13: three consecutive runs
# showed NYT and WSJ consistently returning zero headlines from GitHub's
# runners — 200 OK, real HTML, but no usable content — while FT and
# Guardian were reliably strong (23 and 5 headlines respectively) every
# time. That pattern (successful fetch, empty content) looks like NYT/WSJ's
# own bot-management soft-blocking automated/datacenter traffic, not
# transient flakiness. FT + Guardian alone are still a reasonable baseline;
# revisit if NYT/WSJ scraping ever gets a dedicated fix (lower priority
# than the Substack posting fix, since this fails safe rather than silently).
MIN_BASELINES = 2

# Front-page vision selection (2026-07-17) replaced the old web-only
# selection logic for sources with verified frontpage coverage (~59 of 167
# as of the coverage audit) — sources without it drop out silently, by
# design, rather than falling back to the old heuristic. That means the
# pool feeding curator.curate() is smaller and its size now depends on
# THREE independent things working: kiosko.net/frontpages.com being up,
# each source's own web scrape still succeeding (front-page selection only
# picks among already-scraped candidates, it doesn't source article text
# itself), and the vision call's own judgment. This threshold is a
# starting guess, not a calibrated number — nobody has seen a real
# distribution of daily match counts yet. Revisit after the first week of
# production runs, same as the clustering thresholds already flagged for
# recalibration.
MIN_FRONTPAGE_MATCHES = 15

PENDING_POST_PATH = Path("logs") / "pending_post.json"


def _scrape_curate_build():
    """Steps 1-5, shared by the full run and --build. Returns (post, curated, run_date)."""
    from sources import get_sources, get_baseline_sources
    from scraper import scrape_all, scrape_baselines
    from frontpage_selector import apply_frontpage_selection
    from curator import curate
    from publisher import build_post, load_history

    run_date = datetime.now(timezone.utc)
    logger.info(f"=== World's Front Page pipeline starting — {run_date.strftime('%Y-%m-%d %H:%M UTC')} ===")

    logger.info("Step 1/5: Scraping baseline sources...")
    baseline_sources = get_baseline_sources()
    baselines = scrape_baselines(baseline_sources)
    baseline_ok = sum(1 for b in baselines if b.headline)
    logger.info(f"  Baseline stories: {baseline_ok}/{len(baseline_sources)}")

    if baseline_ok < MIN_BASELINES:
        logger.error(
            f"Only {baseline_ok} baseline sources returned headlines "
            f"(minimum {MIN_BASELINES}). The uniqueness filter would be "
            f"unreliable — aborting rather than publishing a miscalibrated issue."
        )
        sys.exit(1)

    logger.info("Step 2/5: Scraping all sources...")
    sources = get_sources(exclude_baseline=True)
    stories = scrape_all(sources, use_playwright=True)
    successful = sum(1 for s in stories if s.headline and not s.scrape_error)
    failed     = sum(1 for s in stories if s.scrape_error)
    logger.info(f"  Scraped: {successful} success, {failed} failed out of {len(stories)} sources")

    if successful < 10:
        logger.error("Too few successful scrapes — aborting pipeline.")
        sys.exit(1)

    logger.info("Step 3/5: Applying front-page vision selection...")
    sources_by_id = {s["id"]: s for s in sources}
    with_frontpage = sum(1 for s in sources if "frontpage" in s)
    stories, frontpage_logs = apply_frontpage_selection(stories, sources_by_id, on=run_date.date())
    matched = sum(1 for l in frontpage_logs if l.matched)
    logger.info(f"  Front-page selection: {matched}/{with_frontpage} sources with frontpage config matched")
    for log in frontpage_logs:
        if not log.matched:
            logger.info(f"    dropped {log.source_id}: {log.reason}")
        elif log.wire_elements_skipped:
            logger.info(f"    {log.source_id}: skipped {log.wire_elements_skipped} wire-credited element(s) before matching")

    if matched < MIN_FRONTPAGE_MATCHES:
        logger.error(
            f"Only {matched} sources matched via front-page selection "
            f"(minimum {MIN_FRONTPAGE_MATCHES}) — aborting rather than publishing "
            f"a thin issue. Check kiosko.net/frontpages.com availability and the "
            f"vision call's own error log above before re-running."
        )
        sys.exit(1)

    logger.info("Step 4/5: Running LLM curation...")
    recent_coverage = load_history()
    logger.info(f"  Coverage history loaded: {len(recent_coverage)} recent stories")
    curated = curate(stories, baselines, recent_coverage=recent_coverage)
    logger.info(f"  Selected and briefed: {len(curated)} stories")

    logger.info("Step 5/5: Assembling post...")
    post = build_post(curated, date=run_date)
    logger.info(f"  Post title: {post['title']}")

    return post, curated, run_date


def _run_publish_only(pending_path: str):
    """--publish mode: load a previously-built edition and post it via the
    browser-session publisher. Only meant to run on the self-hosted runner."""
    from publisher import update_history
    from publish_to_substack import post_draft_via_browser

    path = Path(pending_path)
    if not path.exists():
        logger.error(f"Pending post file not found: {path}")
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))
    post = data["post"]
    curated = data["stories"]
    run_date = datetime.fromisoformat(data["run_date"])

    logger.info(f"Publish-only mode: posting pending edition ({post['title']}) to Substack...")
    success = post_draft_via_browser(post)
    if success:
        update_history(curated, run_date)
        logger.info("✓ Draft posted to Substack. Ready for your review.")
    else:
        logger.error(
            "✗ Substack post failed — check logs. The assembled edition is "
            f"still at {pending_path} for manual recovery."
        )
        sys.exit(1)

    logger.info("=== Publish step complete ===")


def main(dry_run: bool = False, build_only: bool = False, publish_path: str | None = None):
    if publish_path:
        _run_publish_only(publish_path)
        return

    from publisher import save_local, post_draft, update_history

    post, curated, run_date = _scrape_curate_build()

    log_path = save_local(post, curated, run_date)
    logger.info(f"Local log saved: {log_path}")

    if build_only:
        PENDING_POST_PATH.write_text(
            json.dumps(
                {"post": post, "stories": curated, "run_date": run_date.isoformat()},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(f"Build-only mode: pending post saved to {PENDING_POST_PATH} for the publish job")
        logger.info("=== Build step complete ===")
        return

    if dry_run:
        logger.info("DRY RUN — skipping Substack post and history update. Review output:")
        print("\n" + "="*60)
        print(f"TITLE: {post['title']}")
        print(f"SUBTITLE: {post['subtitle']}")
        print("="*60)
        for story in curated:
            print(f"\n🌐 {story['country']} — {story['publication']}")
            print(f"  {story['brief']}")
            if story.get("why_it_matters"):
                print(f"  WHY IT MATTERS: {story['why_it_matters']}")
        print("="*60 + "\n")
    else:
        logger.info("Posting draft to Substack...")
        success = post_draft(post)
        if success:
            update_history(curated, run_date)
            logger.info("✓ Draft posted to Substack. Ready for your review.")
        else:
            logger.error(
                "✗ Substack post failed — check credentials and logs. "
                f"The assembled edition is saved at {log_path} (and .html) "
                "for manual recovery."
            )
            sys.exit(1)

    logger.info("=== Pipeline complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="World's Front Page pipeline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", action="store_true",
        help="Run pipeline without posting to Substack"
    )
    group.add_argument(
        "--build", action="store_true",
        help="Scrape/curate/assemble only; save pending_post.json for a separate publish step"
    )
    group.add_argument(
        "--publish", metavar="PATH", default=None,
        help="Post a previously-built pending_post.json via the browser-session publisher"
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, build_only=args.build, publish_path=args.publish)
