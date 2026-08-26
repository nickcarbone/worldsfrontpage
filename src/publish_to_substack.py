"""
World's Front Page — Browser-Session Substack Publisher

Runs ONLY on the self-hosted Mac runner, never on GitHub-hosted runners.

Why this exists: the plain-requests / cookie-header approach (publisher.py's
post_draft) got blocked by Cloudflare at the network level even with a valid
session cookie — confirmed via logs on 2026-07-13. Cloudflare's bot
management scores more than the cookie: IP reputation and browser/TLS
fingerprint matter too. A raw `requests.post` from a datacenter IP can never
pass that, no matter how good the cookie is.

This module instead drives a REAL Chromium browser (via Playwright) loaded
with an already-authenticated session, and executes each request as a
`fetch()` call from *inside* the loaded page. That means the request carries
a genuine browser TLS handshake, a real JS engine, and same-origin cookies
attached automatically — not a Python HTTP client pretending to be one.
Running on the self-hosted Mac also means the request originates from a
normal residential IP instead of a GitHub Actions range.

One-time setup (on this machine only, not in CI):
    python capture_substack_session.py
This opens a real Chrome window. Log into Substack manually, handling any
2FA or Cloudflare check exactly as you would day-to-day. It saves the
resulting session to SUBSTACK_STATE_PATH. That file IS a login — never
commit it, never copy it off this machine, never paste it anywhere.

If posting starts failing again, the most likely cause is a stale session
(cookies rotate / expire) — re-run capture_substack_session.py to refresh it.

v2 — structured content (2026-07-13): Substack's real drafts endpoint
rejected an HTML string with "draft_bylines: Invalid value" — its actual
format is a JSON node tree (draft_body) plus a draft_bylines array
identifying the author by user ID. Confirmed via DevTools against a real
draft; see publisher.py's build_post() for how content_doc is constructed.

v3 — auto-publish (2026-08-23): previously this module only ever created a
Substack DRAFT — publishing was a manual step in Substack's editor, on
purpose, as an editorial review checkpoint. That checkpoint was getting
missed regularly (the whole reason this change exists), so on explicit
instruction the pipeline now also fires Substack's publish call, chained
through the same authenticated page used to create the draft. This is an
editorial decision, not just a technical one — see main.py's
_run_publish_only for the tradeoff it encodes (specifically: unverified
figures flagged by curator.py's fabrication guard now go out unreviewed,
by choice, with the flag surviving only in the run log).

Reverse-engineered from apisubstack.com's documented endpoint list and the
ma2za/python-substack reference client (not confirmed against your own
account via DevTools the way the draft-creation endpoint was in v1/v2) —
the two-step shape is:
    GET  /api/v1/drafts/{id}/prepublish   (server-side validation check)
    POST /api/v1/drafts/{id}/publish      (the actual publish)
If Substack's publish response shape differs from what's assumed below,
run test_publish_local.py with --live-publish (see that file) to check the
raw response against a real, disposable-to-you test post before trusting
this in production.
"""

from __future__ import annotations  # lets `bool | None` etc. run on Python < 3.10

import os
import json
import logging
from pathlib import Path
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

SUBSTACK_STATE_PATH = Path(
    os.environ.get("SUBSTACK_STATE_PATH", str(Path.home() / "wfp-runner" / "substack_state.json"))
)
SUBSTACK_PUB_URL = os.environ.get("SUBSTACK_PUB_URL", "https://worldsfrontpage.substack.com")

# Your Substack account's numeric user ID — confirmed via DevTools
# (Network tab, a draft's draftBylines[0].id / postBylines[0].user_id).
# This is not a secret (it's not a login credential), just an account
# identifier, so it's fine as a plain constant rather than piped through
# a GitHub secret. If you ever need to reconfirm it: open any existing
# draft's GET .../api/v1/drafts/<id> response and look at postBylines[0].user_id.
SUBSTACK_USER_ID = int(os.environ.get("SUBSTACK_USER_ID", "1093738"))

# Editorial control, not a secret — toggle here or via GitHub Actions env
# without touching code. Set to "false" to go back to draft-only (the
# original, human-reviewed behavior) at any time.
SUBSTACK_AUTO_PUBLISH_DEFAULT = os.environ.get("SUBSTACK_AUTO_PUBLISH", "true").lower() == "true"
# Whether a successful publish also emails the subscriber list. Kept
# separate from AUTO_PUBLISH so you can go live on the archive page without
# necessarily sending mail, if that's ever useful for recovery scenarios.
SUBSTACK_SEND_EMAIL_DEFAULT = os.environ.get("SUBSTACK_SEND_EMAIL", "true").lower() == "true"

