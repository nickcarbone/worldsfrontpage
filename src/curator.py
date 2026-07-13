"""
World's Front Page — LLM Curation Layer

Model split (v2):
  - SELECTION runs on Sonnet. It's one call per day and it's the editorial
    judgment that defines the entire issue — not the place to economize.
  - Translation and brief-writing (mechanical, high-volume) stay on Haiku.

Grounding (v2):
  - After selection, each story's article page is fetched and the brief is
    written from actual source text with explicit only-stated-facts rules.
    Briefs written from a headline + truncated deckline with instructions
    to "be specific" were a hallucination machine.

Memory (v2):
  - The selection prompt receives the last 7 days of published stories so
    a slow-burn story doesn't lead the newsletter three days straight.

Candidates (v2):
  - Each source now supplies up to 5 candidate headlines. The model picks
    both the source AND the candidate, recovering from scraper mistakes
    and reaching past the mechanical "first headline found."
"""

from __future__ import annotations  # lets `list[dict] | None` etc. run on Python < 3.10

import os
import json
import logging
from anthropic import Anthropic
from scraper import ScrapedStory, Candidate, fetch_article_text
from sources import SOURCES, STATUS_LABELS  # noqa: F401 — STATUS_LABELS kept for callers

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL_FAST = "claude-haiku-4-5-20251001"   # translation + briefs
MODEL_SELECT = "claude-sonnet-4-6"          # story selection — the editorial call
MAX_STORIES = 15
MIN_STORIES = 8
TRANSLATE_CHUNK_SIZE = 20                   # items per translation call — one big
                                            # call blew past max_tokens, truncated
                                            # the JSON, and silently fell back to
                                            # untranslated originals

_SOURCE_STATUS = {s["id"]: s["status"] for s in SOURCES}


def curate(stories: list[ScrapedStory], baselines: list[ScrapedStory],
           recent_coverage: list[dict] | None = None) -> list[dict]:
    """
    Full curation pipeline.
    recent_coverage: list of {date, country, publication, headline} dicts
    from the last 7 days of published editions (see publisher.load_history).
    Returns list of ready-to-publish story dicts.
    """
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

    logger.info("Sample valid headlines:")
    for s in valid[:5]:
        logger.info(f"  [{s.publication}] {s.headline[:80]}")

    baseline_text = _build_baseline_context(baselines)
    logger.info(f"Baseline context built from {len(baselines)} sources")

    recent_text = _build_recent_context(recent_coverage or [])

    valid = _translate_batch(valid)
    selected = _select_stories(valid, baseline_text, recent_text)

    if not selected:
        logger.warning("LLM returned empty selection — falling back to first valid stories (country-deduped)")
        selected = _dedupe_by_country(valid)[:MAX_STORIES]

    logger.info(f"Writing briefs for {len(selected)} stories...")
    briefed = _write_briefs(selected)
    logger.info(f"Briefs written: {len(briefed)}")

    return briefed


# ─────────────────────────────────────────────────────────────────────────────
# Context builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_baseline_context(baselines: list[ScrapedStory]) -> str:
    """Summarize baseline headlines into a global news context string."""
    lines = []
    for b in baselines:
        if b.headline:
            lines.append(f"[{b.publication}]: {b.headline}")
    return "\n".join(lines)


def _build_recent_context(recent: list[dict]) -> str:
    """Format the last 7 days of published stories for the selection prompt."""
    if not recent:
        return "None — no recent editions on record."
    lines = []
    for h in recent[-120:]:  # hard cap; ~15 stories/day * 7 days
        lines.append(f"- {h.get('date', '?')} [{h.get('country', '?')}] {h.get('headline', '')[:120]}")
    return "\n".join(lines)


def _candidates_of(s: ScrapedStory) -> list[Candidate]:
    """Candidate list, synthesizing one from the primary fields if the
    scraper predates multi-candidate extraction or found only the og fallback."""
    if s.candidates:
        return s.candidates
    return [Candidate(headline=s.headline, deckline=s.deckline, article_url=s.article_url)]


def _dedupe_by_country(stories: list[ScrapedStory]) -> list[ScrapedStory]:
    seen, out = set(), []
    for s in stories:
        if s.country not in seen:
            seen.add(s.country)
            out.append(s)
    return out


