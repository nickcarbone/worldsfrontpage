"""
World's Front Page — Post-publish email notification

Sends a review email after a successful auto-publish. This exists because
the human review step that used to happen before publish (a draft sitting
in Substack for you to read before clicking Publish) no longer exists in
the pipeline — see main.py's 2026-08-23 auto-publish note.

This is NOT a gate and NEVER blocks or delays anything: it fires after
publish has already succeeded and update_history() has already run. Its
only job is to put the full edition in front of you within minutes, so
things the fabrication guard doesn't check for — a mistranslation, a story
attributed to the wrong country, a brief that mischaracterizes what
actually happened — still get read by a human, just after the fact instead
of before. If sending fails for any reason, that's a warning in the logs,
never a pipeline failure; the edition is already live regardless.

Setup (one-time):
  1. Enable 2-Step Verification on the sending Google account, if not
     already on (required for App Passwords to exist as an option).
  2. Generate an App Password at https://myaccount.google.com/apppasswords
     (name it something like "wfp-pipeline"). It's a 16-character code,
     NOT your real Google password — never use the real password here.
  3. Store it as the GMAIL_APP_PASSWORD secret in the repo (Settings ->
     Secrets and variables -> Actions), and reference it in daily.yml's
     publish job env block.

Defaults to sending from and to carbonen@gmail.com — override via
GMAIL_ADDRESS / NOTIFY_EMAIL_TO env vars if that ever needs to change
without touching code.
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "carbonen@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
NOTIFY_TO = os.environ.get("NOTIFY_EMAIL_TO", GMAIL_ADDRESS)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _build_body(post: dict, curated: list, draft_id: str, run_date) -> str:
    flagged_count = sum(1 for s in curated if s.get("flagged_figures"))

    lines = [
        f"WORLD'S FRONT PAGE — published {run_date.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Draft ID: {draft_id}",
        f"Edit/view in Substack: https://worldsfrontpage.substack.com/publish/post/{draft_id}",
        "",
        post.get("subtitle", ""),
        "=" * 70,
    ]

    if flagged_count:
        noun = "story" if flagged_count == 1 else "stories"
        lines.append(
            f"\n\u26a0\ufe0f  {flagged_count} {noun} below carry unverified numeric figures — "
            "published anyway per standing policy. Marked inline below.\n"
        )

    for story in curated:
        flag = story.get("flagged_figures")
        marker = f"   \u26a0\ufe0f FLAGGED FIGURES: {', '.join(flag)}" if flag else ""
        lines.append(f"\n\U0001f310 {story.get('country', '?')} — {story.get('publication', '?')}{marker}")
        lines.append(f"  {story.get('brief', '')}")
        if story.get("why_it_matters"):
            lines.append(f"  WHY IT MATTERS: {story['why_it_matters']}")

    lines.append("\n" + "=" * 70)
    lines.append(f"{len(curated)} stories total, {flagged_count} flagged.")
    return "\n".join(lines)


def send_publish_notification(post: dict, curated: list, draft_id: str, run_date) -> bool:
    """
    Best-effort post-publish email. Returns True/False for logging purposes
    only — callers must not treat False as a reason to fail the pipeline;
    the edition is already live and history already updated by the time
    this runs.
    """
    if not GMAIL_APP_PASSWORD:
        logger.warning(
            "GMAIL_APP_PASSWORD not set — skipping post-publish notification email. "
            "The edition published fine; this only affects the review email. "
            "See notify.py's module docstring for setup steps."
        )
        return False

    try:
        msg = MIMEMultipart()
        msg["Subject"] = f"[WFP] Published — {post.get('title', run_date.strftime('%Y-%m-%d'))}"
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = NOTIFY_TO

        body = _build_body(post, curated, draft_id, run_date)
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [NOTIFY_TO], msg.as_string())

        logger.info(f"Post-publish notification emailed to {NOTIFY_TO}.")
        return True
    except Exception as e:
        logger.warning(f"Failed to send post-publish notification email: {e}")
        return False
