#!/usr/bin/env python3
"""
Community Approval-Probability Analyzer
Pulls full message history from the configured Telegram group, scores every
message for FDA-approval-relevant signal, builds a daily probability time
series, and sends a Telegram summary report.

Intended to run on a daily cron (e.g. 0 8 * * *).
Output: data/analysis.json, plus an optional Telegram report.
"""
import json
import sys
import logging
import asyncio
from datetime import datetime, timezone
from collections import defaultdict

from telethon import TelegramClient

import config

API_ID = config.TELEGRAM_API_ID
API_HASH = config.TELEGRAM_API_HASH
GROUP_ID = config.TELEGRAM_GROUP_ID
TICKER = config.TICKER

SESSION = config.SESSION_READER
OUTPUT = config.DATA_DIR / "analysis.json"
LOG_FILE = config.DATA_DIR / "analyzer.log"
LOCK_FILE = config.LOCK_FILE

BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT_ID = config.TELEGRAM_CHAT_ID

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{TICKER}-ANALYZER] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Approval Signal Lexicon ────────────────────────────────────────────────────
# Base lexicon is generic FDA/regulatory-approval and trading language.
# Ticker/drug-specific jargon (e.g. a drug name, trial acronym, or indication)
# should go in EXTRA_*_TERMS in your .env rather than here — see .env.example
# for the NWBO worked example (dcvax, glioblastoma, etc.)
STRONG_BULL = {
    'fda approved', 'approval granted', 'full approval', 'pdufa approved',
    'breakthrough designation', 'priority review granted', 'advisory committee',
    'adcom positive', 'accelerated approval', 'fast track', 'rtu letter',
    'no clinical hold', 'resubmission accepted', 'goal date',
}
MOD_BULL = {
    'fda approval', 'approval likely', 'fda meeting', 'pdufa date', 'pdufa',
    'nda submitted', 'bla submitted', 'nda accepted', 'bla accepted',
    'rolling review', 'survival benefit', 'overall survival',
    'statistically significant', 'phase 3 results', 'phase iii', 'topline',
    'positive data', 'strong data', 'approval chance', 'approval probability',
    'will be approved', 'getting approved', 'fda friendly', 'compelling data',
    'os benefit',
} | config.EXTRA_BULLISH_TERMS
WEAK_BULL = {
    'approval', 'approved', 'fda', 'catalyst', 'positive', 'bullish',
    'optimistic', 'confident', 'hopeful', 'strong', 'promising',
    'good data', 'good results', 'impressed', 'buy', 'long', 'moon',
}
STRONG_BEAR = {
    'complete response letter', 'crl received', 'fda rejected', 'fda denial',
    'approval denied', 'clinical hold', 'safety concern fda',
    'refuse to file', 'rtf letter', 'not approvable',
}
MOD_BEAR = {
    'crl', 'rejected', 'denial', 'fda concern', 'manufacturing issue',
    'delay approval', 'approval delayed', 'approvable letter', 'resubmission',
    'approval unlikely', 'will not be approved', 'fda skeptical',
    'failed trial', 'negative data', 'not significant', 'no survival benefit',
    'dilution', 'offering', 'reverse split', 'bankruptcy risk',
} | config.EXTRA_BEARISH_TERMS
WEAK_BEAR = {
    'bearish', 'worried', 'concern', 'delay', 'risk', 'skeptical',
    'doubt', 'unsure', 'manipulation', 'short', 'dump', 'sell',
    'baghold', 'avoid',
}

# Base approval rate before any community-sentiment adjustment. This is a
# coarse prior for the ticker's indication/regulatory pathway — set
# BASE_APPROVAL_PROB in .env to something appropriate for your own name;
# the default here (0.48) reflects NWBO's Phase 3 overall-survival profile,
# not a general-purpose FDA base rate.
BASE_APPROVAL_PROB = config.BASE_APPROVAL_PROB


def score_message(text: str) -> tuple:
    """
    Returns (score, category, matched_terms)
    Score: positive = bullish approval signal, negative = bearish
    Scale: strong=3, moderate=2, weak=1, per match
    """
    lower = text.lower()
    score = 0.0
    matched = []
    category = 'NEUTRAL'

    for term in STRONG_BULL:
        if term in lower:
            score += 3.0
            matched.append(f'+++ {term}')
    for term in MOD_BULL:
        if term in lower:
            score += 2.0
            matched.append(f'++ {term}')
    for term in WEAK_BULL:
        if term in lower and term not in matched:
            score += 1.0
            matched.append(f'+ {term}')
    for term in STRONG_BEAR:
        if term in lower:
            score -= 3.0
            matched.append(f'--- {term}')
    for term in MOD_BEAR:
        if term in lower:
            score -= 2.0
            matched.append(f'-- {term}')
    for term in WEAK_BEAR:
        if term in lower and term not in matched:
            score -= 1.0
            matched.append(f'- {term}')

    if score >= 2:
        category = 'BULLISH'
    elif score <= -2:
        category = 'BEARISH'

    return score, category, matched


