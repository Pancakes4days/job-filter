#!/usr/bin/env python3
"""
notify.py — outbound alerts for time-sensitive postings. Email, stdlib only.

Email rather than SMS on purpose: the only free SMS path is the carrier
email-to-text gateways (5551234567@vtext.com), and carriers filter and retire
those without notice — you would find out it broke by NOT getting the alert you
cared about. A push-enabled mailbox arrives just as fast and cannot silently
disappear.

Config lives in config/local.json (gitignored) under "notify":

    "notify": {
      "enabled":      false,
      "smtp_host":    "smtp.gmail.com",
      "smtp_port":    587,
      "username":     "you@gmail.com",
      "app_password": "16-char Google app password, NOT your login password",
      "to":           "you@gmail.com"
    }

Gmail rejects account passwords over SMTP; generate an app password at
https://myaccount.google.com/apppasswords (needs 2-step verification on).

Until "enabled" is true every send is a dry run that prints the message instead,
so callers can be built and tested before any credentials exist.

    python3 notify.py --test             # print (or send) a sample alert
"""

import argparse
import json
import smtplib
import ssl
from email.message import EmailMessage

from paths import CONFIG_DIR

LOCAL_JSON = CONFIG_DIR / "local.json"
REQUIRED_KEYS = ("smtp_host", "smtp_port", "username", "app_password", "to")


def load_notify_config():
    """The "notify" block from local.json, or {} if absent/unreadable.

    Deliberately never raises. A missing or malformed local.json has to degrade
    to a dry run — it must not take down the poller that called us.

    A parse failure is reported under _problem rather than swallowed: a block
    pasted outside the closing brace is invalid JSON, and saying "no notify
    block" there sends you looking for a missing key that is right in front of
    you.
    """
    if not LOCAL_JSON.exists():
        return {"_problem": f"{LOCAL_JSON} does not exist"}
    try:
        parsed = json.loads(LOCAL_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"_problem": f"{LOCAL_JSON.name} is not valid JSON — {e}. "
                            f"The notify block goes INSIDE the outer {{ }}, "
                            f"after a comma."}
    except OSError as e:
        return {"_problem": f"cannot read {LOCAL_JSON.name} ({e})"}
    return parsed.get("notify") or {}


def dry_run_reason(cfg, forced=False):
    """Why this send would only be printed, or "" if it would really go out."""
    if forced:
        return "--dry-run"
    if cfg.get("_problem"):
        return cfg["_problem"]
    if not cfg:
        return "no \"notify\" block in config/local.json"
    if not cfg.get("enabled"):
        return "notify.enabled is false"
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        return "notify config missing: " + ", ".join(missing)
    return ""


def send(subject, body, dry_run=False):
    """Email `body` as plain text. True if it was sent (or printed as a dry run).

    A send failure returns False and prints the alert instead, so the caller can
    decline to mark those postings as notified and retry on the next tick — for
    a time-sensitive alert, a duplicate email beats a missed one.
    """
    cfg = load_notify_config()
    reason = dry_run_reason(cfg, forced=dry_run)
    if reason:
        print(f"--- DRY RUN ({reason}) ---")
        print(f"To:      {cfg.get('to') or '(unset)'}")
        print(f"Subject: {subject}")
        print()
        print(body)
        print("--- end dry run ---")
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["username"]
    msg["To"] = cfg["to"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg["username"], cfg["app_password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError, ValueError) as e:
        print(f"  ! email failed ({type(e).__name__}: {e}) — alert follows")
        print(f"  ! {subject}")
        print(body)
        return False
    print(f"  emailed {cfg['to']}: {subject}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Send a test notification.")
    ap.add_argument("--test", action="store_true", help="send a sample alert")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = ap.parse_args()

    cfg = load_notify_config()
    reason = dry_run_reason(cfg, forced=args.dry_run)
    print(f"notify config: {'DRY RUN — ' + reason if reason else 'live, sending to ' + cfg['to']}")
    if args.test:
        send("[job-filter] test alert",
             "If this reached your phone, the fast lane can reach you too.",
             dry_run=args.dry_run)


if __name__ == "__main__":
    main()
