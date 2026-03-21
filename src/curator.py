"""
World's Front Page — LLM Curation Layer
Uses Claude API to:
1. Translate non-English content
2. Score each story for uniqueness (not already globally saturated)
3. Select the best 10-15 stories
4. Write a 3-sentence brief per story
5. Add "why it matters" framing
"""

import os
import json
import logging
from anthropic import Anthropic
from scraper import ScrapedStory
from sources import STATUS_LABELS

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-opus-4-5"
MAX_STORIES = 15
MIN_STORIES = 8


def curate(stories: list[ScrapedStory], baselines: list[ScrapedStory]) -> list[dict]:
    """
    Full curation pipeline.
    Returns list of ready-to-publish story dicts.
    """
    # Filter out failed scrapes
    valid = [s for s in stories if s.headline and not s.scrape_error]
    logger.info(f"Valid stories to curate: {len(valid)} / {len(stories)}")

    if not valid:
        raise ValueError("No valid stories scraped — aborting.")

    # Build baseline context
    baseline_text = _build_baseline_context(baselines)

    # Step 1: Translate non-English headlines/decklines
    valid = _translate_batch(valid)

    # Step 2: Score and select
    selected = _select_stories(valid, baseline_text)

    # Step 3: Write briefs
    briefed = _write_briefs(selected, baseline_text)

    return briefed


def _build_baseline_context(baselines: list[ScrapedStory]) -> str:
    """Summarize baseline headlines into a global news context string."""
    lines = []
    for b in baselines:
        if b.headline:
            lines.append(f"[{b.publication}]: {b.headline}")
    return "\n".join(lines)


def _translate_batch(stories: list[ScrapedStory]) -> list[ScrapedStory]:
    """Translate non-English stories in a single batched API call."""
    to_translate = [s for s in stories if s.language_hint not in ("en",)]
    if not to_translate:
        return stories

    logger.info(f"Translating {len(to_translate)} non-English stories...")

    items = []
    for i, s in enumerate(to_translate):
        items.append({
            "index": i,
            "source_id": s.source_id,
            "language": s.language_hint,
            "headline": s.headline,
            "deckline": s.deckline,
        })

    prompt = f"""You are a professional news translator. Translate each item to English.
Preserve journalistic tone and meaning precisely. Do not summarize or editorialize.
Return ONLY a JSON array with objects: {{"index": N, "headline": "...", "deckline": "..."}}

Items to translate:
{json.dumps(items, ensure_ascii=False, indent=2)}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        translations = json.loads(raw)
        trans_map = {t["index"]: t for t in translations}
        for i, s in enumerate(to_translate):
            if i in trans_map:
                s.headline = trans_map[i].get("headline", s.headline)
                s.deckline = trans_map[i].get("deckline", s.deckline)
    except Exception as e:
        logger.warning(f"Translation parse error: {e} — using originals")

    return stories


def _select_stories(stories: list[ScrapedStory], baseline_text: str) -> list[ScrapedStory]:
    """
    Ask Claude to select the 10-15 most unique, globally underreported stories.
    Returns selected stories in priority order.
    """
    story_list = []
    for i, s in enumerate(stories):
        story_list.append({
            "index": i,
            "source_id": s.source_id,
            "country": s.country,
            "publication": s.publication,
            "headline": s.headline,
            "deckline": s.deckline[:300],
        })

    prompt = f"""You are the senior editor of "World's Front Page," a daily newsletter that surfaces front-page stories from around the world that haven't broken into global news feeds yet.

TODAY'S GLOBAL NEWS BASELINE (what readers already know):
{baseline_text}

YOUR TASK:
Review the front-page stories below from {len(stories)} publications worldwide.
Select {MIN_STORIES}–{MAX_STORIES} stories that best meet ALL of these criteria:

1. UNIQUE — Not already covered in the global baseline above
2. NATIONALLY SIGNIFICANT — Clearly a major story in its home country (front page = editors deemed it the day's most important story)
3. GLOBALLY RELEVANT — Has implications beyond its own borders, or reveals something meaningful about that country/region that the world should know
4. VARIED — No two stories from the same country; aim for geographic spread across regions
5. SUBSTANTIVE — Politics, economics, security, environment, justice, social upheaval. Not sports or celebrity unless it has genuine geopolitical/social weight.

ALSO: If the front page of a state media organ (like People's Daily, Granma, Global Times) leads with something unusual or telling about that government's current priorities or anxieties, that itself IS the story — select it.

Return ONLY a JSON array of selected story indices in priority order (most important first):
{{"selected": [3, 12, 7, ...]}}

Stories to evaluate:
{json.dumps(story_list, ensure_ascii=False, indent=2)}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)
        indices = result.get("selected", [])[:MAX_STORIES]
        selected = [stories[i] for i in indices if i < len(stories)]
        logger.info(f"Selected {len(selected)} stories")
        return selected
    except Exception as e:
        logger.warning(f"Selection parse error: {e} — returning first {MAX_STORIES} valid stories")
        return stories[:MAX_STORIES]


def _write_briefs(stories: list[ScrapedStory], baseline_text: str) -> list[dict]:
    """Write a punchy brief for each selected story."""
    results = []
    for s in stories:
        brief = _write_single_brief(s)
        results.append(brief)
    return results


def _write_single_brief(s: ScrapedStory) -> dict:
    """Write a 3-sentence brief + why-it-matters for a single story."""
    prompt = f"""You are writing for "World's Front Page," a daily newsletter for smart, globally curious American readers who want to know what's front-page news in other countries — stories they probably haven't seen yet.

STORY SOURCE:
- Publication: {s.publication} ({s.country})
- Headline: {s.headline}
- Deckline/summary: {s.deckline}
- Additional context: {s.lede}

WRITE:
1. A BRIEF (3 sentences max): What happened. Key facts. Who's involved and what's at stake. Be specific and direct — this is a briefing, not a feature. No fluff, no hedging.
2. A WHY IT MATTERS line (1 sentence): Who beyond {s.country}'s borders should care about this and why. Be concrete — name the geopolitical, economic, or humanitarian stakes.

TONE: Authoritative. Clear. Like a senior foreign correspondent's one-paragraph cable. No "in a significant development" or "according to reports." Just the news.

Return ONLY JSON:
{{
  "brief": "...",
  "why_it_matters": "..."
}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)
    except Exception as e:
        logger.warning(f"Brief parse error for {s.source_id}: {e}")
        result = {
            "brief": f"{s.headline}. {s.deckline}".strip(),
            "why_it_matters": "",
        }

    # Add status label if applicable
    status_label = STATUS_LABELS.get(
        # We need to look this up from sources — passed in via source dict later
        # For now, placeholder — resolved in publisher
        "",
        "",
    )

    return {
        "source_id": s.source_id,
        "country": s.country,
        "publication": s.publication,
        "article_url": s.article_url or s.url,
        "original_headline": s.headline,
        "brief": result.get("brief", ""),
        "why_it_matters": result.get("why_it_matters", ""),
        "status_label": status_label,
    }
