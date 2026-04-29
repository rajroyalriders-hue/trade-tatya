import discord
import requests
import threading
import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from flask import Flask
import anthropic
from fyers_apiv3 import fyersModel
import os

# =========================
# CONFIG
# =========================
DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
CLAUDE_API_KEY     = os.getenv("CLAUDE_API_KEY")
FYERS_APP_ID       = os.getenv("FYERS_APP_ID", "R19GD9BCZH-200")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN")

MAIN_CHANNEL_ID  = 1498261283584217219
TOKEN_CHANNEL_ID = 1498884238496239626
ALLOWED_USER_ID  = 1158032451659120732

RAILWAY_API_TOKEN  = os.getenv("RAILWAY_API_TOKEN")
RAILWAY_PROJECT_ID = os.getenv("RAILWAY_PROJECT_ID")
RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID")

# =========================
# INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True
client           = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
fyers            = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=FYERS_ACCESS_TOKEN)
executor         = ThreadPoolExecutor(max_workers=4)
flask_app        = Flask(__name__)

@flask_app.route("/")
def home():
    return "Pro Trading Bot Running!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False, use_reloader=False)

# =========================
# ASYNC WRAPPER — prevents Discord freeze
# =========================
async def run_in_thread(func, *args, timeout=15):
    """Run blocking function in thread pool with timeout"""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, func, *args),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        print(f"TIMEOUT: {func.__name__} took too long")
        return None
    except Exception as e:
        print(f"ERROR in {func.__name__}: {e}")
        return None

# =========================
# RAILWAY TOKEN UPDATE
# =========================
def _update_railway_token(new_token):
    headers = {"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"}
    env_resp = requests.post(
        "https://backboard.railway.app/graphql/v2",
        json={"query": 'query { project(id: "%s") { environments { edges { node { id name } } } } }' % RAILWAY_PROJECT_ID},
        headers=headers, timeout=15
    ).json()
    envs   = env_resp["data"]["project"]["environments"]["edges"]
    env_id = next((e["node"]["id"] for e in envs if e["node"]["name"].lower() == "production"), envs[0]["node"]["id"])
    requests.post(
        "https://backboard.railway.app/graphql/v2",
        json={
            "query": "mutation variableUpsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }",
            "variables": {"input": {"projectId": RAILWAY_PROJECT_ID, "environmentId": env_id,
                                    "serviceId": RAILWAY_SERVICE_ID, "name": "FYERS_ACCESS_TOKEN", "value": new_token}}
        },
        headers=headers, timeout=15
    )
    requests.post(
        "https://backboard.railway.app/graphql/v2",
        json={
            "query": "mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) { serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId) }",
            "variables": {"serviceId": RAILWAY_SERVICE_ID, "environmentId": env_id}
        },
        headers=headers, timeout=15
    )
    global fyers, FYERS_ACCESS_TOKEN
    FYERS_ACCESS_TOKEN = new_token
    fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=new_token)
    return True, "Token update + Railway redeploy! Bot 1-2 min mein restart hoga."

# =========================
# SOURCE 1: FYERS (blocking — will be run in thread)
# =========================
def _get_nifty_fyers():
    try:
        resp = fyers.quotes(data={"symbols": "NSE:NIFTY50-INDEX"})
        if "d" not in resp or not resp["d"]:
            return None
        v = resp["d"][0].get("v", {})
        if not v or not v.get("lp"):
            return None
        return {
            "price":      round(float(v.get("lp", 0)), 2),
            "open":       round(float(v.get("open_price", 0)), 2),
            "high":       round(float(v.get("high_price", 0)), 2),
            "low":        round(float(v.get("low_price", 0)), 2),
            "prev_close": round(float(v.get("prev_close_price") or v.get("prev_close", 0)), 2),
            "source": "Fyers"
        }
    except Exception as e:
        print(f"Fyers error: {e}")
        return None