def compute_daily_probability(daily_scores: dict) -> dict:
    """
    For each day, compute cumulative weighted approval probability.
    Recent days weighted more heavily (exponential decay with 14-day half-life).
    """
    today = datetime.now(timezone.utc).date()
    all_dates = sorted(daily_scores.keys())
    if not all_dates:
        return {}

    results = {}
    half_life = 14  # days

    for target_date in all_dates:
        weighted_score = 0.0
        total_weight = 0.0

        for date, data in daily_scores.items():
            age_days = (target_date - date).days
            if age_days < 0:
                continue  # future dates don't affect past
            weight = 0.5 ** (age_days / half_life)
            weighted_score += data['net_score'] * weight
            total_weight += abs(data['net_score']) * weight + 0.01  # avoid div/0

        if total_weight > 0:
            normalized = max(-1.0, min(1.0, weighted_score / (total_weight + 1)))
        else:
            normalized = 0.0

        adjustment = normalized * 0.25  # max +/-25% from community sentiment
        prob = max(0.05, min(0.95, BASE_APPROVAL_PROB + adjustment))
        results[target_date.isoformat()] = round(prob * 100, 1)

    return results


async def fetch_and_analyze():
    log.info('Connecting to Telegram...')
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        entity = await client.get_entity(GROUP_ID)
        log.info(f'Connected to: {entity.title}')

        log.info('Pulling full message history...')
        messages = []
        async for msg in client.iter_messages(entity, limit=None):
            if msg.text and len(msg.text.strip()) > 10:
                messages.append({
                    'id': msg.id,
                    'date': msg.date,
                    'text': msg.text,
                })

        log.info(f'Fetched {len(messages)} messages')

        daily_scores = defaultdict(lambda: {'bull': 0, 'bear': 0, 'neutral': 0,
                                             'net_score': 0.0, 'msg_count': 0,
                                             'top_signals': []})
        all_signals = []
        strong_events = []

        for msg in messages:
            date = msg['date'].astimezone(timezone.utc).date()
            score, category, matched = score_message(msg['text'])

            daily_scores[date]['msg_count'] += 1
            daily_scores[date]['net_score'] += score

            if category == 'BULLISH':
                daily_scores[date]['bull'] += 1
            elif category == 'BEARISH':
                daily_scores[date]['bear'] += 1
            else:
                daily_scores[date]['neutral'] += 1

            if abs(score) >= 2 and matched:
                signal = {
                    'date': date.isoformat(),
                    'score': round(score, 1),
                    'category': category,
                    'text': msg['text'][:200],
                    'terms': matched[:5],
                }
                all_signals.append(signal)
                if abs(score) >= 3:
                    strong_events.append(signal)

            if matched and len(daily_scores[date]['top_signals']) < 3:
                daily_scores[date]['top_signals'].append({
                    'score': round(score, 1),
                    'text': msg['text'][:150],
                    'terms': matched[:3],
                })

        prob_series = compute_daily_probability(daily_scores)

        today = datetime.now(timezone.utc).date()
        last_7_days = [d for d in daily_scores if (today - d).days <= 7]
        last_30_days = [d for d in daily_scores if (today - d).days <= 30]

        def sentiment_ratio(days):
            bull = sum(daily_scores[d]['bull'] for d in days)
            bear = sum(daily_scores[d]['bear'] for d in days)
            total = bull + bear
            return (bull / total * 100) if total else 50.0

        recent_probs = [prob_series[d.isoformat()] for d in last_7_days if d.isoformat() in prob_series]
        older_probs = [prob_series[d.isoformat()] for d in last_30_days
                       if d.isoformat() in prob_series and d not in last_7_days]
        recent_avg = sum(recent_probs) / len(recent_probs) if recent_probs else BASE_APPROVAL_PROB * 100
        older_avg = sum(older_probs) / len(older_probs) if older_probs else BASE_APPROVAL_PROB * 100
        trend = recent_avg - older_avg

        today_prob = prob_series.get(today.isoformat(), BASE_APPROVAL_PROB * 100)

        daily_out = {}
        for d, v in sorted(daily_scores.items()):
            daily_out[d.isoformat()] = {
                'bull': v['bull'],
                'bear': v['bear'],
                'neutral': v['neutral'],
                'net_score': round(v['net_score'], 2),
                'msg_count': v['msg_count'],
                'approval_prob': prob_series.get(d.isoformat(), BASE_APPROVAL_PROB * 100),
                'top_signals': v['top_signals'],
            }

        analysis = {
            'ticker': TICKER,
            'generated': datetime.now(timezone.utc).isoformat(),
            'total_messages': len(messages),
            'total_signals': len(all_signals),
            'strong_events': len(strong_events),
            'date_range': {
                'start': min(d.isoformat() for d in daily_scores),
                'end': today.isoformat(),
            },
            'approval_probability': {
                'today': round(today_prob, 1),
                'trend_7d': round(trend, 1),
                'trend_label': 'IMPROVING' if trend > 2 else 'DECLINING' if trend < -2 else 'STABLE',
                'series': prob_series,
            },
            'sentiment': {
                '7d_bull_pct': round(sentiment_ratio(last_7_days), 1),
                '30d_bull_pct': round(sentiment_ratio(last_30_days), 1),
                '7d_net_score': round(sum(daily_scores[d]['net_score'] for d in last_7_days), 1),
            },
            'recent_strong_events': sorted(strong_events, key=lambda x: abs(x['score']), reverse=True)[:10],
            'daily': daily_out,
        }

        OUTPUT.write_text(json.dumps(analysis, indent=2))
        log.info(f'Analysis saved -> {OUTPUT}')
        log.info(f'Today approval probability: {today_prob:.1f}% (trend: {trend:+.1f}% vs 30d avg)')

        return analysis


