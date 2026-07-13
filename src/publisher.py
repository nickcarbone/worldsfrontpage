"""
World's Front Page — Substack Publisher
Assembles the curated stories into a formatted Substack draft
and posts it via the Substack API for editor review before publish.

v2 additions:
  - Coverage history: history.json (repo root, committed back by the
    Actions workflow) holds the last 7 days of published stories so the
    curator doesn't rerun the same slow-burn story on consecutive days.
  - HTML fallback: save_local now writes the fully assembled HTML next to
    the JSON log, so if Substack's unofficial API breaks, the edition is
    inconvenienced, not lost — grab the HTML from the run artifact and
    paste it into a draft manually.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from sources import SOURCES, STATUS_LABELS, BASELINE_SOURCES

logger = logging.getLogger(__name__)

SUBSTACK_EMAIL    = os.environ.get("SUBSTACK_EMAIL", "")
SUBSTACK_PASSWORD = os.environ.get("SUBSTACK_PASSWORD", "")
SUBSTACK_PUB_URL  = os.environ.get("SUBSTACK_PUB_URL", "https://worldsfrontpage.substack.com")

HISTORY_PATH = Path("history.json")   # repo root — committed back by daily.yml
HISTORY_DAYS = 7

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


# ─────────────────────────────────────────────────────────────────────────────
# Coverage history (7-day memory for the curator)
# ─────────────────────────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    """Load the rolling record of recently published stories.
    Returns [] if no history exists or the file is unreadable."""
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not read {HISTORY_PATH}: {e} — starting fresh")
        return []


def update_history(stories: list[dict], run_date: datetime = None) -> None:
    """Append today's published stories and prune entries older than
    HISTORY_DAYS. Call this only after a real (non-dry-run) publish."""
    if run_date is None:
        run_date = datetime.now(timezone.utc)
    cutoff = (run_date - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    today = run_date.strftime("%Y-%m-%d")

    history = [h for h in load_history() if h.get("date", "") >= cutoff]
    for s in stories:
        history.append({
            "date": today,
            "country": s["country"],
            "publication": s["publication"],
            "headline": s["original_headline"][:200],
        })
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"Coverage history updated: {len(history)} entries "
                f"across last {HISTORY_DAYS} days")


# ─────────────────────────────────────────────────────────────────────────────
# Post assembly
# ─────────────────────────────────────────────────────────────────────────────

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
    Returns True on success. If this fails, the assembled HTML is already
    saved by save_local() — the edition is recoverable from the run artifact.
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
    Save post and raw stories to local JSON — and the assembled HTML — for
    debugging, review, and manual recovery if the Substack post fails.
    Returns the JSON filepath.
    """
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

    # HTML fallback — paste-ready if the unofficial Substack API breaks
    html_path = log_dir / f"{date_slug}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(
            f"<h1>{post['title']}</h1>\n<h2>{post['subtitle']}</h2>\n{post['body_html']}"
        )

    logger.info(f"Saved local log: {filepath} (+ {html_path})")
    return str(filepath)
