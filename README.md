# Biotech Community Sentiment Scanner

A framework for scoring a retail investor community's real-time Telegram
discussion as an approval-odds signal for a heavily-discussed biotech
ticker, paired with the discipline of actually backtesting whether that
signal has any predictive value at all, rather than assuming community
conviction equals edge.

It was built for one ticker (NWBO / Northwest Biotherapeutics, via its
public Telegram investor group) and is extracted here as a standalone,
ticker-agnostic tool. NWBO remains the documented example throughout; swap
the config and it works for any name with an active, heavily-discussed
community.

## What it is, and what risk it addresses

Retail investor communities (Telegram groups, StockTwits, subreddits)
generate huge volumes of noisy, emotionally-driven chatter around
event-driven names (FDA decisions being the canonical case). It's easy to
mistake *volume* or *enthusiasm* for actual *predictive signal*: a group
getting louder and more bullish feels like information, but that feeling is
not evidence.

This project does two things, deliberately kept separate:

1. **Score** the community's discussion into a structured, auditable
   sentiment/approval-probability series (`sentiment_analyzer.py`,
   `telegram_scanner.py`), backed by a retained full-text corpus
   (`corpus_builder.py`) so every number can be traced back to the actual
   messages behind it.
2. **Test** whether that series has any real relationship to subsequent
   price moves (`sentiment_backtest.py`), instead of assuming it does.

The second part is the point. A sentiment scanner that only ever reports
"the community is bullish" without ever checking whether that mattered is
not a research tool, it's a vibe amplifier.

## Safety & governance design

