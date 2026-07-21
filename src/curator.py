"""
World's Front Page — LLM Curation Layer
Uses Claude API to:
1. Filter out wire-service-sourced and syndicated/duplicated stories
2. Translate non-English content
3. Screen for sufficient concrete information to brief on
4. Rank a buffer of candidate stories for uniqueness/significance
5. Write a 3-sentence brief per story, walking the ranked buffer until
   enough good ones are written
6. Add "why it matters" framing

v2 additions (2026-07-13) — in response to four recurring failure modes
seen in production output:
  - MODEL SPLIT RESTORED: selection now runs on Sonnet (SELECTION_MODEL),
    not Haiku. Selection is the single hardest reasoning task in the
    pipeline — holding ~150 candidates and a baseline/history context in
    view and making nuanced global-saturation judgments — and running it
    on Haiku was an intentional cost-saving call made at some point during
    the rewrite. Checked current API pricing: Sonnet's introductory rate is
    roughly 2x Haiku's, and this is a single daily call over ~25-30k input
    tokens — a few cents/day difference, not worth the quality tradeoff.
  - WIRE-SERVICE EXCLUSION: this newsletter exists to show off local
    reportage outlets actually commit resources to, not AP/Reuters/AFP/
    Bloomberg copy republished under a local masthead. Filtered in two
    passes (pre- and post-translation) via scraper.detect_wire_service(),
    plus a third pass after article-text fetch, right before brief-writing,
    since dateline attribution often only appears in full article text.
  - SYNDICATION CLUSTERING: cheap lexical (Jaccard word-overlap) clustering
    across all candidate headlines. A cluster of near-identical headlines
    across several countries is a strong signal of blanket global coverage
    (or wire copy the regex missed) — clusters at or above CLUSTER_CUTOFF
    are dropped entirely, independent of the baseline-comparison check.
  - SUFFICIENCY SCREENING: a batched pre-selection call asks whether each
    story has enough concrete information to support a real brief, so
    stories that are just a bare decree number or a publication's own
    self-description get dropped before a slot is spent on them, instead
    of surfacing as an unreadable brief downstream.
  - BUFFER-BASED SELECTION: _select_stories now returns a ranked buffer of
    up to SELECTION_BUFFER candidates (not a fixed 10-15). The brief-writing
    step walks that buffer, skipping anything that turns out insufficient,
    wire-sourced, or a model refusal on closer inspection, stopping at
    MAX_STORIES good briefs or an exhausted buffer. There is deliberately
    no hard floor — a thin news day produces fewer, better stories rather
    than the same count padded with filler.
  - RECENT-COVERAGE AWARENESS: curate() now actually accepts and uses the
    `recent_coverage` argument main.py has been passing in — the two were
    out of sync (main.py already called curate(..., recent_coverage=...)
    against a curate() that didn't accept the kwarg, which would have
    raised TypeError on the next real run regardless of anything else here).
    History is now folded into the selection prompt alongside the baseline,
    so a slow-burn story is less likely to repeat on consecutive days.
v3 additions (same day) — a second real story slipped through the exact
same pattern the v2 tie-break rule was meant to catch (a French paper's
foreign-desk report on a Ukrainian cabinet dismissal, no French stake at
all), which showed prose-only guidance wasn't reliable enough on its own:
  - EXPANDED BASELINE: sources.py now also scrapes Reuters, AP, BBC News,
    and Bloomberg as comparison-only baseline sources (never publishable —
    same treatment as the original 5). The old 5-source baseline could go
    quiet on a globally huge story if NYT/WSJ's known bot-blocking issue
    hit that morning; wire-agency front pages are a more resilient proxy
    for "is this already blanket-known."
  - STRUCTURED LOCALIZATION SCORE: _screen_stories() (formerly
    _screen_sufficiency) now also emits a 1-5 localization_score per
    story — how directly it concerns the SOURCE'S OWN country (compared
    against sources.py's assigned country field, which correctly credits
    an exile outlet like Meduza for Russia rather than wherever it
    physically operates) rather than being a bystander report on someone
    else's news. This replaces asking the selection model to infer that
    distinction unaided from prose alone. Score 1 (zero connection) is
    hard-excluded pre-selection; scores 2-5 pass through as a ranking
    signal only, specifically so a story like "the US imposes tariffs
    targeting Brazil" (Brazil is a direct target, not a bystander — score
    4) doesn't get caught in the same net as the Hormuz/Fedorov cases.

  - REFUSAL-TEXT LEAK FIX: previously, if the brief-writing model returned
    syntactically valid JSON containing a refusal sentence in the "brief"
    field (e.g. "I cannot write this brief — the article text is
    unavailable..."), no exception fired and that refusal text got
    published verbatim as if it were real copy. Fixed two ways: (1) the
    brief prompt now has an explicit insufficient-information escape hatch
    that returns {"insufficient": true} instead of prose, and (2) a regex
    safety net scans returned brief/why-it-matters text for refusal
    language as a backstop for whichever model doesn't reliably follow (1).

v4 fix (2026-07-21) — a fifth resp.content[0].text call site (this time
in _select_stories, running on SELECTION_MODEL) hit the same ThinkingBlock-
before-TextBlock failure mode already fixed once in frontpage_selector.py.
That fix was never applied here, so all four Claude API call sites in this
file were carrying the identical latent bug — only the selection one had
happened to trip it so far. Replaced every resp.content[0].text.strip()
with a shared _extract_text(resp) helper that finds the actual text block
by type instead of assuming position. See _extract_text() below.

v5 fix (same day, next run) — the v4 fix stopped the crash but exposed
the real root cause underneath it: _extract_text() correctly found NO
text block at all in the selection response, because claude-sonnet-5 runs
adaptive thinking at effort=high by default whenever a request omits a
thinking field, and the call's max_tokens=800 wasn't enough headroom for
that thinking plus the actual JSON output -- the model spent the entire
budget reasoning and hit max_tokens before writing a single output
character. Practically, this means selection has been silently no-op'ing
(falling back to raw candidate order) since the v2 model-split-to-Sonnet
change, regardless of the v4 fix -- explains why the same handful of
countries kept winning run after run, independent of everything else in
this file. Fixed by raising _select_stories' max_tokens to 4000 and
setting output_config={"effort": "medium"} explicitly rather than relying
on the high default, per Anthropic's current Sonnet 5 guidance.
"""