# =========================
# SOURCE 2: YAHOO (blocking)
# =========================
def _get_nifty_yahoo():
    try:
        import yfinance as yf
        ticker = yf.Ticker("^NSEI")
        hist   = ticker.history(period="2d", interval="1m")
        info   = ticker.fast_info
        if hist.empty:
            return None
        price = round(float(info.last_price), 2)
        prev  = round(float(info.previous_close), 2)
        today = hist[hist.index.normalize() == hist.index.normalize()[-1]]
        return {
            "price":      price,
            "open":       round(float(today["Open"].iloc[0]), 2),
            "high":       round(float(today["High"].max()), 2),
            "low":        round(float(today["Low"].min()), 2),
            "prev_close": prev,
            "source": "Yahoo"
        }
    except Exception as e:
        print(f"Yahoo error: {e}")
        return None

# =========================
# SOURCE 3: NSE DIRECT (blocking)
# =========================
def _get_nifty_nse():
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://www.nseindia.com"}
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r = s.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050", headers=headers, timeout=8)
        nifty = next((x for x in r.json()["data"] if x["symbol"] == "NIFTY 50"), None)
        if not nifty:
            return None
        return {
            "price":      round(float(nifty["lastPrice"]), 2),
            "open":       round(float(nifty["open"]), 2),
            "high":       round(float(nifty["dayHigh"]), 2),
            "low":        round(float(nifty["dayLow"]), 2),
            "prev_close": round(float(nifty["previousClose"]), 2),
            "source": "NSE"
        }
    except Exception as e:
        print(f"NSE error: {e}")
        return None

async def get_market_data():
    """Try all 3 sources concurrently with timeout"""
    tasks = [
        run_in_thread(_get_nifty_fyers, timeout=10),
        run_in_thread(_get_nifty_yahoo, timeout=12),
        run_in_thread(_get_nifty_nse,   timeout=10),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    working = [r for r in results if r and not isinstance(r, Exception) and r.get("price", 0) > 0]
    if not working:
        return None
    primary = working[0]
    primary["all_sources"] = " + ".join([x["source"] for x in working[:2]])
    return primary

# =========================
# RSI CALCULATION
# =========================
def _calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)

def _get_rsi_data():
    prices = []
    src    = ""
    try:
        import yfinance as yf
        hist = yf.Ticker("^NSEI").history(period="5d", interval="5m")
        if not hist.empty:
            prices = list(hist["Close"])
            src    = "Yahoo"
    except:
        pass
    if not prices:
        try:
            today = datetime.now()
            resp  = fyers.history(data={
                "symbol": "NSE:NIFTY50-INDEX", "resolution": "5", "date_format": "1",
                "range_from": (today - timedelta(days=5)).strftime("%Y-%m-%d"),
                "range_to":   today.strftime("%Y-%m-%d"), "cont_flag": "1"
            })
            if "candles" in resp:
                prices = [c[4] for c in resp["candles"]]
                src    = "Fyers"
        except:
            pass
    if not prices:
        return None, None, None
    rsi = _calc_rsi(prices)
    if rsi is None:
        return None, None, src
    if rsi >= 70:   sig = f"Overbought — Strong selling pressure"
    elif rsi >= 60: sig = f"Bullish momentum"
    elif rsi >= 45: sig = f"Neutral zone"
    elif rsi >= 30: sig = f"Bearish momentum"
    else:           sig = f"Oversold — Buying opportunity"
    return rsi, sig, src

# =========================
# INDIA VIX
# =========================
def _get_india_vix():
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com"}
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r  = s.get("https://www.nseindia.com/api/equity-stockIndices?index=INDIA%20VIX", headers=headers, timeout=8)
        vd = r.json()["data"][0]
        vx = round(float(vd.get("lastPrice", 0)), 2)
        ch = round(float(vd.get("pChange", 0)), 2)
        if vx < 13:   lv = "Very Low (Complacent)"
        elif vx < 16: lv = "Low — Stable trending"
        elif vx < 20: lv = "Medium — Normal"
        elif vx < 25: lv = "High — Caution"
        else:         lv = "Very High — Panic"
        return {"vix": vx, "chg": ch, "level": lv}
    except Exception as e:
        print(f"VIX error: {e}")
        return None