_FETCH_JS = """async (payload) => {
    const opts = {
        method: payload.method,
        headers: {"Content-Type": "application/json"},
        credentials: "include",
    };
    if (payload.body !== null && payload.body !== undefined) {
        opts.body = JSON.stringify(payload.body);
    }
    const resp = await fetch(payload.url, opts);
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


def _looks_like_cloudflare(text: str) -> bool:
    body_lower = text[:800].lower()
    return any(
        sig in body_lower for sig in
        ("cloudflare", "cf-ray", "attention required", "checking your browser")
    )


def post_draft_via_browser(post: dict, publish: bool | None = None, send: bool | None = None) -> dict:
    """
    Post the assembled content as a Substack draft using a real, already-
    authenticated Chromium session. If `publish` is true (default: the
    SUBSTACK_AUTO_PUBLISH env var, itself defaulting to True), also fires
    the prepublish check and the publish call in the same browser session,
    so the edition goes live and (if `send` is true) emails subscribers
    without a manual click in Substack's editor.

    Returns a dict, not a bool (this changed in v3 — update any caller
    still expecting a plain bool):
        {
            "success": bool,       # draft was created
            "draft_id": str|None,
            "published": bool,     # only meaningful if publish=True was requested
            "error": str|None,     # human-readable reason for any failure
        }
    """
    if publish is None:
        publish = SUBSTACK_AUTO_PUBLISH_DEFAULT
    if send is None:
        send = SUBSTACK_SEND_EMAIL_DEFAULT

    result = {"success": False, "draft_id": None, "published": False, "error": None}

    if not SUBSTACK_STATE_PATH.exists():
        msg = (
            f"No saved Substack session at {SUBSTACK_STATE_PATH}. Run "
            "`python capture_substack_session.py` on this machine once to "
            "log in and create it."
        )
        logger.error(msg)
        result["error"] = msg
        return result

    with sync_playwright() as p:
        # headless=True: this runs unattended as a background service, with
        # no desktop session guaranteed to be active. If Cloudflare still
        # flags this specific combination, the next lever to try is
        # `channel="chrome"` (uses a real installed Google Chrome instead of
        # Playwright's bundled Chromium) — a marginally more convincing
        # fingerprint, at the cost of requiring Chrome.app to be installed.
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(storage_state=str(SUBSTACK_STATE_PATH))
        page = context.new_page()

        try:
            # Load a real page on the pub's own domain first, so any
            # Cloudflare JS challenge on this session gets evaluated the
            # normal way before we try to call the API.
            page.goto(f"{SUBSTACK_PUB_URL}/publish/posts", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            draft_body_json = json.dumps(
                {"type": "doc", "content": post["content_doc"]}, ensure_ascii=False
            )

            create_result = page.evaluate(
                _FETCH_JS,
                {
                    "method": "POST",
                    "url": f"{SUBSTACK_PUB_URL}/api/v1/drafts",
                    "body": {
                        "type":                 "newsletter",
                        "audience":             "everyone",
                        "draft_title":          post["title"],
                        "draft_subtitle":       post["subtitle"],
                        "draft_body":           draft_body_json,
                        "draft_bylines":        [{"id": SUBSTACK_USER_ID, "is_guest": False}],
                        "draft_podcast_url":    None,
                        "draft_podcast_duration": None,
                        "draft_section_id":     None,
                        "section_chosen":       False,
                        "detect_language":      False,
                        "translations":         [],
                    },
                },
            )

            status = create_result["status"]
            if status not in (200, 201):
                if _looks_like_cloudflare(create_result["text"]):
                    msg = (
                        f"Still blocked at the network level even from this machine (status {status}). "
                        "That would mean this residential IP/fingerprint combination is ALSO being "
                        "flagged — try re-running capture_substack_session.py to refresh the session "
                        "before concluding this path is dead; a stale storage_state can look like this too."
                    )
                else:
                    msg = (
                        f"Substack rejected the draft-creation request (status {status}) — this does NOT "
                        "look like a Cloudflare/network block, so treat it as a request-format problem "
                        f"first, not an auth problem. Response body:\n{create_result['text'][:2000]}"
                    )
                logger.error(msg)
                result["error"] = msg
                return result

            try:
                draft_id = json.loads(create_result["text"]).get("id", "unknown")
            except Exception:
                draft_id = "unknown"

            logger.info(f"Draft created via browser session: {draft_id}")
            result["success"] = True
            result["draft_id"] = draft_id

            if publish and draft_id != "unknown":
                prepub_result = page.evaluate(
                    _FETCH_JS,
                    {"method": "GET", "url": f"{SUBSTACK_PUB_URL}/api/v1/drafts/{draft_id}/prepublish", "body": None},
                )
                if prepub_result["status"] not in (200, 201):
                    msg = (
                        f"Prepublish check failed (status {prepub_result['status']}) for draft {draft_id} — "
                        f"not attempting publish. Response body:\n{prepub_result['text'][:2000]}"
                    )
                    logger.error(msg)
                    result["error"] = msg
                    return result

                publish_result = page.evaluate(
                    _FETCH_JS,
                    {
                        "method": "POST",
                        "url": f"{SUBSTACK_PUB_URL}/api/v1/drafts/{draft_id}/publish",
                        "body": {"send": send, "share_automatically": False},
                    },
                )
                if publish_result["status"] in (200, 201):
                    logger.info(f"Draft {draft_id} published{' and emailed' if send else ' (no email sent)'}.")
                    result["published"] = True
                else:
                    msg = (
                        f"Publish call failed (status {publish_result['status']}) for draft {draft_id} — "
                        f"the draft exists but is NOT live. Response body:\n{publish_result['text'][:2000]}"
                    )
                    logger.error(msg)
                    result["error"] = msg
            elif publish and draft_id == "unknown":
                msg = "Draft created but its id couldn't be parsed from the response, so publish was skipped."
                logger.error(msg)
                result["error"] = msg

            # Refresh the saved session after every run. Substack/Cloudflare
            # may rotate cookies on activity, so keep the file current
            # rather than letting it slowly go stale between runs.
            context.storage_state(path=str(SUBSTACK_STATE_PATH))
        finally:
            browser.close()

    return result
