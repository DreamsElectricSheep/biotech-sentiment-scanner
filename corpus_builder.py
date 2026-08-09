#!/usr/bin/env python3
"""
Community Corpus Builder
Re-scrapes the configured Telegram group and saves full analytical message
TEXT (not just scores), filtered for length + analytical-keyword match so
it's a corpus of substantive discussion, not scoring noise/spam.

This is what makes the sentiment pipeline auditable rather than a black box:
every score sentiment_analyzer.py produces can be traced back to the actual
messages that drove it via this corpus.

Output: data/corpus.json, data/corpus.txt (plain text, e.g. for uploading to
an LLM for qualitative review).

NOTE: this aggregates message TEXT for analysis, not for republishing or
attributing individual users; see README "Ethical considerations" before
pointing this at a community.
"""
import json
import asyncio
import logging
import sys

from telethon import TelegramClient

import config

API_ID = config.TELEGRAM_API_ID
API_HASH = config.TELEGRAM_API_HASH
GROUP_ID = config.TELEGRAM_GROUP_ID
TICKER = config.TICKER

SESSION = config.SESSION_READER
OUT_JSON = config.DATA_DIR / "corpus.json"
OUT_TXT = config.DATA_DIR / "corpus.txt"

MIN_LENGTH = 150

# Base keyword set is generic pharma/regulatory/financial vocabulary. Add
# ticker-specific terms (drug names, trial names, indications) via
# EXTRA_ANALYTICAL_KEYWORDS in .env; see .env.example for the NWBO example.
ANALYTICAL_KEYWORDS = {
    'fda', 'trial', 'approval', 'approved', 'clinical', 'data', 'results',
    'pdufa', 'nda', 'bla', 'phase', 'endpoint', 'efficacy', 'safety',
    'interim', 'topline', 'readout', 'manufacturing', 'cmc', 'gmp',
    'complete response', 'crl', 'advisory', 'adcom', 'meeting',
    'probability', 'analysis', 'evidence', 'study', 'statistic',
    'patients', 'survival', 'median', 'p-value', 'significant',
    'immunotherapy', 'catalyst', 'timeline', 'filing', 'submission',
    'dilution', 'offering', 'shares', 'cash', 'runway', 'balance sheet',
    'price target', 'valuation', 'risk', 'upside', 'downside',
    'mhra', 'ema', 'europe', 'uk approval', 'label',
    'insider', 'bought', 'accumulate', 'institution',
    'reverse split', 'rs ', 'bankruptcy', 'fraud',
    'revenue', 'profit', 'milestone', 'royalty',
    'competitor', 'competing', 'market size',
} | config.EXTRA_ANALYTICAL_KEYWORDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CORPUS] %(message)s",
    handlers=[
        logging.FileHandler(str(config.DATA_DIR / 'corpus_builder.log')),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


async def build_corpus():
    corpus = []

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        entity = await client.get_entity(GROUP_ID)
        log.info(f'Connected to: {entity.title}')
        log.info('Pulling messages, this can take a while for large/old groups...')

        total = 0
        kept = 0

        async for msg in client.iter_messages(entity, limit=None):
            total += 1

            if total % 10000 == 0:
                log.info(f'  {total:,} scanned | {kept:,} kept...')

            if not msg.text:
                continue

            text = msg.text.strip()
            if len(text) < MIN_LENGTH:
                continue

            lower = text.lower()
            if not any(kw in lower for kw in ANALYTICAL_KEYWORDS):
                continue

            corpus.append({
                'date': msg.date.strftime('%Y-%m-%d'),
                'text': text[:2000],
            })
            kept += 1

    log.info(f'Done. Scanned: {total:,} | Kept: {kept:,}')

    OUT_JSON.write_text(json.dumps(corpus, indent=2, default=str))
    log.info(f'JSON saved: {OUT_JSON}')

    lines = [
        f'{TICKER} INVESTOR COMMUNITY: ANALYTICAL POSTS',
        f'Total posts extracted: {len(corpus):,}',
        '=' * 60,
        '',
    ]

    for entry in corpus:
        lines.append(f'[{entry["date"]}]')
        lines.append(entry['text'])
        lines.append('-' * 40)

    OUT_TXT.write_text('\n'.join(lines), encoding='utf-8')
    log.info(f'Text file saved: {OUT_TXT}')

    size_mb = OUT_TXT.stat().st_size / 1_000_000
    log.info(f'Text file size: {size_mb:.1f} MB')


if __name__ == '__main__':
    asyncio.run(build_corpus())