# =========================
# OPTION CHAIN
# =========================
def _get_option_chain():
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r       = s.get("https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY", headers=headers, timeout=10)
        records = r.json().get("records", {})
        spot    = records.get("underlyingValue", 0)
        expiry  = records.get("expiryDates", [None])[0]
        oi_data = []
        total_c = total_p = max_c_oi = max_p_oi = 0
        max_c_strike = max_p_strike = 0
        for item in records.get("data", []):
            if item.get("expiryDate") != expiry:
                continue
            strike = item.get("strikePrice", 0)
            ce = item.get("CE", {})
            pe = item.get("PE", {})
            c_oi = ce.get("openInterest", 0) or 0
            p_oi = pe.get("openInterest", 0) or 0
            total_c += c_oi
            total_p += p_oi
            if c_oi > max_c_oi: max_c_oi = c_oi; max_c_strike = strike
            if p_oi > max_p_oi: max_p_oi = p_oi; max_p_strike = strike
            oi_data.append({
                "strike": strike,
                "c_oi": c_oi, "p_oi": p_oi,
                "c_coi": ce.get("changeinOpenInterest", 0) or 0,
                "p_coi": pe.get("changeinOpenInterest", 0) or 0,
                "c_iv": ce.get("impliedVolatility", 0) or 0,
                "p_iv": pe.get("impliedVolatility", 0) or 0,
                "c_ltp": ce.get("lastPrice", 0) or 0,
                "p_ltp": pe.get("lastPrice", 0) or 0,
            })
        pcr      = round(total_p / total_c, 2) if total_c > 0 else 0
        atm      = round(spot / 50) * 50
        atm_data = next((x for x in oi_data if x["strike"] == atm), None)
        near     = sorted(oi_data, key=lambda x: abs(x["strike"] - spot))[:12]
        return {
            "spot": spot, "expiry": expiry, "pcr": pcr,
            "total_c": total_c, "total_p": total_p,
            "max_c_strike": max_c_strike, "max_c_oi": max_c_oi,
            "max_p_strike": max_p_strike, "max_p_oi": max_p_oi,
            "atm": atm, "atm_data": atm_data, "near": near,
        }
    except Exception as e:
        print(f"OC error: {e}")
        return None

# =========================
# GREEKS
# =========================
def _get_greeks(spot, oc_data=None):
    atm        = round(spot / 50) * 50
    expiry_str = ""
    try:
        today      = datetime.now()
        days_ahead = (3 - today.weekday()) % 7
        if days_ahead == 0: days_ahead = 7
        expiry_str = (today + timedelta(days=days_ahead)).strftime("%d%b%y").upper()
        resp = fyers.quotes(data={"symbols": f"NSE:NIFTY{expiry_str}{atm}CE,NSE:NIFTY{expiry_str}{atm}PE"})
        greeks = {}
        if "d" in resp and resp["d"]:
            for item in resp["d"]:
                sym = item.get("n", "")
                v   = item.get("v", {})
                if v.get("lp", 0) > 0:
                    greeks[sym] = {
                        "ltp":   round(float(v.get("lp", 0)), 2),
                        "delta": v.get("delta", 0), "gamma": v.get("gamma", 0),
                        "theta": v.get("theta", 0), "vega":  v.get("vega", 0),
                        "iv":    v.get("iv", 0),
                    }
        if greeks:
            return greeks, atm, expiry_str
    except Exception as e:
        print(f"Greeks error: {e}")
    # Fallback from OC data
    greeks = {}
    if oc_data and oc_data.get("atm_data"):
        ad = oc_data["atm_data"]
        greeks[f"CE_{atm}"] = {"ltp": ad["c_ltp"], "iv": ad["c_iv"], "delta": 0.5,  "theta": -8, "vega": 12}
        greeks[f"PE_{atm}"] = {"ltp": ad["p_ltp"], "iv": ad["p_iv"], "delta": -0.5, "theta": -8, "vega": 12}
    return greeks, atm, expiry_str

