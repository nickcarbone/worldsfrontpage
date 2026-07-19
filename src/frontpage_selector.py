"""
World's Front Page — Front-Page Vision Selection Layer

This is the "Path A" selection step agreed on: for sources with verified
frontpage coverage (see sources.py's "frontpage" key, fetched via
frontpage_fetcher.py), a vision call looks at the ACTUAL print/e-edition
front page and decides what that outlet's editors led with today. That
becomes the authoritative signal for which of scraper.py's existing
web-scraped candidates (ScrapedStory.candidates) gets written up --
overriding curator.py's old purely-web-derived heuristic for these sources.

Architecture recap (agreed, not re-litigated here):
  - The front page is ONLY ever used as an internal signal. Its image is
    never shown to subscribers or reproduced anywhere in the newsletter --
    this module only ever emits text (a matched headline/deckline/URL
    already present in scraper.py's own candidate list).
  - Sources with no working frontpage config, or whose frontpage fetch
    fails for the day (FrontPageUnavailable), are DROPPED SILENTLY from
    this pipeline for that day -- no fallback to the old web-only
    selection logic. That was an explicit, deliberate call, not an
    oversight: the source list itself is expected to evolve based on which
    countries this ends up dropping, rather than papering over gaps with a
    different selection method per source.
  - Wire-led front pages are excluded even when the wire story is the
    banner lead. The vision call is asked to look past the top element to
    the next-most-prominent ELIGIBLE (non-wire) story if needed, mirroring
    the existing multi-candidate philosophy applied to page layout instead
    of a scraped headline list.
  - This module does NOT replace scraper.py's web scrape. It replaces only
    the "which of the scraped candidates is the real story" decision for
    sources that have frontpage coverage. Article text is still fetched
    from the matched candidate's real URL via scraper.fetch_article_text(),
    same as before -- a front page's headline + deck is not enough on its
    own for a factually complete brief.

Known open risk, carried over rather than solved here: non-Latin-script
wire-credit detection was already an unresolved gap in scraper.py's
text-based approach, and reading a small byline off a compressed front-page
image doesn't make it easier -- if anything, it's a worse OCR target than
rendered HTML text. Treat this as the same unsolved problem, not two.
"""

import os
import re
import json
import base64
import logging
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Optional

from anthropic import Anthropic

from scraper import ScrapedStory, Candidate
from frontpage_fetcher import fetch_frontpage, FrontPageUnavailable

logger = logging.getLogger(__name__)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Selection-critical step -- same reasoning as curator.py's own note about
# restoring Sonnet for the selection stage: a single daily vision call per
# source is cheap enough that model quality, not cost, should decide this.
MODEL = "claude-sonnet-5"

_CONTENT_TYPE_TO_MEDIA_TYPE = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}


@dataclass
class FrontPageSelection:
    source_id: str
    matched: bool
    reason: str                     # why matched / why not, for logging
    wire_elements_skipped: int = 0  # how many prominent elements were wire-flagged and passed over
    frontpage_image_url: str = ""


