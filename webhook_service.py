“””
Webhook Service v2.3 - Smart Entry MEMS
Обогащает Phanes сигналы данными от RugCheck и DexScreener.

Новое в v2.3:

- LIQUIDITY_USD - реальная ликвидность в долларах с DexScreener
- PRICE_1H - изменение цены за 1 час
- TXNS_5M_TOTAL - общее число транзакций за 5 минут (BUYS+SELLS)
- Сохранены все поля из v2.2

Deployment: Render Free Tier
URL: https://smart-entry-webhook.onrender.com/enrich
Endpoint: POST /enrich
Health: GET /health (для cron-job.org каждые 10 минут)
“””

import re
import os
from flask import Flask, request, jsonify
import requests

app = Flask(**name**)

# Regex - все Solana CA (включая не-pump токены)

SOLANA_CA_PATTERN = re.compile(r’\b([1-9A-HJ-NP-Za-km-z]{32,44})\b’)

# Слова которые не являются адресами

EXCLUDE_WORDS = {
‘PFM’, ‘TIP’, ‘OKX’, ‘MAE’, ‘BAN’, ‘BNK’, ‘PDR’, ‘BLO’, ‘STB’,
‘TRO’, ‘TRT’, ‘GMG’, ‘PHO’, ‘AXI’, ‘EXP’, ‘TW’,
‘NEW’, ‘live’, ‘meme’, ‘docs’, ‘guide’, ‘matches’
}

DEXSCREENER_URL = “https://api.dexscreener.com/latest/dex/tokens/{ca}”
RUGCHECK_URL = “https://api.rugcheck.xyz/v1/tokens/{ca}/report/summary”

@app.route(’/health’, methods=[‘GET’, ‘HEAD’])
def health():
“”“Health check для cron-job.org keepalive”””
return jsonify({“status”: “ok”, “version”: “2.3”}), 200

@app.route(’/enrich’, methods=[‘POST’])
def enrich():
“”“Основной endpoint - обогащение сигнала”””
try:
data = request.get_json()
if not data or ‘message’ not in data:
return jsonify({“error”: “no message”}), 400

```
    message = data['message']
    ca = extract_ca(message)
    
    if not ca:
        return format_response(empty_data(), source="no_ca")
    
    rugcheck = fetch_rugcheck(ca)
    dexscreener = fetch_dexscreener(ca)
    
    return format_response({
        **rugcheck,
        **dexscreener
    }, source="full")
    
except Exception as e:
    return format_response(empty_data(), source=f"error: {str(e)}")
```

def extract_ca(message):
“””
Извлечь Contract Address из сообщения.
Приоритет: pump-токены (suffix ‘pump’), затем любые Solana CA.
“””
matches = SOLANA_CA_PATTERN.findall(message)

```
candidates = [m for m in matches if m not in EXCLUDE_WORDS]

if not candidates:
    return None

# Приоритет pump-токенам
pump_tokens = [c for c in candidates if c.endswith('pump')]
if pump_tokens:
    return pump_tokens[0]

# Иначе первый найденный
return candidates[0]
```

def fetch_rugcheck(ca):
“”“RugCheck API - SAFE/WARNING/DANGER + flags + LP_LOCKED”””
try:
r = requests.get(RUGCHECK_URL.format(ca=ca), timeout=8)
if r.status_code != 200:
return rugcheck_fallback()

```
    d = r.json()
    
    # Risk level
    score = d.get('score', 0)
    if score < 5:
        risk_level = 'SAFE'
    elif score < 20:
        risk_level = 'WARNING'
    else:
        risk_level = 'DANGER'
    
    # Override: если в risks высокий уровень
    risks = d.get('risks', [])
    if any(r.get('level') == 'danger' for r in risks):
        risk_level = 'DANGER'
    elif any(r.get('level') == 'warn' for r in risks) and risk_level == 'SAFE':
        risk_level = 'WARNING'
    
    # Flags
    flags = []
    for risk in risks:
        name = risk.get('name', '')
        if name and name not in flags:
            flags.append(name)
    
    # LP locked
    lp_locked = d.get('totalLPProviders', 0)
    markets = d.get('markets', [])
    if markets:
        for m in markets:
            lp = m.get('lp', {})
            lp_locked_pct = lp.get('lpLockedPct', 0)
            if lp_locked_pct > 0:
                lp_locked = lp_locked_pct
                break
    
    return {
        'rug_level': risk_level,
        'rug_score': int(score),
        'rug_flags': ', '.join(flags) if flags else 'NONE',
        'lp_locked': int(lp_locked) if lp_locked else 100
    }

except Exception as e:
    return rugcheck_fallback()
```