# =========================
# DEMAND SUPPLY ZONES
# =========================
def get_zones(market, oc_data):
    price = market["price"]
    high  = market["high"]
    low   = market["low"]
    prev  = market["prev_close"]
    pivot = round((high + low + prev) / 3, 2)
    r1 = round((2 * pivot) - low, 2)
    r2 = round(pivot + (high - low), 2)
    r3 = round(high + 2 * (pivot - low), 2)
    s1 = round((2 * pivot) - high, 2)
    s2 = round(pivot - (high - low), 2)
    s3 = round(low - 2 * (high - pivot), 2)

    supply = [r1, r2, r3]
    demand = [s1, s2, s3]

    if oc_data:
        supply.append(oc_data["max_c_strike"])
        demand.append(oc_data["max_p_strike"])
        top_c = sorted(oc_data["near"], key=lambda x: x["c_oi"], reverse=True)[:2]
        top_p = sorted(oc_data["near"], key=lambda x: x["p_oi"], reverse=True)[:2]
        supply += [x["strike"] for x in top_c]
        demand += [x["strike"] for x in top_p]

    base = round(price / 100) * 100
    for i in range(-3, 4):
        lvl = base + i * 100
        if lvl > price + 50:   supply.append(lvl)
        elif lvl < price - 50: demand.append(lvl)

    supply_sorted = sorted(set([z for z in supply if z > price]))[:4]
    demand_sorted = sorted(set([z for z in demand if z < price]), reverse=True)[:4]

    cnt_s = {}
    cnt_d = {}
    for z in supply:
        if z > price:
            k = round(z/50)*50; cnt_s[k] = cnt_s.get(k,0)+1
    for z in demand:
        if z < price:
            k = round(z/50)*50; cnt_d[k] = cnt_d.get(k,0)+1

    strong_supply = sorted([k for k,v in cnt_s.items() if v>=2 and k>price])[:2]
    strong_demand = sorted([k for k,v in cnt_d.items() if v>=2 and k<price], reverse=True)[:2]

    return {
        "supply_zones": supply_sorted, "demand_zones": demand_sorted,
        "strong_supply": strong_supply, "strong_demand": strong_demand,
        "imm_res": supply_sorted[0] if supply_sorted else round(price+100,2),
        "imm_sup": demand_sorted[0] if demand_sorted else round(price-100,2),
        "pivot": pivot, "r1": r1, "r2": r2, "r3": r3,
        "s1": s1, "s2": s2, "s3": s3,
    }

