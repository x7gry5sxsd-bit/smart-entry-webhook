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

app = FastAPI(title='SMART ENTRY Webhook v3.0')

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
    """v2.8: Returns up to 3 candidate keywords sorted by likely specificity."""
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
    cands = extract_keyword_candidates(token_name)
    return cands[0] if cands else None


async def pick_best_keyword(client, candidates):
    """v2.8: Run Reddit search for each candidate, pick the one with highest top_score."""
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
        resp = await client.get(url, timeout=4.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        score = data.get('score_normalised', 0)
        risks_list = data.get('risks', []) or []
        risks = [r.get('name', '') for r in risks_list if r.get('name')]
        if score >= 75:
            level = 'DANGER'
        elif score >= 30:
            level = 'WARNING'
        else:
            level = 'SAFE'
        markets = data.get('markets', []) or []
        lp_locked = 100
        if markets:
            mkt = markets[0]
            lp_obj = mkt.get('lp', {})
            lp_locked_pct = lp_obj.get('lpLockedPct', 100)
            lp_locked = int(lp_locked_pct)
        return {
            'available': True,
            'score': score,
            'level': level,
            'risks': risks,
            'lp_locked': lp_locked,
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
        vol_24h = (pair.get('volume') or {}).get('h24', 0)
        txns_5m = (pair.get('txns') or {}).get('m5', {})
        buys_5m = txns_5m.get('buys', 0)
        sells_5m = txns_5m.get('sells', 0)
        bs_ratio = (buys_5m / sells_5m) if sells_5m > 0 else 0
        txns_1h = (pair.get('txns') or {}).get('h1', {})
        buys_1h = txns_1h.get('buys', 0)
        sells_1h = txns_1h.get('sells', 0)
        txns_1h_total = buys_1h + sells_1h
        avg_5m = vol_1h / 12 if vol_1h else 0
        accel = (vol_5m / avg_5m) if avg_5m > 0 else 0
        price_change_5m = (pair.get('priceChange') or {}).get('m5', 0)
        price_change_1h = (pair.get('priceChange') or {}).get('h1', 0)
        price_change_24h = (pair.get('priceChange') or {}).get('h24', 0)
        liquidity_usd = (pair.get('liquidity') or {}).get('usd', 0)
        fdv = pair.get('fdv', 0) or 0
        market_cap = pair.get('marketCap', 0) or 0
        return {
            'available': True,
            'vol_5m': int(vol_5m),
            'vol_1h': int(vol_1h),
            'vol_24h': int(vol_24h),
            'accel': round(accel, 2),
            'buys_5m': buys_5m,
            'sells_5m': sells_5m,
            'buys_1h': buys_1h,
            'sells_1h': sells_1h,
            'bs_ratio': round(bs_ratio, 2),
            'price_change_5m': round(price_change_5m, 1),
            'price_change_1h': round(price_change_1h, 1),
            'price_change_24h': round(price_change_24h, 1),
            'liquidity_usd': int(liquidity_usd),
            'txns_5m_total': buys_5m + sells_5m,
            'txns_1h_total': txns_1h_total,
            'fdv': int(fdv),
            'market_cap': int(market_cap),
        }
    except Exception as e:
        log.warning('DexScreener error: ' + str(e))
        return {'available': False}


async def fetch_pumpfun(client, ca):
    try:
        url = 'https://frontend-api.pump.fun/coins/' + ca
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        resp = await client.get(url, headers=headers, timeout=3.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        twitter = data.get('twitter')
        telegram = data.get('telegram')
        website = data.get('website')
        image_uri = data.get('image_uri')
        socials = sum(1 for x in [twitter, telegram, website] if x)
        return {
            'available': True,
            'reply_count': data.get('reply_count', 0) or 0,
            'has_twitter': 1 if twitter else 0,
            'has_telegram': 1 if telegram else 0,
            'has_website': 1 if website else 0,
            'has_image': 1 if image_uri else 0,
            'socials_count': socials,
            'was_koh': 1 if data.get('king_of_the_hill_timestamp') else 0,
        }
    except Exception as e:
        log.warning('PumpFun error: ' + str(e))
        return {'available': False}


async def fetch_reddit_hotness(client, keyword):
    try:
        url = 'https://www.reddit.com/search.json'
        params = {'q': keyword, 'sort': 'hot', 'limit': 25, 't': 'day'}
        headers = {'User-Agent': 'SmartEntry/3.0'}
        resp = await client.get(url, params=params, headers=headers, timeout=4.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        children = data.get('data', {}).get('children', [])
        posts_count = len(children)
        if posts_count == 0:
            return {'available': True, 'posts_count': 0, 'top_score': 0}
        scores = [c.get('data', {}).get('score', 0) for c in children]
        return {
            'available': True,
            'posts_count': posts_count,
            'top_score': max(scores),
        }
    except Exception as e:
        log.warning('Reddit error for ' + str(keyword) + ': ' + str(e))
        return {'available': False}


async def fetch_gdelt_news(client, keyword):
    try:
        url = 'https://api.gdeltproject.org/api/v2/doc/doc'
        params = {
            'query': keyword,
            'mode': 'artlist',
            'maxrecords': 50,
            'format': 'json',
            'timespan': '1d',
        }
        resp = await client.get(url, params=params, timeout=4.0)
        if resp.status_code != 200:
            return {'available': False}
        try:
            data = resp.json()
            articles = data.get('articles', [])
        except Exception:
            articles = []
        return {'available': True, 'articles_count': len(articles)}
    except Exception as e:
        log.warning('GDELT error for ' + str(keyword) + ': ' + str(e))
        return {'available': False}
async def fetch_wikipedia_views(client, keyword):
    try:
        title = keyword.title().replace(' ', '_')
        check_url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + title
        check_resp = await client.get(check_url, timeout=3.0)
        if check_resp.status_code != 200:
            return {'available': True, 'has_article': 0, 'views_today': 0, 'spike_ratio': 0}
        today = datetime.now(timezone.utc)
        end = today.strftime('%Y%m%d')
        start = (today - timedelta(days=8)).strftime('%Y%m%d')
        views_url = (
            'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/'
            'en.wikipedia/all-access/all-agents/' + title + '/daily/' + start + '/' + end
        )
        v_resp = await client.get(views_url, timeout=4.0)
        if v_resp.status_code != 200:
            return {'available': True, 'has_article': 1, 'views_today': 0, 'spike_ratio': 0}
        items = v_resp.json().get('items', [])
        if not items:
            return {'available': True, 'has_article': 1, 'views_today': 0, 'spike_ratio': 0}
        views_today = items[-1].get('views', 0)
        prev = [i.get('views', 0) for i in items[:-1]]
        avg_prev = sum(prev) / len(prev) if prev else 0
        spike = round(views_today / avg_prev, 2) if avg_prev > 0 else 0
        return {
            'available': True,
            'has_article': 1,
            'views_today': views_today,
            'spike_ratio': spike,
        }
    except Exception as e:
        log.warning('Wikipedia error for ' + str(keyword) + ': ' + str(e))
        return {'available': False}


async def fetch_radar_match(client, token_name):
    """v3.0: FIXED — был баг с параметром (token_name vs name) и структурой ответа."""
    try:
        url = RADAR_URL + '/match'
        params = {'name': token_name}  # v3.0 FIX: name (не token_name!)
        resp = await client.get(url, params=params, timeout=4.0)
        if resp.status_code != 200:
            log.warning('Radar HTTP ' + str(resp.status_code) + ' for ' + str(token_name))
            return {'available': False}
        data = resp.json()
        # v3.0 FIX: Topic Radar v0.3 возвращает best_keyword/best_score напрямую
        best_keyword = data.get('best_keyword')
        if not best_keyword:
            return {
                'available': True,
                'matched': False,
                'keyword': 'NONE',
                'score': 0,
                'match_type': 'NONE',
                'match_count': 0,
                'topic_age_h': 0.0,
                'strength': 0.0,
            }
        # Берём первый матч из списка для age и strength
        matches = data.get('matches', [])
        first_match = matches[0] if matches else {}
        log.info('Radar MATCH for "' + str(token_name) + '" -> "' + str(best_keyword) +
                 '" score=' + str(data.get('best_score', 0)))
        return {
            'available': True,
            'matched': True,
            'keyword': best_keyword,
            'score': data.get('best_score', 0),
            'match_type': data.get('best_match_type', 'NONE'),
            'match_count': data.get('match_count', 1),
            'topic_age_h': round(first_match.get('age_hours', 0.0), 1),
            'strength': round(first_match.get('match_strength', 0.0), 2),
        }
    except Exception as e:
        log.warning('Radar error: ' + str(e))
        return {'available': False}


def get_time_metrics():
    now = datetime.now(timezone.utc)
    h = now.hour
    return {
        'hour_utc': h,
        'day_of_week': now.weekday(),
        'is_usa_hours': 1 if 14 <= h < 22 else 0,
        'is_asia_hours': 1 if 0 <= h < 8 else 0,
        'is_eu_hours': 1 if 7 <= h < 16 else 0,
    }


def calculate_topic_hot_score(reddit, gdelt, wiki):
    score = 0
    if reddit.get('available'):
        if reddit.get('posts_count', 0) >= 25:
            score += 25
        elif reddit.get('posts_count', 0) >= 10:
            score += 15
        if reddit.get('top_score', 0) >= 1000:
            score += 25
        elif reddit.get('top_score', 0) >= 100:
            score += 10
    if gdelt.get('available') and gdelt.get('articles_count', 0) >= 5:
        score += 15
    if wiki.get('available') and wiki.get('has_article', 0) == 1:
        score += 10
        if wiki.get('spike_ratio', 0) >= 2:
            score += 15
    return min(score, 100)


def compute_derived_metrics(dex_data):
    """v2.9: Compute derived metrics from DexScreener on-chain data."""
    out = {
        'price_accel': 0,
        'vol_accel_24h': 0,
        'avg_tx_5m': 0,
        'avg_tx_1h': 0,
        'tx_size_delta': 0,
        'bs_5m': 0,
        'bs_1h': 0,
        'bs_delta': 0,
    }

    if not dex_data.get('available'):
        return out

    vol_5m = dex_data.get('vol_5m', 0)
    vol_1h = dex_data.get('vol_1h', 0)
    vol_24h = dex_data.get('vol_24h', 0)
    txns_5m = dex_data.get('txns_5m_total', 0)
    txns_1h = dex_data.get('txns_1h_total', 0)
    buys_5m = dex_data.get('buys_5m', 0)
    sells_5m = dex_data.get('sells_5m', 0)
    buys_1h = dex_data.get('buys_1h', 0)
    sells_1h = dex_data.get('sells_1h', 0)
    price_5m = dex_data.get('price_change_5m', 0)
    price_1h = dex_data.get('price_change_1h', 0)

    # PRICE_ACCEL: price_5m vs price_5m-equivalent of last hour
    if price_1h and price_1h != 0:
        out['price_accel'] = round(price_5m / (price_1h / 12), 2)

    # VOL_ACCEL_24h: vol_5m vs avg 5m over last 24h
    if vol_24h and vol_24h > 0:
        avg_5m_24h = vol_24h / 288.0
        if avg_5m_24h > 0:
            out['vol_accel_24h'] = round(vol_5m / avg_5m_24h, 2)

    # AVG_TX_5M, AVG_TX_1H, TX_SIZE_DELTA
    if txns_5m > 0:
        out['avg_tx_5m'] = round(vol_5m / txns_5m, 1)
    if txns_1h > 0:
        out['avg_tx_1h'] = round(vol_1h / txns_1h, 1)
    if out['avg_tx_1h'] > 0:
        out['tx_size_delta'] = round(out['avg_tx_5m'] / out['avg_tx_1h'], 2)

    # BS metrics
    if sells_5m > 0:
        out['bs_5m'] = round(buys_5m / sells_5m, 2)
    elif buys_5m > 0:
        out['bs_5m'] = 99
    if sells_1h > 0:
        out['bs_1h'] = round(buys_1h / sells_1h, 2)
    elif buys_1h > 0:
        out['bs_1h'] = 99
    out['bs_delta'] = round(out['bs_5m'] - out['bs_1h'], 2)

    return out
def build_enrichment_text(rug, dex, pump, time_data, topic, radar):
    # v2.9: derived metrics
    derived = compute_derived_metrics(dex)
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
        vol_24h = dex['vol_24h']
        accel = dex['accel']
        bs_ratio = dex['bs_ratio']
        buys_5m = dex['buys_5m']
        sells_5m = dex['sells_5m']
        buys_1h = dex['buys_1h']
        sells_1h = dex['sells_1h']
        price_5m = dex['price_change_5m']
        price_1h = dex['price_change_1h']
        price_24h = dex['price_change_24h']
        liquidity_usd = dex['liquidity_usd']
        txns_5m_total = dex['txns_5m_total']
        txns_1h_total = dex['txns_1h_total']
        fdv = dex['fdv']
        market_cap = dex['market_cap']
    else:
        dex_status = 'UNAVAILABLE'
        vol_5m = vol_1h = vol_24h = 0
        accel = bs_ratio = 0
        buys_5m = sells_5m = buys_1h = sells_1h = 0
        price_5m = price_1h = price_24h = 0
        liquidity_usd = txns_5m_total = txns_1h_total = 0
        fdv = market_cap = 0
    if pump.get('available'):
        reply_count = pump['reply_count']
        has_twitter = pump['has_twitter']
        has_telegram = pump['has_telegram']
        has_website = pump['has_website']
        has_image = pump['has_image']
        socials_count = pump['socials_count']
        was_koh = pump['was_koh']
    else:
        reply_count = has_twitter = has_telegram = has_website = 0
        has_image = socials_count = was_koh = 0
    topic_keyword = topic.get('keyword', '') or 'NONE'
    topic_hot_score = topic.get('hot_score', 0)
    reddit_data = topic.get('reddit', {})
    gdelt_data = topic.get('gdelt', {})
    wiki_data = topic.get('wiki', {})
    reddit_posts = reddit_data.get('posts_count', 0) if reddit_data.get('available') else 0
    reddit_top = reddit_data.get('top_score', 0) if reddit_data.get('available') else 0
    gdelt_news = gdelt_data.get('articles_count', 0) if gdelt_data.get('available') else 0
    has_wiki = wiki_data.get('has_article', 0) if wiki_data.get('available') else 0
    wiki_views = wiki_data.get('views_today', 0) if wiki_data.get('available') else 0
    wiki_spike = wiki_data.get('spike_ratio', 0) if wiki_data.get('available') else 0

    if radar.get('available') and radar.get('matched'):
        radar_match = radar.get('keyword', 'NONE')
        radar_score = radar.get('score', 0)
        radar_match_type = radar.get('match_type', 'NONE')
        radar_match_count = radar.get('match_count', 0)
        radar_age_h = radar.get('topic_age_h', 0.0)
        radar_strength = radar.get('strength', 0.0)
    else:
        radar_match = 'NONE'
        radar_score = 0
        radar_match_type = 'NONE'
        radar_match_count = 0
        radar_age_h = 0.0
        radar_strength = 0.0

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
        'VOL_24H: ' + str(vol_24h),
        'VOL_ACCEL: ' + str(accel),
        'BS_ONCHAIN: ' + str(bs_ratio),
        'BUYS_5M: ' + str(buys_5m),
        'SELLS_5M: ' + str(sells_5m),
        'BUYS_1H: ' + str(buys_1h),
        'SELLS_1H: ' + str(sells_1h),
        'PRICE_5M: ' + str(price_5m),
        'PRICE_1H: ' + str(price_1h),
        'PRICE_24H: ' + str(price_24h),
        'LIQUIDITY_USD: ' + str(liquidity_usd),
        'TXNS_5M_TOTAL: ' + str(txns_5m_total),
        'TXNS_1H_TOTAL: ' + str(txns_1h_total),
        'FDV: ' + str(fdv),
        'MARKET_CAP: ' + str(market_cap),
        'REPLY_COUNT: ' + str(reply_count),
        'HAS_TWITTER: ' + str(has_twitter),
        'HAS_TELEGRAM: ' + str(has_telegram),
        'HAS_WEBSITE: ' + str(has_website),
        'HAS_IMAGE: ' + str(has_image),
        'SOCIALS_COUNT: ' + str(socials_count),
        'WAS_KOH: ' + str(was_koh),
        'HOUR_UTC: ' + str(time_data['hour_utc']),
        'DAY_OF_WEEK: ' + str(time_data['day_of_week']),
        'IS_USA_HOURS: ' + str(time_data['is_usa_hours']),
        'IS_ASIA_HOURS: ' + str(time_data['is_asia_hours']),
        'IS_EU_HOURS: ' + str(time_data['is_eu_hours']),
        'TOPIC_KEYWORD: ' + str(topic_keyword),
        'TOPIC_HOT_SCORE: ' + str(topic_hot_score),
        'REDDIT_POSTS_24H: ' + str(reddit_posts),
        'REDDIT_TOP_SCORE: ' + str(reddit_top),
        'GDELT_NEWS_24H: ' + str(gdelt_news),
        'HAS_WIKIPEDIA: ' + str(has_wiki),
        'WIKI_VIEWS_TODAY: ' + str(wiki_views),
        'WIKI_SPIKE_RATIO: ' + str(wiki_spike),
        # v2.7: Radar block (v3.0 FIXED!)
        'RADAR_MATCH: ' + str(radar_match),
        'RADAR_SCORE: ' + str(radar_score),
        'RADAR_MATCH_TYPE: ' + str(radar_match_type),
        'RADAR_MATCH_COUNT: ' + str(radar_match_count),
        'RADAR_TOPIC_AGE_H: ' + str(radar_age_h),
        'RADAR_MATCH_STRENGTH: ' + str(radar_strength),
        # v2.9: DERIVED metrics block
        'PRICE_ACCEL: ' + str(derived['price_accel']),
        'VOL_ACCEL_24H: ' + str(derived['vol_accel_24h']),
        'AVG_TX_5M: ' + str(derived['avg_tx_5m']),
        'AVG_TX_1H: ' + str(derived['avg_tx_1h']),
        'TX_SIZE_DELTA: ' + str(derived['tx_size_delta']),
        'BS_5M: ' + str(derived['bs_5m']),
        'BS_1H: ' + str(derived['bs_1h']),
        'BS_DELTA: ' + str(derived['bs_delta']),
        '===============',
    ]
    return '\n'.join(lines)


@app.get('/')
async def root():
    return {'service': 'SMART ENTRY Webhook v3.0', 'status': 'ok', 'version': '3.0'}


@app.get('/health')
async def health():
    return {'status': 'healthy', 'version': '3.0'}


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
        return {'enrichment': '', 'error': 'no_ca'}
    log.info('CA: ' + ca)

    async with httpx.AsyncClient() as client:
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
        # v2.7: Radar always called when token_name available
        if token_name:
            tasks.append(fetch_radar_match(client, token_name))
        results = await asyncio.gather(*tasks, return_exceptions=True)
    rug = results[0] if isinstance(results[0], dict) else {'available': False}
    dex = results[1] if isinstance(results[1], dict) else {'available': False}
    pump = results[2] if isinstance(results[2], dict) else {'available': False}

    # v2.8: parse remaining results — reddit already obtained
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

    hot_score = calculate_topic_hot_score(reddit, gdelt, wiki)
    topic = {'keyword': keyword or 'NONE', 'hot_score': hot_score, 'reddit': reddit, 'gdelt': gdelt, 'wiki': wiki}
    enrichment_text = build_enrichment_text(rug, dex, pump, time_data, topic, radar)
    return {
        'enrichment': enrichment_text,
        'ca': ca,
    }
