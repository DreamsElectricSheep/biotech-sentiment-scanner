#!/usr/bin/env python3
"""
Community Sentiment Backtest
Correlates the community sentiment series (data/analysis.json, produced by
sentiment_analyzer.py) against actual price history. Answers:
  - Does sentiment lead, lag, or coincide with price moves?
  - What lag maximises predictive power?
  - Backtest: buy when prob > BUY_THRESH, sell when prob < SELL_THRESH

IMPORTANT: read this before trusting the output. When this was run against
NWBO's 8-year Telegram history, the honest finding was that sentiment LAGS
price rather than leading it. That is, the community's own conviction tends
to follow price moves rather than predict them. This script is provided so
you can run the same test against your own ticker/community. It is not a
claim that any community's sentiment has directional predictive power. See
the README's "Honest finding" section before using this as a trading signal.

Sends a summary report to Telegram (optional).
"""
import json
import sys
import math
from datetime import datetime
from collections import defaultdict  # noqa: F401  (kept for symmetry with analyzer)

import requests
import yfinance as yf
import numpy as np

import config

TICKER = config.TICKER
ANALYSIS_FILE = config.DATA_DIR / "analysis.json"
OUT_FILE = config.DATA_DIR / "backtest.json"

BUY_THRESH = config.BACKTEST_BUY_THRESHOLD
SELL_THRESH = config.BACKTEST_SELL_THRESHOLD
POSITION_SIZE = 1000   # $ per trade (paper)
MAX_LAG = 20           # days to test for lead/lag correlation

BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT_ID = config.TELEGRAM_CHAT_ID

if not ANALYSIS_FILE.exists():
    print(f'{ANALYSIS_FILE} not found. Run sentiment_analyzer.py first.')
    sys.exit(1)

# ── load sentiment series ─────────────────────────────────────────────────────
print('Loading sentiment series...')
data = json.loads(ANALYSIS_FILE.read_text())
series = data['approval_probability']['series']          # {date_str: float}
sent = {datetime.strptime(k, '%Y-%m-%d').date(): v
        for k, v in series.items()}

# ── load price history ────────────────────────────────────────────────────────
print(f'Downloading {TICKER} price history...')
ticker = yf.Ticker(TICKER)
hist = ticker.history(start=config.BACKTEST_START_DATE, auto_adjust=True)
prices = {}
for ts, row in hist.iterrows():
    prices[ts.date()] = float(row['Close'])

# ── align on common trading days ──────────────────────────────────────────────
common = sorted(set(sent) & set(prices))
if len(common) < 10:
    print(f'Only {len(common)} overlapping days between sentiment series and price '
          f'history: not enough data to backtest.')
    sys.exit(1)
print(f'Common days: {len(common)}  ({common[0]} to {common[-1]})')

s_arr = np.array([sent[d] for d in common])
p_arr = np.array([prices[d] for d in common])


def pearson(x, y):
    if len(x) < 5:
        return 0.0
    mx, my = x.mean(), y.mean()
    num = ((x - mx) * (y - my)).sum()
    den = math.sqrt(((x - mx) ** 2).sum() * ((y - my) ** 2).sum())
    return float(num / den) if den else 0.0


# ── lag correlation: sent[t] vs fwd_return[t+lag] ────────────────────────────
print('Running lag correlation analysis...')
lag_results = {}
for lag in range(-MAX_LAG, MAX_LAG + 1):
    if lag == 0:
        rets = np.diff(p_arr) / p_arr[:-1] * 100
        r = pearson(s_arr[:-1], rets)
    elif lag > 0:
        rets = np.diff(p_arr) / p_arr[:-1] * 100
        n = len(rets) - lag
        if n < 10:
            continue
        r = pearson(s_arr[:n], rets[lag:lag + n])
    else:
        rets = np.diff(p_arr) / p_arr[:-1] * 100
        n = len(rets) - abs(lag)
        if n < 10:
            continue
        r = pearson(s_arr[abs(lag):abs(lag) + n], rets[:n])
    lag_results[lag] = round(r, 4)

best_lag = max(lag_results, key=lambda k: abs(lag_results[k]))
best_r = lag_results[best_lag]

# ── simple sentiment strategy backtest ───────────────────────────────────────
print('Running backtest...')
position = None
trades = []
cash = 10000.0

for i, d in enumerate(common[1:], 1):
    prob = sent[d]
    price = prices[d]
    prev = sent.get(common[i - 1], prob)

    if position is None and prev < BUY_THRESH <= prob:
        shares = (POSITION_SIZE / price)
        position = {'date': d, 'price': price, 'shares': shares}
        cash -= POSITION_SIZE

    elif position is not None and prob < SELL_THRESH:
        proceeds = position['shares'] * price
        pnl = proceeds - POSITION_SIZE
        pnl_pct = pnl / POSITION_SIZE * 100
        hold_days = (d - position['date']).days
        trades.append({
            'entry': str(position['date']), 'exit': str(d),
            'entry_price': round(position['price'], 2),
            'exit_price': round(price, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 1),
            'hold_days': hold_days,
        })
        cash += proceeds
        position = None

if position:
    last_price = prices[common[-1]]
    proceeds = position['shares'] * last_price
    pnl = proceeds - POSITION_SIZE
    trades.append({
        'entry': str(position['date']), 'exit': str(common[-1]),
        'entry_price': round(position['price'], 2),
        'exit_price': round(last_price, 2),
        'pnl': round(pnl, 2),
        'pnl_pct': round(pnl / POSITION_SIZE * 100, 1),
        'hold_days': (common[-1] - position['date']).days,
        'open': True,
    })
    cash += proceeds

