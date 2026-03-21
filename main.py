"""
World's Front Page — Main Pipeline Orchestrator
Run manually: python main.py
Run with dry-run (no Substack post): python main.py --dry-run
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Create logs directory before logging setup
Path("logs").mkdir(exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/pipeline_{datetime.now().strftime('%Y-%m-%d')}.log"),
    ],
)
logger = logging.getLogger("main")


def main(dry_run: bool = False):
    from sources import get_sources, get_baseline_sources
    from scraper import scrape_all, scrape_baselines
    from curator import curate
    from publisher import build_post, post_draft, save_local

    run_date = datetime.now(timezone.utc)
    logger.info(f"=== World's Front Page pipeline starting — {run_date.strftime('%Y-%m-%d %H:%M UTC')} ===")

    # ── 1. Scrape baseline sources (global news calibration) ──────────────────
    logger.info("Step 1/5: Scraping baseline sources...")
    baseline_sources = get_baseline_sources()
    baselines = scrape_baselines(baseline_sources)
    logger.info(f"  Baseline stories: {sum(1 for b in baselines if b.headline)}/{len(baselines)}")

    # ── 2. Scrape all 84 source homepages ─────────────────────────────────────
    logger.info("Step 2/5: Scraping all sources...")
    sources = get_sources(exclude_baseline=True)
    stories = scrape_all(sources, use_playwright=not dry_run)
    successful = sum(1 for s in stories if s.headline and not s.scrape_error)
    failed     = sum(1 for s in stories if s.scrape_error)
    logger.info(f"  Scraped: {successful} success, {failed} failed out of {len(stories)} sources")

    if successful < 10:
        logger.error("Too few successful scrapes — aborting pipeline.")
        sys.exit(1)

    # ── 3. LLM curation: translate, select, write briefs ─────────────────────
    logger.info("Step 3/5: Running LLM curation...")
    curated = curate(stories, baselines)
    logger.info(f"  Selected and briefed: {len(curated)} stories")

    # ── 4. Assemble Substack post ─────────────────────────────────────────────
    logger.info("Step 4/5: Assembling post...")
    post = build_post(curated, date=run_date)
    logger.info(f"  Post title: {post['title']}")

    # ── 5. Save local log (always) and post to Substack (unless dry-run) ──────
    log_path = save_local(post, curated, run_date)
    logger.info(f"  Local log saved: {log_path}")

    if dry_run:
        logger.info("DRY RUN — skipping Substack post. Review output:")
        print("\n" + "="*60)
        print(f"TITLE: {post['title']}")
        print(f"SUBTITLE: {post['subtitle']}")
        print("="*60)
        for story in curated:
            flag = "🌐"
            print(f"\n{flag} {story['country']} — {story['publication']}")
            print(f"  {story['brief']}")
            if story.get("why_it_matters"):
                print(f"  WHY IT MATTERS: {story['why_it_matters']}")
        print("="*60 + "\n")
    else:
        logger.info("Step 5/5: Posting draft to Substack...")
        success = post_draft(post)
        if success:
            logger.info("✓ Draft posted to Substack. Ready for your review.")
        else:
            logger.error("✗ Substack post failed — check credentials and logs.")
            sys.exit(1)

    logger.info("=== Pipeline complete ===")


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)

    parser = argparse.ArgumentParser(description="World's Front Page pipeline")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run pipeline without posting to Substack"
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