# =========================
# MASTER ENGINE
# =========================
def master_engine(market, oc_data, greeks, atm, expiry_str, zones, rsi, vix_data):
    price      = market["price"]
    vwap       = round((market["high"] + market["low"] + price) / 3, 2)
    change_pct = round(((price - market["prev_close"]) / market["prev_close"]) * 100, 2)
    signals    = {}
    sb = sb_bear = 0

    if price > vwap:
        signals["VWAP"] = f"Bullish — Price above VWAP ({vwap})"; sb += 1
    else:
        signals["VWAP"] = f"Bearish — Price below VWAP ({vwap})"; sb_bear += 1

    if rsi:
        if rsi > 70:    signals["RSI"] = f"Overbought ({rsi}) — Bearish signal"; sb_bear += 2
        elif rsi >= 60: signals["RSI"] = f"Bullish momentum ({rsi})"; sb += 1
        elif rsi <= 30: signals["RSI"] = f"Oversold ({rsi}) — Bullish signal"; sb += 2
        elif rsi <= 40: signals["RSI"] = f"Bearish momentum ({rsi})"; sb_bear += 1
        else:           signals["RSI"] = f"Neutral ({rsi})"
    else:
        signals["RSI"] = "N/A"

    if oc_data:
        pcr = oc_data["pcr"]
        if pcr > 1.3:   signals["PCR"] = f"Bullish ({pcr}) — Put writers active"; sb += 1
        elif pcr < 0.7: signals["PCR"] = f"Bearish ({pcr}) — Call writers active"; sb_bear += 1
        else:           signals["PCR"] = f"Neutral ({pcr})"
        if oc_data.get("atm_data"):
            ad = oc_data["atm_data"]
            if ad["c_coi"] < 0 and ad["p_coi"] > 0:
                signals["OI Chg"] = "Bullish — CE unwinding, PE adding"; sb += 1
            elif ad["c_coi"] > 0 and ad["p_coi"] < 0:
                signals["OI Chg"] = "Bearish — CE adding, PE unwinding"; sb_bear += 1
            else:
                signals["OI Chg"] = "Mixed"

    dist_res = abs(price - zones["imm_res"])
    dist_sup = abs(price - zones["imm_sup"])
    if dist_sup < 30:
        signals["Zone"] = f"Near Support ({zones['imm_sup']}) — Bounce zone"; sb += 2
    elif dist_res < 30:
        signals["Zone"] = f"Near Resistance ({zones['imm_res']}) — Rejection zone"; sb_bear += 2
    elif price > zones["pivot"]:
        signals["Zone"] = f"Above Pivot ({zones['pivot']}) — Bullish bias"; sb += 1
    else:
        signals["Zone"] = f"Below Pivot ({zones['pivot']}) — Bearish bias"; sb_bear += 1

    if vix_data:
        vix = vix_data["vix"]
        if vix > 22:   signals["VIX"] = f"High VIX ({vix}) — Use spreads"
        elif vix < 16: signals["VIX"] = f"Low VIX ({vix}) — Buy options"; sb += 1
        else:          signals["VIX"] = f"Normal VIX ({vix})"
    else:
        signals["VIX"] = "N/A"

    ce_key = f"NSE:NIFTY{expiry_str}{atm}CE"
    pe_key = f"NSE:NIFTY{expiry_str}{atm}PE"
    atm_ce = greeks.get(ce_key) or greeks.get(f"CE_{atm}", {})
    atm_pe = greeks.get(pe_key) or greeks.get(f"PE_{atm}", {})
    if atm_ce and atm_pe:
        ced = abs(atm_ce.get("delta", 0.5))
        ped = abs(atm_pe.get("delta", 0.5))
        if ced > ped:   signals["Greeks"] = f"Bullish delta ({ced:.2f} > {ped:.2f})"; sb += 1
        elif ped > ced: signals["Greeks"] = f"Bearish delta ({ped:.2f} > {ced:.2f})"; sb_bear += 1
        else:           signals["Greeks"] = "Equal delta"

    if sb >= 4:
        action = "CALL BUY"
        option = f"NIFTY {atm} CE"
        entry  = atm_ce.get("ltp", 0) if atm_ce else 0
        sl_o   = round(entry * 0.75, 2)
        t1_o   = round(entry * 1.35, 2)
        t2_o   = round(entry * 1.70, 2)
        conf   = min(int((sb / max(sb + sb_bear, 1)) * 100), 95)
    elif sb_bear >= 4:
        action = "PUT BUY"
        option = f"NIFTY {atm} PE"
        entry  = atm_pe.get("ltp", 0) if atm_pe else 0
        sl_o   = round(entry * 0.75, 2)
        t1_o   = round(entry * 1.35, 2)
        t2_o   = round(entry * 1.70, 2)
        conf   = min(int((sb_bear / max(sb + sb_bear, 1)) * 100), 95)
    else:
        action = "NO TRADE"
        option = entry = sl_o = t1_o = t2_o = None
        conf   = 0

    rr = "N/A"
    if entry and sl_o and entry != sl_o:
        risk   = abs(entry - sl_o)
        reward = abs(t1_o - entry) if t1_o else 0
        rr     = f"1:{round(reward/risk,1)}" if risk > 0 else "N/A"

    return {
        "action": action, "option": option, "confidence": conf,
        "entry": round(entry, 2) if entry else None,
        "sl_opt": sl_o, "t1_opt": t1_o, "t2_opt": t2_o,
        "sl_spot": round(zones["imm_sup"] - 25, 2),
        "t1_spot": round(zones["imm_res"], 2),
        "t2_spot": round(zones["r2"], 2),
        "rr": rr, "sb": sb, "sb_bear": sb_bear,
        "signals": signals, "vwap": vwap, "change": change_pct,
        "atm_ce": atm_ce, "atm_pe": atm_pe,
    }

