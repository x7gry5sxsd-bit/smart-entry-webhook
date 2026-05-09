import re
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

app = FastAPI(title='SMART ENTRY Webhook v2.8')

CA_REGEX = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
EXCLUDE_WORDS = {'NEW', 'PUMP', 'BOND', 'BURN', 'COIN', 'TOKEN', 'SCAM', 'RUG'}

NAME_REGEX = re.compile(r'💊\s+(.+?)\s+\[')

STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'inu', 'pump',
    'cure', 'meme', 'coin', 'token', 'sol', 'bonk', 'dog', 'cat',
    'of', 'in', 'to', 'is', 'by', 'on', 'at', 'as', 'so', 'be', 'was',
    'are', 'an', 'or', 'it',
    # v2.8: generic words that always win on Reddit but mean nothing specific
    'agent', 'show', 'horse', 'number', 'world', 'time', 'life',
    'new', 'old', 'good', 'bad', 'big', 'small', 'best', 'top',
    'season', 'episode', 'movie', 'film', 'video', 'photo',
    'trade', 'market', 'buy', 'sell', 'hold', 'moon',
    'eyes', 'face', 'hand', 'head', 'body',
    'man', 'men', 'woman', 'kid', 'baby', 'people',
    'car', 'truck', 'bike', 'plane',
    'red', 'blue', 'green', 'black', 'white',
    'one', 'two', 'three', 'four', 'five',
    'club', 'army', 'team', 'gang', 'crew',
    'ai', 'app', 'web', 'dev',
}

# v2.7: Topic Radar URL (override via env if needed)
RADAR_URL = os.getenv('RADAR_URL', 'https://topic-radar.onrender.com')


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


def extract_token_name(text):
    if not text:
        return None
    m = NAME_REGEX.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    return name if name else None


