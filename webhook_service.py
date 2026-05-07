import re
import asyncio
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title='SMART ENTRY Webhook')

CA_REGEX = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{32,44})\b')

EXCLUDE_WORDS = {
    'PFM', 'TIP', 'NEW', 'OKX', 'MAE', 'BAN', 'BNK', 'PDR', 'BLO', 'STB',
    'TRO', 'TRT', 'GMG', 'PHO', 'AXI', 'EXP', 'TW', 'DEX', 'DEF', 'DP',
    'SOC', 'SOL', 'PVP', 'FDV', 'USD', 'CTO', 'KOL', 'PASS', 'FILTER',
}


def extract_ca(text):
    if not text:
        return None
    matches = CA_REGEX.findall(text)
    if not matches:
        return None
    candidates = [
        m for m in matches
        if 32 <= len(m) <= 44
        and m.upper() not in EXCLUDE_WORDS
        and not m.isdigit()
    ]
    if not candidates:
        return None
    pump_candidates = [c for c in candidates if c.endswith('pump')]
    if pump_candidates:
        return max(pump_candidates, key=len)
    return max(candidates, key=len)


async def fetch_rugcheck(client, ca):
    try:
        url = 'https://api.rugcheck.xyz/v1/tokens/' + ca + '/report'
        resp = await client.get(url, timeout=3.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        score = data.get('score_normalised') or data.get('score') or 0
        risks = data.get('risks', [])
        risk_names = [r.get('name', '') for r in risks if r.get('level') in ('warn', 'danger')]
        lp_locked = 0
        markets = data.get('markets', [])
        if markets and markets[0].get('lp'):
            lp_locked = markets[0]['lp'].get('lpLockedPct', 0)
        if score >= 70:
            level = 'SAFE'
        elif score >= 40:
            level = 'WARNING'
        else:
            level = 'DANGER'
        return {
            'available': True,
            'score': int(score),
            'level': level,
            'risks': risk_names[:3],
            'lp_locked': int(lp_locked),
        }
    except Exception as e:
        log.warning('RugCheck error: ' + str(e))
        return {'available': False}


async def fetch_dexscreener(client, ca):
    try:
        url = 'https://api.dexscreener.com/latest/dex/tokens/' + ca
        resp = await client.get(url, timeout=3.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        pairs = data.get('pairs') or []
        if not pairs:
            return {'available': False}
        pair = max(pairs, key=lambda p: (p.get('liquidity') or {}).get('usd', 0))
        vol_5m = (pair.get('volume') or {}).get('m5', 0)
        vol_1h = (pair.get('volume') or {}).get('h1', 0)
        avg_5m = vol_1h / 12 if vol_1h else 0
        accel = (vol_5m / avg_5m) if avg_5m > 0 else 0
        txns_5m = (pair.get('txns') or {}).get('m5', {})
        buys_5m = txns_5m.get('buys', 0)
        sells_5m = txns_5m.get('sells', 0)
        bs_ratio = (buys_5m / sells_5m) if sells_5m > 0 else 0
        price_change_5m = (pair.get('priceChange') or {}).get('m5', 0)
        price_change_1h = (pair.get('priceChange') or {}).get('h1', 0)
        liquidity_usd = (pair.get('liquidity') or {}).get('usd', 0)
        return {
            'available': True,
            'vol_5m': int(vol_5m),
            'vol_1h': int(vol_1h),
            'accel': round(accel, 2),
            'buys_5m': buys_5m,
            'sells_5m': sells_5m,
            'bs_ratio': round(bs_ratio, 2),
            'price_change_5m': round(price_change_5m, 1),
            'price_change_1h': round(price_change_1h, 1),
            'liquidity_usd': int(liquidity_usd),
            'txns_5m_total': buys_5m + sells_5m,
        }
    except Exception as e:
        log.warning('DexScreener error: ' + str(e))
        return {'available': False}


async def fetch_pumpfun(client, ca):
    try:
        url = 'https://frontend-api.pump.fun/coins/' + ca
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        }
        resp = await client.get(url, headers=headers, timeout=3.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        reply_count = data.get('reply_count', 0) or 0
        twitter = data.get('twitter', '') or ''
        telegram = data.get('telegram', '') or ''
        website = data.get('website', '') or ''
        koh_timestamp = data.get('king_of_the_hill_timestamp', 0) or 0
        complete = data.get('complete', False)
        has_twitter = 1 if twitter and len(twitter) > 5 else 0
        has_telegram = 1 if telegram and len(telegram) > 5 else 0
        has_website = 1 if website and len(website) > 5 else 0
        socials_count = has_twitter + has_telegram + has_website
        was_koh = 1 if koh_timestamp and koh_timestamp > 0 else 0
        return {
            'available': True,
            'reply_count': reply_count,
            'has_twitter': has_twitter,
            'has_telegram': has_telegram,
            'has_website': has_website,
            'socials_count': socials_count,
            'was_koh': was_koh,
        }
    except Exception as e:
        log.warning('Pump.fun error: ' + str(e))
        return {'available': False}


def build_enrichment_text(rug, dex, pump):
    if rug.get('available'):
        rug_level = rug['level']
        rug_score = rug['score']
        rug_flags = ', '.join(rug['risks']) if rug['risks'] else 'NONE'
        lp_locked = rug['lp_locked']
    else:
        rug_level = 'UNKNOWN'
        rug_score = 0
        rug_flags = 'N/A'
        lp_locked = 100

    if dex.get('available'):
        dex_status = 'OK'
        vol_5m = dex['vol_5m']
        vol_1h = dex['vol_1h']
        accel = dex['accel']
        bs_ratio = dex['bs_ratio']
        buys_5m = dex['buys_5m']
        sells_5m = dex['sells_5m']
        price_5m = dex['price_change_5m']
        price_1h = dex['price_change_1h']
        liquidity_usd = dex['liquidity_usd']
        txns_5m_total = dex['txns_5m_total']
    else:
        dex_status = 'UNAVAILABLE'
        vol_5m = 0
        vol_1h = 0
        accel = 0
        bs_ratio = 0
        buys_5m = 0
        sells_5m = 0
        price_5m = 0
        price_1h = 0
        liquidity_usd = 0
        txns_5m_total = 0

    if pump.get('available'):
        reply_count = pump['reply_count']
        has_twitter = pump['has_twitter']
        has_telegram = pump['has_telegram']
        has_website = pump['has_website']
        socials_count = pump['socials_count']
        was_koh = pump['was_koh']
    else:
        reply_count = 0
        has_twitter = 0
        has_telegram = 0
        has_website = 0
        socials_count = 0
        was_koh = 0

    lines = [
        '',
        '=== ON-CHAIN ===',
        'RUG_LEVEL: ' + str(rug_level),
        'RUG_SCORE: ' + str(rug_score),
        'RUG_FLAGS: ' + str(rug_flags),
        'LP_LOCKED: ' + str(lp_locked),
        'DEX_STATUS: ' + str(dex_status),
        'VOL_5M: ' + str(vol_5m),
        'VOL_1H: ' + str(vol_1h),
        'VOL_ACCEL: ' + str(accel),
        'BS_ONCHAIN: ' + str(bs_ratio),
        'BUYS_5M: ' + str(buys_5m),
        'SELLS_5M: ' + str(sells_5m),
        'PRICE_5M: ' + str(price_5m),
        'PRICE_1H: ' + str(price_1h),
        'LIQUIDITY_USD: ' + str(liquidity_usd),
        'TXNS_5M_TOTAL: ' + str(txns_5m_total),
        'REPLY_COUNT: ' + str(reply_count),
        'HAS_TWITTER: ' + str(has_twitter),
        'HAS_TELEGRAM: ' + str(has_telegram),
        'HAS_WEBSITE: ' + str(has_website),
        'SOCIALS_COUNT: ' + str(socials_count),
        'WAS_KOH: ' + str(was_koh),
        '===============',
    ]
    return '\n'.join(lines)


@app.post('/enrich')
async def enrich(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid_json', 'enrichment': ''}, status_code=400)

    message_text = body.get('message', '') or body.get('text', '')
    ca = extract_ca(message_text)

    if not ca:
        return {
            'enrichment': '\n=== ON-CHAIN ===\nRUG_LEVEL: UNKNOWN\nRUG_SCORE: 0\nRUG_FLAGS: N/A\nLP_LOCKED: 100\nDEX_STATUS: UNAVAILABLE\nVOL_5M: 0\nVOL_1H: 0\nVOL_ACCEL: 0\nBS_ONCHAIN: 0\nBUYS_5M: 0\nSELLS_5M: 0\nPRICE_5M: 0\nPRICE_1H: 0\nLIQUIDITY_USD: 0\nTXNS_5M_TOTAL: 0\nREPLY_COUNT: 0\nHAS_TWITTER: 0\nHAS_TELEGRAM: 0\nHAS_WEBSITE: 0\nSOCIALS_COUNT: 0\nWAS_KOH: 0\n===============',
        }

    log.info('Processing CA: ' + ca)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            fetch_rugcheck(client, ca),
            fetch_dexscreener(client, ca),
            fetch_pumpfun(client, ca),
            return_exceptions=True
        )
    rug = results[0] if isinstance(results[0], dict) else {'available': False}
    dex = results[1] if isinstance(results[1], dict) else {'available': False}
    pump = results[2] if isinstance(results[2], dict) else {'available': False}

    enrichment_text = build_enrichment_text(rug, dex, pump)

    return {
        'enrichment': enrichment_text,
        'ca': ca,
        'rug_score': rug.get('score') if rug.get('available') else None,
        'rug_level': rug.get('level') if rug.get('available') else None,
        'vol_accel': dex.get('accel') if dex.get('available') else None,
        'reply_count': pump.get('reply_count') if pump.get('available') else None,
    }


@app.get('/')
async def root():
    return {'service': 'SMART ENTRY Webhook', 'status': 'ok', 'version': '2.4'}


@app.get('/health')
async def health():
    return {'status': 'healthy', 'version': '2.4'}