def send_telegram_report(analysis: dict):
    """Send daily approval-probability report via Telegram bot."""
    if not BOT_TOKEN or not CHAT_ID:
        log.warning('No Telegram bot token/chat id configured — skipping report send')
        return

    import requests

    prob = analysis['approval_probability']
    sent = analysis['sentiment']
    today = prob['today']
    trend = prob['trend_7d']
    label = prob['trend_label']

    if today >= 60:
        prob_emoji = '\U0001F7E2'
    elif today >= 45:
        prob_emoji = '\U0001F7E1'
    else:
        prob_emoji = '\U0001F534'

    trend_arrow = '\U0001F4C8' if trend > 2 else '\U0001F4C9' if trend < -2 else '➡️'

    msg_lines = [
        f'<b>\U0001F9EC {TICKER} Daily FDA Approval Analysis</b>',
        f'<i>{datetime.now().strftime("%B %d, %Y")}</i>',
        '',
        f'{prob_emoji} <b>Approval Probability: {today:.1f}%</b>',
        f'{trend_arrow} 7-Day Trend: <b>{trend:+.1f}%</b> ({label})',
        '',
        f'\U0001F4CA <b>Community Sentiment</b>',
        f'  Last 7 days:  {sent["7d_bull_pct"]:.0f}% bullish',
        f'  Last 30 days: {sent["30d_bull_pct"]:.0f}% bullish',
        f'  7d net score: {sent["7d_net_score"]:+.0f}',
        '',
        f'\U0001F4CB <b>Coverage</b>',
        f'  {analysis["total_messages"]:,} messages analyzed',
        f'  {analysis["strong_events"]} high-signal events found',
        f'  Data from {analysis["date_range"]["start"]} -> {analysis["date_range"]["end"]}',
    ]

    recent = analysis.get('recent_strong_events', [])[:3]
    if recent:
        msg_lines += ['', '\U0001F511 <b>Key Recent Signals</b>']
        for ev in recent:
            cat_emoji = '\U0001F7E2' if ev['category'] == 'BULLISH' else '\U0001F534'
            msg_lines.append(f'{cat_emoji} [{ev["date"]}] {ev["text"][:120]}...')

    msg_lines += [
        '',
        f'<i>Base rate: {int(BASE_APPROVAL_PROB*100)}% (configured prior). Community sentiment +/-25% adjustment.</i>',
    ]

    text = '\n'.join(msg_lines)
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=10,
        )
        if r.ok:
            log.info('Telegram report sent')
        else:
            log.warning(f'Telegram send failed: {r.text}')
    except Exception as e:
        log.warning(f'Telegram send error: {e}')


async def main():
    if LOCK_FILE.exists():
        age_secs = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        if age_secs < 3600:
            log.warning(f'Lock file exists ({age_secs:.0f}s old) — another instance running. Exiting.')
            return None
        log.warning(f'Removing stale lock file ({age_secs:.0f}s old)')
        LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.touch()
    try:
        analysis = await fetch_and_analyze()
        send_telegram_report(analysis)
        return analysis
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == '__main__':
    asyncio.run(main())
