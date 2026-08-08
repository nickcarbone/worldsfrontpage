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

v6 additions (2026-07-29) — the 2026-07-28 baseline rewrite (see
scraper.py) widened the baseline pool from 5 paywalled dailies to 10
sources including live wire/broadcast feeds, and that made a real
saturation failure visible for the first time: Le Monde's lead that day
(Macron visiting Bordeaux over a southwestern France wildfire) and the
BBC baseline's top item that same day (Gironde wildfires ahead of a
heatwave) were the same event. Checking why UNIQUE (criterion 1 below)
didn't already catch this exposed the same lesson already learned about
localization -- prose-only instructions aren't reliable, structural
signals are -- but never applied to the saturation axis. A lexical check
was tried first and rejected: Jaccard word-overlap between those two
headlines is 0.0 (france/french, wildfire/wildfires, and especially
bordeaux/gironde share no tokens at all -- Bordeaux is a city IN the
Gironde department, a relationship no string-overlap method can see).
That's exactly why _cluster_and_filter_syndicated's identical technique
doesn't catch this either: syndication clustering targets near-verbatim
wire copy, and saturation is close to the opposite case by design --
genuinely distinct local phrasing describing the same underlying event.
Only a model that knows the place-name relationship can judge that, so
this adds a SATURATION_SCORE to _screen_stories (now baseline-aware)
rather than a cheap heuristic, mirroring how localization_score already
works: a structured 1-5 field, ranking signal only (not hard-excluded --
see SATURATION_HARD_EXCLUDE_SCORE below for why), fed into
_select_stories alongside localization_score with its own explicit
tie-break rule.

Also this round: a fabrication caught in the 2026-07-28 SCMP brief (an
invented "$11 billion in H1 2024 cross-border biotech deals" statistic
grafted onto an HKU leadership-succession story that has nothing to do
with biotech) turned out NOT to be a scraping contamination bug -- the
actual fetched article text was clean, HKU-only, no such figure anywhere
in it. That means the brief-writing model introduced the figure from its
own training data, unprompted, to make the "why it matters" line sound
more consequential. _write_single_brief's prompt now explicitly forbids
introducing facts/figures not present in the given source material, and
_write_briefs adds a cheap grounding check: any dollar amount,
percentage, or large number in the written brief that doesn't also appear
in the headline/deckline/context given to the model is treated as a
fabrication risk and the brief is discarded (same skip-and-try-next-
candidate path already used for insufficient-information and refusal-text
cases). This is a blunt instrument -- it won't catch a fabricated claim
with no numbers in it -- but the specific failure observed was numeric,
and it's cheap grounding for exactly that pattern.

v7 rewrite (2026-08-01) — two consecutive single-story editions. The
supply side was healthy (40 front-page matches against a floor of 15);
the losses were all downstream, and all of the same kind: a stage lacking
the evidence to judge well, judging anyway, and dropping terminally.
40 candidates -> 14 screened -> 7 selected -> 1 published.

  - PREFETCH BEFORE SCREEN: the sufficiency screen rejected 22 of 40 real
    front-page stories (Reforma on sargassum, Folha/O Globo on the Lula
    inquiry, Corriere on Meloni's EU letter, SMH on a fatal hospital
    equipment fault) because it read 300 chars of homepage teaser. Article
    text was being fetched one stage LATER. Now fetched in parallel for all
    candidates, before the screen, and cached on the story.
  - SUFFICIENCY NARROWED TO BOILERPLATE: "enough information to brief on"
    is a depth-of-evidence judgment that belongs at brief-writing, where
    the full text and the front-page image are both available. Asking it
    twice with a terminal drop on either was double jeopardy. The screen
    now only rejects things that aren't stories: section labels, promos,
    cookie notices, legal pages.
  - REPAIR BEFORE DROPPING: an insufficient verdict now retries against the
    front-page image (retained by frontpage_selector rather than discarded)
    before abandoning the candidate.
  - FIGURES FLAGGED, NOT DROPPED: the numeric grounding guard validated
    against whatever grounding text survived the fetch, so a fetch failure
    became a fabrication accusation. It killed El Mundo's Ceuta lead over
    "50,000" — a figure corroborated elsewhere in the same day's corpus.
    Ungrounded figures now ride along as `flagged_figures` and surface as a
    deletable review marker in the Substack draft.
  - ENFORCED FLOOR + BACKFILL LADDER: MIN_STORIES was never enforced; it
    only logged. FLOOR_STORIES = 6 now triggers a tiered backfill over
    pools that were previously discarded. Wire exclusion, the localization
    score-1 hard exclude, and one-story-per-country are never relaxed at
    any tier.
