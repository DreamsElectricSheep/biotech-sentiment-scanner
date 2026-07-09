#!/usr/bin/env python3
"""
Telegram Community Scanner
Monitors a Telegram discussion group in real time for price targets,
sentiment, and catalyst mentions. Writes rolling state to signals.json for
downstream dashboard/alert consumption.

First run: python3 telegram_scanner.py --auth   (authenticate + join group)
Normal run: python3 telegram_scanner.py         (daemon mode)
"""
import sys
import json
import asyncio
import logging
import re
import argparse
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import UserAlreadyParticipantError

import config

API_ID = config.TELEGRAM_API_ID
API_HASH = config.TELEGRAM_API_HASH
TARGET_CHAT = config.TELEGRAM_GROUP_ID
INVITE_HASH = config.TELEGRAM_INVITE_HASH
TICKER = config.TICKER

SESSION = config.SESSION_LIVE
OUTPUT = config.DATA_DIR / "signals.json"
LOG_FILE = config.DATA_DIR / "scanner.log"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{TICKER}] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Signal patterns ───────────────────────────────────────────────────────────
PRICE_RE = re.compile(r'\$(\d{1,4}(?:\.\d{1,2})?)')
TARGET_RE = re.compile(r'(?:pt|price\s*target|target)[:\s]+\$?(\d{1,4}(?:\.\d{1,2})?)', re.IGNORECASE)
PERCENT_RE = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*%')

# Base lexicon is generic trading/FDA-catalyst language, deliberately not
# tied to any one drug or company. Add community-specific jargon via
# EXTRA_BULLISH_TERMS / EXTRA_BEARISH_TERMS / EXTRA_CATALYST_TERMS in .env
# (see .env.example for the NWBO worked example: dcvax, glioblastoma, etc.)
BASE_BULLISH_TERMS = {
    'buy', 'long', 'bullish', 'squeeze', 'moon', 'rocket', 'breakout',
    'approval', 'approved', 'catalyst', 'fda', 'positive data',
    'accumulate', 'adding', 'loaded', 'calls', 'undervalued', 'lotto',
    'going up', 'run', 'rip', 'flying',
}
BASE_BEARISH_TERMS = {
    'sell', 'short', 'bearish', 'dump', 'baghold', 'dilution', 'offering',
    'reverse split', 'rs', 'bankruptcy', 'fraud', 'avoid', 'puts',
    'going down', 'drop', 'crash', 'tank', 'halt',
}
BASE_CATALYST_TERMS = {
    'fda', 'approval', 'approved', 'trial', 'data', 'results',
    'interim', 'pdufa', 'nda', 'bla', 'phase', 'clinical', 'topline',
    'readout', 'pr ', 'press release', 'sec filing', 'earnings',
}

BULLISH_TERMS = BASE_BULLISH_TERMS | config.EXTRA_BULLISH_TERMS
BEARISH_TERMS = BASE_BEARISH_TERMS | config.EXTRA_BEARISH_TERMS
CATALYST_TERMS = BASE_CATALYST_TERMS | config.EXTRA_CATALYST_TERMS


def score_message(text: str) -> dict:
    """Score a message for bullish/bearish signals and extract price info."""
    lower = text.lower()

    bull_hits = [t for t in BULLISH_TERMS if t in lower]
    bear_hits = [t for t in BEARISH_TERMS if t in lower]
    cat_hits = [t for t in CATALYST_TERMS if t in lower]

    prices = PRICE_RE.findall(text)
    targets = TARGET_RE.findall(text)
    pcts = PERCENT_RE.findall(text)

    bull_score = len(bull_hits)
    bear_score = len(bear_hits)

    if bull_score > bear_score:
        sentiment = 'BULLISH'
        confidence = min(95, 40 + bull_score * 15)
    elif bear_score > bull_score:
        sentiment = 'BEARISH'
        confidence = min(95, 40 + bear_score * 15)
    else:
        sentiment = 'NEUTRAL'
        confidence = 30

    return {
        'sentiment': sentiment,
        'confidence': confidence,
        'bull_hits': bull_hits,
        'bear_hits': bear_hits,
        'catalysts': cat_hits,
        'prices': prices,
        'targets': targets,
        'pcts': pcts,
        'has_signal': bool(bull_hits or bear_hits or cat_hits or targets),
    }


def load_state() -> dict:
    if OUTPUT.exists():
        try:
            return json.loads(OUTPUT.read_text())
        except Exception:
            pass
    return {
        'ticker': TICKER,
        'last_updated': None,
        'message_count': 0,
        'sentiment_24h': 'NEUTRAL',
        'bull_count': 0,
        'bear_count': 0,
        'recent_signals': [],
        'price_targets': [],
        'catalysts': [],
    }


def save_state(state: dict):
    OUTPUT.write_text(json.dumps(state, indent=2, default=str))


