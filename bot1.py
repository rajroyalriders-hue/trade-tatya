import discord
import requests
import threading
import asyncio
import math
from datetime import datetime, timedelta
from flask import Flask, request as flask_request
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
client         = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
fyers          = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=FYERS_ACCESS_TOKEN)
discord_loop   = None
token_channel  = None
flask_app      = Flask(__name__)

# =========================
# FLASK
# =========================
@flask_app.route("/")
def home():
    return "Pro Trading Bot Running!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False, use_reloader=False)

# =========================
# RAILWAY TOKEN UPDATE
# =========================
def update_railway_token(new_token):
    try:
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
                "variables": {"input": {"projectId": RAILWAY_PROJECT_ID, "environmentId": env_id, "serviceId": RAILWAY_SERVICE_ID, "name": "FYERS_ACCESS_TOKEN", "value": new_token}}
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
        return True, "✅ Token update + Railway redeploy! Bot 1-2 min mein restart hoga."
    except Exception as e:
        return False, f"❌ Error: {str(e)}"

# =========================
# DATA SOURCE 1: FYERS
# =========================
def get_nifty_fyers():
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
            "source": "Fyers 🔵"
        }
    except Exception as e:
        print(f"Fyers error: {e}")
        return None

# =========================
# DATA SOURCE 2: YAHOO
# =========================
def get_nifty_yahoo():
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
            "source": "Yahoo 🟡"
        }
    except Exception as e:
        print(f"Yahoo error: {e}")
        return None

# =========================
# DATA SOURCE 3: NSE DIRECT
# =========================
def get_nifty_nse():
    try:
        headers = {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.nseindia.com",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r = s.get(
            "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",
            headers=headers, timeout=8
        )
        nifty = next((x for x in r.json()["data"] if x["symbol"] == "NIFTY 50"), None)
        if not nifty:
            return None
        return {
            "price":      round(float(nifty["lastPrice"]), 2),
            "open":       round(float(nifty["open"]), 2),
            "high":       round(float(nifty["dayHigh"]), 2),
            "low":        round(float(nifty["dayLow"]), 2),
            "prev_close": round(float(nifty["previousClose"]), 2),
            "source": "NSE 🟢"
        }
    except Exception as e:
        print(f"NSE error: {e}")
        return None

def get_market_data():
    """Fyers → Yahoo → NSE fallback. Returns best available data + sources used."""
    results  = []
    sources  = []
    for name, fn in [("Fyers", get_nifty_fyers), ("Yahoo", get_nifty_yahoo), ("NSE", get_nifty_nse)]:
        d = fn()
        if d and d["price"] > 0:
            results.append(d)
            sources.append(d["source"])
            if len(results) >= 2:
                break

    if not results:
        return None

    # Use first successful source as primary
    primary = results[0]
    primary["all_sources"] = " + ".join(sources)
    return primary

# =========================
# RSI CALCULATION
# =========================
def get_rsi(prices, period=14):
    """Calculate RSI from price list"""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100
    rs  = avg_gain / avg_loss
    rsi = round(100 - (100 / (1 + rs)), 2)
    return rsi

def get_rsi_data():
    """Get RSI from Yahoo 5m candles, fallback to Fyers"""
    prices = []
    source = ""

    # Try Yahoo first
    try:
        import yfinance as yf
        hist = yf.Ticker("^NSEI").history(period="5d", interval="5m")
        if not hist.empty:
            prices = list(hist["Close"])
            source = "Yahoo"
    except:
        pass

    # Fallback: Fyers historical
    if not prices:
        try:
            today    = datetime.now()
            date_str = today.strftime("%Y-%m-%d")
            resp     = fyers.history(data={
                "symbol":     "NSE:NIFTY50-INDEX",
                "resolution": "5",
                "date_format": "1",
                "range_from": (today - timedelta(days=5)).strftime("%Y-%m-%d"),
                "range_to":   date_str,
                "cont_flag":  "1"
            })
            if "candles" in resp:
                prices = [c[4] for c in resp["candles"]]  # close prices
                source = "Fyers"
        except:
            pass

    if not prices:
        return None, None, None

    rsi       = get_rsi(prices)
    rsi_5     = get_rsi(prices[-30:], period=5) if len(prices) >= 6 else None

    if rsi is None:
        return None, None, source

    # RSI signal
    if rsi >= 70:
        signal = "Overbought 🔴 — Sell pressure likely"
    elif rsi >= 60:
        signal = "Bullish momentum 🟠"
    elif rsi >= 45:
        signal = "Neutral ⚪"
    elif rsi >= 30:
        signal = "Bearish momentum 🟡"
    else:
        signal = "Oversold 🟢 — Buy pressure likely"

    return rsi, signal, source

