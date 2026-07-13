"""
World's Front Page — Substack Publisher
Assembles the curated stories into a formatted Substack draft
and posts it via the Substack API for editor review before publish.
"""

import os
import json
import logging
from datetime import datetime, timezone
import requests
from sources import SOURCES, STATUS_LABELS, BASELINE_SOURCES

logger = logging.getLogger(__name__)

SUBSTACK_EMAIL    = os.environ.get("SUBSTACK_EMAIL", "")
SUBSTACK_PASSWORD = os.environ.get("SUBSTACK_PASSWORD", "")
SUBSTACK_PUB_URL  = os.environ.get("SUBSTACK_PUB_URL", "https://worldsfrontpage.substack.com")

# Country flag emoji lookup
COUNTRY_FLAGS = {
    "USA": "🇺🇸", "Canada": "🇨🇦", "Mexico": "🇲🇽", "Brazil": "🇧🇷",
    "Argentina": "🇦🇷", "Colombia": "🇨🇴", "Chile": "🇨🇱", "Venezuela": "🇻🇪",
    "Cuba": "🇨🇺", "Jamaica": "🇯🇲", "Peru": "🇵🇪",
    "UK": "🇬🇧", "Ireland": "🇮🇪", "France": "🇫🇷", "Germany": "🇩🇪",
    "Switzerland": "🇨🇭", "Netherlands": "🇳🇱", "Belgium": "🇧🇪", "Austria": "🇦🇹",
    "Spain": "🇪🇸", "Italy": "🇮🇹", "Portugal": "🇵🇹", "Sweden": "🇸🇪",
    "Norway": "🇳🇴", "Denmark": "🇩🇰", "Finland": "🇫🇮", "Poland": "🇵🇱",
    "Czech Republic": "🇨🇿", "Hungary": "🇭🇺", "Greece": "🇬🇷", "Turkey": "🇹🇷",
    "Ukraine": "🇺🇦", "Russia": "🇷🇺",
    "South Africa": "🇿🇦", "Nigeria": "🇳🇬", "Kenya": "🇰🇪", "Ghana": "🇬🇭",
    "Ethiopia": "🇪🇹", "Egypt": "🇪🇬", "Morocco": "🇲🇦",
    "Israel": "🇮🇱", "Lebanon": "🇱🇧", "UAE": "🇦🇪", "Saudi Arabia": "🇸🇦",
    "Iran": "🇮🇷", "Jordan": "🇯🇴",
    "Hong Kong": "🇭🇰", "China": "🇨🇳", "Taiwan": "🇹🇼", "Japan": "🇯🇵",
    "South Korea": "🇰🇷", "India": "🇮🇳", "Pakistan": "🇵🇰", "Bangladesh": "🇧🇩",
    "Singapore": "🇸🇬", "Thailand": "🇹🇭", "Philippines": "🇵🇭", "Indonesia": "🇮🇩",
    "Malaysia": "🇲🇾", "Myanmar": "🇲🇲", "Australia": "🇦🇺", "New Zealand": "🇳🇿",
}

# Build source status lookup
SOURCE_STATUS = {s["id"]: s["status"] for s in SOURCES}


def _source_stats() -> dict:
    """
    Compute current monitoring stats directly from sources.py so the
    newsletter's self-description never goes stale as the source list
    grows. Counts ALL sources (including the 5 baseline-only ones,
    since those are genuinely scraped/monitored each run, just excluded
    from story selection).
    """
    total = len(SOURCES)
    countries = len({s["country"] for s in SOURCES})
    icij = sum(1 for s in SOURCES if s.get("icij"))
    return {"total": total, "countries": countries, "icij": icij}


