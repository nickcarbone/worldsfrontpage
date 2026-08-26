"""
World's Front Page — Local smoke test for the browser-session publisher

Run this directly on your Mac (same machine as the self-hosted runner) to
verify capture_substack_session.py + publish_to_substack.py actually work,
WITHOUT waiting on GitHub Actions or the ~10+ minute scrape/curate cycle.

IMPORTANT — read this before running (changed 2026-08-23):
publish_to_substack.post_draft_via_browser() now defaults to auto-publish
(SUBSTACK_AUTO_PUBLISH=true), because that's what the production pipeline
wants. This script does NOT want that default: publishing a post titled
"WFP TEST DRAFT" to a live newsletter, and by default emailing it to every
real subscriber, is not a useful test. So this script explicitly passes
publish=False unless you opt in.

Default behavior (`python test_publish_local.py`):
    Creates a draft only. Nothing goes live, nobody gets emailed. Same as
    this script always did. Delete the test draft from Substack afterward.

Opt-in live test (`python test_publish_local.py --live-publish`):
    Actually exercises the prepublish + publish calls end-to-end, so you
    can confirm the real response shape before trusting it in production
    (the shape was reverse-engineered from public docs, not confirmed
    against this account the way draft-creation was — see
    publish_to_substack.py's v3 note). This WILL make a real post appear
    on your public archive page. It passes send=False regardless of your
    SUBSTACK_SEND_EMAIL setting, so it will NOT email subscribers — but it
    is still a real, live, publicly visible post until you delete it.
    Requires typing a confirmation phrase; there's no flag that skips it.

Prerequisites:
  1. You've already run `python capture_substack_session.py` at least once.
  2. `pip install -r requirements.txt` and `playwright install chromium`
     have been run in this environment.

Usage:
    python test_publish_local.py
    python test_publish_local.py --live-publish
"""

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

sys.path.insert(0, "src")  # so `import publish_to_substack` works run from repo root

from publish_to_substack import post_draft_via_browser  # noqa: E402

TEST_POST = {
    "title": "WFP TEST DRAFT — safe to delete",
    "subtitle": "Local smoke test of the browser-session publisher — not a real edition.",
    "body_html": (
        "<p><em>This is a test draft created by test_publish_local.py to verify "
        "the Playwright-based Substack publisher works end to end. It was never "
        "emailed to anyone — delete it whenever.</em></p>"
    ),
    "content_doc": [
        {
            "type": "paragraph",
            "attrs": {"textAlign": None},
            "content": [
                {
                    "type": "text",
                    "marks": [{"type": "em"}],
                    "text": (
                        "This is a test draft created by test_publish_local.py to verify "
                        "the Playwright-based Substack publisher works end to end. It was "
                        "never emailed to anyone — delete it whenever."
                    ),
                }
            ],
        },
    ],
}

CONFIRMATION_PHRASE = "PUBLISH LIVE TEST"


def _confirm_live_publish() -> bool:
    print(
        "\n--live-publish will make a REAL post go live on your public Substack "
        "archive page (worldsfrontpage.substack.com) titled 'WFP TEST DRAFT — safe "
        "to delete'. It will NOT email subscribers (send=False is forced regardless "
        "of your SUBSTACK_SEND_EMAIL setting), but it IS publicly visible until you "
        f"delete it.\n\nType '{CONFIRMATION_PHRASE}' to proceed, anything else to abort: "
    )
    typed = input("> ").strip()
    return typed == CONFIRMATION_PHRASE


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke test for the Substack browser-session publisher")
    parser.add_argument(
        "--live-publish", action="store_true",
        help="Actually run the publish step too (draft-only is the default). Publicly visible; never emails."
    )
    args = parser.parse_args()

    do_publish = False
    if args.live_publish:
        if not _confirm_live_publish():
            print("Aborted — no confirmation given.")
            sys.exit(1)
        do_publish = True

    print("\nAttempting to post a test draft to Substack via the browser session...")
    result = post_draft_via_browser(TEST_POST, publish=do_publish, send=False)

    if not result["success"]:
        print(f"\n✗ FAILED to create draft — error: {result.get('error')}")
        print("  See the log messages above for which failure mode it was:")
        print("  - looks like a network/Cloudflare block → this machine is still being flagged")
        print("  - looks like a rejected/stale session → re-run capture_substack_session.py")
        sys.exit(1)

    print(f"\n✓ Draft created: {result['draft_id']}")

    if not do_publish:
        print('  Check your Substack dashboard for "WFP TEST DRAFT — safe to delete".')
        print("  It was NOT published (default). Delete it once confirmed, or re-run with")
        print("  --live-publish to also test the publish step.")
        sys.exit(0)

    if result["published"]:
        print("\n✓ PUBLISHED (live, no email sent) — go delete the live test post from")
        print("  your Substack dashboard now.")
        sys.exit(0)
    else:
        print(f"\n✗ Draft created but publish FAILED — error: {result.get('error')}")
        print("  The draft exists but is not live. This is useful signal: it means the")
        print("  publish/prepublish endpoint shapes in publish_to_substack.py need a fix")
        print("  before production auto-publish can be trusted. Check the response body")
        print("  logged above against real DevTools output from a manual publish.")
        sys.exit(1)