import os
import re
import json
import logging
from anthropic import Anthropic
from scraper import ScrapedStory, fetch_article_text, detect_wire_service
from sources import STATUS_LABELS  # noqa: F401 — kept for callers that label by source_id
from publisher import HISTORY_DAYS

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-haiku-4-5-20251001"       # translation, sufficiency screening, briefs
SELECTION_MODEL = "claude-sonnet-5"       # selection only — the hardest reasoning task here

MAX_STORIES = 15          # target number of briefs to actually write
MIN_STORIES = 8           # soft guidance only, logged if missed — NOT enforced, see docstring
SELECTION_BUFFER = 25     # ranked candidates returned by selection, walked by brief-writing

CLUSTER_SIMILARITY_THRESHOLD = 0.5   # Jaccard word-overlap to count as "same story"
CLUSTER_SIZE_CUTOFF = 4              # cluster this size or larger gets dropped entirely
# Both of the above are provisional starting values, not tuned against real
# output yet — worth revisiting once a couple weeks of cluster logs exist.

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with",
    "at", "by", "from", "is", "as", "after", "over", "amid", "into", "its",
    "it", "his", "her", "their", "this", "that", "will", "has", "have",
}

REFUSAL_MARKERS = [
    "i cannot", "i can't", "unable to", "cannot write", "cannot provide",
    "does not contain", "do not contain", "not contain specific news",
    "insufficient information", "no article text", "text is unavailable",
    "unable to provide", "cannot accurately", "only a headline",
    "not contain enough", "lacks enough",
]