"""

import os
import re
import json
import base64
import logging
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
from anthropic import Anthropic
from scraper import ScrapedStory, fetch_article_text, detect_wire_service
from sources import STATUS_LABELS  # noqa: F401 — kept for callers that label by source_id
from publisher import HISTORY_DAYS

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-haiku-4-5-20251001"       # translation, sufficiency screening, briefs
SELECTION_MODEL = "claude-sonnet-5"       # selection only — the hardest reasoning task here

MAX_STORIES = 15          # target number of briefs to actually write
FLOOR_STORIES = 6         # ENFORCED minimum — below this, the backfill ladder runs.
                          # Replaces the old MIN_STORIES = 8, which was never
                          # enforced anywhere; it only produced a log line while
                          # the run shipped whatever it happened to have.
SELECTION_BUFFER = 25     # ranked candidates returned by selection, walked by brief-writing

# If the backfill ladder exhausts every tier and STILL can't reach
# FLOOR_STORIES, publish what we have rather than aborting. An aborted run
# means no edition at all, which is strictly worse for subscribers than a
# short one. Flip to True only if you'd rather skip a day than run thin.
ABORT_BELOW_FLOOR = False

# Article text is now fetched for EVERY candidate before screening, not just
# for the handful of finalists at brief-writing time. Rationale (2026-08-01):
# the sufficiency screen was rejecting 22 of 40 real front-page stories
# because it was judging headline + 300 chars of deckline + 300 chars of
# lede — effectively testing whether the scraper got a clean grab, not
# whether a story exists. The evidence that resolves the question was being
# fetched one stage LATER, at brief-writing. This inverts that order.
# Cost: ~40 plain HTTP fetches (no API cost) and ~15k extra Haiku input
# tokens across the screen calls — roughly 1.5 cents a run.
PREFETCH_WORKERS = 8
SCREEN_ARTICLE_CHARS = 1200   # per-story article excerpt included in the screen prompt
SCREEN_BATCH_SIZE = 10        # stories per screening call — see _screen_stories

CLUSTER_SIMILARITY_THRESHOLD = 0.5   # Jaccard word-overlap to count as "same story"
CLUSTER_SIZE_CUTOFF = 4              # cluster this size or larger gets dropped entirely
# Both of the above are provisional starting values, not tuned against real
# output yet — worth revisiting once a couple weeks of cluster logs exist.

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with",
    "at", "by", "from", "is", "as", "after", "over", "amid", "into", "its",
    "it", "his", "her", "their", "this", "that", "will", "has", "have",
}

# Media types accepted by the vision path in _write_single_brief, mirroring
# frontpage_selector's own map — the bytes and content_type are cached on the
# ScrapedStory by apply_frontpage_selection.
_IMAGE_MEDIA_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
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


def _prefetch_article_text(stories: list[ScrapedStory]) -> None:
    """
    Fetch article body text for every candidate in parallel and cache it on
    the story as `.article_text`, so both the screen and (later) brief-
    writing judge the same real evidence instead of a truncated homepage
    teaser. Sequential fetching was fine at ~7 finalists; at ~40 candidates,
    with 20s timeouts and a reliable crop of 403s and dead hosts, it is not.

    Failures are cached as "" — a fetch failure is a fact about the fetch,
    NOT an editorial verdict, and nothing downstream may treat it as one.
    """
    targets = [s for s in stories if s.article_url]
    if not targets:
        return

    def _one(s: ScrapedStory):
        try:
            return s, fetch_article_text(s.article_url)
        except Exception as e:
            logger.info(f"  Article prefetch failed for {s.publication}: {e}")
            return s, ""

    got = 0
    with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
        for fut in as_completed([pool.submit(_one, s) for s in targets]):
            s, text = fut.result()
            s.article_text = text
            if text:
                got += 1

    logger.info(f"Article prefetch: {got}/{len(targets)} candidates returned usable body text "
                f"({len(targets) - got} paywalled, blocked, or empty — these are NOT dropped)")


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

    # ── Article prefetch, BEFORE screening ──────────────────────────────────
    # Order matters here and it used to be wrong. See _prefetch_article_text.
    _prefetch_article_text(valid)

    # ── Boilerplate + localization + saturation screen ──────────────────────
    valid, screen_rejects = _screen_stories(valid, baseline_text)
    if not valid:
        # Caught in smoke testing: if the screen rejects everything, the old
        # code raised here — even though the rejects are now a usable backfill
        # pool and brief-writing does its own, better-evidenced sufficiency
        # check. A screen that rejects 100% is far more likely to be
        # misfiring than to be right, so promote the rejects rather than
        # aborting the run on its say-so.
        if screen_rejects:
            logger.warning(f"Screen kept 0 of {len(screen_rejects)} stories — that is more "
                           f"likely a screen malfunction than a genuinely empty news day. "
                           f"Promoting all rejects and letting brief-writing judge them.")
            valid, screen_rejects = screen_rejects, []
        else:
            raise ValueError("No stories survived the boilerplate/localization screen — aborting.")

    ranked = _select_stories(valid, baseline_text, history_text)
    if not ranked:
        logger.warning("LLM returned empty selection — falling back to first candidates")
        ranked = valid[:SELECTION_BUFFER]

    logger.info(f"Writing briefs from a ranked buffer of {len(ranked)} candidates (target {MAX_STORIES})...")
    briefed = _write_briefs(ranked, target=MAX_STORIES)
    logger.info(f"Briefs written: {len(briefed)} (target {MAX_STORIES}, enforced floor {FLOOR_STORIES})")

    # ── Backfill ladder ─────────────────────────────────────────────────────
    # Only runs when the edition is genuinely short. Each tier is a strictly
    # weaker pool than the last, so a good day never touches this and a bad
    # day degrades in a known order rather than silently shipping one story.
    #
    # What is NEVER relaxed, at any tier: wire-service exclusion, the
    # localization score-1 hard exclude, and one-story-per-country. Those
    # three are the newsletter's premise, not tuning parameters.
    if len(briefed) < FLOOR_STORIES:
        used_ids = {b["source_id"] for b in briefed}
        used_countries = {b["country"] for b in briefed}
        # Identity, not dataclass equality: ScrapedStory is a plain @dataclass,
        # so `s in ranked` would do a full field-by-field compare including
        # candidate lists and cached image bytes.
        ranked_ids = {id(s) for s in ranked}

        tiers = [
            ("screened but not selected",
             [s for s in valid if s.source_id not in used_ids
              and id(s) not in ranked_ids]),
            ("screen-rejected (boilerplate call re-tested against full article text)",
             [s for s in screen_rejects if s.source_id not in used_ids]),
        ]

        for label, pool in tiers:
            if len(briefed) >= FLOOR_STORIES:
                break
            pool = [s for s in pool if s.country not in used_countries]
            if not pool:
                continue
            logger.info(f"Below floor ({len(briefed)}/{FLOOR_STORIES}) — backfilling from "
                        f"tier '{label}' ({len(pool)} candidates available)")
            extra = _write_briefs(
                pool,
                target=FLOOR_STORIES - len(briefed),
                allow_thin=True,
            )
            for b in extra:
                if b["country"] in used_countries:
                    continue
                briefed.append(b)
                used_countries.add(b["country"])
            logger.info(f"  Tier '{label}' contributed {len(extra)} brief(s)")

    if len(briefed) < FLOOR_STORIES:
        msg = (f"Backfill exhausted every tier and reached only {len(briefed)} "
               f"of {FLOOR_STORIES} stories.")
        if ABORT_BELOW_FLOOR:
            raise ValueError(msg + " ABORT_BELOW_FLOOR is set — aborting.")
        logger.warning(msg + " Publishing short rather than skipping the day.")

    flagged = sum(1 for b in briefed if b.get("flagged_figures"))
    if flagged:
        logger.warning(f"{flagged} brief(s) carry unverified numeric figures — "
                       f"REVIEW THESE IN THE DRAFT before publishing.")

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


def _screen_stories(stories: list[ScrapedStory],
                    baseline_text: str = "") -> tuple[list[ScrapedStory], list[ScrapedStory]]:
    """
    Pre-selection screen producing three judgments per story. Returns
    (kept, rejected) — rejected is no longer discarded, it becomes a
    backfill tier, because this screen has a demonstrated false-positive
    problem and a rejection here should not be terminal.

    2026-08-01 rewrite, after the screen dropped 22 of 40 stories in one
    run — among them Reforma on a 27-fold sargassum influx, Folha and O
    Globo on a second federal police inquiry into Lula's son, Corriere on
    Meloni's 22-leader letter to the EU, and the SMH on a fatal hospital
    equipment fault. Those are not thin stories; the screen simply could
    not see them, because it was reading 300 characters of homepage
    teaser. Two changes address that:

      - It now reads fetched article text (see _prefetch_article_text).
      - SUFFICIENT has been narrowed from "is there enough information to
        brief on" to "is this a news story at all." The former is a
        judgment about evidence depth that properly belongs at brief-
        writing, where the full text and the front-page image are both in
        hand; asking it twice, with a terminal drop on either, was
        double jeopardy. This stage now only catches things that are not
        stories: section labels, subscription prompts, cookie banners,
        contest promos, legal pages.

    The three judgments:
      - sufficient: is this an actual news story rather than site furniture?
      - localization_score (1-5): how directly does this story concern the
        SOURCE'S OWN country (sources.py's assigned country — which
        correctly credits an exile outlet like Meduza for Russia, its
        assigned subject country, rather than wherever it physically
        operates) — as opposed to being a foreign-desk report on
        someone else's news with no distinctive local stake?
      - saturation_score (1-5, added 2026-07-29): is this the SAME
        underlying event as something already in today's baseline, or
        does it add real information beyond it? A cheap lexical check
        (the same Jaccard technique _cluster_and_filter_syndicated uses)
        was tried and rejected here — it scored 0.0 similarity between
        Le Monde's "Macron visits Bordeaux over southwestern France
        wildfire" and the BBC baseline's "Gironde wildfires ahead of
        heatwave," the same event, because Bordeaux/Gironde and
        wildfire/wildfires share no literal tokens. Only a model that
        knows the place-name relationship can catch that, hence a scored
        LLM judgment rather than a heuristic.

    Drops insufficient stories and stories scoring exactly
    LOCALIZATION_HARD_EXCLUDE_SCORE. saturation_score is NEVER hard-
    excluded here — see the comment at its assignment above for why —
    it passes through as a ranking signal for _select_stories.
    """
    # Batched into chunks rather than one 40-story call. Same token cost,
    # marginally more latency, materially better attention per story — a
    # single call judging 40 items across ~60k characters judges each one
    # worse than four calls judging ten. Each chunk fails open independently,
    # so one malformed response no longer bypasses the screen for the whole
    # run (which the previous single-call structure did silently).
    kept: list[ScrapedStory] = []
    rejected: list[ScrapedStory] = []
    dropped_insufficient: list[ScrapedStory] = []
    dropped_foreign: list[ScrapedStory] = []

    for start in range(0, len(stories), SCREEN_BATCH_SIZE):
        chunk = stories[start:start + SCREEN_BATCH_SIZE]
        k, ins, foreign = _screen_batch(chunk, baseline_text)
        kept.extend(k)
        dropped_insufficient.extend(ins)
        dropped_foreign.extend(foreign)

    # Boilerplate rejects become a backfill tier; localization score-1
    # rejects do NOT — "no connection to the source's own country" is a
    # structural disqualifier, not a marginal call, and re-admitting those
    # would reintroduce exactly the foreign-desk copy this newsletter exists
    # to avoid. A thin day is not a reason to publish a French paper's
    # report on Ukraine.
    rejected = list(dropped_insufficient)

    dist = {}
    for s in kept:
        dist[s.localization_score] = dist.get(s.localization_score, 0) + 1
    logger.info(f"Localization distribution (kept): { {k: dist[k] for k in sorted(dist)} }")
    sat_dist = {}
    for s in kept:
        sat_dist[s.saturation_score] = sat_dist.get(s.saturation_score, 0) + 1
    logger.info(f"Saturation distribution (kept, 1=already known/5=fresh): "
                f"{ {k: sat_dist[k] for k in sorted(sat_dist)} }")
    for s in sorted(kept, key=lambda x: (x.localization_score, x.saturation_score)):
        logger.info(f"  loc={s.localization_score} sat={s.saturation_score} "
                    f"[{s.country}] {s.publication}: {s.headline[:70]}")
    if dropped_insufficient:
        logger.info(f"Boilerplate screen rejected {len(dropped_insufficient)} non-story items "
                    f"(retained as backfill tier 2): {[s.publication for s in dropped_insufficient]}")
    if dropped_foreign:
        logger.info(f"Localization screen dropped {len(dropped_foreign)} stories with zero "
                    f"connection to their source's own country (NOT retained): "
                    f"{[s.publication for s in dropped_foreign]}")

    return kept, rejected


def _screen_batch(stories: list[ScrapedStory], baseline_text: str):
    """Screen one chunk. Returns (kept, insufficient, foreign). Fails open
    on any error — an unparseable response must not silently delete a
    tenth of the day's candidates."""
    items = []
    for i, s in enumerate(stories):
        items.append({
            "index": i,
            "country": s.country,
            "headline": s.headline,
            "deckline": s.deckline[:300],
            "lede": s.lede[:300],
            "article_text": (getattr(s, "article_text", "") or "")[:SCREEN_ARTICLE_CHARS],
        })

    baseline_block = baseline_text or "(no baseline available today)"

    prompt = f"""You are screening candidate news items for a daily international briefing aimed at readers with zero prior context on any of these stories.

Each item includes an "article_text" field containing the opening paragraphs of the actual article where they could be retrieved. This field is EMPTY for many items — the outlet was paywalled, blocked the fetch, or returned nothing. An empty article_text tells you NOTHING about whether the story is real or worth covering; judge those items on their headline, deckline and lede exactly as you would if the field weren't there. Never mark an item down for having no article text.

TODAY'S GLOBAL NEWS BASELINE (wire/broadcast headlines — what readers already know from mainstream global coverage):
{baseline_block}

For each item, provide three judgments:

1. SUFFICIENT: Is this item AN ACTUAL NEWS STORY, as opposed to website furniture that the scraper picked up by mistake?
   Mark FALSE only for things that are not stories at all: section headings ("Sports", "Opinion"), subscription or paywall prompts, cookie/privacy notices, legal/imprint pages, newsletter self-promotion, reader contests, "download our app" promos, error pages, or a bare navigational label.
   Mark TRUE for any real reported event, decision, development, investigation, court ruling, policy change, disaster, or announcement — EVEN IF the information provided here is brief, partial, or lacks background. Thin sourcing is NOT a reason to mark false. A one-line report of a real event is still a real story, and a later stage decides whether there is enough material to brief on it.
   When genuinely unsure, mark TRUE.

2. LOCALIZATION_SCORE (1-5): How directly does this story concern or affect THIS ITEM'S OWN COUNTRY (the "country" field given for each item) — not just the world in general?
   5 = The story is fundamentally about this country's own people, government, institutions, or internal affairs.
   4 = The story concerns an external actor or event, but this country is a direct, specifically-named target, party, or beneficiary of it (e.g. tariffs imposed specifically on this country, a bilateral deal this country is signing, a foreign court ruling specifically about this country's citizens).
   3 = The story concerns a regional bloc or grouping this country belongs to, with real, specific impact on this country described (not just membership).
   2 = The story is primarily about a foreign country or global event, with this country's angle limited to secondary commentary, reaction quotes, or general analysis — no direct stake.
   1 = The story is essentially a foreign-desk report on another country's internal affairs, with no distinctive connection to this country at all.

3. SATURATION_SCORE (1-5): Does this item describe the SAME underlying event, decision, or development as something already in TODAY'S GLOBAL NEWS BASELINE above — even if worded completely differently, using different place names (e.g. a city vs. the region/department it's in), or covering it from a different national angle? Judge by the actual underlying event, not by word overlap.
   5 = Not reflected in the baseline at all — this is information the baseline sources aren't reporting.
   4 = The general subject or region is touched on in the baseline, but this item's specific facts/development are genuinely new.
   3 = The baseline covers the same broad event, and this item adds a real, distinct national angle (a specific decision, stake, or consequence for its own country) beyond what the baseline reports.
   2 = The baseline already covers this same specific event, and this item's local detail is mostly color or reaction rather than materially new information.
   1 = This item describes the same underlying event as something already in the baseline, with no additional information or angle beyond what a reader already knows from the baseline alone.

Return ONLY a JSON array: [{{"index": N, "sufficient": true/false, "localization_score": 1-5, "saturation_score": 1-5}}, ...]
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
                s.localization_score = getattr(s, "localization_score", 3)
                s.saturation_score = getattr(s, "saturation_score", 3)
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
            # SATURATION_SCORE (added 2026-07-29): NOT hard-excluded even at
            # its lowest value (1 = same underlying event as something
            # already in the baseline). Unlike localization's score-1 case
            # (zero connection to the source's own country, which is a
            # clean structural disqualifier), a low saturation score alone
            # doesn't tell you enough — the canonical counterexample is a
            # Brazilian paper covering US tariffs specifically targeting
            # Brazil: the underlying event IS globally known, but the
            # story still deserves a slot because of its direct, named
            # stake. That judgment needs localization_score in the loop
            # too, so it's left as a ranking signal for _select_stories'
            # explicit tie-break rule rather than filtered here.
            s.saturation_score = v.get("saturation_score", 3)
            kept.append(s)
        return kept, dropped_insufficient, dropped_foreign
    except Exception as e:
        logger.warning(f"Screening batch failed: {e} — passing this batch through unscored")
        for s in stories:
            s.localization_score = getattr(s, "localization_score", 3)
            s.saturation_score = getattr(s, "saturation_score", 3)
        return stories, [], []


# ─────────────────────────────────────────────────────────────────────────────
# Selection
# ─────────────────────────────────────────────────────────────────────────────
def _enforce_one_per_country(ranked: list[ScrapedStory]) -> list[ScrapedStory]:
    """One story per country per edition, enforced structurally rather than
    left to the selection prompt's criterion 5. Order is preserved, so the
    highest-ranked story from each country survives and later ones from that
    country are dropped -- which also means adding a SECOND outlet per
    country becomes a quality lever rather than redundancy: the selector can
    take the domestic story and discard the bystander one. On 2026-07-27
    Norway had exactly one outlet and it led on a French wildfire, so there
    was no alternative to fall back to."""
    seen, out, dropped = set(), [], []
    for s in ranked:
        if s.country in seen:
            dropped.append(f"{s.country}/{s.publication}")
            continue
        seen.add(s.country)
        out.append(s)
    if dropped:
        logger.info(f"One-per-country rule dropped {len(dropped)} lower-ranked "
                    f"duplicates: {dropped}")
    return out
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
            "saturation_score": getattr(s, "saturation_score", 3),
        })

    prompt = f"""You are the senior editor of "World's Front Page," a daily newsletter that surfaces front-page stories from around the world that haven't broken into global news feeds yet — and specifically showcases local news outlets' OWN reporting, not wire-service copy.