# =========================
# INDIA VIX
# =========================
def get_india_vix():
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com"}
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r = s.get("https://www.nseindia.com/api/equity-stockIndices?index=INDIA%20VIX", headers=headers, timeout=8)
        vd  = r.json()["data"][0]
        vix = round(float(vd.get("lastPrice", 0)), 2)
        chg = round(float(vd.get("pChange", 0)), 2)

        if vix < 13:   level = "Very Low 😴"
        elif vix < 16: level = "Low 🟢 (Stable)"
        elif vix < 20: level = "Medium 🟡 (Normal)"
        elif vix < 25: level = "High 🟠 (Caution)"
        else:          level = "Very High 🔴 (Panic)"

        return {"vix": vix, "chg": chg, "level": level}
    except Exception as e:
        print(f"VIX error: {e}")
        return None

# =========================
# OPTION CHAIN (NSE)
# =========================
def get_option_chain():
    try:
        headers = {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.nseindia.com/option-chain",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r = s.get("https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY", headers=headers, timeout=10)
        records  = r.json().get("records", {})
        spot     = records.get("underlyingValue", 0)
        expiries = records.get("expiryDates", [])
        expiry   = expiries[0] if expiries else None

        oi_data = []
        total_c = total_p = 0
        max_c_oi = max_p_oi = 0
        max_c_strike = max_p_strike = 0

        for item in records.get("data", []):
            if item.get("expiryDate") != expiry:
                continue
            strike = item.get("strikePrice", 0)
            ce = item.get("CE", {})
            pe = item.get("PE", {})

            c_oi  = ce.get("openInterest", 0) or 0
            p_oi  = pe.get("openInterest", 0) or 0
            c_coi = ce.get("changeinOpenInterest", 0) or 0
            p_coi = pe.get("changeinOpenInterest", 0) or 0
            c_iv  = ce.get("impliedVolatility", 0) or 0
            p_iv  = pe.get("impliedVolatility", 0) or 0
            c_ltp = ce.get("lastPrice", 0) or 0
            p_ltp = pe.get("lastPrice", 0) or 0

            total_c += c_oi
            total_p += p_oi

            if c_oi > max_c_oi: max_c_oi = c_oi; max_c_strike = strike
            if p_oi > max_p_oi: max_p_oi = p_oi; max_p_strike = strike

            oi_data.append({
                "strike": strike,
                "c_oi": c_oi, "p_oi": p_oi,
                "c_coi": c_coi, "p_coi": p_coi,
                "c_iv": c_iv, "p_iv": p_iv,
                "c_ltp": c_ltp, "p_ltp": p_ltp,
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
# DEMAND / SUPPLY ZONES
# =========================
def get_demand_supply_zones(market, oc_data):
    """Multi-source D/S zone detection"""
    price = market["price"]
    high  = market["high"]
    low   = market["low"]
    prev  = market["prev_close"]

    zones = {"supply": [], "demand": []}

    # Zone 1: Pivot-based
    pivot = (high + low + prev) / 3
    r1 = (2 * pivot) - low
    r2 = pivot + (high - low)
    r3 = high + 2 * (pivot - low)
    s1 = (2 * pivot) - high
    s2 = pivot - (high - low)
    s3 = low - 2 * (high - pivot)

    zones["supply"] += [round(r1, 2), round(r2, 2), round(r3, 2)]
    zones["demand"] += [round(s1, 2), round(s2, 2), round(s3, 2)]

    # Zone 2: OI walls (max pain zones)
    if oc_data:
        zones["supply"].append(oc_data["max_c_strike"])  # Call wall = resistance
        zones["demand"].append(oc_data["max_p_strike"])  # Put wall = support

        # Top 3 OI strikes as zones
        near = oc_data["near"]
        top_c = sorted(near, key=lambda x: x["c_oi"], reverse=True)[:2]
        top_p = sorted(near, key=lambda x: x["p_oi"], reverse=True)[:2]
        zones["supply"] += [x["strike"] for x in top_c]
        zones["demand"] += [x["strike"] for x in top_p]

    # Zone 3: Round number zones near price
    base = round(price / 100) * 100
    for i in range(-3, 4):
        lvl = base + i * 100
        if lvl > price + 50:
            zones["supply"].append(lvl)
        elif lvl < price - 50:
            zones["demand"].append(lvl)

    # Clean & sort
    supply_sorted = sorted(set([z for z in zones["supply"] if z > price]))[:4]
    demand_sorted = sorted(set([z for z in zones["demand"] if z < price]), reverse=True)[:4]

    # Immediate S/R
    imm_res = supply_sorted[0] if supply_sorted else round(price + 100, 2)
    imm_sup = demand_sorted[0] if demand_sorted else round(price - 100, 2)

    # Zone strength (how many sources agree)
    sup_counts = {}
    dem_counts = {}
    for z in zones["supply"]:
        if z > price:
            key = round(z / 50) * 50
            sup_counts[key] = sup_counts.get(key, 0) + 1
    for z in zones["demand"]:
        if z < price:
            key = round(z / 50) * 50
            dem_counts[key] = dem_counts.get(key, 0) + 1

    # Strong zones = confirmed by 2+ sources
    strong_supply = sorted([k for k, v in sup_counts.items() if v >= 2 and k > price])[:2]
    strong_demand = sorted([k for k, v in dem_counts.items() if v >= 2 and k < price], reverse=True)[:2]

    return {
        "supply_zones": supply_sorted,
        "demand_zones": demand_sorted,
        "strong_supply": strong_supply,
        "strong_demand": strong_demand,
        "imm_resistance": imm_res,
        "imm_support":    imm_sup,
        "pivot": round(pivot, 2),
        "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
        "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
    }

# =========================
# GREEKS (FYERS fallback OC)
# =========================
def get_greeks(spot, oc_data=None):
    atm        = round(spot / 50) * 50
    expiry_str = ""

    try:
        today      = datetime.now()
        days_ahead = (3 - today.weekday()) % 7
        if days_ahead == 0: days_ahead = 7
        expiry_dt  = today + timedelta(days=days_ahead)
        expiry_str = expiry_dt.strftime("%d%b%y").upper()

        symbols = [
            f"NSE:NIFTY{expiry_str}{atm}CE",
            f"NSE:NIFTY{expiry_str}{atm}PE",
        ]
        resp    = fyers.quotes(data={"symbols": ",".join(symbols)})
        greeks  = {}
        if "d" in resp and resp["d"]:
            for item in resp["d"]:
                sym = item.get("n", "")
                v   = item.get("v", {})
                if v.get("lp", 0) > 0:
                    greeks[sym] = {
                        "ltp":   round(float(v.get("lp", 0)), 2),
                        "delta": v.get("delta", 0),
                        "gamma": v.get("gamma", 0),
                        "theta": v.get("theta", 0),
                        "vega":  v.get("vega", 0),
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
        greeks[f"CE_{atm}"] = {"ltp": ad["c_ltp"], "iv": ad["c_iv"], "delta": 0.5,  "theta": -8,  "vega": 12}
        greeks[f"PE_{atm}"] = {"ltp": ad["p_ltp"], "iv": ad["p_iv"], "delta": -0.5, "theta": -8,  "vega": 12}

    return greeks, atm, expiry_str

# =========================
# MASTER TRADE ENGINE
# =========================
def master_engine(market, oc_data, greeks, atm, expiry_str, zones, rsi, vix_data):
    price  = market["price"]
    vwap   = round((market["high"] + market["low"] + market["price"]) / 3, 2)
    change = round(((price - market["prev_close"]) / market["prev_close"]) * 100, 2)

    signals     = {}
    score_bull  = 0
    score_bear  = 0

    # --- Signal 1: Price vs VWAP ---
    if price > vwap:
        signals["VWAP"] = "🟢 Bullish (Price > VWAP)"
        score_bull += 1
    else:
        signals["VWAP"] = "🔴 Bearish (Price < VWAP)"
        score_bear += 1

    # --- Signal 2: RSI ---
    if rsi:
        if rsi >= 60:
            signals["RSI"] = f"🟠 Bullish momentum ({rsi})"
            score_bull += 1
        elif rsi <= 40:
            signals["RSI"] = f"🟡 Bearish momentum ({rsi})"
            score_bear += 1
        elif rsi < 30:
            signals["RSI"] = f"🟢 Oversold ({rsi}) — Buy signal"
            score_bull += 2
        elif rsi > 70:
            signals["RSI"] = f"🔴 Overbought ({rsi}) — Sell signal"
            score_bear += 2
        else:
            signals["RSI"] = f"⚪ Neutral ({rsi})"
    else:
        signals["RSI"] = "⚪ No data"

    # --- Signal 3: OI / PCR ---
    if oc_data:
        pcr = oc_data["pcr"]
        if pcr > 1.3:
            signals["PCR"] = f"🟢 Bullish ({pcr}) — Put writers defending"
            score_bull += 1
        elif pcr < 0.7:
            signals["PCR"] = f"🔴 Bearish ({pcr}) — Call writers dominant"
            score_bear += 1
        else:
            signals["PCR"] = f"⚪ Neutral ({pcr})"

        # OI change direction
        if oc_data.get("atm_data"):
            ad = oc_data["atm_data"]
            if ad["c_coi"] < 0 and ad["p_coi"] > 0:
                signals["OI Change"] = "🟢 Bullish (CE unwinding, PE adding)"
                score_bull += 1
            elif ad["c_coi"] > 0 and ad["p_coi"] < 0:
                signals["OI Change"] = "🔴 Bearish (CE adding, PE unwinding)"
                score_bear += 1
            else:
                signals["OI Change"] = "⚪ Mixed OI change"

    # --- Signal 4: Demand/Supply Zone ---
    imm_res = zones["imm_resistance"]
    imm_sup = zones["imm_support"]
    dist_res = abs(price - imm_res)
    dist_sup = abs(price - imm_sup)

    if dist_sup < 30:
        signals["Zone"] = f"🟢 Near Support ({imm_sup}) — Bounce expected"
        score_bull += 2
    elif dist_res < 30:
        signals["Zone"] = f"🔴 Near Resistance ({imm_res}) — Rejection possible"
        score_bear += 2
    elif price > zones["pivot"]:
        signals["Zone"] = f"🟢 Above Pivot ({zones['pivot']})"
        score_bull += 1
    else:
        signals["Zone"] = f"🔴 Below Pivot ({zones['pivot']})"
        score_bear += 1

    # --- Signal 5: VIX ---
    vix_score = 0
    if vix_data:
        vix = vix_data["vix"]
        if vix < 16:
            signals["VIX"] = f"🟢 Low VIX ({vix}) — Trending market"
            vix_score = 1
        elif vix > 22:
            signals["VIX"] = f"🔴 High VIX ({vix}) — Avoid naked options"
            vix_score = -1
        else:
            signals["VIX"] = f"🟡 Normal VIX ({vix})"

    # --- Signal 6: Greeks ---
    ce_key = f"NSE:NIFTY{expiry_str}{atm}CE"
    pe_key = f"NSE:NIFTY{expiry_str}{atm}PE"
    atm_ce = greeks.get(ce_key) or greeks.get(f"CE_{atm}", {})
    atm_pe = greeks.get(pe_key) or greeks.get(f"PE_{atm}", {})

    if atm_ce and atm_pe:
        ce_d = abs(atm_ce.get("delta", 0.5))
        pe_d = abs(atm_pe.get("delta", 0.5))
        if ce_d > pe_d:
            signals["Greeks"] = f"🟢 CE Delta stronger ({ce_d:.2f} vs {pe_d:.2f})"
            score_bull += 1
        elif pe_d > ce_d:
            signals["Greeks"] = f"🔴 PE Delta stronger ({pe_d:.2f} vs {ce_d:.2f})"
            score_bear += 1
        else:
            signals["Greeks"] = "⚪ Equal delta"

    # --- Final Decision ---
    total      = score_bull + score_bear
    confidence = 0

    if score_bull >= 4:
        action     = "CALL BUY 🟢"
        option     = f"NIFTY {atm} CE"
        entry      = atm_ce.get("ltp", price) if atm_ce else price
        sl_spot    = imm_sup - 25
        t1_spot    = imm_res
        t2_spot    = zones["r2"]
        sl_opt     = round(entry * 0.75, 2)
        t1_opt     = round(entry * 1.35, 2)
        t2_opt     = round(entry * 1.70, 2)
        confidence = min(int((score_bull / max(total, 1)) * 100), 95)

    elif score_bear >= 4:
        action     = "PUT BUY 🔴"
        option     = f"NIFTY {atm} PE"
        entry      = atm_pe.get("ltp", price) if atm_pe else price
        sl_spot    = imm_res + 25
        t1_spot    = imm_sup
        t2_spot    = zones["s2"]
        sl_opt     = round(entry * 0.75, 2)
        t1_opt     = round(entry * 1.35, 2)
        t2_opt     = round(entry * 1.70, 2)
        confidence = min(int((score_bear / max(total, 1)) * 100), 95)

    else:
        action     = "WAIT / NO TRADE ⚪"
        option     = None
        entry      = sl_opt = t1_opt = t2_opt = None
        sl_spot    = imm_sup
        t1_spot    = imm_res
        t2_spot    = zones["r2"]
        confidence = 0

    # Risk:Reward
    rr = "N/A"
    if entry and sl_opt and t1_opt and entry != sl_opt:
        risk   = abs(entry - sl_opt)
        reward = abs(t1_opt - entry)
        rr     = f"1:{round(reward/risk, 1)}" if risk > 0 else "N/A"

    return {
        "action": action, "option": option, "confidence": confidence,
        "entry": round(entry, 2) if entry else None,
        "sl_opt": sl_opt, "t1_opt": t1_opt, "t2_opt": t2_opt,
        "sl_spot": round(sl_spot, 2), "t1_spot": round(t1_spot, 2), "t2_spot": round(t2_spot, 2),
        "rr": rr, "score_bull": score_bull, "score_bear": score_bear,
        "signals": signals, "vwap": vwap, "change": change,
        "atm_ce": atm_ce, "atm_pe": atm_pe,
    }

# =========================
# FORMAT OUTPUT
# =========================
def format_full_output(res, market, oc_data, zones, rsi, rsi_signal, vix_data):
    price = market["price"]
    chg   = res["change"]
    chg_e = "📈" if chg >= 0 else "📉"

    # Header
    msg = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 **NIFTY PRO SIGNAL** | {datetime.now().strftime("%d %b %Y  %H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 **Data Source:** {market.get('all_sources', market.get('source', 'N/A'))}

💹 **MARKET**
Price: `₹{price}` {chg_e} `{chg:+.2f}%`
Open: `{market['open']}` | High: `{market['high']}` | Low: `{market['low']}`
VWAP: `{res['vwap']}` | Prev Close: `{market['prev_close']}`
"""

    # VIX
    if vix_data:
        msg += f"""
⚡ **INDIA VIX**
`{vix_data['vix']}` ({vix_data['chg']:+.2f}%) — {vix_data['level']}
"""

    # RSI
    if rsi:
        msg += f"""
📉 **RSI (14)**
`{rsi}` — {rsi_signal}
"""

    # Zones
    msg += f"""
🏗️ **DEMAND & SUPPLY ZONES**
```
Supply Zones (Resistance):  {' | '.join([str(z) for z in zones['supply_zones'][:3]])}
Demand Zones (Support):     {' | '.join([str(z) for z in zones['demand_zones'][:3]])}

Strong Supply: {zones['strong_supply'] if zones['strong_supply'] else 'Forming...'}
Strong Demand: {zones['strong_demand'] if zones['strong_demand'] else 'Forming...'}

Immediate Resistance: {zones['imm_resistance']}
Immediate Support:    {zones['imm_support']}
```

📐 **PIVOT LEVELS**
Pivot: `{zones['pivot']}` | R1: `{zones['r1']}` | R2: `{zones['r2']}` | R3: `{zones['r3']}`
S1: `{zones['s1']}` | S2: `{zones['s2']}` | S3: `{zones['s3']}`
"""

    # OI
    if oc_data:
        pcr_emoji = "🟢" if oc_data["pcr"] > 1.2 else ("🔴" if oc_data["pcr"] < 0.8 else "🟡")
        msg += f"""
📈 **OPTION CHAIN** (Expiry: {oc_data['expiry']})
ATM Strike: `{oc_data['atm']}` | PCR: {pcr_emoji} `{oc_data['pcr']}`
Max Call OI: `{oc_data['max_c_strike']}` → {oc_data['max_c_oi']:,} ← **Resistance Wall**
Max Put OI:  `{oc_data['max_p_strike']}` → {oc_data['max_p_oi']:,} ← **Support Wall**
"""
        # Near strikes table
        near_top = [x for x in oc_data["near"][:6]]
        table    = "```\nStrike  | CE OI    | PE OI    | CE IV | PE IV\n"
        table   += "--------|----------|----------|-------|------\n"
        for s in near_top:
            atm_mark = " ◄ATM" if s["strike"] == oc_data["atm"] else ""
            table += f"{s['strike']}{atm_mark:<5} | {s['c_oi']:>8,} | {s['p_oi']:>8,} | {s['c_iv']:>5}% | {s['p_iv']:>5}%\n"
        table += "```"
        msg += table

    # Greeks
    ce = res["atm_ce"]
    pe = res["atm_pe"]
    if ce or pe:
        msg += f"""
🔢 **ATM GREEKS**
CE → LTP: `{ce.get('ltp','N/A')}` | IV: `{ce.get('iv','N/A')}%` | Delta: `{ce.get('delta','N/A')}` | Theta: `{ce.get('theta','N/A')}`
PE → LTP: `{pe.get('ltp','N/A')}` | IV: `{pe.get('iv','N/A')}%` | Delta: `{pe.get('delta','N/A')}` | Theta: `{pe.get('theta','N/A')}`
"""

    # Signals confluence
    msg += "\n🎯 **SIGNAL CONFLUENCE**\n"
    for k, v in res["signals"].items():
        msg += f"`{k}:` {v}\n"
    msg += f"\nBull Score: `{res['score_bull']}` | Bear Score: `{res['score_bear']}`\n"

    # Final Trade
    action_emoji = "🟢" if "CALL" in res["action"] else ("🔴" if "PUT" in res["action"] else "⚪")
    msg += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{action_emoji} **FINAL SIGNAL: {res['action']}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    if res["option"]:
        msg += f"""**Option:** `{res['option']}`
**Entry:** `₹{res['entry']}`
**Stop Loss:** `₹{res['sl_opt']}` (Option) | Spot SL: `{res['sl_spot']}`
**Target 1:** `₹{res['t1_opt']}` (Option) | Spot T1: `{res['t1_spot']}`
**Target 2:** `₹{res['t2_opt']}` (Option) | Spot T2: `{res['t2_spot']}`
**Risk:Reward:** `{res['rr']}`
**Confidence:** `{res['confidence']}%`
"""
    else:
        msg += f"**Support:** `{res['sl_spot']}` | **Resistance:** `{res['t1_spot']}`\nWait for clear breakout above `{zones['imm_resistance']}` or bounce from `{zones['imm_support']}`\n"

    msg += "\n⚠️ *Not SEBI registered. Manual execution only. Trade at your own risk.*"
    return msg

# =========================
# AI ANALYSIS
# =========================
def get_ai_analysis(market, oc_data, zones, rsi, vix_data, res):
    vix_str = f"India VIX: {vix_data['vix']} ({vix_data['level']})" if vix_data else "VIX: N/A"
    oc_str  = ""
    if oc_data:
        oc_str = f"PCR: {oc_data['pcr']}, Max Call OI: {oc_data['max_c_strike']}, Max Put OI: {oc_data['max_p_strike']}"

    prompt = f"""You are an expert NIFTY50 intraday options trader. Give a precise trade recommendation.

MARKET:
- Price: {market['price']} | Change: {res['change']}%
- Open: {market['open']} | High: {market['high']} | Low: {market['low']}
- VWAP: {res['vwap']} | Prev Close: {market['prev_close']}

INDICATORS:
- RSI(14): {rsi or 'N/A'}
- {vix_str}
- Bull Score: {res['score_bull']}/6 | Bear Score: {res['score_bear']}/6

ZONES:
- Immediate Resistance: {zones['imm_resistance']}
- Immediate Support: {zones['imm_support']}
- Pivot: {zones['pivot']} | R1: {zones['r1']} | S1: {zones['s1']}
- Strong Supply: {zones['strong_supply']}
- Strong Demand: {zones['strong_demand']}

OPTION CHAIN:
{oc_str}

SYSTEM SIGNAL: {res['action']} (Confidence: {res['confidence']}%)

Rules:
- Min 65% confidence for trade
- If unclear → NO TRADE with reason
- Consider RSI + VIX + Zones + OI together

Respond in this EXACT format:
SIGNAL: [CALL BUY / PUT BUY / NO TRADE]
OPTION: [strike + type]
ENTRY: [price]
STOP LOSS: [price]
TARGET 1: [price]
TARGET 2: [price]
CONFIDENCE: [X%]
REASON: [2-3 lines max]
RISK: [1 line]"""

    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text
    except Exception as e:
        return f"❌ AI Error: {str(e)}"

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    global discord_loop, token_channel
    discord_loop  = asyncio.get_event_loop()
    token_channel = client.get_channel(TOKEN_CHANNEL_ID)
    print(f"✅ Bot logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # ── TOKEN CHANNEL ──
    if message.channel.id == TOKEN_CHANNEL_ID:
        if message.author.id != ALLOWED_USER_ID:
            await message.delete()
            return

        new_token = message.content.strip()
        try:
            await message.delete()
        except:
            pass

        if len(new_token) < 20:
            await message.channel.send("⚠️ Token bahut chhota lag raha hai.", delete_after=5)
            return

        await message.channel.send("⏳ Token update ho raha hai...")
        ok, msg = update_railway_token(new_token)
        await message.channel.send(msg)
        return

    # ── MAIN COMMANDS ──
    cmd = message.content.lower().strip()

    if cmd == "trade!":
        await message.channel.send("⏳ Collecting data from all sources...")

        # Gather all data
        market = get_market_data()
        if not market:
            await message.channel.send("❌ Market data unavailable from all 3 sources (Fyers/Yahoo/NSE). Markets may be closed.")
            return

        await message.channel.send(f"✅ Market data: {market.get('all_sources', market['source'])}")

        oc_data = get_option_chain()
        if not oc_data:
            await message.channel.send("⚠️ NSE Option Chain unavailable — continuing with available data...")

        rsi, rsi_signal, rsi_src = get_rsi_data()
        vix_data                  = get_india_vix()
        zones                     = get_demand_supply_zones(market, oc_data)
        greeks, atm, expiry_str   = get_greeks(market["price"], oc_data)
        res                       = master_engine(market, oc_data, greeks, atm, expiry_str, zones, rsi, vix_data)

        output = format_full_output(res, market, oc_data, zones, rsi, rsi_signal, vix_data)
        await message.channel.send(output)

        await message.channel.send("🤖 Getting AI deep analysis...")
        ai = get_ai_analysis(market, oc_data, zones, rsi, vix_data, res)
        await message.channel.send(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 **AI DEEP ANALYSIS** | {datetime.now().strftime("%H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{ai}

⚠️ *Not SEBI registered. Manual execution only.*""")

    elif cmd == "oi!":
        await message.channel.send("📡 Fetching Option Chain...")
        oc_data = get_option_chain()
        if not oc_data:
            await message.channel.send("❌ NSE Option Chain unavailable right now.")
            return

        near  = oc_data["near"][:8]
        table = "```\nStrike   | CE OI     | PE OI     | CE IV  | PE IV\n"
        table += "---------|-----------|-----------|--------|-------\n"
        for s in near:
            mark = " ◄" if s["strike"] == oc_data["atm"] else "  "
            table += f"{s['strike']}{mark:<3} | {s['c_oi']:>9,} | {s['p_oi']:>9,} | {s['c_iv']:>6}% | {s['p_iv']:>5}%\n"
        table += "```"

        await message.channel.send(f"""📈 **OI SNAPSHOT** | {datetime.now().strftime("%H:%M:%S")}
Spot: `{oc_data['spot']}` | Expiry: `{oc_data['expiry']}` | ATM: `{oc_data['atm']}`
PCR: `{oc_data['pcr']}` | Call OI: `{oc_data['total_c']:,}` | Put OI: `{oc_data['total_p']:,}`
🔴 Call Wall (Resistance): `{oc_data['max_c_strike']}` — {oc_data['max_c_oi']:,}
🟢 Put Wall (Support): `{oc_data['max_p_strike']}` — {oc_data['max_p_oi']:,}
{table}""")

    elif cmd == "rsi!":
        await message.channel.send("📉 Calculating RSI...")
        rsi, signal, src = get_rsi_data()
        if not rsi:
            await message.channel.send("❌ RSI data unavailable.")
            return
        await message.channel.send(f"📉 **RSI Analysis** (Source: {src})\nRSI(14): `{rsi}` — {signal}")

    elif cmd == "vix!":
        vix = get_india_vix()
        if not vix:
            await message.channel.send("❌ VIX data unavailable.")
            return
        await message.channel.send(f"⚡ **India VIX:** `{vix['vix']}` ({vix['chg']:+.2f}%)\n{vix['level']}")

    elif cmd == "zones!":
        await message.channel.send("🏗️ Calculating Demand/Supply Zones...")
        market = get_market_data()
        if not market:
            await message.channel.send("❌ Market data unavailable.")
            return
        oc_data = get_option_chain()
        zones   = get_demand_supply_zones(market, oc_data)
        await message.channel.send(f"""🏗️ **DEMAND & SUPPLY ZONES**
Price: `{market['price']}`

**Supply (Resistance):** `{'` | `'.join([str(z) for z in zones['supply_zones']])}`
**Demand (Support):**    `{'` | `'.join([str(z) for z in zones['demand_zones']])}`

**Strong Supply:** `{zones['strong_supply'] or 'Forming...'}`
**Strong Demand:** `{zones['strong_demand'] or 'Forming...'}`

**Immediate Resistance:** `{zones['imm_resistance']}`
**Immediate Support:**    `{zones['imm_support']}`

**Pivots:** P:`{zones['pivot']}` R1:`{zones['r1']}` R2:`{zones['r2']}` S1:`{zones['s1']}` S2:`{zones['s2']}`""")

    elif cmd == "help!":
        await message.channel.send("""📋 **COMMANDS**

`trade!` — Full PRO analysis (All data + RSI + VIX + OI + Zones + AI)
`oi!`    — Option Chain snapshot
`rsi!`   — RSI analysis only
`vix!`   — India VIX only
`zones!` — Demand/Supply zones only
`help!`  — This menu

🔐 **Token Update** — Paste new token in private channel directly""")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    print("✅ Flask started")
    client.run(DISCORD_TOKEN)