def apply_frontpage_selection(
    stories: list[ScrapedStory],
    sources_by_id: dict[str, dict],
    on: Optional[date_cls] = None,
) -> tuple[list[ScrapedStory], list[FrontPageSelection]]:
    """
    Filters `stories` down to only those whose source has a working
    frontpage config AND for which the vision call found an eligible
    (non-wire) story matching one of the scraped web candidates.

    For each surviving story, headline/deckline/article_url are OVERWRITTEN
    with the matched candidate's fields -- the front page decided which
    candidate is the real one, even if scraper.py's own heuristic had
    guessed differently.

    Returns (surviving_stories, all_selection_logs). The log list covers
    every source that HAD frontpage config, including the ones that got
    dropped, so a daily run can show exactly which countries went dark and
    why -- useful for the "is a country genuinely underrepresented, or did
    we just have a bad day" question raised earlier, even though nothing
    from that log is shown to subscribers.
    """
    on = on or date_cls.today()
    survivors: list[ScrapedStory] = []
    logs: list[FrontPageSelection] = []

    stories_by_id = {s.source_id: s for s in stories}

    for source_id, source in sources_by_id.items():
        if "frontpage" not in source:
            continue  # no frontpage config at all -- not this pipeline's concern
        story = stories_by_id.get(source_id)
        if story is None or story.scrape_error or not story.candidates:
            logs.append(FrontPageSelection(
                source_id=source_id, matched=False,
                reason="no usable web-scraped candidates to match against",
            ))
            continue

        try:
            fp_result = fetch_frontpage(source, on=on)
        except FrontPageUnavailable as e:
            logs.append(FrontPageSelection(source_id=source_id, matched=False, reason=str(e)))
            continue

        try:
            vision_result = _rank_and_match(story, fp_result.image_bytes, fp_result.content_type)
        except Exception as e:
            logger.warning(f"{source_id}: vision selection call failed: {e}")
            logs.append(FrontPageSelection(
                source_id=source_id, matched=False,
                reason=f"vision call failed: {e}",
                frontpage_image_url=fp_result.image_url,
            ))
            continue

        wire_skipped = sum(1 for el in vision_result.get("elements", []) if el.get("wire_service"))
        idx = vision_result.get("selected_candidate_index")

        if idx is None or not (0 <= idx < len(story.candidates)):
            logs.append(FrontPageSelection(
                source_id=source_id, matched=False,
                reason=vision_result.get("reason_no_match", "no eligible non-wire candidate match found"),
                wire_elements_skipped=wire_skipped,
                frontpage_image_url=fp_result.image_url,
            ))
            continue

        matched: Candidate = story.candidates[idx]
        story.headline = matched.headline
        story.deckline = matched.deckline
        story.article_url = matched.article_url or story.article_url
        survivors.append(story)
        logs.append(FrontPageSelection(
            source_id=source_id, matched=True,
            reason=vision_result.get("reason", "matched"),
            wire_elements_skipped=wire_skipped,
            frontpage_image_url=fp_result.image_url,
        ))

    logger.info(
        f"Front-page selection: {len(survivors)} matched / "
        f"{len([s for s in sources_by_id.values() if 'frontpage' in s])} with frontpage config "
        f"({sum(1 for l in logs if not l.matched)} dropped)"
    )
    return survivors, logs


def _rank_and_match(story: ScrapedStory, image_bytes: bytes, content_type: str) -> dict:
    """
    One vision call per source: rank the front page's prominent story
    elements, flag wire-service credit, and match the top eligible element
    against this source's own scraped web candidates.
    """
    media_type = _CONTENT_TYPE_TO_MEDIA_TYPE.get(content_type, "image/jpeg")
    b64_image = base64.standard_b64encode(image_bytes).decode("ascii")

    candidate_list = [
        {"index": i, "headline": c.headline, "deckline": c.deckline[:200]}
        for i, c in enumerate(story.candidates)
    ]

    prompt = f"""You are looking at today's actual front page of {story.publication} ({story.country}).

TASK:
1. Identify the prominent story elements on this front page (banner/lead, secondary stories, teasers) -- typically 2-5 elements, most prominent first.
2. For EACH element, note if it is credited to a wire service (AP, Reuters, AFP, Bloomberg, dpa, EFE, or that wire service's name transliterated/translated into this page's language). Wire credit is often printed directly under or beside a headline in small type.
3. Find the MOST PROMINENT element that is NOT wire-credited. If the true banner lead is wire-credited, skip it and look at the next-most-prominent eligible element -- do not default to the banner just because it's biggest.
4. Match that eligible element against the list of web-scraped candidate headlines below (they come from this same outlet's website, so should describe the same underlying story even if the exact wording differs from the print headline). If none of the candidates plausibly describes the same story as any eligible front-page element, say so explicitly -- do not force a weak match.

WEB-SCRAPED CANDIDATES (from this outlet's own website today):
{json.dumps(candidate_list, ensure_ascii=False, indent=2)}

Return ONLY this JSON, nothing else:
{{
  "elements": [
    {{"rank": 1, "headline_on_page": "...", "prominence": "banner|secondary|teaser", "wire_service": null or "AP"/"Reuters"/etc.}},
    ...
  ],
  "selected_candidate_index": <int index from the candidate list above, or null if no eligible match>,
  "reason": "<one sentence: which front-page element you matched and why>",
  "reason_no_match": "<if selected_candidate_index is null, one sentence why -- e.g. 'entire front page is wire-led' or 'no scraped candidate describes the eligible lead story'>"
}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_image}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)