Each story below already carries two pre-computed 1-5 scores:

localization_score — how directly the story concerns the source's OWN country:
  5 = fundamentally about this country's own affairs
  4 = an external event/actor, but this country is a direct, named target/party/beneficiary
  3 = a regional bloc this country belongs to, with specific described impact
  2 = mostly foreign news with only secondary local commentary or reaction
(Score-1 stories — zero connection to the source's own country — have already been removed entirely.)

saturation_score — whether the story is the SAME underlying event as something already in today's global baseline, judged by the actual event, not by wording:
  5 = not reflected in the baseline at all
  4 = general subject/region touched on, but this item's specifics are genuinely new
  3 = same broad event, but this item adds a real, distinct national angle/stake beyond the baseline
  2 = same specific event, and this item's local detail is mostly color/reaction, not new information
  1 = same underlying event as the baseline, no additional information or angle at all

TODAY'S GLOBAL NEWS BASELINE (what readers already know):
{baseline_text}

STORIES COVERED IN THE LAST {HISTORY_DAYS} DAYS (avoid re-running the same underlying story on consecutive days):
{history_text}

YOUR TASK:
Review the front-page stories below from {len(stories)} publications worldwide. (Wire-service-sourced and cross-source-duplicated stories have already been removed from this list.)
Rank as many stories as genuinely qualify — up to {SELECTION_BUFFER} — most important first, using ALL of these criteria:

1. UNIQUE — Weight saturation_score heavily here: a 4 or 5 is exactly what this criterion wants. A 1 or 2 means the baseline already reported this same event; also check the baseline text yourself since the pre-computed score can miss things, and cross-reference the recent history above the same way.
2. LOCAL CONNECTION — Favor higher localization_score. A 4 or 5 should generally outrank a 2 unless the 2 is dramatically more nationally significant. A high score alone isn't sufficient on its own — the story still needs to clear the other criteria too — but a low score (2) should be treated as a real strike against a story, on par with a criterion failure, not a minor tiebreaker.
3. NATIONALLY SIGNIFICANT — front page = editors deemed it the day's most important story
4. GLOBALLY RELEVANT — has implications beyond its own borders, or reveals something meaningful about that country/region the world should know
5. VARIED — no two stories from the same country in the top ranks; aim for geographic spread across regions
6. SUBSTANTIVE — politics, economics, security, environment, justice, social upheaval. Not sports or celebrity unless it has genuine geopolitical/social weight.

TIE-BREAK RULE (LOCAL CONNECTION vs. GLOBAL RELEVANCE): When LOCAL CONNECTION and GLOBALLY RELEVANT conflict — i.e., a story is relevant mainly BECAUSE it's a huge global event with only a score of 2 for this particular source — LOCAL CONNECTION WINS. A story already dominating US front pages, told from a source with no distinctive stake in it, should rank low here regardless of its objective world importance; that's the entire premise of this newsletter. A high-scoring (4-5) multi-country story — e.g. a bilateral trade dispute where this country is the direct, named target — is a different case and should be judged on its merits, not suppressed.

TIE-BREAK RULE (SATURATION vs. LOCAL CONNECTION): A high localization_score does NOT automatically rescue a low saturation_score. "This country's own president visited its own disaster" can score 5 on localization while still being the identical event the world already knows about via the baseline (saturation 1-2) — local color alone (who visited, which tactics were used) isn't a distinct angle if it doesn't add real information beyond the baseline. The Brazil-tariffs case is the test for the other direction: localization 4, saturation may be low too since the tariffs are globally reported — but the story survives because it has a genuinely distinct, named stake (Brazil is the direct target), not because of local color. Ask: does this outlet's version give the reader real information the baseline doesn't already provide? If the honest answer is "no, just a different narrator," rank it low regardless of how high either individual score is.

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
        selected = _enforce_one_per_country(selected)
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


# Matches a dollar/euro/pound/yen/won/rupee figure, percentage, or a number
# with a magnitude word — deliberately NOT bare small integers or plain
# years, which are common and rarely the kind of confabulated "impressive
# statistic" the 2026-07-28 SCMP incident produced (a fabricated "$11
# billion in H1 2024 cross-border biotech deals" grafted onto an unrelated
# HKU succession story). This is a blunt, numbers-only net — it won't catch
# a fabricated claim that has no figure in it — but it's cheap and it
# directly targets the failure mode actually observed.
#
# 2026-08-08 update: three consecutive briefs (Canada, Germany, Argentina)
# were flagged for review even though the figures were correct — all three
# for the same underlying reason: the source used a DIFFERENT but
# EQUIVALENT format for the same figure than the brief did, and the old
# check compared them as literal strings:
#   - "15%" (brief) vs. "15 per cent" (Globe and Mail's own house style)
#   - "€210 billion" (brief) vs. "210 Milliarden" (FAZ, in German)
#   - "2.9%" (brief) vs. "2,9%" (La Nación, Spanish decimal-comma convention)
# Fixed by resolving every matched figure to its actual real-world numeric
# VALUE (so "$21 billion", "210 Milliarden", and "210亿" all normalize to
# the same 21,000,000,000) rather than comparing raw text. This also
# correctly handles CJK sources, which don't have million/billion words at
# all and count in base-10,000/100,000,000 units instead (arithmetic, not
# translation) — see _CJK_UNIT_MULTIPLIERS below.
#
# False-friend guard: several European languages reuse "billion"-family
# words for LONG-SCALE billion = 10^12 (English "trillion") — German
# "Billion(en)", French/Spanish "billion/billón", Dutch "biljoen", etc.
# Those are placed under the trillion bucket, never the billion one, so
# this fix can never cause the guard to validate a figure that's off by
# 1000x. Anything not in the word list below — including any language or
# script not covered — simply doesn't register as a marker, and the guard
# keeps its prior (safe) behavior of flagging it for human review rather
# than silently accepting it.
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

CURRENCY_SYMBOLS = "$€£¥₩₹"

_MAGNITUDE_WORDS = {
    1_000_000_000: [  # billion, short scale (10^9)
        "billion", "billions", "bn",
        "milliarde", "milliarden",                      # German
        "milliard", "milliards",                        # French
        "miliardo", "miliardi",                          # Italian
        "mil millones", "millardo",                      # Spanish (NOT billón)
        "bilhao", "bilhoes", "bilhão", "bilhões",        # Portuguese
        "miljard",                                        # Dutch
        "миллиард", "миллиарда", "миллиардов", "млрд",   # Russian
        "مليار",                                           # Arabic
    ],
    1_000_000: [  # million (10^6)
        "million", "millions", "mn",
        "millionen",                                       # German
        "millón", "millones",                              # Spanish
        "milione", "milioni",                              # Italian
        "milhao", "milhoes", "milhão", "milhões",          # Portuguese
        "miljoen",                                          # Dutch
        "миллион", "миллиона", "миллионов", "млн",         # Russian
        "مليون",                                             # Arabic
    ],
    1_000_000_000_000: [  # trillion / long-scale "billion" false friends (10^12)
        "trillion", "trillions", "tn",
        "billionen",                                        # German "Billion(en)"
        "billón", "billones",                               # Spanish
        "biljoen",                                           # Dutch
        "триллион", "триллиона", "триллионов", "трлн",     # Russian
        "تريليون",                                            # Arabic
    ],
}
_WORD_TO_MULT = {w.lower(): mult for mult, words in _MAGNITUDE_WORDS.items() for w in words}
_MAGNITUDE_ALTS = sorted(_WORD_TO_MULT.keys(), key=len, reverse=True)
_MAGNITUDE_PATTERN = "|".join(re.escape(w) for w in _MAGNITUDE_ALTS)
_PERCENT_WORDS = r"(?:%|％|per\s*cent|percent)"

_FIGURE_RE = re.compile(
    rf"[{re.escape(CURRENCY_SYMBOLS)}]?"
    rf"\d[\d.,]*"
    rf"(?:\s?(?:{_MAGNITUDE_PATTERN}|{_PERCENT_WORDS}))?",
    re.I,
)

# Chinese/Japanese/Korean don't have million/billion words at all — they
# group in 10,000s and 100,000,000s. "210亿" means 210 x 10^8, not "210
# [word for billion]"; this is arithmetic, not translation. Only the
# simple single-unit case (digits immediately followed by one unit
# character) is handled — combined expressions like "12亿3000万" (Chinese
# for 1.23 billion) are NOT summed and won't parse correctly. That's a
# known, accepted gap consistent with this guard's "cheap blunt net"
# design — an unparsed CJK figure still fails toward being flagged for
# review, which is the safe direction.
_CJK_UNIT_MULTIPLIERS = {
    "万": 10_000, "萬": 10_000, "만": 10_000,
    "億": 100_000_000, "亿": 100_000_000, "억": 100_000_000,
    "兆": 1_000_000_000_000, "조": 1_000_000_000_000,
}
_CJK_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([万萬億亿兆만억조])")


def _normalize_decimal(numeral: str) -> "Decimal | None":
    """Convert a numeral string to a Decimal, handling US/UK (1,234.56),
    European (1.234,56), and single-separator (2,9 or 50.000) numeral
    conventions. Two ambiguous single-separator cases are disambiguated by
    the length of the trailing digit group, which is reliable in practice
    (nobody writes 3 trailing zeros to mean a decimal): a group of 1-2
    trailing digits after a lone comma or period is a decimal point (e.g.
    "2,9" -> 2.9, the Argentina/La Nación case); a group of exactly 3 is a
    thousands separator (e.g. Spanish "50.000" -> 50000, the earlier
    El Mundo/Ceuta false positive this same guard produced on 2026-08-01).
    When both separators are present, whichever appears LAST is the
    decimal point and the other is thousands grouping, covering both the
    US (1,234.56) and European long-form (1.234,56) conventions."""
    numeral = numeral.strip().rstrip(".,")
    if not numeral:
        return None

    has_comma, has_period = "," in numeral, "." in numeral

    if has_comma and has_period:
        if numeral.rfind(",") > numeral.rfind("."):
            numeral = numeral.replace(".", "").replace(",", ".")
        else:
            numeral = numeral.replace(",", "")
    elif has_comma:
        parts = numeral.split(",")
        if len(parts) == 2 and parts[0] and 1 <= len(parts[1]) <= 2:
            numeral = f"{parts[0]}.{parts[1]}"
        elif len(parts) > 1 and parts[0] and all(len(p) == 3 for p in parts[1:]):
            numeral = "".join(parts)
        else:
            numeral = numeral.replace(",", "")
    elif has_period:
        parts = numeral.split(".")
        if len(parts) > 1 and parts[0] and all(len(p) == 3 for p in parts[1:]):
            numeral = "".join(parts)
        # else: a lone period with a non-3-digit tail is a genuine decimal
        # point (the international/US default) — left as-is.

    try:
        return Decimal(numeral)
    except InvalidOperation:
        return None


def _extract_figures(text: str) -> dict:
    """Pull out normalized numeric 'claims' worth grounding-checking,
    keyed by (real-world value, unit) — unit is '%' or 'count' — mapped to
    the ORIGINAL matched text, so a flagged figure can still be reported
    to the human reviewer in the words the brief actually used. Resolving
    to actual numeric value (rather than comparing raw text) means
    equivalent figures in different languages/formatting conventions
    match instead of false-flagging: "$21 billion", "210 Milliarden", and
    "210亿" all normalize to the same 21,000,000,000. Bare small numbers
    with no currency/percent/magnitude marker are still ignored — those
    are far more often incidental (a count of towns, a year) than the
    kind of invented statistic this check targets."""
    if not text:
        return {}
    text = text.translate(_ARABIC_INDIC_DIGITS)
    out = {}

    for m in _FIGURE_RE.finditer(text):
        raw = m.group(0)
        currency = raw[0] if raw[0] in CURRENCY_SYMBOLS else ""
        rest = raw[len(currency):] if currency else raw
        num_match = re.match(r"[\d.,]+", rest)
        if not num_match:
            continue
        numeral = num_match.group(0)
        marker_text = re.sub(r"\s+", " ", rest[len(numeral):].strip().lower())

        value = _normalize_decimal(numeral)
        if value is None:
            continue

        if marker_text in ("%", "％") or re.fullmatch(r"per\s*cent|percent", marker_text):
            out.setdefault((value.normalize(), "%"), raw.strip())
            continue

        mult = _WORD_TO_MULT.get(marker_text)
        has_marker = bool(mult) or bool(currency)
        digits_len = len(numeral.replace(",", "").replace(".", ""))
        if has_marker or digits_len >= 4:
            final_value = value * mult if mult else value
            out.setdefault((final_value.normalize(), "count"), raw.strip())

    for m in _CJK_UNIT_RE.finditer(text):
        num_str, unit = m.group(1), m.group(2)
        try:
            value = Decimal(num_str) * _CJK_UNIT_MULTIPLIERS[unit]
        except InvalidOperation:
            continue
        out.setdefault((value.normalize(), "count"), m.group(0))

    return out


def _has_ungrounded_figures(brief_text: str, why_it_matters: str, grounding_source: str) -> set:
    """Return any numeric claims in the written brief that don't appear
    anywhere in what the model was actually given (headline + deckline +
    fetched article text / lede) once currency/decimal/magnitude
    formatting is normalized to actual value. A non-empty result means the
    model most likely introduced a statistic from its own training data
    rather than the story it was asked to write about. Returns the
    original brief-text wording (not the normalized value) for each
    ungrounded figure, since that's what the human reviewer needs to see."""
    claimed = _extract_figures(brief_text)
    claimed.update(_extract_figures(why_it_matters))
    if not claimed:
        return set()
    grounded_keys = set(_extract_figures(grounding_source).keys())
    return {claimed[k] for k in claimed if k not in grounded_keys}


def _write_briefs(ranked_stories: list[ScrapedStory], target: int,
                  allow_thin: bool = False) -> list[dict]:
    """
    Walk the ranked candidate buffer writing briefs, stopping once `target`
    good briefs are written or the buffer is exhausted.

    2026-08-01: this stage was terminating six of seven finalists in a
    single run. Three changes, all of the same shape — repair before
    dropping:

      - Article text is no longer fetched here; it was already fetched in
        parallel before screening and cached on the story. No double fetch.
      - An "insufficient information" verdict now triggers a RETRY grounded
        on the front-page image itself (already in memory from the vision
        selection step) before the candidate is abandoned. Print front
        pages carry the opening paragraphs the web fetch often can't reach
        past a paywall or a 403.
      - Ungrounded numeric figures no longer kill a brief. They are
        recorded on the story dict as `flagged_figures` and surfaced in the
        draft for human review. The guard was validating against whatever
        grounding text happened to survive the fetch, so a fetch failure
        was being converted into a fabrication accusation — it killed El
        Mundo's Ceuta lead over "50,000", a figure corroborated elsewhere
        in the same day's corpus.

    `allow_thin` is set by the backfill ladder: it tells the brief prompt
    that a short, flat, purely descriptive brief is an acceptable result
    rather than something to refuse over.
    """
    results = []
    for s in ranked_stories:
        if len(results) >= target:
            break

        article_text = getattr(s, "article_text", "") or ""

        # Wire-service pass 3: full article text, right before writing.
        # Dateline attribution frequently only appears in the body, not the
        # homepage teaser checked in curate()'s earlier passes. This one
        # still drops — wire copy is never publishable here at any tier.
        if detect_wire_service(s.headline, s.deckline, article_text):
            logger.info(f"  Skipped {s.publication}: wire-service attribution found in fetched article text")
            continue

        try:
            brief = _write_single_brief(s, article_text, allow_thin=allow_thin)
        except Exception as e:
            logger.warning(f"  Brief failed for {s.publication}: {e} — skipping, trying next candidate")
            continue

        # Retry on the front page itself before giving up. Only possible for
        # sources that came through front-page vision selection; homepage-tier
        # and backfill candidates have no image and fall straight through.
        if brief is None and getattr(s, "frontpage_image_bytes", None):
            logger.info(f"  {s.publication}: thin web text — retrying grounded on the front-page image")
            try:
                brief = _write_single_brief(s, article_text, allow_thin=True, use_image=True)
            except Exception as e:
                logger.warning(f"  Front-page retry failed for {s.publication}: {e}")

        if brief is None:
            logger.info(f"  Skipped {s.publication}: insufficient information even from the front page")
            continue
        if _looks_like_refusal(brief.get("brief", "")) or _looks_like_refusal(brief.get("why_it_matters", "")):
            logger.info(f"  Skipped {s.publication}: refusal-pattern detected in brief text — treating as failure")
            continue

        # Grounding check: FLAG, DO NOT DROP (changed 2026-08-01). Checked
        # against everything the model actually saw — headline, deckline,
        # and fetched article text/lede.
        grounding_source = f"{s.headline} {s.deckline} {article_text or s.lede}"
        ungrounded = _has_ungrounded_figures(
            brief.get("brief", ""), brief.get("why_it_matters", ""), grounding_source
        )
        if ungrounded:
            brief["flagged_figures"] = sorted(ungrounded)
            logger.warning(
                f"  FLAGGED {s.publication}: brief contains figure(s) not found in the "
                f"fetched source text ({sorted(ungrounded)}) — published for review, VERIFY BEFORE SENDING"
            )

        results.append(brief)
        logger.info(f"  Brief written: [{s.country}] {s.headline[:60]}")

    return results


def _write_single_brief(s: ScrapedStory, article_text: str = "",
                        allow_thin: bool = False, use_image: bool = False) -> dict | None:
    """Write a 3-sentence brief + why-it-matters for a single story, or
    return None if there genuinely isn't enough real information.

    `use_image` attaches the front-page image captured during vision
    selection, so a story whose web text was paywalled or 403'd can still
    be briefed from what's actually printed on the page.
    `allow_thin` tells the model a short descriptive brief is a success
    state — used by the image retry and by the backfill ladder, where two
    accurate flat sentences beat an empty slot.
    Note: the Substack-facing status label (state organ / exile / etc.) is
    resolved in publisher.py from sources.py by source_id — not here."""
    context_block = article_text if article_text else s.lede

    if allow_thin:
        thin_clause = """If the information is thin, WRITE THE BRIEF ANYWAY, shorter. Two plain factual sentences stating only what is actually reported — with no background, no analysis, and no explanation of significance — is a CORRECT and acceptable result. Only return the insufficient marker if you cannot state even one concrete fact about a real event. Do not pad, do not speculate, do not supply context from your own knowledge to make it feel complete."""
    else:
        thin_clause = """If the information above is too thin, vague, or fragmentary to write a factual, comprehensible brief for a reader with zero context — for example, it only names a decree/policy/case number with no explanation of what it actually does, or it's just a publication's self-description — return ONLY this JSON and nothing else:
{"insufficient": true}"""

    image_clause = ""
    if use_image:
        image_clause = """
An image of this publication's actual printed front page is attached. The web text above was incomplete or unavailable. Read the front page and use what is printed there — the headline, subheads, and any visible opening paragraphs of the relevant story — as your source material. Report only what you can actually read on the page; if the printed text is too small or cut off to read reliably, do not guess at it."""

    prompt = f"""You are writing for "World's Front Page," a daily newsletter for smart, globally curious American readers who want to know what's front-page news in other countries — stories they probably haven't seen yet.

STORY SOURCE:
- Publication: {s.publication} ({s.country})
- Headline: {s.headline}
- Deckline/summary: {s.deckline}
- Additional context (fetched article text when available): {context_block}
{image_clause}
{thin_clause}

Do NOT introduce facts, statistics, examples, or background context that are not present in the STORY SOURCE above — even true, well-known facts about the broader topic. If the source material doesn't fully explain why this matters beyond its own borders, write a more general — but still accurate — why-it-matters line rather than reaching for an outside statistic to make it sound more significant. A thin but honest brief is correct; a fluent one padded with an invented figure is not.

Otherwise, WRITE:
1. A BRIEF (3 sentences max): What happened. Key facts. Who's involved and what's at stake. Be specific and direct — this is a briefing, not a feature. No fluff, no hedging.
2. A WHY IT MATTERS line (1 sentence): Who beyond {s.country}'s borders should care about this and why. Be concrete — name the geopolitical, economic, or humanitarian stakes, but only using what's actually supported by the source material above.

TONE: Authoritative. Clear. Like a senior foreign correspondent's one-paragraph cable. No "in a significant development" or "according to reports." Just the news.

Return ONLY this JSON, nothing else:
{{
  "brief": "...",
  "why_it_matters": "..."
}}"""

    if use_image:
        media_type = _IMAGE_MEDIA_TYPES.get(
            getattr(s, "frontpage_content_type", ""), "image/jpeg"
        )
        b64 = base64.standard_b64encode(s.frontpage_image_bytes).decode("ascii")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": prompt},
        ]
        # The image path needs a vision-capable model; MODEL (Haiku) is,
        # but front-page print is small and dense, so this one call goes to
        # the same model the front-page selector already trusts to read it.
        model = SELECTION_MODEL
    else:
        content = prompt
        model = MODEL

    resp = client.messages.create(
        model=model,
        max_tokens=1000,
        messages=[{"role": "user", "content": content}],
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
        "grounded_on": "frontpage_image" if use_image else ("article_text" if article_text else "lede"),
        "flagged_figures": [],
    }
