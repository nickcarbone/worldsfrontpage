"""
World's Front Page — LLM Curation Layer
"""

import os
import json
import logging
from anthropic import Anthropic
from scraper import ScrapedStory
from sources import STATUS_LABELS

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-haiku-4-5-20251001"
MAX_STORIES = 15
MIN_STORIES = 8


def curate(stories: list[ScrapedStory], baselines: list[ScrapedStory]) -> list[dict]:
    valid = [s for s in stories if s.headline and not s.scrape_error]
    empty_headline = [s for s in stories if not s.headline and not s.scrape_error]
    errored = [s for s in stories if s.scrape_error]

    logger.info(f"Scrape results: {len(valid)} valid, {len(empty_headline)} empty headline, {len(errored)} errored")
    for s in errored[:5]:
        logger.info(f"  Error sample — {s.publication}: {s.scrape_error[:100]}")
    for s in empty_headline[:5]:
        logger.info(f"  Empty headline — {s.publication} ({s.country})")

    if not valid:
        logger.error("No valid stories — dumping all scrape results for diagnosis:")
        for s in stories:
            logger.error(f"  {s.publication}: headline='{s.headline[:60] if s.headline else ''}' error='{s.scrape_error or ''}'")
        raise ValueError("No valid stories scraped — aborting.")

    logger.info(f"Sample valid headlines:")
    for s in valid[:5]:
        logger.info(f"  [{s.publication}] {s.headline[:80]}")

    baseline_text = _build_baseline_context(baselines)
    logger.info(f"Baseline context built from {len(baselines)} sources")

    valid = _translate_batch(valid)
    selected = _select_stories(valid, baseline_text)

    if not selected:
        logger.warning("LLM returned empty selection — falling back to first valid stories")
        selected = valid[:MAX_STORIES]

    logger.info(f"Writing briefs for {len(selected)} stories...")
    briefed = _write_briefs(selected)
    logger.info(f"Briefs written: {len(briefed)}")

    return briefed


def _build_baseline_context(baselines: list[ScrapedStory]) -> str:
    lines = []
    for b in baselines:
        if b.headline:
            lines.append(f"[{b.publication}]: {b.headline}")
    return "\n".join(lines)


def _translate_batch(stories: list[ScrapedStory]) -> list[ScrapedStory]:
    to_translate = [s for s in stories if s.language_hint not in ("en",)]
    if not to_translate:
        logger.info("No translation needed — all stories in English")
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
No preamble, no explanation, just the JSON array.

Items to translate:
{json.dumps(items, ensure_ascii=False, indent=2)}"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        translations = json.loads(raw)
        trans_map = {t["index"]: t for t in translations}
        for i, s in enumerate(to_translate):
            if i in trans_map:
                s.headline = trans_map[i].get("headline", s.headline)
                s.deckline = trans_map[i].get("deckline", s.deckline)
        logger.info(f"Translation complete for {len(translations)} items")
    except Exception as e:
        logger.warning(f"Translation failed: {e} — using originals")

    return stories


def _select_stories(stories: list[ScrapedStory], baseline_text: str) -> list[ScrapedStory]:
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

    prompt = f"""You are the senior editor of "World's Front Page," a daily newsletter surfacing front-page stories from around the world that haven't broken into global news feeds yet.

TODAY'S GLOBAL NEWS BASELINE (what readers already know):
{baseline_text}

YOUR TASK:
Review the front-page stories below. Select {MIN_STORIES}-{MAX_STORIES} stories meeting these criteria:
1. UNIQUE — not already in the global baseline above
2. NATIONALLY SIGNIFICANT — front page means editors deemed it important
3. GLOBALLY RELEVANT — implications beyond its own borders
4. VARIED — no two stories from the same country
5. SUBSTANTIVE — politics, economics, security, environment, justice

If a state media organ (People's Daily, Granma, Global Times) leads with something revealing about that government's priorities, select it.

IMPORTANT: You must select at least {MIN_STORIES} stories. If stories seem globally known, select the most locally unique ones anyway — our readers want to see what's front page in each country regardless.

Return ONLY this JSON, nothing else:
{{"selected": [0, 5, 12, 3, 8, 15, 22, 7]}}

Stories:
{json.dumps(story_list, ensure_ascii=False, indent=2)}"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        logger.info(f"Selection API response: {raw[:200]}")
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)
        indices = result.get("selected", [])[:MAX_STORIES]
        logger.info(f"LLM selected indices: {indices}")
        selected = [stories[i] for i in indices if i < len(stories)]
        logger.info(f"Selected {len(selected)} stories")
        return selected
    except Exception as e:
        logger.warning(f"Selection failed: {e} — falling back to first {MAX_STORIES} stories")
        return stories[:MAX_STORIES]


def _write_briefs(stories: list[ScrapedStory]) -> list[dict]:
    results = []
    for s in stories:
        try:
            brief = _write_single_brief(s)
            results.append(brief)
            logger.info(f"  Brief written: [{s.country}] {s.headline[:60]}")
        except Exception as e:
            logger.warning(f"  Brief failed for {s.publication}: {e} — using headline fallback")
            results.append({
                "source_id": s.source_id,
                "country": s.country,
                "publication": s.publication,
                "article_url": s.article_url or s.url,
                "original_headline": s.headline,
                "brief": f"{s.headline}. {s.deckline}".strip(),
                "why_it_matters": "",
                "status_label": "",
            })
    return results


def _write_single_brief(s: ScrapedStory) -> dict:
    prompt = f"""You are writing for "World's Front Page," a daily newsletter for globally curious American readers.

STORY:
- Publication: {s.publication} ({s.country})
- Headline: {s.headline}
- Summary: {s.deckline}
- Context: {s.lede}

Write:
1. BRIEF (3 sentences max): What happened, key facts, who's involved, what's at stake. Direct and specific. No hedging.
2. WHY IT MATTERS (1 sentence): Who beyond {s.country} should care and why. Name the concrete stakes.

Tone: Senior foreign correspondent's cable. No "in a significant development." Just the news.

Return ONLY this JSON, nothing else:
{{
  "brief": "...",
  "why_it_matters": "..."
}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    result = json.loads(raw)

    return {
        "source_id": s.source_id,
        "country": s.country,
        "publication": s.publication,
        "article_url": s.article_url or s.url,
        "original_headline": s.headline,
        "brief": result.get("brief", ""),
        "why_it_matters": result.get("why_it_matters", ""),
        "status_label": "",
    }
