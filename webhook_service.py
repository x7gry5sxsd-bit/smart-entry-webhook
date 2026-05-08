import re
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title='SMART ENTRY Webhook v2.7')

CA_REGEX = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{32,44})\b')

EXCLUDE_WORDS = {
    'PFM', 'TIP', 'NEW', 'OKX', 'MAE', 'BAN', 'BNK', 'PDR', 'BLO', 'STB',
    'TRO', 'TRT', 'GMG', 'PHO', 'AXI', 'EXP', 'TW', 'DEX', 'DEF', 'DP',
    'SOC', 'SOL', 'PVP', 'FDV', 'USD', 'CTO', 'KOL', 'PASS', 'FILTER',
}

STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'inu', 'pump',
    'cure', 'meme', 'coin', 'token', 'sol', 'bonk', 'dog', 'cat',
    'of', 'in', 'to', 'is', 'by', 'on', 'at', 'as', 'so', 'be', 'was',
    'are', 'an', 'or', 'it',
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
    m = re.search(r'\xf0\x9f\x92\x8a', text)
    m = re.search('pill', text)
    pos = -1
    for emoji in ['\U0001f48a']:
        i = text.find(emoji)
        if i >= 0:
            pos = i + 1
            break
    if pos < 0:
        m = re.search(r'\$([A-Z][A-Za-z0-9_]{1,15})', text)
        if m:
            return m.group(1)
        return None
    rest = text[pos:].strip()
    end_chars = ['[', '$', '\n']
    end_pos = len(rest)
    for c in end_chars:
        i = rest.find(c)
        if 0 < i < end_pos:
            end_pos = i
    name = rest[:end_pos].strip()
    return name if name else None