def extract_keyword_candidates(token_name):
    """v2.8: Returns up to 3 candidate keywords sorted by likely specificity.
    First candidate = full multiword (if applicable), then individual words."""
    if not token_name:
        return []
    name = token_name.strip().lower()
    name = re.sub(r'[^\w\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    words = [w for w in name.split() if w and w not in STOPWORDS and len(w) > 2]
    if not words:
        return [name] if name else []

    candidates = []
    if len(words) == 1:
        candidates.append(words[0])
    elif len(words) >= 2:
        phrase = ' '.join(words[:3])
        candidates.append(phrase)
        for w in words[:3]:
            if len(w) >= 4 and w not in candidates:
                candidates.append(w)

    return candidates[:3]


def extract_keyword(token_name):
    """Backward compat: returns first candidate (multiword phrase)."""
    cands = extract_keyword_candidates(token_name)
    return cands[0] if cands else None


async def pick_best_keyword(client, candidates):
    """v2.8: Run Reddit search for each candidate, pick the one with highest top_score.
    Returns (best_keyword, reddit_data_for_that_keyword)."""
    if not candidates:
        return None, {'available': False}
    if len(candidates) == 1:
        rd = await fetch_reddit_hotness(client, candidates[0])
        return candidates[0], rd

    tasks = [fetch_reddit_hotness(client, c) for c in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    best_idx = 0
    best_score = -1
    for i, r in enumerate(results):
        if not isinstance(r, dict) or not r.get('available'):
            continue
        s = r.get('top_score', 0) or 0
        if s > best_score:
            best_score = s
            best_idx = i

    best_kw = candidates[best_idx]
    best_data = results[best_idx] if isinstance(results[best_idx], dict) else {'available': False}
    log.info('keyword candidates: ' + str(candidates) + ' | scores: ' +
             str([(r.get('top_score', 0) if isinstance(r, dict) and r.get('available') else 'NA') for r in results]) +
             ' | picked: ' + best_kw)
    return best_kw, best_data


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

        if score >= 75:
            level = 'DANGER'
        elif score >= 50:
            level = 'WARNING'
        elif score > 0:
            level = 'SAFE'
        else:
            level = 'UNKNOWN'

        return {
            'available': True,
            'level': level,
            'score': score,
            'flags': risk_names,
            'lp_locked': lp_locked,
        }
    except Exception as e:
        log.warning('Rugcheck error: ' + str(e))
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
        pairs.sort(key=lambda p: (p.get('liquidity') or {}).get('usd', 0), reverse=True)
        p = pairs[0]
        vol = p.get('volume') or {}
        txns = p.get('txns') or {}
        price = p.get('priceChange') or {}
        m5 = txns.get('m5') or {}
        h1 = txns.get('h1') or {}

        vol_5m = float(vol.get('m5', 0) or 0)
        vol_1h = float(vol.get('h1', 0) or 0)
        # vol_5m_avg over 1h would be vol_1h/12. accel = vol_5m / avg
        avg_5m_in_1h = vol_1h / 12 if vol_1h > 0 else 0
        vol_accel = (vol_5m / avg_5m_in_1h) if avg_5m_in_1h > 0 else 0

        buys_5m = int(m5.get('buys', 0) or 0)
        sells_5m = int(m5.get('sells', 0) or 0)
        bs_oc = (buys_5m / sells_5m) if sells_5m > 0 else (99 if buys_5m > 0 else 0)

        return {
            'available': True,
            'status': 'OK',
            'vol_5m': vol_5m,
            'vol_1h': vol_1h,
            'vol_24h': float(vol.get('h24', 0) or 0),
            'vol_accel': round(vol_accel, 2),
            'buys_5m': buys_5m,
            'sells_5m': sells_5m,
            'buys_1h': int(h1.get('buys', 0) or 0),
            'sells_1h': int(h1.get('sells', 0) or 0),
            'bs_onchain': round(bs_oc, 2),
            'price_5m': float(price.get('m5', 0) or 0),
            'price_1h': float(price.get('h1', 0) or 0),
            'price_24h': float(price.get('h24', 0) or 0),
            'liq_usd': float((p.get('liquidity') or {}).get('usd', 0) or 0),
            'fdv': float(p.get('fdv', 0) or 0),
            'mcap': float(p.get('marketCap', 0) or 0),
            'txns_5m_total': buys_5m + sells_5m,
            'txns_1h_total': int(h1.get('buys', 0) or 0) + int(h1.get('sells', 0) or 0),
        }
    except Exception as e:
        log.warning('DexScreener error: ' + str(e))
        return {'available': False, 'status': 'UNAVAILABLE'}


async def fetch_pumpfun(client, ca):
    try:
        url = 'https://frontend-api.pump.fun/coins/' + ca
        resp = await client.get(
            url, timeout=3.0,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; SmartEntryBot/1.0)'},
        )
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        twitter = data.get('twitter') or ''
        telegram = data.get('telegram') or ''
        website = data.get('website') or ''
        image = data.get('image_uri') or ''
        socials_count = sum(1 for x in [twitter, telegram, website] if x)
        return {
            'available': True,
            'reply_count': data.get('reply_count', 0),
            'has_twitter': 1 if twitter else 0,
            'has_telegram': 1 if telegram else 0,
            'has_website': 1 if website else 0,
            'has_image': 1 if image else 0,
            'socials_count': socials_count,
            'was_koh': 1 if data.get('king_of_the_hill_timestamp') else 0,
        }
    except Exception as e:
        log.warning('Pumpfun error: ' + str(e))
        return {'available': False}


async def fetch_reddit_hotness(client, keyword):
    if not keyword or len(keyword) < 3:
        return {'available': False}
    try:
        url = 'https://www.reddit.com/search.json'
        params = {'q': keyword, 'sort': 'hot', 't': 'day', 'limit': 25}
        headers = {'User-Agent': 'Mozilla/5.0 SmartEntryBot/1.0'}
        resp = await client.get(url, params=params, headers=headers, timeout=5.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        posts = data.get('data', {}).get('children', [])
        if not posts:
            return {'available': True, 'posts_count': 0, 'top_score': 0, 'total_score': 0, 'subreddits_count': 0}
        scores = [p['data'].get('score', 0) for p in posts]
        subs = set(p['data'].get('subreddit', '') for p in posts)
        return {
            'available': True,
            'posts_count': len(posts),
            'top_score': max(scores) if scores else 0,
            'total_score': sum(scores),
            'subreddits_count': len(subs),
        }
    except Exception as e:
        log.warning('Reddit error: ' + str(e))
        return {'available': False}


async def fetch_gdelt_news(client, keyword):
    if not keyword or len(keyword) < 3:
        return {'available': False}
    try:
        url = 'https://api.gdeltproject.org/api/v2/doc/doc'
        params = {'query': keyword, 'mode': 'ArtList', 'format': 'json', 'maxrecords': 25, 'timespan': '24h', 'sort': 'datedesc'}
        resp = await client.get(url, params=params, timeout=5.0)
        if resp.status_code != 200:
            return {'available': False}
        try:
            data = resp.json()
        except Exception:
            return {'available': True, 'articles_count': 0, 'sources_count': 0}
        articles = data.get('articles', [])
        return {
            'available': True,
            'articles_count': len(articles),
            'sources_count': len(set(a.get('domain', '') for a in articles)),
        }
    except Exception as e:
        log.warning('GDELT error: ' + str(e))
        return {'available': False}


async def fetch_wikipedia_views(client, keyword):
    if not keyword or len(keyword) < 3:
        return {'available': False}
    try:
        # First check if article exists
        title = keyword.replace(' ', '_').title()
        check_url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + title
        check_resp = await client.get(check_url, timeout=5.0)
        if check_resp.status_code != 200:
            return {'available': True, 'has_wikipedia': 0, 'views_today': 0, 'spike_ratio': 0}

        # Get page views
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y%m%d')

        views_url = ('https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/'
                     'en.wikipedia.org/all-access/all-agents/' + title + '/daily/' +
                     week_ago + '/' + today)
        views_resp = await client.get(views_url, timeout=5.0)
        if views_resp.status_code != 200:
            return {'available': True, 'has_wikipedia': 1, 'views_today': 0, 'spike_ratio': 0}
        data = views_resp.json()
        items = data.get('items', [])
        if not items:
            return {'available': True, 'has_wikipedia': 1, 'views_today': 0, 'spike_ratio': 0}

        views_today = items[-1].get('views', 0) if items else 0
        if len(items) >= 2:
            prev_avg = sum(i.get('views', 0) for i in items[:-1]) / (len(items) - 1)
            spike = views_today / prev_avg if prev_avg > 0 else 0
        else:
            spike = 0

        return {
            'available': True,
            'has_wikipedia': 1,
            'views_today': views_today,
            'spike_ratio': round(spike, 2),
        }
    except Exception as e:
        log.warning('Wiki error: ' + str(e))
        return {'available': False}


# ===== v2.7: Topic Radar =====
async def fetch_radar_match(client, token_name):
    if not token_name:
        return {'available': False}
    try:
        url = RADAR_URL.rstrip('/') + '/match'
        params = {'name': token_name}
        resp = await client.get(url, params=params, timeout=4.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        match = data.get('match')
        if not match:
            return {
                'available': True,
                'has_match': False,
                'keyword': 'NONE',
                'score': 0,
                'match_type': 'NONE',
                'match_count': 0,
                'topic_age_h': 0.0,
                'strength': 0.0,
            }
        return {
            'available': True,
            'has_match': True,
            'keyword': match.get('keyword', 'NONE'),
            'score': match.get('score', 0),
            'match_type': match.get('match_type', 'NONE'),
            'match_count': data.get('total_matches', 1),
            'topic_age_h': round(match.get('age_hours', 0), 1),
            'strength': round(match.get('strength', 0), 2),
        }
    except Exception as e:
        log.warning('Radar error: ' + str(e))
        return {'available': False}


def get_time_metrics():
    now = datetime.now(timezone.utc)
    return {
        'hour_utc': now.hour,
        'day_of_week': now.weekday(),  # 0=Mon
        'is_usa_hours': 1 if 14 <= now.hour <= 22 else 0,
        'is_asia_hours': 1 if 0 <= now.hour <= 8 else 0,
        'is_eu_hours': 1 if 7 <= now.hour <= 16 else 0,
    }


def build_enrichment_text(rug, dex, pump, time_data, topic_data, radar_data):
    lines = ['', '=== ON-CHAIN ===']

    if rug.get('available'):
        lines.append('RUG_LEVEL: ' + str(rug.get('level', 'UNKNOWN')))
        lines.append('RUG_SCORE: ' + str(rug.get('score', 0)))
        flags = rug.get('flags', [])
        flags_str = ', '.join(flags) if flags else 'N/A'
        lines.append('RUG_FLAGS: ' + flags_str)
        lines.append('LP_LOCKED: ' + str(rug.get('lp_locked', 0)))
    else:
        lines.append('RUG_LEVEL: UNKNOWN')
        lines.append('RUG_SCORE: 0')
        lines.append('RUG_FLAGS: N/A')
        lines.append('LP_LOCKED: 100')

    if dex.get('available'):
        lines.append('DEX_STATUS: ' + str(dex.get('status', 'OK')))
        lines.append('VOL_5M: ' + str(int(dex.get('vol_5m', 0))))
        lines.append('VOL_1H: ' + str(int(dex.get('vol_1h', 0))))
        lines.append('VOL_24H: ' + str(int(dex.get('vol_24h', 0))))
        lines.append('VOL_ACCEL: ' + str(dex.get('vol_accel', 0)))
        lines.append('BS_ONCHAIN: ' + str(dex.get('bs_onchain', 0)))
        lines.append('BUYS_5M: ' + str(dex.get('buys_5m', 0)))
        lines.append('SELLS_5M: ' + str(dex.get('sells_5m', 0)))
        lines.append('BUYS_1H: ' + str(dex.get('buys_1h', 0)))
        lines.append('SELLS_1H: ' + str(dex.get('sells_1h', 0)))
        lines.append('PRICE_5M: ' + str(round(dex.get('price_5m', 0), 1)))
        lines.append('PRICE_1H: ' + str(round(dex.get('price_1h', 0), 1)))
        lines.append('PRICE_24H: ' + str(round(dex.get('price_24h', 0), 1)))
        lines.append('LIQUIDITY_USD: ' + str(int(dex.get('liq_usd', 0))))
        lines.append('TXNS_5M_TOTAL: ' + str(dex.get('txns_5m_total', 0)))
        lines.append('TXNS_1H_TOTAL: ' + str(dex.get('txns_1h_total', 0)))
        lines.append('FDV: ' + str(int(dex.get('fdv', 0))))
        lines.append('MARKET_CAP: ' + str(int(dex.get('mcap', 0))))
    else:
        lines.append('DEX_STATUS: UNAVAILABLE')
        for k in ['VOL_5M', 'VOL_1H', 'VOL_24H', 'VOL_ACCEL', 'BS_ONCHAIN', 'BUYS_5M', 'SELLS_5M',
                  'BUYS_1H', 'SELLS_1H', 'PRICE_5M', 'PRICE_1H', 'PRICE_24H', 'LIQUIDITY_USD',
                  'TXNS_5M_TOTAL', 'TXNS_1H_TOTAL', 'FDV', 'MARKET_CAP']:
            lines.append(k + ': 0')

    if pump.get('available'):
        lines.append('REPLY_COUNT: ' + str(pump.get('reply_count', 0)))
        lines.append('HAS_TWITTER: ' + str(pump.get('has_twitter', 0)))
        lines.append('HAS_TELEGRAM: ' + str(pump.get('has_telegram', 0)))
        lines.append('HAS_WEBSITE: ' + str(pump.get('has_website', 0)))
        lines.append('HAS_IMAGE: ' + str(pump.get('has_image', 0)))
        lines.append('SOCIALS_COUNT: ' + str(pump.get('socials_count', 0)))
        lines.append('WAS_KOH: ' + str(pump.get('was_koh', 0)))
    else:
        for k in ['REPLY_COUNT', 'HAS_TWITTER', 'HAS_TELEGRAM', 'HAS_WEBSITE', 'HAS_IMAGE', 'SOCIALS_COUNT', 'WAS_KOH']:
            lines.append(k + ': 0')

    lines.append('HOUR_UTC: ' + str(time_data.get('hour_utc', 0)))
    lines.append('DAY_OF_WEEK: ' + str(time_data.get('day_of_week', 0)))
    lines.append('IS_USA_HOURS: ' + str(time_data.get('is_usa_hours', 0)))
    lines.append('IS_ASIA_HOURS: ' + str(time_data.get('is_asia_hours', 0)))
    lines.append('IS_EU_HOURS: ' + str(time_data.get('is_eu_hours', 0)))

    lines.append('TOPIC_KEYWORD: ' + str(topic_data.get('keyword', 'NONE')))
    lines.append('TOPIC_HOT_SCORE: ' + str(topic_data.get('hot_score', 0)))

    reddit = topic_data.get('reddit') or {}
    if reddit.get('available'):
        lines.append('REDDIT_POSTS_24H: ' + str(reddit.get('posts_count', 0)))
        lines.append('REDDIT_TOP_SCORE: ' + str(reddit.get('top_score', 0)))
    else:
        lines.append('REDDIT_POSTS_24H: 0')
        lines.append('REDDIT_TOP_SCORE: 0')

    gdelt = topic_data.get('gdelt') or {}
    if gdelt.get('available'):
        lines.append('GDELT_NEWS_24H: ' + str(gdelt.get('articles_count', 0)))
    else:
        lines.append('GDELT_NEWS_24H: 0')

    wiki = topic_data.get('wiki') or {}
    if wiki.get('available'):
        lines.append('HAS_WIKIPEDIA: ' + str(wiki.get('has_wikipedia', 0)))
        lines.append('WIKI_VIEWS_TODAY: ' + str(wiki.get('views_today', 0)))
        lines.append('WIKI_SPIKE_RATIO: ' + str(wiki.get('spike_ratio', 0)))
    else:
        lines.append('HAS_WIKIPEDIA: 0')
        lines.append('WIKI_VIEWS_TODAY: 0')
        lines.append('WIKI_SPIKE_RATIO: 0')

    # v2.7: Radar block
    if radar_data and radar_data.get('available'):
        lines.append('RADAR_MATCH: ' + str(radar_data.get('keyword', 'NONE')))
        lines.append('RADAR_SCORE: ' + str(radar_data.get('score', 0)))
        lines.append('RADAR_MATCH_TYPE: ' + str(radar_data.get('match_type', 'NONE')))
        lines.append('RADAR_MATCH_COUNT: ' + str(radar_data.get('match_count', 0)))
        lines.append('RADAR_TOPIC_AGE_H: ' + str(radar_data.get('topic_age_h', 0.0)))
        lines.append('RADAR_MATCH_STRENGTH: ' + str(radar_data.get('strength', 0.0)))
    else:
        lines.append('RADAR_MATCH: UNAVAILABLE')
        lines.append('RADAR_SCORE: 0')
        lines.append('RADAR_MATCH_TYPE: NONE')
        lines.append('RADAR_MATCH_COUNT: 0')
        lines.append('RADAR_TOPIC_AGE_H: 0.0')
        lines.append('RADAR_MATCH_STRENGTH: 0.0')

    lines.append('===============')
    return '\n'.join(lines)


def compute_topic_hot_score(reddit, gdelt, wiki):
    score = 0
    if reddit.get('available'):
        posts = reddit.get('posts_count', 0)
        top = reddit.get('top_score', 0)
        if posts >= 20: score += 10
        elif posts >= 10: score += 5
        if top >= 1000: score += 15
        elif top >= 100: score += 10
        elif top >= 10: score += 5
    if gdelt.get('available'):
        arts = gdelt.get('articles_count', 0)
        if arts >= 10: score += 15
        elif arts >= 3: score += 10
        elif arts >= 1: score += 5
    if wiki.get('available'):
        if wiki.get('has_wikipedia') == 1: score += 5
        spike = wiki.get('spike_ratio', 0)
        if spike >= 2.0: score += 15
        elif spike >= 1.5: score += 10
    return score


@app.get('/')
async def root():
    return {'service': 'SMART ENTRY Webhook v2.8', 'status': 'running'}


@app.get('/health')
async def health():
    return {'status': 'healthy', 'version': '2.8'}


@app.post('/enrich')
async def enrich(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid_json', 'enrichment': ''}, status_code=400)
    message_text = body.get('message', '') or body.get('text', '')
    ca = extract_ca(message_text)
    time_data = get_time_metrics()
    token_name = extract_token_name(message_text)
    candidates = extract_keyword_candidates(token_name) if token_name else []
    log.info('Token: ' + str(token_name) + ' | candidates: ' + str(candidates))
    if not ca:
        topic_empty = {'keyword': (candidates[0] if candidates else 'NONE'), 'hot_score': 0, 'reddit': {}, 'gdelt': {}, 'wiki': {}}
        radar_empty = {'available': False}
        return {'enrichment': build_enrichment_text(
            {'available': False}, {'available': False}, {'available': False},
            time_data, topic_empty, radar_empty)}
    log.info('Processing CA: ' + ca)
    async with httpx.AsyncClient() as client:
        # v2.8: pick best keyword from candidates BEFORE other tasks
        keyword = None
        reddit = {'available': False}
        if candidates:
            keyword, reddit = await pick_best_keyword(client, candidates)
            log.info('Picked keyword: ' + str(keyword))

        tasks = [
            fetch_rugcheck(client, ca),
            fetch_dexscreener(client, ca),
            fetch_pumpfun(client, ca),
        ]
        if keyword:
            tasks.append(fetch_gdelt_news(client, keyword))
            tasks.append(fetch_wikipedia_views(client, keyword))
        if token_name:
            tasks.append(fetch_radar_match(client, token_name))
        results = await asyncio.gather(*tasks, return_exceptions=True)
    rug = results[0] if isinstance(results[0], dict) else {'available': False}
    dex = results[1] if isinstance(results[1], dict) else {'available': False}
    pump = results[2] if isinstance(results[2], dict) else {'available': False}

    idx = 3
    if keyword:
        gdelt = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {'available': False}
        idx += 1
        wiki = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {'available': False}
        idx += 1
    else:
        gdelt = wiki = {'available': False}
    if token_name:
        radar = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {'available': False}
    else:
        radar = {'available': False}

    hot_score = compute_topic_hot_score(reddit, gdelt, wiki)
    topic_data = {
        'keyword': keyword or 'NONE',
        'hot_score': hot_score,
        'reddit': reddit,
        'gdelt': gdelt,
        'wiki': wiki,
    }

    return {'enrichment': build_enrichment_text(rug, dex, pump, time_data, topic_data, radar)}
