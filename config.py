"""
Shared configuration for the biotech community sentiment scanner.

Everything here is loaded from environment variables (optionally via a
`.env` file; see `.env.example`). Nothing in this file is a real credential
or a community-specific value; the NWBO defaults that appear as fallbacks
are the *documented example* this tool was originally built around
(Northwest Biotherapeutics' Telegram investor community), not a requirement
to use that community.

To point this at a different ticker/community, set the env vars in
`.env.example`. You don't need to touch any of the six scripts.
"""
import os
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars can be set any other way


def _get(name, default=None):
    return os.environ.get(name, default)


def _get_required(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"See .env.example for the full list of required config."
        )
    return val


def _split_terms(name, default=""):
    raw = _get(name, default)
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


# ── Telegram API credentials (from https://my.telegram.org) ──────────────────
# These belong to a Telegram *user* account (Telethon uses the MTProto user
# API, not the bot API, so it can read full group history like a member can).
TELEGRAM_API_ID = int(_get("TELEGRAM_API_ID", "0") or "0")
TELEGRAM_API_HASH = _get("TELEGRAM_API_HASH")

# ── Target community group ────────────────────────────────────────────────────
# Numeric Telegram chat/channel ID for the community you're monitoring.
# Find yours by running telegram_scanner.py --auth once and reading the
# dialog list it prints, or by resolving an invite link with TELEGRAM_INVITE_HASH.
_group_id_raw = _get("TELEGRAM_GROUP_ID")
TELEGRAM_GROUP_ID = int(_group_id_raw) if _group_id_raw else None
TELEGRAM_INVITE_HASH = _get("TELEGRAM_INVITE_HASH")  # hash from t.me/joinchat/HASH, if joining via invite link

# ── Bot used to push alerts/reports (separate from the API creds above) ──────
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")

# ── Gemini: used ONLY for rationale/summary generation, never for signal
#    generation or trading decisions. See README "Autonomy boundary". ────────
GEMINI_API_KEY = _get("GEMINI_API_KEY")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.0-flash-lite")

# ── Ticker / company under coverage ───────────────────────────────────────────
TICKER = _get("TICKER", "NWBO")
SEC_CIK = _get("SEC_CIK", "0000849399")  # SEC EDGAR CIK; look yours up at sec.gov/cgi-bin/browse-edgar
COMPANY_DESCRIPTION = _get(
    "COMPANY_DESCRIPTION",
    "Northwest Biotherapeutics (NWBO), a clinical-stage glioblastoma "
    "immunotherapy company (DCVax-L product)",
)

# ── Approval-probability model base rate ──────────────────────────────────────
# Starting point the sentiment adjustment is applied on top of. The NWBO
# default below reflects a rough base rate for an oncology drug with positive
# Phase 3 overall-survival data. Pick a number appropriate to your own name's
# regulatory pathway/indication, this is a coarse prior, not a calibrated model.
BASE_APPROVAL_PROB = float(_get("BASE_APPROVAL_PROB", "0.48"))

# ── Optional ticker/community-specific lexicon additions ─────────────────────
# The base sentiment lexicon in each script is generic trading/FDA-catalyst
# language. Add your community's specific jargon (drug names, trial names,
# nicknames) here rather than editing the scripts. Values below are commented
# out; uncomment/edit in your own .env. The strings shown in .env.example are
# what NWBO's own community used, kept only as a worked example.
EXTRA_BULLISH_TERMS = _split_terms("EXTRA_BULLISH_TERMS")
EXTRA_BEARISH_TERMS = _split_terms("EXTRA_BEARISH_TERMS")
EXTRA_CATALYST_TERMS = _split_terms("EXTRA_CATALYST_TERMS")
EXTRA_ANALYTICAL_KEYWORDS = _split_terms("EXTRA_ANALYTICAL_KEYWORDS")

# ── Alert / backtest thresholds ───────────────────────────────────────────────
ALERT_THRESHOLD = float(_get("ALERT_THRESHOLD", "62.0"))
BACKTEST_BUY_THRESHOLD = float(_get("BACKTEST_BUY_THRESHOLD", "62.0"))
BACKTEST_SELL_THRESHOLD = float(_get("BACKTEST_SELL_THRESHOLD", "57.0"))
BACKTEST_START_DATE = _get("BACKTEST_START_DATE", "2018-01-01")

# ── Local data/session paths ──────────────────────────────────────────────────
# Everything the scripts read/write at runtime (Telethon sessions, scraped
# corpus, analysis output, state files, logs) lives under one data directory
# so it's easy to .gitignore in one shot. See .gitignore.
DATA_DIR = Path(_get("SCANNER_DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Two separate Telethon session files by design: the live listener
# (telegram_scanner.py) and any read-only history pull (sentiment_analyzer.py,
# corpus_builder.py) must never share a session file, or Telethon's session
# lock causes one of them to fail whenever both run at the same time.
SESSION_LIVE = str(DATA_DIR / "session_live")
SESSION_READER = str(DATA_DIR / "session_reader")

LOCK_FILE = Path(tempfile.gettempdir()) / f"{TICKER.lower()}_sentiment_analyzer.lock"