# ── metrics ───────────────────────────────────────────────────────────────────
total_trades = len(trades)
wins = [t for t in trades if t['pnl'] > 0]
losses = [t for t in trades if t['pnl'] <= 0]
win_rate = len(wins) / total_trades * 100 if total_trades else 0
total_pnl = sum(t['pnl'] for t in trades)
avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
profit_factor = (sum(t['pnl'] for t in wins) /
                 abs(sum(t['pnl'] for t in losses))) if losses else 999
avg_hold = sum(t['hold_days'] for t in trades) / total_trades if trades else 0

bh_return = (prices[common[-1]] - prices[common[0]]) / prices[common[0]] * 100
raw_corr = pearson(s_arr, p_arr)

diffs = [(common[i], sent[common[i]] - sent[common[i - 1]])
         for i in range(1, len(common))]
biggest_drops = sorted(diffs, key=lambda x: x[1])[:5]
biggest_spikes = sorted(diffs, key=lambda x: x[1], reverse=True)[:5]

results = {
    'ticker': TICKER,
    'generated': datetime.utcnow().isoformat(),
    'common_days': len(common),
    'date_range': {'start': str(common[0]), 'end': str(common[-1])},
    'correlation': {
        'raw_sent_vs_price': round(raw_corr, 4),
        'best_lag_days': best_lag,
        'best_lag_r': best_r,
        'lag_series': lag_results,
    },
    'backtest': {
        'buy_threshold': BUY_THRESH,
        'sell_threshold': SELL_THRESH,
        'total_trades': total_trades,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate_pct': round(win_rate, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'avg_hold_days': round(avg_hold, 1),
        'buyhold_return_pct': round(bh_return, 1),
        'trades': trades,
    },
    'inflection': {
        'biggest_drops': [(str(d), round(v, 1)) for d, v in biggest_drops],
        'biggest_spikes': [(str(d), round(v, 1)) for d, v in biggest_spikes],
    },
}
OUT_FILE.write_text(json.dumps(results, indent=2))
print(f'Results saved -> {OUT_FILE}')

# ── format report ─────────────────────────────────────────────────────────────
lag_desc = (f'sentiment LEADS price by {best_lag}d' if best_lag > 0
            else f'sentiment LAGS price by {abs(best_lag)}d' if best_lag < 0
            else 'sentiment coincides with price')

top5 = sorted(trades, key=lambda t: t['pnl_pct'], reverse=True)[:5]
bot5 = sorted(trades, key=lambda t: t['pnl_pct'])[:5]


def trade_line(t):
    icon = 'W' if t['pnl'] > 0 else 'L'
    return (f"  [{icon}] {t['entry']} to {t['exit']} "
            f"${t['entry_price']:.2f}->${t['exit_price']:.2f}  "
            f"{t['pnl_pct']:+.1f}%  ({t['hold_days']}d)")


report = f"""<b>{TICKER} Sentiment Backtest Report</b>
<i>{len(common):,} trading days analysed  |  {common[0]} to {common[-1]}</i>

<b>Correlation Analysis</b>
Raw sentiment vs price:  <code>{raw_corr:+.3f}</code>
Best predictive lag:     <code>{best_lag:+d} days</code>  ({lag_desc})
Lag correlation r:       <code>{best_r:+.3f}</code>
{'Sentiment has WEAK predictive power (|r|&lt;0.15)' if abs(best_r) < 0.15
 else 'Sentiment has MODERATE predictive power (0.15&lt;|r|&lt;0.35)' if abs(best_r) < 0.35
 else 'Sentiment has STRONG predictive power (|r|&gt;0.35)'}

<b>Backtest  (Buy &gt;{BUY_THRESH}% / Sell &lt;{SELL_THRESH}%)</b>
Trades:         {total_trades}  |  W/L {len(wins)}/{len(losses)}  ({win_rate:.0f}% WR)
Total PnL:      <code>${total_pnl:+,.2f}</code>
Avg win:        <code>${avg_win:+.2f}</code>   Avg loss: <code>${avg_loss:+.2f}</code>
Profit factor:  <code>{profit_factor:.2f}x</code>
Avg hold:       {avg_hold:.0f} days
Buy-and-hold:   {bh_return:+.1f}% over same period

<b>Best Trades</b>
{''.join(trade_line(t)+chr(10) for t in top5)}
<b>Worst Trades</b>
{''.join(trade_line(t)+chr(10) for t in bot5)}
<b>Biggest Sentiment Drops</b>
{''.join(f'  {d}  {v:+.1f}pp' + chr(10) for d, v in biggest_drops)}
<b>Biggest Sentiment Spikes</b>
{''.join(f'  {d}  {v:+.1f}pp' + chr(10) for d, v in biggest_spikes)}"""

if BOT_TOKEN and CHAT_ID:
    resp = requests.post(
        f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
        json={'chat_id': CHAT_ID, 'text': report, 'parse_mode': 'HTML'},
        timeout=15,
    )
    if resp.status_code == 200:
        print('Report sent to Telegram.')
    else:
        print(f'Telegram error {resp.status_code}: {resp.text[:200]}')
else:
    print('\n(No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured, printing report instead)')

print('\n--- REPORT ---')
print(report.replace('<b>', '').replace('</b>', '').replace('<i>', '')
      .replace('</i>', '').replace('<code>', '').replace('</code>', ''))
