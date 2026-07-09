#!/usr/bin/env python3
"""
Ticker Tracker
Monitors the configured ticker for:
  - Significant price movements (from prior close, or intraday spike)
  - New SEC filings (8-K, 10-Q, 10-K, S-3, DEF14A, ...) via EDGAR
  - News from yfinance + StockTwits sentiment
  - An LLM-generated rationale for why it's moving + overall sentiment

The LLM (Gemini by default) is used ONLY to summarize/explain a move that
already happened from data already fetched — it never decides whether to
alert, and it never generates a trade signal. See README "Autonomy boundary".

Intended to run every few minutes via cron during market hours, less often
off-hours.
"""

import os
import json
import time
from datetime import datetime, timezone

import requests
import yfinance as yf
import feedparser

import config

TICKER = config.TICKER
DATA_DIR = config.DATA_DIR
STATE_FILE = DATA_DIR / 'tracker_state.json'
LOG_FILE = DATA_DIR / 'tracker.log'

GEMINI_API_KEY = config.GEMINI_API_KEY
GEMINI_MODEL = config.GEMINI_MODEL
GEMINI_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID

# SEC EDGAR CIK for the ticker under coverage — see config.py / .env.example
SEC_CIK = config.SEC_CIK
SEC_RSS_URL = f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={SEC_CIK}&type=&dateb=&owner=include&count=10&search_text=&output=atom'

# Alert thresholds
PRICE_ALERT_PCT_FROM_CLOSE = 8.0   # % from prior close
PRICE_ALERT_PCT_INTRADAY = 5.0     # % move since last check
PRICE_ALERT_COOLDOWN_MIN = 240     # min between price alerts
NEWS_ALERT_COOLDOWN_MIN = 120      # min between news alerts

HEADERS = {'User-Agent': f'biotech-sentiment-scanner/1.0 {TICKER.lower()}-tracker (research)'}


# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'last_price': None,
        'prior_close': None,
        'last_price_alert_ts': None,
        'last_news_alert_ts': None,
        'seen_news_ids': [],
        'seen_sec_ids': [],
        'last_sentiment': None,
        'last_analysis': None,
        'weekly_brief_date': None,
    }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log('No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured — skipping send')
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': msg,
            'parse_mode': 'Markdown'
        }, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")


def minutes_since(ts_str):
    if not ts_str:
        return 9999
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except Exception:
        return 9999


# ── Gemini (rationale generation only — see module docstring) ────────────────
def gemini_analyze(prompt):
    if not GEMINI_API_KEY:
        log('No GEMINI_API_KEY configured — skipping analysis')
        return None
    for attempt in range(3):
        try:
            res = requests.post(
                GEMINI_URL,
                params={'key': GEMINI_API_KEY},
                json={'contents': [{'parts': [{'text': prompt}]}]},
                timeout=35
            )
            if res.status_code == 200:
                return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            elif res.status_code in (429, 503):
                time.sleep(2 ** attempt)
            else:
                log(f"Gemini error {res.status_code}: {res.text[:200]}")
                return None
        except Exception as e:
            log(f"Gemini exception: {e}")
            time.sleep(2 ** attempt)
    return None


# ── Data Fetchers ─────────────────────────────────────────────────────────────
def get_price_data():
    """Returns (current_price, prior_close, day_open, day_high, day_low, volume)"""
    try:
        t = yf.Ticker(TICKER)
        info = t.fast_info
        current = float(info.last_price) if hasattr(info, 'last_price') and info.last_price else None
        prior_close = float(info.previous_close) if hasattr(info, 'previous_close') and info.previous_close else None
        hist = t.history(period='1d', interval='5m')
        if not hist.empty:
            day_open = float(hist['Open'].iloc[0])
            day_high = float(hist['High'].max())
            day_low = float(hist['Low'].min())
            volume = int(hist['Volume'].sum())
        else:
            day_open = day_high = day_low = volume = None
        return current, prior_close, day_open, day_high, day_low, volume
    except Exception as e:
        log(f"Price fetch error: {e}")
        return None, None, None, None, None, None


