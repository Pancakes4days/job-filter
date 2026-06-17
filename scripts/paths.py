"""Shared directory layout for the job_filter pipeline.

Project tree:
    job_filter/
      scripts/   the .py files (this module lives here)
      config/    config.json, scraper_config.json, companies.txt
      data/      runtime state + outputs (state, seen, scraped, matched, ...)
      docs/      README.md, CHANGES.md

Importing this keeps every path in one place, so the tree can be reorganized
without hunting down hardcoded paths in each script.
"""

from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
CONFIG_DIR  = BASE_DIR / "config"
DATA_DIR    = BASE_DIR / "data"

# Runtime outputs live under data/; make sure it exists for first-run writes.
DATA_DIR.mkdir(parents=True, exist_ok=True)