def build_post(stories: list[dict], date: datetime = None) -> dict:
    """
    Assemble the full Substack post from curated stories.
    Returns a dict with title, subtitle, and body_html.
    """
    if date is None:
        date = datetime.now(timezone.utc)

    date_str  = date.strftime("%A, %B %-d, %Y")
    today_str = date.strftime("%B %-d")

    title    = f"World's Front Page — {today_str}"
    subtitle = f"What's on the front pages that didn't make your feed. {date_str}."

    # Intro
    story_count = len(stories)
    stats = _source_stats()
    html_parts = [
        f'<p><em>Today we monitored front pages from {stats["total"]} publications across '
        f'{stats["countries"]} countries. Here are the {story_count} stories that made the '
        f'front page somewhere in the world and probably didn\'t make yours.</em></p>',
        '<hr/>',
    ]

    for story in stories:
        flag  = COUNTRY_FLAGS.get(story["country"], "🌐")
        label = f"{flag} <strong>{story['country']} — {story['publication']}</strong>"
        status_key   = SOURCE_STATUS.get(story["source_id"], "")
        status_label = STATUS_LABELS.get(status_key, "")

        block = f'<h3>{label}</h3>\n'

        if status_label:
            block += f'<p><small>{status_label}</small></p>\n'

        block += f'<p>{story["brief"]}</p>\n'

        if story.get("why_it_matters"):
            block += (
                f'<p><strong>Why it matters:</strong> '
                f'{story["why_it_matters"]}</p>\n'
            )

        if story.get("article_url"):
            block += (
                f'<p><a href="{story["article_url"]}">'
                f'→ Read more at {story["publication"]}</a></p>\n'
            )

        block += '<hr/>\n'
        html_parts.append(block)

    # Footer
    html_parts.append(
        f'<p><em>World\'s Front Page monitors {stats["total"]} publications across '
        f'{stats["countries"]} countries daily, including {stats["icij"]} ICIJ media partners. '
        f'Stories are selected for national significance and global underreporting. '
        f'State-affiliated sources are labeled. All stories translated to English.</em></p>'
    )

    return {
        "title":     title,
        "subtitle":  subtitle,
        "body_html": "\n".join(html_parts),
    }


def post_draft(post: dict) -> bool:
    """
    Post the assembled content as a Substack draft.
    Uses Substack's internal API (unofficial but stable).
    Returns True on success.
    """
    if not SUBSTACK_EMAIL or not SUBSTACK_PASSWORD:
        logger.error("SUBSTACK_EMAIL and SUBSTACK_PASSWORD must be set.")
        return False

    session = requests.Session()

    # Authenticate
    auth_resp = session.post(
        f"{SUBSTACK_PUB_URL}/api/v1/email-login",
        json={"email": SUBSTACK_EMAIL, "password": SUBSTACK_PASSWORD},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )

    if auth_resp.status_code != 200:
        logger.error(f"Substack auth failed: {auth_resp.status_code} {auth_resp.text[:200]}")
        return False

    # Create draft
    draft_resp = session.post(
        f"{SUBSTACK_PUB_URL}/api/v1/drafts",
        json={
            "type":          "newsletter",
            "draft_title":   post["title"],
            "draft_subtitle": post["subtitle"],
            "draft_body":    post["body_html"],
            "audience":      "everyone",
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )

    if draft_resp.status_code in (200, 201):
        draft_data = draft_resp.json()
        draft_id   = draft_data.get("id", "unknown")
        logger.info(f"Draft created: {draft_id}")
        return True
    else:
        logger.error(f"Draft creation failed: {draft_resp.status_code} {draft_resp.text[:200]}")
        return False


def save_local(post: dict, stories: list[dict], run_date: datetime = None) -> str:
    """
    Save post and raw stories to local JSON for debugging/review.
    Returns the filepath.
    """
    import os
    from pathlib import Path

    if run_date is None:
        run_date = datetime.now(timezone.utc)

    date_slug = run_date.strftime("%Y-%m-%d")
    log_dir   = Path("logs")
    log_dir.mkdir(exist_ok=True)

    payload = {
        "date":    date_slug,
        "post":    post,
        "stories": stories,
    }

    filepath = log_dir / f"{date_slug}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved local log: {filepath}")
    return str(filepath)
