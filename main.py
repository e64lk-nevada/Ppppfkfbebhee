import os, time, logging, asyncio
from datetime import datetime, timezone
from typing import Optional
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format="%(asctime)s [%(levelname)s] %(message)s")
log=logging.getLogger("bot")

PAPER_MODE=True
PAPER_BALANCE=float(os.getenv("PAPER_BALANCE","1000"))
STAKE=float(os.getenv("PAPER_STAKE","10"))
MAX_PRICE_AGE=10
MAX_PRICE_DIVERGENCE_PCT=float(os.getenv("MAX_PRICE_DIVERGENCE_PCT","0.15"))
MIN_SECONDS_TO_MARKET_END=int(os.getenv("MIN_SECONDS_TO_END","35"))
MARKET_REFRESH_SECONDS=30
PRICE_REFRESH_SECONDS=3
PREDICTION_REFRESH_SECONDS=30

app=FastAPI(title="BTC Polymarket Paper Monitor", version="2.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

state={
    "prices":{}, "market":None, "prediction":None, "last_updated":None,
    "paper_balance":PAPER_BALANCE, "paper_stake":STAKE, "paper_bets":[],
    "blocked_reason":"Starting up", "status":"STARTING", "price_validation":None,
    "stats":{"wins":0,"losses":0,"total":0}
}
client: Optional[httpx.AsyncClient]=None

def now_ts(): return time.time()
def iso_now(): return datetime.now(timezone.utc).isoformat()

async def get_json(url, params=None):
    global client
    try:
        r=await client.get(url, params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Fetch failed %s: %s", url, e)
        return None

async def fetch_prices():
    # Two spot sources are compared. Neither is claimed to be Polymarket's official resolution oracle.
    binance=await get_json("https://api.binance.com/api/v3/ticker/price", {"symbol":"BTCUSDT"})
    coinbase=await get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    prices={}
    try:
        prices["binance"]={"value":float(binance["price"]),"timestamp":now_ts(),"source":"Binance BTCUSDT"}
    except Exception: pass
    try:
        prices["coinbase"]={"value":float(coinbase["price"]),"timestamp":now_ts(),"source":"Coinbase BTC-USD"}
    except Exception: pass
    state["prices"]=prices
    if len(prices)>=2:
        a,b=prices["binance"]["value"],prices["coinbase"]["value"]
        diff_pct=abs(a-b)/((a+b)/2)*100
        state["price_validation"]={"valid":diff_pct<=MAX_PRICE_DIVERGENCE_PCT,"difference_usd":round(abs(a-b),2),"difference_pct":round(diff_pct,5),"limit_pct":MAX_PRICE_DIVERGENCE_PCT}
    else:
        state["price_validation"]={"valid":False,"reason":"One or more price sources unavailable"}
    return prices

async def fetch_markets():
    # Gamma discovery. We intentionally require exact duration/name clues and refuse synthetic fallback.
    data=await get_json("https://gamma-api.polymarket.com/markets", {
        "active":"true","closed":"false","limit":200,"order":"volume24hr","ascending":"false"
    })
    candidates=[]
    if isinstance(data,list):
        for m in data:
            title=(m.get("question") or m.get("title") or "").lower()
            slug=(m.get("slug") or "").lower()
            text=title+" "+slug
            if ("bitcoin" in text or "btc" in text) and ("5 minute" in text or "5-min" in text or "5m" in slug):
                candidates.append(m)
    candidates.sort(key=lambda x: float(x.get("volume24hr") or x.get("volume") or 0), reverse=True)
    if not candidates:
        state["market"]=None
        return None
    m=candidates[0]
    # Parse common Gamma fields; end time is required before any paper entry.
    end=m.get("endDate") or m.get("endDateIso") or m.get("end_date")
    tokens=m.get("clobTokenIds") or m.get("tokens")
    outcomes=m.get("outcomes")
    prices=m.get("outcomePrices")
    if isinstance(tokens,str):
        try: import json; tokens=json.loads(tokens)
        except Exception: tokens=None
    if isinstance(outcomes,str):
        try: import json; outcomes=json.loads(outcomes)
        except Exception: outcomes=None
    if isinstance(prices,str):
        try: import json; prices=json.loads(prices)
        except Exception: prices=None
    market={"question":m.get("question") or m.get("title"),"slug":m.get("slug"),"id":m.get("id"),
            "condition_id":m.get("conditionId"),"end_time":end,"tokens":tokens,"outcomes":outcomes,
            "outcome_prices":prices,"url":"https://polymarket.com/event/"+str(m.get("slug") or ""),
            "updated_at":now_ts()}
    state["market"]=market
    return market

def parse_end(end):
    if not end: return None
    try:
        if isinstance(end,(int,float)): return float(end/1000 if end>1e12 else end)
        s=str(end).replace("Z","+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception: return None

def analyze_signal():
    # Simple transparent baseline only; not a trained prediction model.
    prices=state["prices"]
    p=state["price_validation"]
    if not p or not p.get("valid") or "binance" not in prices:
        state["prediction"]={"direction":"NO SIGNAL","confidence":0,"timestamp":now_ts(),"reason":"Price validation failed"}
        state["blocked_reason"]="Price validation failed"
        return
    # Uses short recent Binance klines. Lightweight momentum indicator, no claims of predictive calibration.
    # The worker stores candle-derived values after fetch_candles.
    trend=state.get("trend",{})
    direction=trend.get("direction","NO SIGNAL")
    confidence=trend.get("confidence",0)
    state["prediction"]={"direction":direction,"confidence":confidence,"timestamp":now_ts(),
                         "price":prices["binance"]["value"],"reason":trend.get("reason","Insufficient candle data")}
    state["blocked_reason"]="Signal monitor only; paper execution disabled by default"

async def fetch_trend():
    k=await get_json("https://api.binance.com/api/v3/klines",{"symbol":"BTCUSDT","interval":"1m","limit":30})
    if not isinstance(k,list) or len(k)<15:
        state["trend"]={"direction":"NO SIGNAL","confidence":0,"reason":"Not enough candles"}
        return
    try:
        closes=[float(row[4]) for row in k]
        # Basic moving average + 5-candle momentum. This is a toy baseline, not a trading strategy.
        fast=sum(closes[-5:])/5
        slow=sum(closes[-15:])/15
        momentum=(closes[-1]-closes[-6])/closes[-6]
        if fast>slow and momentum>0:
            d="UP"
        elif fast<slow and momentum<0:
            d="DOWN"
        else:
            d="NO SIGNAL"
        conf=min(0.65, 0.50+abs(momentum)*120+abs(fast-slow)/slow*50) if d!="NO SIGNAL" else 0.0
        state["trend"]={"direction":d,"confidence":round(conf*100,1),"reason":f"MA5={fast:.2f}, MA15={slow:.2f}, 5m momentum={momentum:.5%}"}
    except Exception as e:
        state["trend"]={"direction":"NO SIGNAL","confidence":0,"reason":str(e)}

async def worker():
    global client
    async with httpx.AsyncClient(headers={"User-Agent":"BTC-Polymarket-PaperMonitor/2.0"}) as c:
        client=c
        while True:
            try:
                await fetch_prices()
                await fetch_trend()
                analyze_signal()
                await fetch_markets()
                m=state.get("market")
                end=parse_end(m.get("end_time")) if m else None
                seconds=(end-now_ts()) if end else None
                validation=state.get("price_validation") or {}
                market_valid=bool(m and end and seconds and seconds>MIN_SECONDS_TO_MARKET_END)
                if not validation.get("valid"):
                    state["status"]="BLOCKED"; state["blocked_reason"]="Price sources invalid or unavailable"
                elif not market_valid:
                    state["status"]="BLOCKED"; state["blocked_reason"]="No verified active market or insufficient time remaining"
                elif (state.get("prediction") or {}).get("direction")=="NO SIGNAL":
                    state["status"]="MONITORING"; state["blocked_reason"]="No clear baseline signal"
                else:
                    state["status"]="MONITORING"; state["blocked_reason"]="Paper-only monitor. No orders are submitted."
                state["market_seconds_remaining"]=seconds
                state["last_updated"]=iso_now()
            except Exception as e:
                log.exception("Worker loop error")
                state["status"]="ERROR"; state["blocked_reason"]=str(e)
            await asyncio.sleep(PRICE_REFRESH_SECONDS)

@app.on_event("startup")
async def startup():
    asyncio.create_task(worker())

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(open("static/index.html",encoding="utf-8").read())

@app.get("/api/health")
async def health():
    return {"ok":True,"mode":"PAPER MONITOR ONLY","updated":state["last_updated"]}

@app.get("/api/status")
async def status():
    out=dict(state)
    out["mode"]="PAPER MONITOR ONLY — no orders"
    return out

@app.get("/api/price")
async def price():
    return {"prices":state["prices"],"validation":state["price_validation"],"updated":state["last_updated"]}

@app.get("/api/market")
async def market():
    return {"market":state["market"],"seconds_remaining":state.get("market_seconds_remaining"),"status":state["status"]}

@app.get("/api/prediction")
async def prediction():
    return {"prediction":state["prediction"],"trend":state.get("trend"),"status":state["status"],"blocked_reason":state["blocked_reason"]}

@app.get("/api/paper/stats")
async def paper_stats():
    return {"mode":"MONITOR ONLY (no trades simulated)","balance":state["paper_balance"],"stake_config":state["paper_stake"],"stats":state["stats"],"bets":[]}