- **Deterministic, auditable scoring.** Sentiment/approval scores come from
  a fixed keyword lexicon and simple arithmetic (`sentiment_analyzer.py`,
  `telegram_scanner.py`), not an opaque model. `corpus_builder.py` retains
  the full analytical message *text* behind the scores (filtered for
  length + keyword relevance so it's substantive discussion, not noise),
  so any score can be checked against the messages that produced it.
- **Edge-triggered alerting with hysteresis.** `threshold_alert.py` fires
  exactly once when the probability series crosses a threshold, then
  requires it to cross back before firing again (`alerted_above` state).
  This is a deliberate anti-noise choice: without it, a value oscillating
  near the threshold would re-alert on every cron tick. Alert fatigue is a
  real failure mode for this kind of tool: a scanner that cries wolf gets
  ignored, which defeats the point of having one.
- **LLM constrained to rationale generation.** `ticker_tracker.py` calls
  Gemini only to *summarize why a price move already detected by other data
  (price, SEC filings, news) might be happening*. It never decides whether
  to alert, never scores sentiment, and never proposes a trade; that logic
  is all deterministic code above it. The model gets already-fetched facts
  and is asked to write four to six sentences about them.
- **Rate-limited, session-respectful Telegram API usage.** `telegram_scanner.py`
  (the live listener) and `sentiment_analyzer.py` / `corpus_builder.py`
  (read-only history pulls) use **separate Telethon session files**
  (`SESSION_LIVE` vs `SESSION_READER` in `config.py`) specifically so a
  scheduled historical re-analysis never contends for the same session lock
  as the always-on listener. Running both against one session file is a
  real, easy-to-hit failure mode with Telethon; this avoids it by
  construction rather than by remembering not to do it.

## The honest finding: sentiment lags price, it doesn't lead it

This is the most important thing in this README, not a footnote.

Running `sentiment_backtest.py` against NWBO's ~8-year Telegram history
found that the community's own approval-probability sentiment **lags**
price rather than leading it. The lag-correlation analysis consistently
found the strongest relationship at *negative* lag, meaning the community's
conviction moves *after* the stock does, not before.

In plain terms: when NWBO's price moved, the community's sentiment shifted
to match it afterward. It did not reliably shift *first*. A naive
"buy when the community turns bullish" strategy built on this signal would,
per this data, be trading on a lagging indicator: buying into moves that
have already happened rather than anticipating ones about to happen.

**What this tool is validated for:** a conviction/attention filter. It is
genuinely useful for flagging "this name's community sentiment just crossed
a threshold, a human should look at this" and for building an auditable,
quantified record of what a community was saying and when.

**What this tool is explicitly NOT validated for:** a standalone directional
trading signal. Don't wire the threshold alert straight into an execution
system. That would be building on a result this project's own backtest
argues against.

If you point this at a different ticker/community, run
`sentiment_backtest.py` yourself before trusting the signal at all.
The NWBO result is not assumed to generalize; it's reported because it's
what was actually found, and the same discipline should be applied to
whatever you point this at next.

## Autonomy boundary

This tool **scores, alerts, and reports. It never trades or acts on its own
signal.** Every script here is read/analyze/notify. There is no order
execution, no brokerage API integration, and no code path that turns a
probability crossing a threshold into an actual position. Any decision to
act on what this tool reports is a human decision, made outside this
codebase.

## Ethical considerations

- This scrapes a **public but community-specific** Telegram group. Anyone
  deploying this against a different community should check that
  community's own norms and Terms of Service around scraping/analysis
  before pointing a Telethon session at it.
- `corpus_builder.py` aggregates message **text for analysis**, not for
  republishing or attributing individual users. The corpus it produces is
  explicitly excluded from this repo (see `.gitignore`); it's the
  community's data, not code, and shouldn't ship alongside the tool.
- Aggregate sentiment analysis (counting how many messages hit bullish vs.
  bearish keywords, this week vs. last) is a materially different thing
  from surveilling or profiling individual posters. Nothing here builds
  per-user profiles or tracks individuals across time; `telegram_scanner.py`
  keeps a sender name on live alerts only as much as the original Telegram
  message already exposes it, purely for immediate context.

## How it works

```
telegram_scanner.py    ── live daemon, Telethon event listener
                           → data/signals.json (rolling real-time state)

corpus_builder.py       ── full-history scrape, filtered to analytical posts
                           → data/corpus.json, data/corpus.txt

sentiment_analyzer.py   ── full-history scrape + scoring, daily probability series
                           → data/analysis.json (+ optional Telegram report)

threshold_alert.py      ── reads data/analysis.json, edge-triggered alert
                           → data/alert_state.json

sentiment_backtest.py   ── reads data/analysis.json + yfinance price history
                           → data/backtest.json (+ optional Telegram report)
                           lag-correlation + simple threshold backtest

ticker_tracker.py       ── independent: price/SEC/news mover tracker
                           → data/tracker_state.json (+ optional Telegram alerts)
                           Gemini used only for rationale text
```

`telegram_scanner.py`, `sentiment_analyzer.py`, `corpus_builder.py`, and
`ticker_tracker.py` are independent entry points; you don't need all four
running to get value from one of them. `threshold_alert.py` and
`sentiment_backtest.py` both depend on `sentiment_analyzer.py` having run
at least once (they read `data/analysis.json`).

### Usage

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your Telegram/Gemini credentials and target group

# One-time: authenticate the Telegram user session and discover your group ID
python telegram_scanner.py --auth

# Live real-time listener (run as a long-lived process/service)
python telegram_scanner.py

# Daily: full-history scrape + scoring (cron, e.g. 0 8 * * *)
python sentiment_analyzer.py

# After the above: edge-triggered threshold alert
python threshold_alert.py

# Anytime after sentiment_analyzer.py has produced data/analysis.json:
python sentiment_backtest.py

# Anytime: build/refresh the full-text analytical corpus (slow for large groups)
python corpus_builder.py

# Independent price/news/SEC-filing tracker (cron, every few minutes)
python ticker_tracker.py
```

All state, logs, Telethon sessions, and scraped output live under `data/`
by default (override with `SCANNER_DATA_DIR`). Nothing under `data/` is
committed to this repo; see `.gitignore`.

## Repo layout

```
config.py               shared config, loaded entirely from env vars / .env
telegram_scanner.py      real-time Telegram listener
sentiment_analyzer.py    historical scoring + daily probability series
sentiment_backtest.py    sentiment vs. price backtest + lag correlation
corpus_builder.py        full-text analytical corpus builder
threshold_alert.py       edge-triggered threshold alert
ticker_tracker.py        price/SEC-filing/news tracker with LLM rationale
requirements.txt
.env.example
.gitignore
LICENSE
README.md
```
