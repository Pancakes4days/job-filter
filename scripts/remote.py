"""Shared laptop-sync plumbing: config/local.json + scp.

Used by orchestrator.py (automated sync) and prune_workbook.py (manual prune)
so the transport options and the remote path format live in exactly one place —
if they drift, the manual prune can push to a different location than the
automated sync pulls from.
"""

import json
import subprocess

from paths import CONFIG_DIR

LOCAL_JSON = CONFIG_DIR / "local.json"


def load_local_config():
    """Parse config/local.json, exiting with a setup hint if it's missing."""
    if not LOCAL_JSON.exists():
        raise SystemExit(
            f"Missing {LOCAL_JSON}\n"
            f"Copy config/local.example.json → config/local.json and fill in "
            f"your Tailscale details."
        )
    return json.loads(LOCAL_JSON.read_text(encoding="utf-8"))


def remote_base(cfg):
    """The user@host:dir/ prefix for scp paths on the laptop."""
    return f"{cfg['remote_user']}@{cfg['remote_host']}:{cfg['remote_dir']}/"


def scp(args):
    """Run scp with the standard non-interactive options. Returns CompletedProcess."""
    return subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", *args])