# =========================
# FORMAT OUTPUT
# =========================
def format_output(res, market, oc_data, zones, rsi, rsi_sig, vix_data):
    price = market["price"]
    chg   = res["change"]
    arrow = "📈" if chg >= 0 else "📉"
    ae    = "🟢" if "CALL" in res["action"] else ("🔴" if "PUT" in res["action"] else "⚪")

    msg = f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 **NIFTY PRO SIGNAL** | {datetime.now().strftime("%d %b  %H:%M")}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 **Source:** {market.get('all_sources', market.get('source','N/A'))}

💹 **NIFTY50:** `₹{price}` {arrow} `{chg:+.2f}%`
O:`{market['open']}` H:`{market['high']}` L:`{market['low']}` PC:`{market['prev_close']}`
VWAP: `{res['vwap']}`"""

    if vix_data:
        msg += f"\n\n⚡ **India VIX:** `{vix_data['vix']}` ({vix_data['chg']:+.2f}%) — {vix_data['level']}"
    else:
        msg += f"\n\n⚡ **India VIX:** Unavailable"

    if rsi:
        bar = "█" * int(rsi/10) + "░" * (10 - int(rsi/10))
        msg += f"\n\n📉 **RSI(14):** `{rsi}` [{bar}]\n{rsi_sig}"
    else:
        msg += f"\n\n📉 **RSI:** Unavailable (market may be closed)"

    msg += f"""

🏗️ **DEMAND & SUPPLY ZONES**
```
Supply (Resistance): {" | ".join([str(z) for z in zones["supply_zones"][:3]])}
Demand (Support):    {" | ".join([str(z) for z in zones["demand_zones"][:3]])}
Strong Supply: {zones["strong_supply"] if zones["strong_supply"] else "Forming..."}
Strong Demand: {zones["strong_demand"] if zones["strong_demand"] else "Forming..."}
Imm Resistance: {zones["imm_res"]}  |  Imm Support: {zones["imm_sup"]}
```
📐 P:`{zones['pivot']}` R1:`{zones['r1']}` R2:`{zones['r2']}` R3:`{zones['r3']}`
   S1:`{zones['s1']}` S2:`{zones['s2']}` S3:`{zones['s3']}`"""

    if oc_data:
        pe = "🟢" if oc_data["pcr"] > 1.2 else ("🔴" if oc_data["pcr"] < 0.8 else "🟡")
        msg += f"""

📈 **OPTION CHAIN** (Expiry: {oc_data["expiry"]})
ATM:`{oc_data["atm"]}` | PCR:{pe}`{oc_data["pcr"]}`
Call Wall: `{oc_data["max_c_strike"]}` {oc_data["max_c_oi"]:,} ← Resistance
Put Wall:  `{oc_data["max_p_strike"]}` {oc_data["max_p_oi"]:,} ← Support
```
Strike  | CE OI     | PE OI     |C.IV|P.IV"""
        for s in oc_data["near"][:7]:
            mk = "◄" if s["strike"] == oc_data["atm"] else " "
            msg += f"\n{s['strike']}{mk}| {s['c_oi']:>9,} | {s['p_oi']:>9,} |{s['c_iv']:>4}%|{s['p_iv']:>4}%"
        msg += "\n```"
    else:
        msg += "\n\n📈 **Option Chain:** Unavailable (NSE down)"

    ce = res["atm_ce"]
    pe_g = res["atm_pe"]
    if ce or pe_g:
        msg += f"""
🔢 **ATM GREEKS**
CE: LTP`{ce.get('ltp','?')}` IV`{ce.get('iv','?')}%` Δ`{ce.get('delta','?')}` θ`{ce.get('theta','?')}`
PE: LTP`{pe_g.get('ltp','?')}` IV`{pe_g.get('iv','?')}%` Δ`{pe_g.get('delta','?')}` θ`{pe_g.get('theta','?')}`"""
    else:
        msg += "\n🔢 **Greeks:** Unavailable (token may have expired)"

    msg += f"\n\n🎯 **SIGNALS** (Bull:`{res['sb']}` Bear:`{res['sb_bear']}`)"
    for k, v in res["signals"].items():
        msg += f"\n`{k}:` {v}"

    msg += f"""

━━━━━━━━━━━━━━━━━━━━━━━━━━━
{ae} **SIGNAL: {res['action']}** | Confidence: `{res['confidence']}%`
━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    if res["option"]:
        msg += f"""