def _extract_text(resp) -> str:
    """Find the actual text response block, rather than assuming
    resp.content[0] is always it. Confirmed live: Sonnet (and, as of
    2026-07-21, apparently other models in this rotation too) can return a
    ThinkingBlock before the TextBlock even without extended thinking
    explicitly requested, and content[0].text then doesn't exist. Every
    Claude API call site in this file should route through here instead of
    touching resp.content[0] directly."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    raise ValueError(f"No text block found in response content: {resp.content!r}")


def curate(stories: list[ScrapedStory], baselines: list[ScrapedStory],
           recent_coverage: list[dict] = None) -> list[dict]:
    """
    Full curation pipeline.
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

    # ── Wire-service exclusion, pass 1 (original-language teaser text) ─────
    valid = _filter_wire_service(valid, stage="pre-translation")
    if not valid:
        raise ValueError("All valid stories were wire-service-sourced — aborting.")

    baseline_text = _build_baseline_context(baselines)
    logger.info(f"Baseline context built from {len(baselines)} sources")
    history_text = _build_history_context(recent_coverage or [])
    logger.info(f"History context built from {len(recent_coverage or [])} recent entries")

    valid = _translate_batch(valid)

    # ── Wire-service exclusion, pass 2 (post-translation) ──────────────────
    # Catches attribution only legible after translation — e.g. a
    # transliterated or non-Latin-script mention of a wire service.
    valid = _filter_wire_service(valid, stage="post-translation")
    if not valid:
        raise ValueError("All translated stories were wire-service-sourced — aborting.")

    # ── Cross-source syndication clustering ─────────────────────────────────
    valid = _cluster_and_filter_syndicated(valid)
    if not valid:
        raise ValueError("All stories were dropped as syndicated/duplicated — aborting.")

    # ── Sufficiency + localization screen ───────────────────────────────────
    valid = _screen_stories(valid)
    if not valid:
        raise ValueError("No stories survived the sufficiency/localization screen — aborting.")

    ranked = _select_stories(valid, baseline_text, history_text)
    if not ranked:
        logger.warning("LLM returned empty selection — falling back to first candidates")
        ranked = valid[:SELECTION_BUFFER]

    logger.info(f"Writing briefs from a ranked buffer of {len(ranked)} candidates (target {MAX_STORIES})...")
    briefed = _write_briefs(ranked, target=MAX_STORIES)
    logger.info(f"Briefs written: {len(briefed)} (target {MAX_STORIES}, usual guidance floor {MIN_STORIES})")
    if len(briefed) < MIN_STORIES:
        logger.info(f"Below the usual {MIN_STORIES}-story guidance today — "
                    f"publishing {len(briefed)} rather than padding with weak stories.")

    return briefed


def _build_baseline_context(baselines: list[ScrapedStory]) -> str:
    """Summarize baseline headlines into a global news context string."""
    lines = []
    for b in baselines:
        if b.headline:
            lines.append(f"[{b.publication}]: {b.headline}")
    return "\n".join(lines)


def _build_history_context(recent_coverage: list[dict]) -> str:
    """Summarize the last several days of published stories so the
    selection model can avoid re-running the same underlying story on
    consecutive days. Entries come from publisher.load_history()."""
    if not recent_coverage:
        return "(no recent coverage history available)"
    lines = [
        f"- [{h.get('date', '')}] {h.get('country', '')} — {h.get('publication', '')}: {h.get('headline', '')}"
        for h in recent_coverage
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Wire-service exclusion
# ─────────────────────────────────────────────────────────────────────────────

def _filter_wire_service(stories: list[ScrapedStory], stage: str = "") -> list[ScrapedStory]:
    """Drop any story whose current headline/deckline/lede text carries a
    wire-service attribution marker. Recomputed fresh at each call (rather
    than trusting the wire_service flag set once at scrape time) since
    translation changes the text being checked."""
    kept, dropped = [], []
    for s in stories:
        if detect_wire_service(s.headline, s.deckline, s.lede):
            dropped.append(s)
        else:
            kept.append(s)
    if dropped:
        label = f" ({stage})" if stage else ""
        logger.info(f"Dropped {len(dropped)} wire-service-sourced stories{label}: "
                    f"{[d.publication for d in dropped]}")
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Cross-source syndication clustering
# ─────────────────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _cluster_and_filter_syndicated(
    stories: list[ScrapedStory],
    threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
    cutoff: int = CLUSTER_SIZE_CUTOFF,
) -> list[ScrapedStory]:
    """
    Group stories whose (translated) headlines are near-duplicates by word
    overlap — a strong signal of shared wire copy the regex filter missed,
    or a globally saturated event independently picked up across many
    front pages. Any cluster at or above `cutoff` size is dropped in full.
    Pure lexical/local computation — no API cost.
    """
    n = len(stories)
    token_sets = [_tokenize(s.headline) for s in stories]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(token_sets[i], token_sets[j]) >= threshold:
                union(i, j)

    clusters: dict = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    dropped_indices = set()
    for root, members in clusters.items():
        if len(members) >= cutoff:
            dropped_indices.update(members)
            names = [stories[i].publication for i in members]
            logger.info(f"Dropped syndication cluster of {len(members)} near-duplicate "
                        f"headlines (likely the same underlying story across outlets): {names}")

    return [s for i, s in enumerate(stories) if i not in dropped_indices]