def extract_keyword(token_name):
    if not token_name:
        return None
    name = token_name.strip().lower()
    name = re.sub(r'[^\w\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    words = [w for w in name.split() if w and w not in STOPWORDS and len(w) > 2]
    if not words:
        return name
    if len(words) >= 2:
        if len(words[-1]) > 4:
            return words[-1]
        else:
            return ' '.join(words[-2:])
    return words[0]


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
        reply_count = data.get('reply_count', 0) or 0
        twitter = data.get('twitter', '') or ''
        telegram = data.get('telegram', '') or ''
        website = data.get('website', '') or ''
        koh_timestamp = data.get('king_of_the_hill_timestamp', 0) or 0
        image_uri = data.get('image_uri', '') or ''
        has_twitter = 1 if twitter and len(twitter) > 5 else 0
        has_telegram = 1 if telegram and len(telegram) > 5 else 0
        has_website = 1 if website and len(website) > 5 else 0
        has_image = 1 if image_uri and len(image_uri) > 5 else 0
        socials_count = has_twitter + has_telegram + has_website
        was_koh = 1 if koh_timestamp and koh_timestamp > 0 else 0
        return {
            'available': True,
            'reply_count': reply_count,
            'has_twitter': has_twitter,
            'has_telegram': has_telegram,
            'has_website': has_website,
            'has_image': has_image,
            'socials_count': socials_count,
            'was_koh': was_koh,
        }
    except Exception as e:
        log.warning('Pump.fun error: ' + str(e))
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
        kw = keyword.replace(' ', '_').lower()
        end = datetime.utcnow()
        start = end - timedelta(days=7)
        s_str = start.strftime('%Y%m%d')
        e_str = end.strftime('%Y%m%d')
        url = 'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/' + kw + '/daily/' + s_str + '/' + e_str
        headers = {'User-Agent': 'SmartEntryBot/1.0'}
        resp = await client.get(url, headers=headers, timeout=5.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        items = data.get('items', [])
        if not items:
            return {'available': True, 'has_wiki': 0, 'views_today': 0, 'avg_views': 0, 'spike_ratio': 0}
        views = [it.get('views', 0) for it in items]
        if len(views) < 2:
            return {'available': True, 'has_wiki': 1, 'views_today': views[0] if views else 0, 'avg_views': 0, 'spike_ratio': 0}
        today_views = views[-1]
        prior_avg = sum(views[:-1]) / len(views[:-1]) if views[:-1] else 0
        spike = (today_views / prior_avg) if prior_avg > 0 else 0
        return {
            'available': True,
            'has_wiki': 1,
            'views_today': today_views,
            'avg_views': int(prior_avg),
            'spike_ratio': round(spike, 2),
        }
    except Exception as e:
        log.warning('Wikipedia error: ' + str(e))
        return {'available': False}


# ===== v2.7: Topic Radar =====
async def fetch_radar_match(client, token_name):
    """Calls Topic Radar /match endpoint with token name."""
    if not token_name:
        return {'available': False}
    try:
        url = RADAR_URL + '/match'
        params = {'name': token_name}
        resp = await client.get(url, params=params, timeout=4.0)
        if resp.status_code != 200:
            return {'available': False}
        data = resp.json()
        matches = data.get('matches') or []
        first_match = matches[0] if matches else {}
        return {
            'available': True,
            'best_keyword': data.get('best_keyword') or 'NONE',
            'best_score': int(data.get('best_score', 0) or 0),
            'best_match_type': data.get('best_match_type') or 'NONE',
            'match_count': int(data.get('match_count', 0) or 0),
            'top_match_age_h': float(first_match.get('age_hours', 0) or 0),
            'top_match_strength': float(first_match.get('match_strength', 0) or 0),
        }
    except Exception as e:
        log.warning('Radar error: ' + str(e))
        return {'available': False}


def calculate_topic_hot_score(reddit, gdelt, wiki):
    score = 0
    if reddit.get('available'):
        posts = reddit.get('posts_count', 0)
        top = reddit.get('top_score', 0)
        if posts >= 20:
            score += 20
        elif posts >= 10:
            score += 10
        elif posts >= 5:
            score += 5
        if top >= 1000:
            score += 20
        elif top >= 100:
            score += 10
        elif top >= 10:
            score += 5
    if gdelt.get('available'):
        articles = gdelt.get('articles_count', 0)
        if articles >= 25:
            score += 30
        elif articles >= 10:
            score += 20
        elif articles >= 5:
            score += 10
        elif articles >= 1:
            score += 5
    if wiki.get('available') and wiki.get('has_wiki'):
        spike = wiki.get('spike_ratio', 0)
        views_today = wiki.get('views_today', 0)
        if spike >= 5:
            score += 20
        elif spike >= 2:
            score += 10
        elif spike >= 1.5:
            score += 5
        if views_today >= 10000:
            score += 10
        elif views_today >= 1000:
            score += 5
    return min(score, 100)


def get_time_metrics():
    now = datetime.now(timezone.utc)
    hour_utc = now.hour
    day_of_week = now.weekday()
    is_usa_hours = 1 if 14 <= hour_utc <= 23 else 0
    is_asia_hours = 1 if 0 <= hour_utc < 8 else 0
    is_eu_hours = 1 if 8 <= hour_utc < 14 else 0
    return {
        'hour_utc': hour_utc,
        'day_of_week': day_of_week,
        'is_usa_hours': is_usa_hours,
        'is_asia_hours': is_asia_hours,
        'is_eu_hours': is_eu_hours,
    }


def build_enrichment_text(rug, dex, pump, time_data, topic, radar):
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
    wiki_views = wiki_data.get('views_today', 0) if wiki_data.get('available') else 0
    wiki_spike = wiki_data.get('spike_ratio', 0) if wiki_data.get('available') else 0
    has_wiki = wiki_data.get('has_wiki', 0) if wiki_data.get('available') else 0

    if radar.get('available'):
        radar_match = radar.get('best_keyword', 'NONE') or 'NONE'
        radar_score = radar.get('best_score', 0)
        radar_match_type = radar.get('best_match_type', 'NONE') or 'NONE'
        radar_match_count = radar.get('match_count', 0)
        radar_age_h = radar.get('top_match_age_h', 0.0)
        radar_strength = radar.get('top_match_strength', 0.0)
    else:
        radar_match = 'UNAVAILABLE'
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
        'RADAR_MATCH: ' + str(radar_match),
        'RADAR_SCORE: ' + str(radar_score),
        'RADAR_MATCH_TYPE: ' + str(radar_match_type),
        'RADAR_MATCH_COUNT: ' + str(radar_match_count),
        'RADAR_TOPIC_AGE_H: ' + str(radar_age_h),
        'RADAR_MATCH_STRENGTH: ' + str(radar_strength),
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
    time_data = get_time_metrics()
    token_name = extract_token_name(message_text)
    keyword = extract_keyword(token_name) if token_name else None
    log.info('Token: ' + str(token_name) + ' | keyword: ' + str(keyword))
    if not ca:
        topic_empty = {'keyword': keyword or 'NONE', 'hot_score': 0, 'reddit': {}, 'gdelt': {}, 'wiki': {}}
        radar_empty = {'available': False}
        return {'enrichment': build_enrichment_text(
            {'available': False}, {'available': False}, {'available': False},
            time_data, topic_empty, radar_empty)}
    log.info('Processing CA: ' + ca)
    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_rugcheck(client, ca),
            fetch_dexscreener(client, ca),
            fetch_pumpfun(client, ca),
        ]
        if keyword:
            tasks.append(fetch_reddit_hotness(client, keyword))
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
        reddit = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {'available': False}
        idx += 1
        gdelt = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {'available': False}
        idx += 1
        wiki = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {'available': False}
        idx += 1
    else:
        reddit = gdelt = wiki = {'available': False}
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
        'token_name': token_name,
        'keyword': keyword,
        'topic_hot_score': hot_score,
        'rug_score': rug.get('score') if rug.get('available') else None,
        'rug_level': rug.get('level') if rug.get('available') else None,
        'vol_accel': dex.get('accel') if dex.get('available') else None,
        'reply_count': pump.get('reply_count') if pump.get('available') else None,
        'radar_match': radar.get('best_keyword') if radar.get('available') else None,
        'radar_score': radar.get('best_score') if radar.get('available') else None,
    }


@app.get('/')
async def root():
    return {'service': 'SMART ENTRY Webhook', 'status': 'ok', 'version': '2.7'}


@app.get('/health')
async def health():
    return {'status': 'healthy', 'version': '2.7'}
