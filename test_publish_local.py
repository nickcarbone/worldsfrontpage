"""
World's Front Page — Local smoke test for the browser-session publisher

Run this directly on your Mac (same machine as the self-hosted runner) to
verify capture_substack_session.py + publish_to_substack.py actually work,
WITHOUT waiting on GitHub Actions or the ~10+ minute scrape/curate cycle.

This posts a real (but obviously-a-test) draft to Substack. It does NOT
publish or email anyone — drafts are private until you hit Publish yourself
inside Substack's editor. Delete the test draft afterward.

Prerequisites:
  1. You've already run `python capture_substack_session.py` at least once.
  2. `pip install -r requirements.txt` and `playwright install chromium`
     have been run in this environment.

Usage:
    python test_publish_local.py
"""

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
        "published or emailed to anyone — delete it whenever.</em></p>"
    ),
}

if __name__ == "__main__":
    print("Attempting to post a test draft to Substack via the browser session...")
    success = post_draft_via_browser(TEST_POST)
    if success:
        print("\n✓ SUCCESS — check your Substack dashboard for a draft titled")
        print('  "WFP TEST DRAFT — safe to delete". Delete it once confirmed.')
        sys.exit(0)
    else:
        print("\n✗ FAILED — see the log messages above for which failure mode it was:")
        print("  - looks like a network/Cloudflare block → this machine is still being flagged")
        print("  - looks like a rejected/stale session → re-run capture_substack_session.py")
        sys.exit(1)