# ─────────────────────────────────────────────────────────────────────────────
# Translation
# ─────────────────────────────────────────────────────────────────────────────

def _translate_batch(stories: list[ScrapedStory]) -> list[ScrapedStory]:
    """Translate non-English stories in a single batched API call."""
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
        raw = _extract_text(resp)
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


# ─────────────────────────────────────────────────────────────────────────────
# Sufficiency + localization screening
# ─────────────────────────────────────────────────────────────────────────────

# A story rated 1 (zero connection to the source's own country) is the
# pattern behind two repeat failures: a Canadian paper's wire-style report
# on a Strait of Hormuz strike, and a French paper's foreign-desk report on
# a Ukrainian cabinet dismissal. Both had zero distinctive stake for the
# source's own country — they were just well-written foreign-desk copy.
# Scores 2-5 are NOT filtered here, only used as a ranking signal in
# selection, specifically so a story like "the US imposes tariffs
# specifically targeting Brazil" (Brazil is a direct, named target — score
# 4) doesn't get caught in the same net. Only the unambiguous "no
# connection at all" case is auto-dropped.
LOCALIZATION_HARD_EXCLUDE_SCORE = 1


def _screen_stories(stories: list[ScrapedStory]) -> list[ScrapedStory]:
    """
    Batched pre-selection screen producing two judgments per story:
      - sufficient: is there enough concrete information (a real,
        explainable event/decision/development) for a factual brief?
      - localization_score (1-5): how directly does this story concern the
        SOURCE'S OWN country (sources.py's assigned country — which
        correctly credits an exile outlet like Meduza for Russia, its
        assigned subject country, rather than wherever it physically
        operates) — as opposed to being a foreign-desk report on
        someone else's news with no distinctive local stake?
    Drops insufficient stories and stories scoring exactly
    LOCALIZATION_HARD_EXCLUDE_SCORE. Everything else passes through with
    its score attached, to be weighed (not filtered) during selection.
    """
    items = []
    for i, s in enumerate(stories):
        items.append({
            "index": i,
            "country": s.country,
            "headline": s.headline,
            "deckline": s.deckline[:300],
            "lede": s.lede[:300],
        })

    prompt = f"""You are screening candidate news items for a daily international briefing aimed at readers with zero prior context on any of these stories.

For each item, provide two judgments:

1. SUFFICIENT: Is there enough concrete information to write a factual 3-sentence brief — a real, explainable event, decision, or development? Mark false for anything too vague, fragmentary, or self-referential (e.g. a bare policy/decree number with no explanation of what it does, or a publication promoting its own newsletter).

2. LOCALIZATION_SCORE (1-5): How directly does this story concern or affect THIS ITEM'S OWN COUNTRY (the "country" field given for each item) — not just the world in general?
   5 = The story is fundamentally about this country's own people, government, institutions, or internal affairs.
   4 = The story concerns an external actor or event, but this country is a direct, specifically-named target, party, or beneficiary of it (e.g. tariffs imposed specifically on this country, a bilateral deal this country is signing, a foreign court ruling specifically about this country's citizens).
   3 = The story concerns a regional bloc or grouping this country belongs to, with real, specific impact on this country described (not just membership).
   2 = The story is primarily about a foreign country or global event, with this country's angle limited to secondary commentary, reaction quotes, or general analysis — no direct stake.
   1 = The story is essentially a foreign-desk report on another country's internal affairs, with no distinctive connection to this country at all.

Return ONLY a JSON array: [{{"index": N, "sufficient": true/false, "localization_score": 1-5}}, ...]
No preamble, no explanation, just the JSON array.

Items:
{json.dumps(items, ensure_ascii=False, indent=2)}"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _extract_text(resp)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        verdicts = json.loads(raw)
        verdict_map = {v["index"]: v for v in verdicts}

        kept, dropped_insufficient, dropped_foreign = [], [], []
        for i, s in enumerate(stories):
            v = verdict_map.get(i)
            if v is None:
                # No verdict returned for this index — fail open rather
                # than silently dropping a story the model just didn't rank.
                kept.append(s)
                continue
            if not v.get("sufficient", True):
                dropped_insufficient.append(s)
                continue
            score = v.get("localization_score", 3)
            if score == LOCALIZATION_HARD_EXCLUDE_SCORE:
                dropped_foreign.append(s)
                continue
            s.localization_score = score
            kept.append(s)

        if dropped_insufficient:
            logger.info(f"Sufficiency screen dropped {len(dropped_insufficient)} thin/promo stories: "
                        f"{[s.publication for s in dropped_insufficient]}")
        if dropped_foreign:
            logger.info(f"Localization screen dropped {len(dropped_foreign)} stories with zero "
                        f"connection to their source's own country: {[s.publication for s in dropped_foreign]}")
        return kept
    except Exception as e:
        logger.warning(f"Screening failed: {e} — skipping screen, passing all through unscored")
        return stories


# ─────────────────────────────────────────────────────────────────────────────
# Selection
# ─────────────────────────────────────────────────────────────────────────────

def _select_stories(stories: list[ScrapedStory], baseline_text: str, history_text: str) -> list[ScrapedStory]:
    """
    Ask Claude to rank a buffer of up to SELECTION_BUFFER candidate stories,
    most important first. Runs on SELECTION_MODEL (Sonnet) — this is the
    hardest reasoning task in the pipeline, weighing uniqueness against a
    baseline AND recent history, geographic spread, and significance
    simultaneously across ~150 candidates.
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
            "localization_score": getattr(s, "localization_score", 3),
        })

    prompt = f"""You are the senior editor of "World's Front Page," a daily newsletter that surfaces front-page stories from around the world that haven't broken into global news feeds yet — and specifically showcases local news outlets' OWN reporting, not wire-service copy.

Each story below already carries a localization_score (1-5, pre-computed) indicating how directly it concerns the source's OWN country:
  5 = fundamentally about this country's own affairs
  4 = an external event/actor, but this country is a direct, named target/party/beneficiary
  3 = a regional bloc this country belongs to, with specific described impact
  2 = mostly foreign news with only secondary local commentary or reaction
(Score-1 stories — zero connection to the source's own country — have already been removed entirely.)

TODAY'S GLOBAL NEWS BASELINE (what readers already know):
{baseline_text}

STORIES COVERED IN THE LAST {HISTORY_DAYS} DAYS (avoid re-running the same underlying story on consecutive days):
{history_text}

YOUR TASK:
Review the front-page stories below from {len(stories)} publications worldwide. (Wire-service-sourced and cross-source-duplicated stories have already been removed from this list.)
Rank as many stories as genuinely qualify — up to {SELECTION_BUFFER} — most important first, using ALL of these criteria:

1. UNIQUE — Not already covered in the global baseline above, and not a story already covered in the recent history above
2. LOCAL CONNECTION — Favor higher localization_score. A 4 or 5 should generally outrank a 2 unless the 2 is dramatically more nationally significant. A high score alone isn't sufficient on its own — the story still needs to clear the other criteria too — but a low score (2) should be treated as a real strike against a story, on par with a criterion failure, not a minor tiebreaker.
3. NATIONALLY SIGNIFICANT — front page = editors deemed it the day's most important story
4. GLOBALLY RELEVANT — has implications beyond its own borders, or reveals something meaningful about that country/region the world should know
5. VARIED — no two stories from the same country in the top ranks; aim for geographic spread across regions
6. SUBSTANTIVE — politics, economics, security, environment, justice, social upheaval. Not sports or celebrity unless it has genuine geopolitical/social weight.

TIE-BREAK RULE: When LOCAL CONNECTION and GLOBALLY RELEVANT conflict — i.e., a story is relevant mainly BECAUSE it's a huge global event with only a score of 2 for this particular source — LOCAL CONNECTION WINS. A story already dominating US front pages, told from a source with no distinctive stake in it, should rank low here regardless of its objective world importance; that's the entire premise of this newsletter. A high-scoring (4-5) multi-country story — e.g. a bilateral trade dispute where this country is the direct, named target — is a different case and should be judged on its merits, not suppressed.

ALSO: If a state media organ's front page leads with something unusual or telling about that government's current priorities or anxieties, that itself IS the story — rank it accordingly.

Return ONLY a JSON array of ranked story indices, most important first, up to {SELECTION_BUFFER} entries — fewer is fine if fewer genuinely qualify:
{{"selected": [3, 12, 7, ...]}}

Stories to evaluate:
{json.dumps(story_list, ensure_ascii=False, indent=2)}"""

    try:
        resp = client.messages.create(
            model=SELECTION_MODEL,
            max_tokens=4000,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _extract_text(resp)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        logger.info(f"Selection API response: {raw[:200]}")
        result = json.loads(raw)
        indices = result.get("selected", [])[:SELECTION_BUFFER]
        logger.info(f"LLM ranked indices: {indices}")
        selected = [stories[i] for i in indices if i < len(stories)]
        logger.info(f"Selection returned {len(selected)} ranked candidates")
        return selected
    except Exception as e:
        logger.warning(f"Selection failed: {e} — falling back to first {SELECTION_BUFFER} valid stories")
        return stories[:SELECTION_BUFFER]


# ─────────────────────────────────────────────────────────────────────────────
# Brief writing
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_refusal(text: str) -> bool:
    t = (text or "").lower()
    return any(marker in t for marker in REFUSAL_MARKERS)


def _write_briefs(ranked_stories: list[ScrapedStory], target: int) -> list[dict]:
    """
    Walk the ranked candidate buffer writing briefs, stopping once `target`
    good briefs are written or the buffer is exhausted. Skips (rather than
    publishes a degraded fallback for) anything that turns out wire-sourced,
    insufficient, or a model refusal on closer inspection — the buffer means
    there's always a next candidate to try instead of a dead slot.
    """
    results = []
    for s in ranked_stories:
        if len(results) >= target:
            break

        article_text = ""
        if s.article_url:
            try:
                article_text = fetch_article_text(s.article_url)
            except Exception as e:
                logger.info(f"  Article fetch failed for {s.publication}: {e}")

        # Wire-service pass 3: full article text, right before writing.
        # Dateline attribution frequently only appears in the body, not the
        # homepage teaser checked in curate()'s earlier passes.
        if detect_wire_service(s.headline, s.deckline, article_text):
            logger.info(f"  Skipped {s.publication}: wire-service attribution found in fetched article text")
            continue

        try:
            brief = _write_single_brief(s, article_text)
        except Exception as e:
            logger.warning(f"  Brief failed for {s.publication}: {e} — skipping, trying next candidate")
            continue

        if brief is None:
            logger.info(f"  Skipped {s.publication}: model flagged insufficient information")
            continue
        if _looks_like_refusal(brief.get("brief", "")) or _looks_like_refusal(brief.get("why_it_matters", "")):
            logger.info(f"  Skipped {s.publication}: refusal-pattern detected in brief text — treating as failure")
            continue

        results.append(brief)
        logger.info(f"  Brief written: [{s.country}] {s.headline[:60]}")

    return results


def _write_single_brief(s: ScrapedStory, article_text: str = "") -> dict | None:
    """Write a 3-sentence brief + why-it-matters for a single story, or
    return None if the model determines there isn't enough real information
    to work with. Uses fetched article text for grounding when available,
    falling back to the scraper's lede paragraph otherwise.
    Note: the Substack-facing status label (state organ / exile / etc.) is
    resolved in publisher.py from sources.py by source_id — not here."""
    context_block = article_text if article_text else s.lede

    prompt = f"""You are writing for "World's Front Page," a daily newsletter for smart, globally curious American readers who want to know what's front-page news in other countries — stories they probably haven't seen yet.

STORY SOURCE:
- Publication: {s.publication} ({s.country})
- Headline: {s.headline}
- Deckline/summary: {s.deckline}
- Additional context (fetched article text when available): {context_block}

If the information above is too thin, vague, or fragmentary to write a factual, comprehensible brief for a reader with zero context — for example, it only names a decree/policy/case number with no explanation of what it actually does, or it's just a publication's self-description — return ONLY this JSON and nothing else:
{{"insufficient": true}}

Otherwise, WRITE:
1. A BRIEF (3 sentences max): What happened. Key facts. Who's involved and what's at stake. Be specific and direct — this is a briefing, not a feature. No fluff, no hedging.
2. A WHY IT MATTERS line (1 sentence): Who beyond {s.country}'s borders should care about this and why. Be concrete — name the geopolitical, economic, or humanitarian stakes.

TONE: Authoritative. Clear. Like a senior foreign correspondent's one-paragraph cable. No "in a significant development" or "according to reports." Just the news.

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

    raw = _extract_text(resp)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    result = json.loads(raw)

    if result.get("insufficient"):
        return None

    return {
        "source_id": s.source_id,
        "country": s.country,
        "publication": s.publication,
        "article_url": s.article_url or s.url,
        "original_headline": s.headline,
        "brief": result.get("brief", ""),
        "why_it_matters": result.get("why_it_matters", ""),
    }
