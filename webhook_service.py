# Webhook Service v2.2 - Smart Entry MEMS

# ASCII-clean version - no Smart Punctuation issues

# Deployed on Render Free Tier

# URL: https://smart-entry-webhook.onrender.com/enrich

# Health endpoint: /health (for cron-job.org keepalive)

import re
import os
import logging
from flask import Flask, request, jsonify
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(‘webhook_service’)

app = Flask(**name**)

# Regex - matches all Solana CA addresses (32-44 chars, base58)

SOLANA_CA_PATTERN = re.compile(r’\b([1-9A-HJ-NP-Za-km-z]{32,44})\b’)

# Words that look like CA but are not addresses

EXCLUDE_WORDS = {
‘PFM’, ‘TIP’, ‘OKX’, ‘MAE’, ‘BAN’, ‘BNK’, ‘PDR’, ‘BLO’, ‘STB’,
‘TRO’, ‘TRT’, ‘GMG’, ‘PHO’, ‘AXI’, ‘EXP’, ‘TW’,
‘NEW’, ‘live’, ‘meme’, ‘docs’, ‘guide’, ‘matches’
}

DEXSCREENER_URL = ‘https://api.dexscreener.com/latest/dex/tokens/{ca}’
RUGCHECK_URL = ‘https://api.rugcheck.xyz/v1/tokens/{ca}/report/summary’

@app.route(’/health’, methods=[‘GET’, ‘HEAD’])
def health():
return jsonify({‘status’: ‘ok’, ‘version’: ‘2.2’}), 200

@app.route(’/enrich’, methods=[‘POST’])
def enrich():
try:
data = request.get_json()
if not data or ‘message’ not in data:
return jsonify({‘error’: ‘no message’}), 400

```
    message = data['message']
    ca = extract_ca(message)
    
    if not ca:
        return format_response(empty_data(), source='no_ca')
    
    logger.info('Processing CA: ' + ca)
    
    rugcheck = fetch_rugcheck(ca)
    dexscreener = fetch_dexscreener(ca)
    
    return format_response({
        **rugcheck,
        **dexscreener
    }, source='full')
    
except Exception as e:
    logger.error('Error: ' + str(e))
    return format_response(empty_data(), source='error: ' + str(e))
```

def extract_ca(message):
matches = SOLANA_CA_PATTERN.findall(message)

```
candidates = [m for m in matches if m not in EXCLUDE_WORDS]

if not candidates:
    return None

# Priority for pump tokens
pump_tokens = [c for c in candidates if c.endswith('pump')]
if pump_tokens:
    return pump_tokens[0]

return candidates[0]
```

def fetch_rugcheck(ca):
try:
r = requests.get(RUGCHECK_URL.format(ca=ca), timeout=8)
if r.status_code != 200:
return rugcheck_fallback()

```
    d = r.json()
    
    score = d.get('score', 0)
    if score < 5:
        risk_level = 'SAFE'
    elif score < 20:
        risk_level = 'WARNING'
    else:
        risk_level = 'DANGER'
    
    risks = d.get('risks', [])
    if any(r.get('level') == 'danger' for r in risks):
        risk_level = 'DANGER'
    elif any(r.get('level') == 'warn' for r in risks) and risk_level == 'SAFE':
        risk_level = 'WARNING'
    
    flags = []
    for risk in risks:
        name = risk.get('name', '')
        if name and name not in flags:
            flags.append(name)
    
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

except Exception:
    return rugcheck_fallback()
```

def fetch_dexscreener(ca):
try:
r = requests.get(DEXSCREENER_URL.format(ca=ca), timeout=8)
if r.status_code != 200:
return dexscreener_fallback()

```
    d = r.json()
    pairs = d.get('pairs', [])
    
    if not pairs:
        return {**dexscreener_fallback(), 'dex_status': 'UNAVAILABLE'}
    
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
    
    return {
        'dex_status': 'OK',
        'vol_5m': int(vol_5m),
        'vol_1h': int(vol_1h),
        'vol_accel': round(vol_accel, 2),
        'bs_onchain': round(bs_onchain, 2),
        'buys_5m': buys,
        'sells_5m': sells,
        'price_5m': round(price_5m, 1)
    }

except Exception:
    return dexscreener_fallback()
```

def rugcheck_fallback():
return {
‘rug_level’: ‘UNKNOWN’,
‘rug_score’: 0,
‘rug_flags’: ‘NONE’,
‘lp_locked’: 100
}

def dexscreener_fallback():
return {
‘dex_status’: ‘UNAVAILABLE’,
‘vol_5m’: 0,
‘vol_1h’: 0,
‘vol_accel’: 0,
‘bs_onchain’: 0,
‘buys_5m’: 0,
‘sells_5m’: 0,
‘price_5m’: 0
}

def empty_data():
return {
**rugcheck_fallback(),
**dexscreener_fallback()
}

def format_response(data, source=‘full’):
response_text = ‘=== ON-CHAIN ===\n’
response_text += ’RUG_LEVEL: ’ + str(data[‘rug_level’]) + ‘\n’
response_text += ’RUG_SCORE: ’ + str(data[‘rug_score’]) + ‘\n’
response_text += ’RUG_FLAGS: ’ + str(data[‘rug_flags’]) + ‘\n’
response_text += ’LP_LOCKED: ’ + str(data[‘lp_locked’]) + ‘\n’
response_text += ’DEX_STATUS: ’ + str(data[‘dex_status’]) + ‘\n’
response_text += ’VOL_5M: ’ + str(data[‘vol_5m’]) + ‘\n’
response_text += ’VOL_1H: ’ + str(data[‘vol_1h’]) + ‘\n’
response_text += ’VOL_ACCEL: ’ + str(data[‘vol_accel’]) + ‘\n’
response_text += ’BS_ONCHAIN: ’ + str(data[‘bs_onchain’]) + ‘\n’
response_text += ’BUYS_5M: ’ + str(data[‘buys_5m’]) + ‘\n’
response_text += ’SELLS_5M: ’ + str(data[‘sells_5m’]) + ‘\n’
response_text += ’PRICE_5M: ’ + str(data[‘price_5m’]) + ‘\n’
response_text += ‘===============’

```
return jsonify({'response': response_text, 'source': source}), 200
```

if **name** == ‘**main**’:
port = int(os.environ.get(‘PORT’, 5000))
app.run(host=‘0.0.0.0’, port=port)
