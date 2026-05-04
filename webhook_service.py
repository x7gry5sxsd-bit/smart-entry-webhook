"""
SMART ENTRY MEMS — Webhook сервис обогащения сигналов
Версия 2: Фиксированный формат ответа.
"""

import re
import asyncio
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="SMART ENTRY Webhook")

CA_REGEX = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{32,44})\b')


def extract_ca(text):
    if not text:
        return None
    matches = CA_REGEX.findall(text)
    candidates = [m for m in matches if 40 <= len(m) <= 44]
    if not candidates:
        return None
    return max(candidates, key=len)


async def fetch_rugcheck(client, ca):
    try:
        resp = await client.get(f"https://api.rugcheck.xyz/v1/tokens/{ca}/report", timeout=3.0)
        if resp.status_code != 200:
            return {"available": False}
        data = resp.json()
        score = data.get("score_normalised") or data.get("score") or 0
        risks = data.get("risks", [])
        risk_names = [r.get("name", "") for r in risks if r.get("level") in ("warn", "danger")]
        lp_locked = 0
        markets = data.get("markets", [])
        if markets and markets[0].get("lp"):
            lp_locked = markets[0]["lp"].get("lpLockedPct", 0)
        if score >= 70:
            level = "SAFE"
        elif score >= 40:
            level = "WARNING"
        else:
            level = "DANGER"
        return {
            "available": True,
            "score": int(score),
            "level": level,
            "risks": risk_names[:3],
            "lp_locked": int(lp_locked),
        }
    except Exception as e:
        log.warning(f"RugCheck error: {e}")
        return {"available": False}


async def fetch_dexscreener(client, ca):
    try:
        resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=3.0)
        if resp.status_code != 200:
            return {"available": False}
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return {"available": False}
        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
        vol_5m = (pair.get("volume") or {}).get("m5", 0)
        vol_1h = (pair.get("volume") or {}).get("h1", 0)
        avg_5m = vol_1h / 12 if vol_1h else 0
        accel = (vol_5m / avg_5m) if avg_5m > 0 else 0
        txns_5m = (pair.get("txns") or {}).get("m5", {})
        buys_5m = txns_5m.get("buys", 0)
        sells_5m = txns_5m.get("sells", 0)
        bs_ratio = (buys_5m / sells_5m) if sells_5m > 0 else 0
        price_change_5m = (pair.get("priceChange") or {}).get("m5", 0)
        return {
            "available": True,
            "vol_5m": int(vol_5m),
            "vol_1h": int(vol_1h),
            "accel": round(accel, 2),
            "buys_5m": buys_5m,
            "sells_5m": sells_5m,
            "bs_ratio": round(bs_ratio, 2),
            "price_change_5m": round(price_change_5m, 1),
        }
    except Exception as e:
        log.warning(f"DexScreener error: {e}")
        return {"available": False}


def build_enrichment_text(rug, dex):
    if rug.get("available"):
        rug_level = rug["level"]
        rug_score = rug["score"]
        rug_flags = ", ".join(rug["risks"]) if rug["risks"] else "NONE"
        lp_locked = rug["lp_locked"]
    else:
        rug_level = "UNKNOWN"
        rug_score = 0
        rug_flags = "N/A"
        lp_locked = 0
    
    if dex.get("available"):
        dex_status = "OK"
        vol_5m = dex["vol_5m"]
        vol_1h = dex["vol_1h"]
        accel = dex["accel"]
        bs_ratio = dex["bs_ratio"]
        buys_5m = dex["buys_5m"]
        sells_5m = dex["sells_5m"]
        price_5m = dex["price_change_5m"]
    else:
        dex_status = "UNAVAILABLE"
        vol_5m = 0
        vol_1h = 0
        accel = 0
        bs_ratio = 0
        buys_5m = 0
        sells_5m = 0
        price_5m = 0
    
    lines = [
        "",
        "=== ON-CHAIN ===",
        f"RUG_LEVEL: {rug_level}",
        f"RUG_SCORE: {rug_score}",
        f"RUG_FLAGS: {rug_flags}",
        f"LP_LOCKED: {lp_locked}",
        f"DEX_STATUS: {dex_status}",
        f"VOL_5M: {vol_5m}",
        f"VOL_1H: {vol_1h}",
        f"VOL_ACCEL: {accel}",
        f"BS_ONCHAIN: {bs_ratio}",
        f"BUYS_5M: {buys_5m}",
        f"SELLS_5M: {sells_5m}",
        f"PRICE_5M: {price_5m}",
        "===============",
    ]
    return "\n".join(lines)


@app.post("/enrich")
async def enrich(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "enrichment": ""}, status_code=400)
    
    message_text = body.get("message", "") or body.get("text", "")
    ca = extract_ca(message_text)
    
    if not ca:
        return {
            "enrichment": "\n=== ON-CHAIN ===\nRUG_LEVEL: NO_CA\nRUG_SCORE: 0\nRUG_FLAGS: N/A\nLP_LOCKED: 0\nDEX_STATUS: NO_CA\nVOL_5M: 0\nVOL_1H: 0\nVOL_ACCEL: 0\nBS_ONCHAIN: 0\nBUYS_5M: 0\nSELLS_5M: 0\nPRICE_5M: 0\n===============",
        }
    
    log.info(f"Processing CA: {ca}")
    
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            fetch_rugcheck(client, ca),
            fetch_dexscreener(client, ca),
            return_exceptions=True
        )
    rug = results[0] if isinstance(results[0], dict) else {"available": False}
    dex = results[1] if isinstance(results[1], dict) else {"available": False}
    
    enrichment_text = build_enrichment_text(rug, dex)
    
    return {
        "enrichment": enrichment_text,
        "ca": ca,
        "rug_score": rug.get("score") if rug.get("available") else None,
        "rug_level": rug.get("level") if rug.get("available") else None,
        "vol_accel": dex.get("accel") if dex.get("available") else None,
    }


@app.get("/")
async def root():
    return {"service": "SMART ENTRY Webhook", "status": "ok", "version": "2"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
