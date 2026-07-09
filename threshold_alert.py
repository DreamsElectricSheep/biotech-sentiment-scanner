#!/usr/bin/env python3
"""
Approval-probability threshold alert.
Fires a Telegram message when data/analysis.json crosses ALERT_THRESHOLD.

Edge-triggered by design: it only fires ONCE on a threshold crossing (up or
down), not every run while the value stays above/below the line. This is a
deliberate anti-noise choice — without the hysteresis in alerted_above, this
would re-fire on every single cron tick while sentiment sits near the
threshold, which is exactly the alert fatigue this tool is trying to avoid.

Intended to run shortly after sentiment_analyzer.py on the same cron cadence.
"""
import json
import os
import sys
from datetime import datetime

import requests

import config

TICKER = config.TICKER
THRESHOLD = config.ALERT_THRESHOLD
ANALYSIS_FILE = config.DATA_DIR / "analysis.json"
ALERT_STATE = config.DATA_DIR / "alert_state.json"

TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print('[threshold_alert] No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured — skipping send')
        return
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                      json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        print(f'Telegram error: {e}')


def main():
    if not os.path.exists(ANALYSIS_FILE):
        print(f'[threshold_alert] {ANALYSIS_FILE} not found — run sentiment_analyzer.py first')
        return

    data = json.load(open(ANALYSIS_FILE))
    ap = data.get('approval_probability', {})
    prob = float(ap.get('today', 0) if isinstance(ap, dict) else ap or 0)
    trend = float(ap.get('trend_7d', 0) if isinstance(ap, dict) else 0)
    label = ap.get('trend_label', '') if isinstance(ap, dict) else ''
    generated = data.get('generated', 'unknown')

    state = {}
    if os.path.exists(ALERT_STATE):
        state = json.load(open(ALERT_STATE))

    prev_prob = float(state.get('last_prob', 0))
    was_above = state.get('alerted_above', False)

    crossed_up = prob >= THRESHOLD and prev_prob < THRESHOLD
    crossed_down = prob < THRESHOLD and prev_prob >= THRESHOLD

    if crossed_up and not was_above:
        send_telegram(
            f'<b>{TICKER} Approval-Probability Alert</b>\n'
            f'Probability <b>{prob:.1f}%</b> crossed threshold ({THRESHOLD:.0f}%)\n'
            f'Trend: {trend:+.1f}% 7d ({label})\nGenerated: {generated}\n\n'
            f'This is a conviction/attention flag, not a trading signal — DYOR.'
        )
        state['alerted_above'] = True
        print(f'[threshold_alert] ALERT SENT — {prob:.1f}% crossed {THRESHOLD:.0f}%')
    elif crossed_down and was_above:
        send_telegram(
            f'<b>{TICKER} Alert</b> — dropped below threshold\n'
            f'Probability: <b>{prob:.1f}%</b> (was above {THRESHOLD:.0f}%)'
        )
        state['alerted_above'] = False
        print(f'[threshold_alert] Reset — {prob:.1f}% fell below {THRESHOLD:.0f}%')
    else:
        print(f'[threshold_alert] No cross. prob={prob:.1f}% prev={prev_prob:.1f}% above={was_above}')

    state['last_prob'] = prob
    state['last_checked'] = datetime.now().isoformat()
    json.dump(state, open(ALERT_STATE, 'w'), indent=2)


if __name__ == '__main__':
    main()