def _parse_json_response(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Translation (Haiku, chunked)
# ─────────────────────────────────────────────────────────────────────────────

def _translate_batch(stories: list[ScrapedStory]) -> list[ScrapedStory]:
    """Translate non-English stories in chunked API calls. Also translates
    alternate candidate headlines so selection sees them in English."""
    to_translate = [s for s in stories if s.language_hint not in ("en",)]
    if not to_translate:
        logger.info("No translation needed — all stories in English")
        return stories

    logger.info(f"Translating {len(to_translate)} non-English stories "
                f"in chunks of {TRANSLATE_CHUNK_SIZE}...")

    translated_count = 0
    for start in range(0, len(to_translate), TRANSLATE_CHUNK_SIZE):
        chunk = to_translate[start:start + TRANSLATE_CHUNK_SIZE]
        items = []
        for i, s in enumerate(chunk):
            cands = _candidates_of(s)
            items.append({
                "index": i,
                "language": s.language_hint,
                "headline": s.headline,
                "deckline": s.deckline,
                "alt_headlines": [c.headline for c in cands[1:]],
            })

        prompt = f"""You are a professional news translator. Translate each item to English.
Preserve journalistic tone and meaning precisely. Do not summarize or editorialize.
Return ONLY a JSON array with objects:
{{"index": N, "headline": "...", "deckline": "...", "alt_headlines": ["...", ...]}}
Keep alt_headlines in the same order and length as given. No preamble, no explanation, just the JSON array.

Items to translate:
{json.dumps(items, ensure_ascii=False, indent=2)}"""

        try:
            resp = client.messages.create(
                model=MODEL_FAST,
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            translations = _parse_json_response(resp.content[0].text)
            trans_map = {t["index"]: t for t in translations}
            for i, s in enumerate(chunk):
                t = trans_map.get(i)
                if not t:
                    continue
                s.headline = t.get("headline", s.headline)
                s.deckline = t.get("deckline", s.deckline)
                cands = _candidates_of(s)
                if s.candidates:
                    cands[0].headline = s.headline
                    cands[0].deckline = s.deckline
                    alts = t.get("alt_headlines", [])
                    for c, alt in zip(cands[1:], alts):
                        if alt:
                            c.headline = alt
                translated_count += 1
        except Exception as e:
            logger.warning(f"Translation chunk {start}-{start + len(chunk)} failed: {e} — "
                           f"using originals for that chunk")

    logger.info(f"Translation complete for {translated_count}/{len(to_translate)} stories")
    return stories


# ─────────────────────────────────────────────────────────────────────────────
# Selection (Sonnet)
# ─────────────────────────────────────────────────────────────────────────────

def _select_stories(stories: list[ScrapedStory], baseline_text: str,
                    recent_text: str) -> list[ScrapedStory]:
    """
    Ask Sonnet to select the 8-15 most unique, globally underreported
    stories — choosing both the source and WHICH candidate headline is the
    real story. Returns selected stories in priority order, with the chosen
    candidate promoted onto the story object.
    """
    story_list = []
    for i, s in enumerate(stories):
        cands = _candidates_of(s)
        cand_entries = []
        for j, c in enumerate(cands):
            entry = {"c": j, "headline": c.headline}
            if j == 0 and (c.deckline or s.deckline):
                entry["deckline"] = (c.deckline or s.deckline)[:300]
            cand_entries.append(entry)
        story_list.append({
            "index": i,
            "country": s.country,
            "publication": s.publication,
            "status": _SOURCE_STATUS.get(s.source_id, "independent"),
            "candidates": cand_entries,
        })

    prompt = f"""You are the senior editor of "World's Front Page," a daily newsletter that surfaces front-page stories from around the world that haven't broken into global news feeds yet.

TODAY'S GLOBAL NEWS BASELINE (what readers already know):
{baseline_text}

RECENTLY COVERED IN THIS NEWSLETTER (last 7 days — do NOT reselect these stories or minor follow-ups; a genuinely major NEW development in the same saga is fine):
{recent_text}

YOUR TASK:
Review the front-page stories below from {len(stories)} publications worldwide.
Each publication lists up to 5 candidate headlines in the order they appeared on the page. Candidate 0 is our scraper's best guess at the lead story, but the scraper can be wrong — use your judgment about which candidate is actually the day's most significant story for that country.

Select {MIN_STORIES}–{MAX_STORIES} stories that best meet ALL of these criteria:

1. UNIQUE — Not already covered in the global baseline above, and not covered by this newsletter in the last 7 days
2. NATIONALLY SIGNIFICANT — Clearly a major story in its home country (front page = editors deemed it the day's most important story)
3. GLOBALLY RELEVANT — Has implications beyond its own borders, or reveals something meaningful about that country/region that the world should know
4. VARIED — No two stories from the same country; aim for geographic spread across regions
5. SUBSTANTIVE — Politics, economics, security, environment, justice, social upheaval. Not sports or celebrity unless it has genuine geopolitical/social weight.

ALSO: If the front page of a state media organ (status "state_controlled", like People's Daily, Granma, Global Times) leads with something unusual or telling about that government's current priorities or anxieties, that itself IS the story — select it.

IMPORTANT: You must select at least {MIN_STORIES} stories. If stories seem globally known, select the most locally unique ones anyway — our readers want to see what's front page in each country regardless.

Return ONLY this JSON, in priority order (most important first), nothing else:
{{"selected": [{{"index": 3, "candidate": 0}}, {{"index": 12, "candidate": 2}}, ...]}}

Stories to evaluate:
{json.dumps(story_list, ensure_ascii=False, indent=2)}"""

    try:
        resp = client.messages.create(
            model=MODEL_SELECT,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        logger.info(f"Selection API response: {raw[:200]}")
        result = _parse_json_response(raw)
        picks = result.get("selected", [])[:MAX_STORIES]

        selected, seen_countries = [], set()
        for pick in picks:
            # Accept both {"index": N, "candidate": M} and bare-int legacy form
            if isinstance(pick, dict):
                idx, cand_idx = pick.get("index"), pick.get("candidate", 0)
            else:
                idx, cand_idx = pick, 0
            if idx is None or not (0 <= idx < len(stories)):
                continue
            story = stories[idx]
            if story.country in seen_countries:  # belt-and-suspenders dedupe
                continue
            seen_countries.add(story.country)

            # Promote the chosen candidate onto the story object
            cands = _candidates_of(story)
            if 0 < cand_idx < len(cands):
                c = cands[cand_idx]
                story.headline = c.headline
                story.article_url = c.article_url or story.article_url
                story.deckline = c.deckline
                story.lede = ""
            selected.append(story)

        logger.info(f"Selected {len(selected)} stories "
                    f"({sum(1 for p in picks if isinstance(p, dict) and p.get('candidate', 0) > 0)} "
                    f"from non-primary candidates)")
        return selected
    except Exception as e:
        logger.warning(f"Selection failed: {e} — falling back to first "
                       f"{MAX_STORIES} valid stories (country-deduped)")
        return _dedupe_by_country(stories)[:MAX_STORIES]


# ─────────────────────────────────────────────────────────────────────────────
# Briefs (Haiku, grounded in fetched article text)
# ─────────────────────────────────────────────────────────────────────────────

def _write_briefs(stories: list[ScrapedStory]) -> list[dict]:
    """Write a grounded brief for each selected story. Fetches each story's
    article page first (~8–15 fetches/run) so the brief is written from real
    source text, not from a headline the model must 'be specific' about."""
    results = []
    grounded = 0
    for s in stories:
        article_text = fetch_article_text(s.article_url or "")
        if article_text:
            grounded += 1
        try:
            brief = _write_single_brief(s, article_text)
            results.append(brief)
            logger.info(f"  Brief written ({'grounded' if article_text else 'headline-only'}): "
                        f"[{s.country}] {s.headline[:60]}")
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
            })
    logger.info(f"Briefs grounded in article text: {grounded}/{len(stories)}")
    return results


def _write_single_brief(s: ScrapedStory, article_text: str = "") -> dict:
    """Write a brief + why-it-matters for a single story, grounded in the
    fetched article text. Note: the Substack-facing status label (state
    organ / exile / etc.) is resolved in publisher.py from sources.py by
    source_id — not here."""
    prompt = f"""You are writing for "World's Front Page," a daily newsletter for smart, globally curious American readers who want to know what's front-page news in other countries — stories they probably haven't seen yet.

SOURCE MATERIAL:
- Publication: {s.publication} ({s.country})
- Headline: {s.headline}
- Deckline/summary: {s.deckline or "(none)"}
- Additional context: {s.lede or "(none)"}
- Article text (may be partial or machine-scraped):
{article_text or "(article text unavailable — headline and deckline are your ONLY source material)"}

WRITE:
1. A BRIEF (3 sentences max): What happened. Key facts. Who's involved and what's at stake. Direct and declarative — this is a briefing, not a feature.
2. A WHY IT MATTERS line (1 sentence): Who beyond {s.country}'s borders should care about this and why.

GROUNDING RULES (these override everything else):
- Use ONLY facts stated in the source material above. Do not add names, numbers, dates, locations, or causal claims that are not in it.
- If the material supports only one or two sentences, write one or two. Shorter and accurate beats longer and invented.
- If you cannot say who beyond the country's borders should care WITHOUT inventing facts, return an empty string for why_it_matters.
- No "in a significant development," no "according to reports." Just the news, as far as the source material actually goes.

TONE: Authoritative. Clear. Like a senior foreign correspondent's one-paragraph cable.

Return ONLY this JSON, nothing else:
{{
  "brief": "...",
  "why_it_matters": "..."
}}"""

    resp = client.messages.create(
        model=MODEL_FAST,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    result = _parse_json_response(resp.content[0].text)

    return {
        "source_id": s.source_id,
        "country": s.country,
        "publication": s.publication,
        "article_url": s.article_url or s.url,
        "original_headline": s.headline,
        "brief": result.get("brief", ""),
        "why_it_matters": result.get("why_it_matters", ""),
    }
