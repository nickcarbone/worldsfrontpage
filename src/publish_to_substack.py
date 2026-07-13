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
with an already-authenticated session, and executes the draft-creation
request as a `fetch()` call from *inside* the loaded page. That means the
request carries a genuine browser TLS handshake, a real JS engine, and
same-origin cookies attached automatically — not a Python HTTP client
pretending to be one. Running on the self-hosted Mac also means the request
originates from a normal residential IP instead of a GitHub Actions range.

One-time setup (on this machine only, not in CI):
    python capture_substack_session.py
This opens a real Chrome window. Log into Substack manually, handling any
2FA or Cloudflare check exactly as you would day-to-day. It saves the
resulting session to SUBSTACK_STATE_PATH. That file IS a login — never
commit it, never copy it off this machine, never paste it anywhere.

If posting starts failing again, the most likely cause is a stale session
(cookies rotate / expire) — re-run capture_substack_session.py to refresh it.
"""

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

_DRAFT_FETCH_JS = """async (payload) => {
    const resp = await fetch(payload.url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        credentials: "include",
        body: JSON.stringify(payload.body),
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


def post_draft_via_browser(post: dict) -> bool:
    """
    Post the assembled content as a Substack draft using a real, already-
    authenticated Chromium session. Returns True on success.
    """
    if not SUBSTACK_STATE_PATH.exists():
        logger.error(
            f"No saved Substack session at {SUBSTACK_STATE_PATH}. Run "
            "`python capture_substack_session.py` on this machine once to "
            "log in and create it."
        )
        return False

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

            result = page.evaluate(
                _DRAFT_FETCH_JS,
                {
                    "url": f"{SUBSTACK_PUB_URL}/api/v1/drafts",
                    "body": {
                        "type":           "newsletter",
                        "draft_title":    post["title"],
                        "draft_subtitle": post["subtitle"],
                        "draft_body":     post["body_html"],
                        "audience":       "everyone",
                    },
                },
            )

            # Refresh the saved session after every run. Substack/Cloudflare
            # may rotate cookies on activity, so keep the file current
            # rather than letting it slowly go stale between runs.
            context.storage_state(path=str(SUBSTACK_STATE_PATH))
        finally:
            browser.close()

    status = result["status"]
    if status in (200, 201):
        try:
            draft_id = json.loads(result["text"]).get("id", "unknown")
        except Exception:
            draft_id = "unknown"
        logger.info(f"Draft created via browser session: {draft_id}")
        return True

    body_lower = result["text"][:800].lower()
    looks_like_cloudflare = any(
        sig in body_lower for sig in
        ("cloudflare", "cf-ray", "attention required", "checking your browser")
    )
    if looks_like_cloudflare:
        logger.error(
            f"Still blocked at the network level even from this machine (status {status}). "
            "That would mean this residential IP/fingerprint combination is ALSO being "
            "flagged — try re-running capture_substack_session.py to refresh the session "
            "before concluding this path is dead; a stale storage_state can look like this too."
        )
    else:
        logger.error(
            f"Substack rejected the request (status {status}) — this does NOT look like a "
            "Cloudflare/network block, so treat it as a request-format problem first, not an "
            f"auth problem. Response body:\n{result['text'][:2000]}"
        )
    return False