def get_yf_news():
    """Returns list of {id, title, publisher, link, published} for recent news"""
    try:
        t = yf.Ticker(TICKER)
        news = t.news or []
        results = []
        for item in news[:10]:
            content = item.get('content', {})
            title = content.get('title', item.get('title', ''))
            pub = content.get('provider', {})
            publisher = pub.get('displayName', '') if isinstance(pub, dict) else str(pub)
            link = content.get('canonicalUrl', {}).get('url', item.get('link', ''))
            pub_time = content.get('pubDate', '')
            item_id = item.get('id', item.get('uuid', str(hash(title))))
            results.append({
                'id': str(item_id),
                'title': title,
                'publisher': publisher,
                'link': link,
                'published': pub_time,
            })
        return results
    except Exception as e:
        log(f"News fetch error: {e}")
        return []


def get_sec_filings():
    """Returns list of {id, title, filing_type, link, published} from EDGAR RSS"""
    try:
        feed = feedparser.parse(SEC_RSS_URL)
        results = []
        for entry in feed.entries[:10]:
            filing_id = entry.get('id', entry.get('link', ''))
            title = entry.get('title', '')
            link = entry.get('link', '')
            published = entry.get('published', '')
            filing_type = title.split(' - ')[0].strip() if ' - ' in title else 'UNKNOWN'
            results.append({
                'id': filing_id,
                'title': title,
                'filing_type': filing_type,
                'link': link,
                'published': published,
            })
        return results
    except Exception as e:
        log(f"SEC EDGAR fetch error: {e}")
        return []


def get_stocktwits_sentiment():
    """Returns (sentiment_str, bull_count, bear_count) from StockTwits"""
    try:
        url = f'https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json'
        res = requests.get(url, timeout=10, headers=HEADERS)
        if res.status_code != 200:
            return None, 0, 0
        data = res.json()
        messages = data.get('messages', [])
        bull = sum(1 for m in messages if m.get('entities', {}).get('sentiment', {}) and
                   m['entities']['sentiment'].get('basic') == 'Bullish')
        bear = sum(1 for m in messages if m.get('entities', {}).get('sentiment', {}) and
                   m['entities']['sentiment'].get('basic') == 'Bearish')
        total = bull + bear
        if total == 0:
            return 'NEUTRAL', 0, 0
        sentiment = 'BULLISH' if bull > bear else ('BEARISH' if bear > bull else 'NEUTRAL')
        return sentiment, bull, bear
    except Exception as e:
        log(f"StockTwits error: {e}")
        return None, 0, 0


# ── Analysis ──────────────────────────────────────────────────────────────────
def build_analysis_prompt(trigger, current_price, prior_close, pct_change,
                           news_items, sec_filings, st_sentiment, bull, bear):
    news_block = "\n".join([f"- [{n['publisher']}] {n['title']}" for n in news_items[:5]]) or "No recent news found."
    sec_block = "\n".join([f"- {s['filing_type']}: {s['title']} ({s['published'][:10]})" for s in sec_filings[:3]]) or "No recent SEC filings."
    st_block = f"{st_sentiment} ({bull} bullish / {bear} bearish messages)" if st_sentiment else "N/A"

    return f"""You are a biotech equity analyst covering {config.COMPANY_DESCRIPTION}.

Current situation:
- Trigger: {trigger}
- Current price: ${current_price:.4f}
- Prior close: ${prior_close:.4f}
- Change: {pct_change:+.1f}%

Recent news:
{news_block}

Recent SEC filings:
{sec_block}

StockTwits sentiment: {st_block}

Tasks:
1. WHY IS IT MOVING? Identify the most likely catalyst(s) driving today's price action. Be specific — mention the news item, filing, or macro factor if identifiable.
2. SENTIMENT: What is the overall market sentiment right now? (BULLISH / BEARISH / NEUTRAL) and why.
3. RISK FACTORS: List 1-2 key near-term risks to watch.
4. CONFIDENCE: How confident are you in your analysis? (LOW / MEDIUM / HIGH)

Keep response concise — 4-6 sentences total. Start with the sentiment label on its own line."""


