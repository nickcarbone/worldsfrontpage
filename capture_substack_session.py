"""
World's Front Page — One-time Substack session capture

Run this directly on the self-hosted Mac (never on GitHub's runners, and
never anywhere else). Opens a real, visible Chrome window; you log into
Substack by hand, exactly as you normally would — including any 2FA prompt
or Cloudflare "checking your browser" screen. Once you're looking at your
publish dashboard, come back to the terminal and press Enter. The script
saves that authenticated session to disk for publish_to_substack.py to reuse.

Run it again any time posting starts failing with a "session likely stale"
error in the logs — sessions can expire or get invalidated.

Usage:
    python capture_substack_session.py

The output file is equivalent to being logged into your Substack account.
Do not commit it, upload it, paste it anywhere, or copy it off this machine.
"""

import os
from pathlib import Path
from playwright.sync_api import sync_playwright

SUBSTACK_STATE_PATH = Path(
    os.environ.get("SUBSTACK_STATE_PATH", str(Path.home() / "wfp-runner" / "substack_state.json"))
)
SUBSTACK_PUB_URL = os.environ.get("SUBSTACK_PUB_URL", "https://worldsfrontpage.substack.com")


def main():
    SUBSTACK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{SUBSTACK_PUB_URL}/publish/login")

        print("\nA Chrome window just opened.")
        print("Log into Substack normally in that window — handle 2FA or any")
        print("Cloudflare check exactly as you would in your everyday browser.")
        print("Once you can see your publish dashboard, come back here and press Enter.")
        input()

        context.storage_state(path=str(SUBSTACK_STATE_PATH))
        browser.close()

    print(f"\nSaved authenticated session to {SUBSTACK_STATE_PATH}")
    print("This file is equivalent to your login. Keep it on this machine only —")
    print("never commit it, upload it, or paste its contents anywhere.")


if __name__ == "__main__":
    main()