def fetch_dexscreener(ca):
“”“DexScreener API - все DEX метрики + НОВЫЕ поля v2.3”””
try:
r = requests.get(DEXSCREENER_URL.format(ca=ca), timeout=8)
if r.status_code != 200:
return dexscreener_fallback()

```
    d = r.json()
    pairs = d.get('pairs', [])
    
    if not pairs:
        return {**dexscreener_fallback(), 'dex_status': 'UNAVAILABLE'}
    
    # Берём пару с наибольшей ликвидностью (актуальный пул)
    pair = max(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0))
    
    vol_5m = pair.get('volume', {}).get('m5', 0)
    vol_1h = pair.get('volume', {}).get('h1', 0)
    vol_accel = (vol_5m * 12) / vol_1h if vol_1h > 0 else 0
    if vol_accel > 12:
        vol_accel = 12
    
    txns = pair.get('txns', {}).get('m5', {})
    buys = txns.get('buys', 0)
    sells = txns.get('sells', 0)
    bs_onchain = buys / sells if sells > 0 else (buys if buys > 0 else 0)
    
    price_5m = pair.get('priceChange', {}).get('m5', 0)
    price_1h = pair.get('priceChange', {}).get('h1', 0)  # НОВОЕ
    
    # НОВОЕ - ликвидность в USD
    liquidity_usd = pair.get('liquidity', {}).get('usd', 0)
    
    # НОВОЕ - общее число транзакций за 5 минут
    txns_5m_total = buys + sells
    
    return {
        'dex_status': 'OK',
        'vol_5m': int(vol_5m),
        'vol_1h': int(vol_1h),
        'vol_accel': round(vol_accel, 2),
        'bs_onchain': round(bs_onchain, 2),
        'buys_5m': buys,
        'sells_5m': sells,
        'price_5m': round(price_5m, 1),
        'liquidity_usd': int(liquidity_usd),  # НОВОЕ
        'price_1h': round(price_1h, 1),  # НОВОЕ
        'txns_5m_total': txns_5m_total  # НОВОЕ
    }

except Exception as e:
    return dexscreener_fallback()
```

def rugcheck_fallback():
“”“Нейтральный fallback - не блокировать”””
return {
‘rug_level’: ‘UNKNOWN’,
‘rug_score’: 0,
‘rug_flags’: ‘NONE’,
‘lp_locked’: 100  # Нейтральный — не блокировать сигнал
}

def dexscreener_fallback():
“”“Нейтральный fallback”””
return {
‘dex_status’: ‘UNAVAILABLE’,
‘vol_5m’: 0,
‘vol_1h’: 0,
‘vol_accel’: 0,
‘bs_onchain’: 0,
‘buys_5m’: 0,
‘sells_5m’: 0,
‘price_5m’: 0,
‘liquidity_usd’: 0,  # НОВОЕ
‘price_1h’: 0,  # НОВОЕ
‘txns_5m_total’: 0  # НОВОЕ
}

def empty_data():
return {
**rugcheck_fallback(),
**dexscreener_fallback()
}

def format_response(data, source=“full”):
“”“Формат ответа для FlowIn substitution”””
response_text = f”””=== ON-CHAIN ===
RUG_LEVEL: {data[‘rug_level’]}
RUG_SCORE: {data[‘rug_score’]}
RUG_FLAGS: {data[‘rug_flags’]}
LP_LOCKED: {data[‘lp_locked’]}
DEX_STATUS: {data[‘dex_status’]}
VOL_5M: {data[‘vol_5m’]}
VOL_1H: {data[‘vol_1h’]}
VOL_ACCEL: {data[‘vol_accel’]}
BS_ONCHAIN: {data[‘bs_onchain’]}
BUYS_5M: {data[‘buys_5m’]}
SELLS_5M: {data[‘sells_5m’]}
PRICE_5M: {data[‘price_5m’]}
LIQUIDITY_USD: {data[‘liquidity_usd’]}
PRICE_1H: {data[‘price_1h’]}
TXNS_5M_TOTAL: {data[‘txns_5m_total’]}
===============”””

```
return jsonify({"response": response_text, "source": source}), 200
```

if **name** == ‘**main**’:
port = int(os.environ.get(‘PORT’, 5000))
app.run(host=‘0.0.0.0’, port=port)