def run_analysis(trigger, current_price, prior_close, pct_change, news_items, sec_filings):
    st_sentiment, bull, bear = get_stocktwits_sentiment()
    prompt = build_analysis_prompt(trigger, current_price, prior_close, pct_change,
                                    news_items, sec_filings, st_sentiment, bull, bear)
    return gemini_analyze(prompt), st_sentiment, bull, bear


# ── Alert Formatters ──────────────────────────────────────────────────────────
def format_price_alert(current_price, prior_close, pct_from_close, pct_intraday, analysis, st_sentiment, bull, bear):
    direction = "\U0001F7E2" if pct_from_close > 0 else "\U0001F534"
    return f"""{direction} *{TICKER} Price Alert*

Price: *${current_price:.4f}*
vs Prior Close: *{pct_from_close:+.1f}%* (${prior_close:.4f})
Intraday Move: *{pct_intraday:+.1f}%*
StockTwits: {st_sentiment or 'N/A'} ({bull} bull / {bear} bear)

{analysis or '_Analysis unavailable_'}"""


def format_news_alert(news_item, sec_filing, current_price, pct_from_close, analysis, st_sentiment, bull, bear):
    if sec_filing:
        header = f"*{TICKER} SEC Filing: {sec_filing['filing_type']}*\n_{sec_filing['title']}_"
    else:
        header = f"*{TICKER} News Alert*\n_{news_item['title']}_\n— {news_item.get('publisher', '')}"

    return f"""{header}

Price: *${current_price:.4f}* ({pct_from_close:+.1f}% vs close)
StockTwits: {st_sentiment or 'N/A'} ({bull} bull / {bear} bear)

{analysis or '_Analysis unavailable_'}"""