# ── Auth + join mode ──────────────────────────────────────────────────────────
async def auth_and_join():
    """Authenticate and join the target group. Run this once interactively."""
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f'\nAuthenticated as: {me.first_name} (@{me.username})')

        if INVITE_HASH:
            print(f'\nJoining group via invite hash: {INVITE_HASH}')
            try:
                result = await client(ImportChatInviteRequest(INVITE_HASH))
                chat = result.chats[0]
                print(f'Joined: {chat.title} (id={chat.id})')
            except UserAlreadyParticipantError:
                print('Already a member of this group.')
            except Exception as e:
                print(f'Join error: {e}')

        print('\nYour groups:')
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)):
                print(f'  [{entity.id}] {dialog.name}')

        print('\nDone. Set TELEGRAM_GROUP_ID in your .env, then run without --auth to start monitoring.')


# ── Main scanner daemon ───────────────────────────────────────────────────────
async def run_scanner():
    if TARGET_CHAT is None and not INVITE_HASH:
        log.error('No TELEGRAM_GROUP_ID or TELEGRAM_INVITE_HASH configured. Run --auth first to discover your group ID.')
        sys.exit(1)

    state = load_state()
    log.info(f'Starting {TICKER} scanner')

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:

        # Resolve entity — handle invite link, numeric ID, or username
        entity = None
        try:
            entity = await client.get_entity(TARGET_CHAT)
        except Exception:
            if INVITE_HASH:
                try:
                    result = await client(ImportChatInviteRequest(INVITE_HASH))
                    entity = result.chats[0]
                    log.info(f'Joined and connected to: {entity.title}')
                except UserAlreadyParticipantError:
                    async for dialog in client.iter_dialogs():
                        if TARGET_CHAT is not None and dialog.entity.id == TARGET_CHAT:
                            entity = dialog.entity
                            break
                except Exception as e:
                    log.error(f'Could not connect to group: {e}')
                    sys.exit(1)

        if entity is None:
            log.error('Could not resolve target group. Run --auth first.')
            sys.exit(1)

        log.info(f'Monitoring: {getattr(entity, "title", entity)}')

        # Load recent history on startup (last 100 messages)
        log.info('Loading recent message history...')
        try:
            async for msg in client.iter_messages(entity, limit=100):
                if not msg.text:
                    continue
                scored = score_message(msg.text)
                if scored['has_signal']:
                    state['message_count'] += 1
                    if scored['sentiment'] == 'BULLISH':
                        state['bull_count'] += 1
                    elif scored['sentiment'] == 'BEARISH':
                        state['bear_count'] += 1
            log.info(f'History loaded. Bull={state["bull_count"]} Bear={state["bear_count"]}')
        except Exception as e:
            log.warning(f'Could not load history: {e}')

        # Real-time listener
        @client.on(events.NewMessage(chats=entity))
        async def handler(event):
            msg = event.message
            if not msg.text or len(msg.text) < 10:
                return

            scored = score_message(msg.text)
            state['message_count'] += 1
            state['last_updated'] = datetime.now(timezone.utc).isoformat()

            if not scored['has_signal']:
                return

            sender = await event.get_sender()
            sender_name = getattr(sender, 'first_name', 'Unknown')
            if hasattr(sender, 'last_name') and sender.last_name:
                sender_name += f' {sender.last_name}'

            signal = {
                'ts': datetime.now(timezone.utc).isoformat(),
                'sender': sender_name,
                'sentiment': scored['sentiment'],
                'confidence': scored['confidence'],
                'text': msg.text[:300],
                'prices': scored['prices'],
                'targets': scored['targets'],
                'catalysts': scored['catalysts'],
            }

            if scored['sentiment'] == 'BULLISH':
                state['bull_count'] += 1
            elif scored['sentiment'] == 'BEARISH':
                state['bear_count'] += 1

            state['recent_signals'].insert(0, signal)
            state['recent_signals'] = state['recent_signals'][:50]

            for t in scored['targets']:
                entry = {'ts': signal['ts'], 'target': t, 'sender': sender_name}
                if entry not in state['price_targets']:
                    state['price_targets'].insert(0, entry)
            state['price_targets'] = state['price_targets'][:20]

            for c in scored['catalysts']:
                entry = {'ts': signal['ts'], 'catalyst': c, 'text': msg.text[:150]}
                state['catalysts'].insert(0, entry)
            state['catalysts'] = state['catalysts'][:20]

            total = state['bull_count'] + state['bear_count']
            if total > 0:
                bull_pct = state['bull_count'] / total * 100
                if bull_pct >= 60:
                    state['sentiment_24h'] = 'BULLISH'
                elif bull_pct <= 40:
                    state['sentiment_24h'] = 'BEARISH'
                else:
                    state['sentiment_24h'] = 'NEUTRAL'

            save_state(state)
            log.info(
                f'{scored["sentiment"]} ({scored["confidence"]}%) | '
                f'{sender_name}: {msg.text[:80]}'
            )

        log.info('Listening for new messages...')
        await client.run_until_disconnected()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth', action='store_true', help='Authenticate and join group, then exit')
    args = parser.parse_args()

    if args.auth:
        asyncio.run(auth_and_join())
    else:
        asyncio.run(run_scanner())