Option: `{res['option']}`
Entry: `₹{res['entry']}` | R:R `{res['rr']}`
Option SL:`₹{res['sl_opt']}` → T1:`₹{res['t1_opt']}` → T2:`₹{res['t2_opt']}`
Spot SL:`{res['sl_spot']}` → T1:`{res['t1_spot']}` → T2:`{res['t2_spot']}`"""
    else:
        msg += f"\nWait for move above `{zones['imm_res']}` or bounce from `{zones['imm_sup']}`"

    msg += "\n\n⚠️ *Not SEBI registered. Manual execution only.*"
    return msg

# =========================
# AI ANALYSIS
# =========================
def _get_ai_analysis(market, oc_data, zones, rsi, vix_data, res):
    oc_str = f"PCR:{oc_data['pcr']}, Call Wall:{oc_data['max_c_strike']}, Put Wall:{oc_data['max_p_strike']}" if oc_data else "N/A"
    prompt = f"""Expert NIFTY50 intraday options trader. Give precise trade call.

Price={market['price']} Change={res['change']}% VWAP={res['vwap']}
O={market['open']} H={market['high']} L={market['low']}
RSI={rsi or 'N/A'} | VIX={vix_data['vix'] if vix_data else 'N/A'}
Bull:{res['sb']}/7 Bear:{res['sb_bear']}/7

Resistance={zones['imm_res']} Support={zones['imm_sup']}
Pivot={zones['pivot']} R1={zones['r1']} S1={zones['s1']}
Strong Supply={zones['strong_supply']} Strong Demand={zones['strong_demand']}
OI: {oc_str}
System: {res['action']} ({res['confidence']}%)

Rules: Min 65% confidence. NO TRADE if unclear.