def format_weekly_brief(current_price, prior_close, pct_from_close, day_high, day_low, volume,
                         news_items, sec_filings, analysis, st_sentiment, bull, bear):
    news_lines = "\n".join([f"  - {n['title'][:80]}" for n in news_items[:3]]) or "  None"
    sec_lines = "\n".join([f"  - {s['filing_type']}: {s['title'][:60]}" for s in sec_filings[:2]]) or "  None"
    vol_str = f"{volume:,}" if volume else "N/A"
    hi_lo = f"${day_high:.4f} / ${day_low:.4f}" if day_high else "N/A"

    return f"""*{TICKER} Weekly Brief — {datetime.now().strftime('%b %d, %Y')}*

Price: *${current_price:.4f}* ({pct_from_close:+.1f}% vs close)
Hi/Lo: {hi_lo} | Vol: {vol_str}
StockTwits: {st_sentiment or 'N/A'} ({bull} bull / {bear} bear)

Recent News:
{news_lines}

Recent SEC Filings:
{sec_lines}

Analysis:
{analysis or '_Unavailable_'}"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log("Tracker run start")
    state = load_state()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime('%Y-%m-%d')

    current_price, prior_close, day_open, day_high, day_low, volume = get_price_data()
    if not current_price:
        log("Could not fetch price — aborting")
        return

    if not prior_close and state.get('prior_close'):
        prior_close = state['prior_close']

    pct_from_close = ((current_price - prior_close) / prior_close * 100) if prior_close else 0
    last_price = state.get('last_price') or prior_close or current_price
    pct_intraday = ((current_price - last_price) / last_price * 100) if last_price else 0

    log(f"Price: ${current_price:.4f} | vs close: {pct_from_close:+.1f}% | vs last: {pct_intraday:+.1f}%")

    news_items = get_yf_news()
    sec_filings = get_sec_filings()

    seen_news = set(state.get('seen_news_ids', []))
    seen_sec = set(state.get('seen_sec_ids', []))

    new_news = [n for n in news_items if n['id'] not in seen_news]
    new_sec = [s for s in sec_filings if s['id'] not in seen_sec]

    log(f"New news: {len(new_news)} | New SEC filings: {len(new_sec)}")

    # ── Weekly brief (Mondays, ~9am ET / 13:00 UTC) ──────────────────────────
    is_market_hours = 13 <= now_utc.hour < 21
    last_brief = state.get('weekly_brief_date')
    days_since_brief = 999
    if last_brief:
        try:
            days_since_brief = (datetime.strptime(today, '%Y-%m-%d').date() -
                                 datetime.strptime(last_brief, '%Y-%m-%d').date()).days
        except ValueError:
            pass

    if now_utc.weekday() == 0 and days_since_brief >= 6 and now_utc.hour >= 12:
        log("Sending weekly brief")
        analysis, st_sent, bull, bear = run_analysis(
            "Weekly brief", current_price, prior_close or current_price,
            pct_from_close, news_items, sec_filings
        )
        msg = format_weekly_brief(current_price, prior_close or current_price,
                                   pct_from_close, day_high, day_low, volume,
                                   news_items, sec_filings, analysis, st_sent, bull, bear)
        send_telegram(msg)
        state['weekly_brief_date'] = today
        state['last_sentiment'] = st_sent
        state['last_analysis'] = analysis
        log("Weekly brief sent")

    elif new_sec and minutes_since(state.get('last_news_alert_ts')) >= NEWS_ALERT_COOLDOWN_MIN:
        filing = new_sec[0]
        log(f"New SEC filing: {filing['filing_type']} — {filing['title']}")
        analysis, st_sent, bull, bear = run_analysis(
            f"New SEC filing: {filing['filing_type']}", current_price,
            prior_close or current_price, pct_from_close,
            news_items, [filing]
        )
        msg = format_news_alert(None, filing, current_price, pct_from_close, analysis, st_sent, bull, bear)
        send_telegram(msg)
        state['last_news_alert_ts'] = now_utc.isoformat()
        state['last_sentiment'] = st_sent
        state['last_analysis'] = analysis
        log("SEC filing alert sent")

    elif new_news and minutes_since(state.get('last_news_alert_ts')) >= NEWS_ALERT_COOLDOWN_MIN:
        item = new_news[0]
        log(f"New news: {item['title']}")
        analysis, st_sent, bull, bear = run_analysis(
            f"New article: {item['title']}", current_price,
            prior_close or current_price, pct_from_close,
            news_items, sec_filings
        )
        msg = format_news_alert(item, None, current_price, pct_from_close, analysis, st_sent, bull, bear)
        send_telegram(msg)
        state['last_news_alert_ts'] = now_utc.isoformat()
        state['last_sentiment'] = st_sent
        state['last_analysis'] = analysis
        log("News alert sent")

    elif (abs(pct_from_close) >= PRICE_ALERT_PCT_FROM_CLOSE or
          abs(pct_intraday) >= PRICE_ALERT_PCT_INTRADAY) and \
            minutes_since(state.get('last_price_alert_ts')) >= PRICE_ALERT_COOLDOWN_MIN:
        trigger = (f"{pct_from_close:+.1f}% from prior close"
                   if abs(pct_from_close) >= PRICE_ALERT_PCT_FROM_CLOSE
                   else f"{pct_intraday:+.1f}% intraday spike")
        log(f"Price alert: {trigger}")
        analysis, st_sent, bull, bear = run_analysis(
            trigger, current_price, prior_close or current_price,
            pct_from_close, news_items, sec_filings
        )
        msg = format_price_alert(current_price, prior_close or current_price,
                                  pct_from_close, pct_intraday, analysis, st_sent, bull, bear)
        send_telegram(msg)
        state['last_price_alert_ts'] = now_utc.isoformat()
        state['last_sentiment'] = st_sent
        state['last_analysis'] = analysis
        log("Price alert sent")

    else:
        log(f"No trigger — price {pct_from_close:+.1f}% from close, {pct_intraday:+.1f}% intraday, no new news")

    state['last_price'] = current_price
    state['prior_close'] = prior_close or state.get('prior_close')
    state['seen_news_ids'] = list((seen_news | {n['id'] for n in news_items}))[-50:]
    state['seen_sec_ids'] = list((seen_sec | {s['id'] for s in sec_filings}))[-50:]
    save_state(state)
    log("Run complete")


if __name__ == '__main__':
    main()