SIGNAL: [CALL BUY/PUT BUY/NO TRADE]
OPTION:
ENTRY:
STOP LOSS:
TARGET 1:
TARGET 2:
CONFIDENCE:
REASON: (2 lines)
RISK: (1 line)"""
    try:
        r = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=350,
            messages=[{"role": "user", "content": prompt}]
        )
        return r.content[0].text
    except Exception as e:
        return f"AI Error: {str(e)}"

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    print(f"Bot ready: {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # TOKEN CHANNEL
    if message.channel.id == TOKEN_CHANNEL_ID:
        if message.author.id != ALLOWED_USER_ID:
            try: await message.delete()
            except: pass
            return
        tok = message.content.strip()
        try: await message.delete()
        except: pass
        if len(tok) < 20:
            await message.channel.send("Token too short!", delete_after=5)
            return
        await message.channel.send("Updating token...")
        try:
            ok, msg = await run_in_thread(_update_railway_token, tok, timeout=30)
            await message.channel.send(f"{'✅' if ok else '❌'} {msg}")
        except:
            await message.channel.send("❌ Update failed. Check Railway variables.")
        return

    cmd = message.content.lower().strip()

    if cmd == "trade!":
        status_msg = await message.channel.send("⏳ Fetching all data (parallel)...")

        # Run ALL data fetches concurrently — no blocking!
        market, oc_data, rsi_result, vix_data = await asyncio.gather(
            get_market_data(),
            run_in_thread(_get_option_chain, timeout=12),
            run_in_thread(_get_rsi_data, timeout=15),
            run_in_thread(_get_india_vix, timeout=10),
        )

        if not market:
            await status_msg.edit(content="❌ No market data from Fyers/Yahoo/NSE. Markets may be closed.")
            return

        rsi, rsi_sig, rsi_src = rsi_result if rsi_result else (None, None, None)

        src_info = f"✅ Data: **{market.get('all_sources', market['source'])}**"
        if not oc_data: src_info += " | ⚠️ OI unavailable"
        if not rsi:     src_info += " | ⚠️ RSI unavailable"
        await status_msg.edit(content=src_info)

        # Greeks also in thread
        greeks_result = await run_in_thread(_get_greeks, market["price"], oc_data, timeout=10)
        if greeks_result:
            greeks, atm, expiry_str = greeks_result
        else:
            greeks, atm, expiry_str = {}, round(market["price"]/50)*50, ""

        zones = get_zones(market, oc_data)
        res   = master_engine(market, oc_data, greeks, atm, expiry_str, zones, rsi, vix_data)
        out   = format_output(res, market, oc_data, zones, rsi, rsi_sig, vix_data)

        await message.channel.send(out)

        await message.channel.send("🤖 AI analysis...")
        ai = await run_in_thread(_get_ai_analysis, market, oc_data, zones, rsi, vix_data, res, timeout=30)
        if ai:
            await message.channel.send(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n🤖 **AI ANALYSIS** | {datetime.now().strftime('%H:%M')}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n{ai}\n\n⚠️ *Not SEBI registered.*")

    elif cmd == "oi!":
        oc = await run_in_thread(_get_option_chain, timeout=12)
        if not oc:
            await message.channel.send("❌ OI unavailable"); return
        rows = "```\nStrike  | CE OI     | PE OI     |C.IV|P.IV\n"
        for s in oc["near"][:8]:
            mk = "◄" if s["strike"] == oc["atm"] else " "
            rows += f"{s['strike']}{mk}| {s['c_oi']:>9,} | {s['p_oi']:>9,} |{s['c_iv']:>4}%|{s['p_iv']:>4}%\n"
        rows += "```"
        await message.channel.send(f"📈 **OI** | {oc['expiry']} | Spot:`{oc['spot']}` ATM:`{oc['atm']}` PCR:`{oc['pcr']}`\nCall Wall:`{oc['max_c_strike']}` ({oc['max_c_oi']:,}) | Put Wall:`{oc['max_p_strike']}` ({oc['max_p_oi']:,})\n{rows}")

    elif cmd == "rsi!":
        result = await run_in_thread(_get_rsi_data, timeout=15)
        if not result or not result[0]:
            await message.channel.send("❌ RSI unavailable"); return
        rsi, sig, src = result
        bar = "█" * int(rsi/10) + "░" * (10 - int(rsi/10))
        await message.channel.send(f"📉 **RSI(14):** `{rsi}` [{bar}] (src: {src})\n{sig}")

    elif cmd == "vix!":
        v = await run_in_thread(_get_india_vix, timeout=10)
        if not v:
            await message.channel.send("❌ VIX unavailable"); return
        await message.channel.send(f"⚡ **India VIX:** `{v['vix']}` ({v['chg']:+.2f}%)\n{v['level']}")

    elif cmd == "zones!":
        market = await get_market_data()
        if not market:
            await message.channel.send("❌ No market data"); return
        oc = await run_in_thread(_get_option_chain, timeout=12)
        z  = get_zones(market, oc)
        await message.channel.send(f"🏗️ **ZONES** | Price:`{market['price']}`\nSupply: `{'` `'.join([str(x) for x in z['supply_zones']])}`\nDemand: `{'` `'.join([str(x) for x in z['demand_zones']])}`\nStrong Supply:`{z['strong_supply']}` Strong Demand:`{z['strong_demand']}`\nRes:`{z['imm_res']}` Sup:`{z['imm_sup']}` Pivot:`{z['pivot']}`\nR1:`{z['r1']}` R2:`{z['r2']}` S1:`{z['s1']}` S2:`{z['s2']}`")

    elif cmd == "help!":
        await message.channel.send("📋 **COMMANDS**\n`trade!` — Full PRO analysis (Parallel fetch, no freeze)\n`oi!` — Option chain\n`rsi!` — RSI only\n`vix!` — India VIX\n`zones!` — Demand/Supply zones\n`help!` — This menu\n\n🔐 Token channel mein naya token paste karo directly")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Flask started")
    client.run(DISCORD_TOKEN)
