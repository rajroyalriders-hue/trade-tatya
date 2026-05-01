import discord
import requests
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from flask import Flask
import anthropic
from fyers_apiv3 import fyersModel
import os
import math

# =========================
# CONFIG
# =========================
DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
CLAUDE_API_KEY     = os.getenv("CLAUDE_API_KEY")
FYERS_APP_ID       = os.getenv("FYERS_APP_ID", "R19GD9BCZH-200")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN")
FYERS_SECRET_KEY   = os.getenv("FYERS_SECRET_KEY")

TOKEN_CHANNEL_ID = 1498884238496239626
ALLOWED_USER_ID  = 1158032451659120732

# =========================
# ASSET CONFIG
# =========================
ASSETS = {
    "nifty": {
        "name": "Nifty50",
        "fyers_symbol": "NSE:NIFTY50-INDEX",
        "yahoo_symbol": "^NSEI",
        "type": "index",
        "lot_size": 25,
        "strike_gap": 50,
        "unit": "₹",
    },
    "sensex": {
        "name": "Sensex",
        "fyers_symbol": "BSE:SENSEX-INDEX",
        "yahoo_symbol": "^BSESN",
        "type": "index",
        "lot_size": 10,
        "strike_gap": 100,
        "unit": "₹",
    },
    "gold": {
        "name": "MCX Gold",
        "fyers_symbol": None,
        "yahoo_symbol": None,
        "type": "commodity",
        "lot_size": 100,
        "strike_gap": 100,
        "unit": "₹",
        "mcx_key": "gold",
    },
    "oil": {
        "name": "MCX Crude Oil",
        "fyers_symbol": None,
        "yahoo_symbol": None,
        "type": "commodity",
        "lot_size": 100,
        "strike_gap": 100,
        "unit": "₹",
        "mcx_key": "oil",
    },
    "equity": {
        "name": "Top Volume Stocks",
        "type": "equity",
        "unit": "₹",
    },
}

# =========================
# INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True
client           = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
fyers            = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=FYERS_ACCESS_TOKEN)
executor         = ThreadPoolExecutor(max_workers=8)
flask_app        = Flask(__name__)

@flask_app.route("/")
def home():
    return "Pro Trading Bot Running!", 200

@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False, use_reloader=False)

# =========================
# ASYNC WRAPPER
# =========================
async def run_in_thread(func, *args, timeout=15):
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, func, *args),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        print(f"TIMEOUT: {func.__name__}")
        return None
    except Exception as e:
        print(f"ERROR in {func.__name__}: {e}")
        return None


# =========================
# AUTO MCX SYMBOL
# =========================
def get_mcx_symbol(commodity):
    """Auto generate current/next month MCX symbol"""
    today = datetime.now()
    # MCX expiry is around 5th of each month
    # If past 4th, use next month
    # Try current month first, fallback to next month
    month_str = today.strftime("%b").upper()
    year_str  = today.strftime("%y")
    next_month = today.replace(day=1) + timedelta(days=32)
    nm_str = next_month.strftime("%b").upper()
    ny_str = next_month.strftime("%y")

    if commodity == "gold":
        # Gold expiry around 5th of month — use next-to-next month
        # Gold trades 2 months ahead typically
        two_months = today.replace(day=1) + timedelta(days=63)
        tm_str = two_months.strftime("%b").upper()
        ty_str = two_months.strftime("%y")
        if today.day > 4:
            sym = "MCX:GOLD" + ty_str + tm_str + "FUT"
        else:
            sym = "MCX:GOLD" + ny_str + nm_str + "FUT"
    elif commodity == "oil":
        # Crude oil expiry around 20th
        if today.day > 18:
            sym = "MCX:CRUDEOIL" + ny_str + nm_str + "FUT"
        else:
            sym = "MCX:CRUDEOIL" + year_str + month_str + "FUT"
    else:
        sym = "MCX:" + commodity.upper() + year_str + month_str + "FUT"

    print("MCX Symbol: " + sym)
    return sym

# =========================
# TOKEN UPDATE
# =========================
def _update_token(new_token):
    global fyers, FYERS_ACCESS_TOKEN
    try:
        env_path = "/root/bot/variables.env"
        try:
            with open(env_path, "r") as ef:
                lines = ef.readlines()
        except:
            lines = []
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith("FYERS_ACCESS_TOKEN="):
                new_lines.append("FYERS_ACCESS_TOKEN=" + new_token + "\n")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append("FYERS_ACCESS_TOKEN=" + new_token + "\n")
        with open(env_path, "w") as ef:
            ef.writelines(new_lines)
        FYERS_ACCESS_TOKEN = new_token
        fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=new_token)
        return True, "✅ Token update ho gaya! Bot ready hai."
    except Exception as e:
        FYERS_ACCESS_TOKEN = new_token
        fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=new_token)
        return True, "✅ Token memory mein update ho gaya!"

# =========================
# MARKET DATA — FYERS
# =========================
def _get_fyers_data(symbol):
    try:
        resp = fyers.quotes(data={"symbols": symbol})
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
        print(f"Fyers error {symbol}: {e}")
        return None

# =========================
# MARKET DATA — YAHOO
# =========================
def _get_yahoo_data(symbol):
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
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
        print(f"Yahoo error {symbol}: {e}")
        return None

# =========================
# MARKET DATA — NSE (Index only)
# =========================
def _get_nse_data(symbol_name):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=6)
        r = s.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050", headers=headers, timeout=8)
        nifty = next((x for x in r.json()["data"] if x["symbol"] == symbol_name), None)
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

async def get_market_data(asset_key):
    asset = ASSETS[asset_key]

    if asset_key == "equity":
        return await run_in_thread(_get_top_stocks, timeout=15)

    # Auto symbol for MCX commodities
    fyers_sym = asset.get("fyers_symbol")
    if asset.get("mcx_key"):
        fyers_sym = get_mcx_symbol(asset["mcx_key"])

    tasks = []
    if fyers_sym:
        tasks.append(run_in_thread(_get_fyers_data, fyers_sym, timeout=10))
    if asset.get("yahoo_symbol"):
        tasks.append(run_in_thread(_get_yahoo_data, asset["yahoo_symbol"], timeout=12))
    if asset_key == "nifty":
        tasks.append(run_in_thread(_get_nse_data, "NIFTY 50", timeout=10))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    working = [r for r in results if r and not isinstance(r, Exception) and r.get("price", 0) > 0]

    if not working:
        return None
    primary = working[0]
    primary["all_sources"] = " + ".join([x["source"] for x in working[:2]])
    return primary

# =========================
# TOP VOLUME STOCKS
# =========================
def _get_top_stocks():
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=6)
        r = s.get("https://www.nseindia.com/api/live-analysis-volume-gainers", headers=headers, timeout=10)
        data = r.json().get("data", [])[:5]
        stocks = []
        for item in data:
            stocks.append({
                "symbol":     item.get("symbol", ""),
                "price":      round(float(item.get("lastPrice", 0)), 2),
                "change":     round(float(item.get("pChange", 0)), 2),
                "volume":     item.get("totalTradedVolume", 0),
                "high":       round(float(item.get("dayHigh", 0)), 2),
                "low":        round(float(item.get("dayLow", 0)), 2),
                "prev_close": round(float(item.get("previousClose", 0)), 2),
            })
        return stocks if stocks else None
    except Exception as e:
        print(f"Top stocks error: {e}")
        return _get_top_stocks_yahoo()

def _get_top_stocks_yahoo():
    try:
        import yfinance as yf
        symbols = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"]
        stocks  = []
        for sym in symbols:
            try:
                t    = yf.Ticker(sym)
                info = t.fast_info
                stocks.append({
                    "symbol":     sym.replace(".NS", ""),
                    "price":      round(float(info.last_price), 2),
                    "change":     0,
                    "volume":     int(info.three_month_average_volume or 0),
                    "high":       round(float(info.day_high or 0), 2),
                    "low":        round(float(info.day_low or 0), 2),
                    "prev_close": round(float(info.previous_close or 0), 2),
                })
            except:
                pass
        return stocks if stocks else None
    except:
        return None

# =========================
# RSI
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

def _get_rsi(yahoo_sym, fyers_sym):
    prices = []
    src    = ""
    try:
        import yfinance as yf
        hist = yf.Ticker(yahoo_sym).history(period="5d", interval="5m")
        if not hist.empty:
            prices = list(hist["Close"])
            src    = "Yahoo"
    except:
        pass
    if not prices and fyers_sym:
        try:
            today = datetime.now()
            resp  = fyers.history(data={
                "symbol": fyers_sym, "resolution": "5", "date_format": "1",
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
    if rsi >= 70:   sig = "Overbought — Selling pressure"
    elif rsi >= 60: sig = "Bullish momentum"
    elif rsi >= 45: sig = "Neutral zone"
    elif rsi >= 30: sig = "Bearish momentum"
    else:           sig = "Oversold — Buying opportunity"
    return rsi, sig, src

# =========================
# EMA CALCULATION
# =========================
def _calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

def _get_ema_data(yahoo_sym, fyers_sym):
    prices = []
    src    = ""
    try:
        import yfinance as yf
        hist = yf.Ticker(yahoo_sym).history(period="5d", interval="5m")
        if not hist.empty:
            prices = list(hist["Close"])
            src    = "Yahoo"
    except:
        pass
    if not prices and fyers_sym:
        try:
            today = datetime.now()
            resp  = fyers.history(data={
                "symbol": fyers_sym, "resolution": "5", "date_format": "1",
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

    ema9  = _calc_ema(prices, 9)
    ema14 = _calc_ema(prices, 14)

    if not ema9 or not ema14:
        return None, None, src

    current = prices[-1]

    if current > ema9 > ema14:
        signal = "Strong Bullish 🟢 (Price > EMA9 > EMA14)"
        score  = 2
    elif current > ema9 and ema9 < ema14:
        signal = "Bullish crossover 🟡 (EMA9 crossing up)"
        score  = 1
    elif current < ema9 < ema14:
        signal = "Strong Bearish 🔴 (Price < EMA9 < EMA14)"
        score  = -2
    elif current < ema9 and ema9 > ema14:
        signal = "Bearish crossover 🟠 (EMA9 crossing down)"
        score  = -1
    else:
        signal = "Neutral ⚪"
        score  = 0

    return {"ema9": ema9, "ema14": ema14, "signal": signal, "score": score}, src, prices

# =========================
# FIBONACCI LEVELS
# =========================
def _calc_fibonacci(high, low, current_price):
    diff = high - low
    if diff == 0:
        return None

    levels = {
        "0.0":   round(high, 2),
        "23.6":  round(high - 0.236 * diff, 2),
        "38.2":  round(high - 0.382 * diff, 2),
        "50.0":  round(high - 0.500 * diff, 2),
        "61.8":  round(high - 0.618 * diff, 2),
        "78.6":  round(high - 0.786 * diff, 2),
        "100.0": round(low, 2),
    }

    # Find nearest support and resistance from fib levels
    above = {k: v for k, v in levels.items() if v > current_price}
    below = {k: v for k, v in levels.items() if v < current_price}

    nearest_res = min(above.values()) if above else None
    nearest_sup = max(below.values()) if below else None

    # Find which fib zone price is in
    fib_zone = "N/A"
    fib_score = 0
    level_list = sorted(levels.values(), reverse=True)
    for i in range(len(level_list) - 1):
        if level_list[i+1] <= current_price <= level_list[i]:
            # Find key names
            for k, v in levels.items():
                if v == level_list[i]:
                    upper_key = k
                if v == level_list[i+1]:
                    lower_key = k

            if lower_key in ["61.8", "78.6"]:
                fib_zone  = f"Strong Support zone ({lower_key}%) 🟢"
                fib_score = 2
            elif lower_key in ["38.2", "50.0"]:
                fib_zone  = f"Support zone ({lower_key}%) 🟡"
                fib_score = 1
            elif upper_key in ["23.6", "38.2"]:
                fib_zone  = f"Resistance zone ({upper_key}%) 🔴"
                fib_score = -1
            else:
                fib_zone  = f"Between {lower_key}% - {upper_key}%"
                fib_score = 0
            break

    return {
        "levels":      levels,
        "nearest_res": nearest_res,
        "nearest_sup": nearest_sup,
        "zone":        fib_zone,
        "score":       fib_score,
    }

# =========================
# INDIA VIX
# =========================
def _get_vix():
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com"}
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        r  = s.get("https://www.nseindia.com/api/equity-stockIndices?index=INDIA%20VIX", headers=headers, timeout=8)
        vd = r.json()["data"][0]
        vx = round(float(vd.get("lastPrice", 0)), 2)
        ch = round(float(vd.get("pChange", 0)), 2)
        if vx < 13:   lv = "Very Low"
        elif vx < 16: lv = "Low — Stable"
        elif vx < 20: lv = "Medium — Normal"
        elif vx < 25: lv = "High — Caution"
        else:         lv = "Very High — Panic"
        return {"vix": vx, "chg": ch, "level": lv}
    except Exception as e:
        print(f"VIX error: {e}")
        return None

# =========================
# OPTION CHAIN — NSE
# =========================
def _get_oc_nse(index="NIFTY"):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=8)
        s.get("https://www.nseindia.com/option-chain", headers=headers, timeout=8)
        r = s.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={index}", headers=headers, timeout=12)
        if r.status_code != 200:
            return None
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
                "strike": strike, "c_oi": c_oi, "p_oi": p_oi,
                "c_coi": ce.get("changeinOpenInterest", 0) or 0,
                "p_coi": pe.get("changeinOpenInterest", 0) or 0,
                "c_iv": ce.get("impliedVolatility", 0) or 0,
                "p_iv": pe.get("impliedVolatility", 0) or 0,
                "c_ltp": ce.get("lastPrice", 0) or 0,
                "p_ltp": pe.get("lastPrice", 0) or 0,
            })
        if total_c == 0:
            return None
        pcr      = round(total_p / total_c, 2)
        gap      = ASSETS["nifty"]["strike_gap"] if index == "NIFTY" else 100
        atm      = round(spot / gap) * gap
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
        print(f"OC NSE error: {e}")
        return None

# =========================
# GREEKS — FYERS
# =========================
def _get_greeks(spot, asset_key, oc_data=None):
    asset      = ASSETS[asset_key]
    gap        = asset.get("strike_gap", 50)
    atm        = round(spot / gap) * gap
    expiry_str = ""
    greeks     = {}
    try:
        today      = datetime.now()
        days_ahead = (3 - today.weekday()) % 7
        if days_ahead == 0: days_ahead = 7
        expiry_str = (today + timedelta(days=days_ahead)).strftime("%d%b%y").upper()

        if asset_key in ["nifty", "sensex"]:
            prefix = "NIFTY" if asset_key == "nifty" else "SENSEX"
            exchange = "NSE" if asset_key == "nifty" else "BSE"
            symbols = [
                f"{exchange}:{prefix}{expiry_str}{atm}CE",
                f"{exchange}:{prefix}{expiry_str}{atm}PE",
            ]
            resp = fyers.quotes(data={"symbols": ",".join(symbols)})
            if "d" in resp and resp["d"]:
                for item in resp["d"]:
                    sym = item.get("n", "")
                    v   = item.get("v", {})
                    if v.get("lp", 0) > 0:
                        greeks[sym] = {
                            "ltp":   round(float(v.get("lp", 0)), 2),
                            "delta": v.get("delta", 0),
                            "theta": v.get("theta", 0),
                            "iv":    v.get("iv", 0),
                        }
    except Exception as e:
        print(f"Greeks error: {e}")

    # Fallback from OC
    if not greeks and oc_data and oc_data.get("atm_data"):
        ad = oc_data["atm_data"]
        greeks[f"CE_{atm}"] = {"ltp": ad["c_ltp"], "iv": ad["c_iv"], "delta": 0.5,  "theta": -8}
        greeks[f"PE_{atm}"] = {"ltp": ad["p_ltp"], "iv": ad["p_iv"], "delta": -0.5, "theta": -8}

    return greeks, atm, expiry_str

# =========================
# DEMAND SUPPLY ZONES
# =========================
def get_zones(market):
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

    base = round(price / 100) * 100
    for i in range(-3, 4):
        lvl = base + i * 100
        if lvl > price + 50:   supply.append(lvl)
        elif lvl < price - 50: demand.append(lvl)

    supply_sorted = sorted(set([z for z in supply if z > price]))[:4]
    demand_sorted = sorted(set([z for z in demand if z < price]), reverse=True)[:4]

    return {
        "supply_zones": supply_sorted, "demand_zones": demand_sorted,
        "imm_res": supply_sorted[0] if supply_sorted else round(price + 100, 2),
        "imm_sup": demand_sorted[0] if demand_sorted else round(price - 100, 2),
        "pivot": pivot, "r1": r1, "r2": r2, "r3": r3,
        "s1": s1, "s2": s2, "s3": s3,
    }

# =========================
# MASTER ENGINE
# =========================
def master_engine(market, oc_data, greeks, atm, expiry_str, zones, rsi, vix_data, asset_key, ema_data=None, fib_data=None):
    asset      = ASSETS[asset_key]
    price      = market["price"]
    vwap       = round((market["high"] + market["low"] + price) / 3, 2)
    change_pct = round(((price - market["prev_close"]) / market["prev_close"]) * 100, 2) if market["prev_close"] else 0
    signals    = {}
    sb = sb_bear = 0

    if price > vwap:
        signals["VWAP"] = f"Bullish — Above VWAP ({vwap})"; sb += 1
    else:
        signals["VWAP"] = f"Bearish — Below VWAP ({vwap})"; sb_bear += 1

    if rsi:
        if rsi > 70:    signals["RSI"] = f"Overbought ({rsi})"; sb_bear += 2
        elif rsi >= 60: signals["RSI"] = f"Bullish ({rsi})"; sb += 1
        elif rsi <= 30: signals["RSI"] = f"Oversold ({rsi})"; sb += 2
        elif rsi <= 40: signals["RSI"] = f"Bearish ({rsi})"; sb_bear += 1
        else:           signals["RSI"] = f"Neutral ({rsi})"

    # EMA Signal
    if ema_data:
        ema_score = ema_data.get("score", 0)
        signals["EMA"] = f"EMA9:{ema_data['ema9']} EMA14:{ema_data['ema14']} — {ema_data['signal']}"
        if ema_score >= 2:   sb += 2
        elif ema_score == 1: sb += 1
        elif ema_score <= -2: sb_bear += 2
        elif ema_score == -1: sb_bear += 1
    else:
        signals["EMA"] = "N/A"

    # Fibonacci Signal
    if fib_data:
        fib_score = fib_data.get("score", 0)
        signals["Fib"] = f"{fib_data['zone']} | Sup:{fib_data['nearest_sup']} Res:{fib_data['nearest_res']}"
        if fib_score >= 2:   sb += 2
        elif fib_score == 1: sb += 1
        elif fib_score <= -2: sb_bear += 2
        elif fib_score == -1: sb_bear += 1
    else:
        signals["Fib"] = "N/A"

    if oc_data:
        pcr = oc_data["pcr"]
        if pcr > 1.3:   signals["PCR"] = f"Bullish ({pcr})"; sb += 1
        elif pcr < 0.7: signals["PCR"] = f"Bearish ({pcr})"; sb_bear += 1
        else:           signals["PCR"] = f"Neutral ({pcr})"
        if oc_data.get("atm_data"):
            ad = oc_data["atm_data"]
            if ad["c_coi"] < 0 and ad["p_coi"] > 0:
                signals["OI"] = "Bullish — CE unwinding"; sb += 1
            elif ad["c_coi"] > 0 and ad["p_coi"] < 0:
                signals["OI"] = "Bearish — PE unwinding"; sb_bear += 1

    dist_res = abs(price - zones["imm_res"])
    dist_sup = abs(price - zones["imm_sup"])
    if dist_sup < zones["imm_res"] * 0.003:
        signals["Zone"] = f"Near Support ({zones['imm_sup']})"; sb += 2
    elif dist_res < zones["imm_res"] * 0.003:
        signals["Zone"] = f"Near Resistance ({zones['imm_res']})"; sb_bear += 2
    elif price > zones["pivot"]:
        signals["Zone"] = f"Above Pivot ({zones['pivot']})"; sb += 1
    else:
        signals["Zone"] = f"Below Pivot ({zones['pivot']})"; sb_bear += 1

    if vix_data:
        vix = vix_data["vix"]
        if vix > 22:   signals["VIX"] = f"High ({vix}) — Use spreads"
        elif vix < 16: signals["VIX"] = f"Low ({vix}) — Good for buying"; sb += 1
        else:          signals["VIX"] = f"Normal ({vix})"

    ce_key = f"NSE:NIFTY{expiry_str}{atm}CE" if asset_key == "nifty" else f"CE_{atm}"
    pe_key = f"NSE:NIFTY{expiry_str}{atm}PE" if asset_key == "nifty" else f"PE_{atm}"
    atm_ce = greeks.get(ce_key) or greeks.get(f"CE_{atm}", {})
    atm_pe = greeks.get(pe_key) or greeks.get(f"PE_{atm}", {})

    if atm_ce and atm_pe:
        ced = abs(atm_ce.get("delta", 0.5))
        ped = abs(atm_pe.get("delta", 0.5))
        if ced > ped:   signals["Greeks"] = f"Bullish Δ ({ced:.2f})"; sb += 1
        elif ped > ced: signals["Greeks"] = f"Bearish Δ ({ped:.2f})"; sb_bear += 1

    if sb >= 5:
        action = "CALL BUY"
        entry  = atm_ce.get("ltp", 0) if atm_ce else 0
        sl_o   = round(entry * 0.75, 2)
        t1_o   = round(entry * 1.35, 2)
        t2_o   = round(entry * 1.70, 2)
        conf   = min(int((sb / max(sb + sb_bear, 1)) * 100), 95)
        option = f"{asset['name'].split()[0] if asset_key not in ['nifty','sensex'] else ('NIFTY' if asset_key=='nifty' else 'SENSEX')} {atm} CE"
    elif sb_bear >= 5:
        action = "PUT BUY"
        entry  = atm_pe.get("ltp", 0) if atm_pe else 0
        sl_o   = round(entry * 0.75, 2)
        t1_o   = round(entry * 1.35, 2)
        t2_o   = round(entry * 1.70, 2)
        conf   = min(int((sb_bear / max(sb + sb_bear, 1)) * 100), 95)
        option = f"{asset['name'].split()[0] if asset_key not in ['nifty','sensex'] else ('NIFTY' if asset_key=='nifty' else 'SENSEX')} {atm} PE"
    else:
        action = "NO TRADE"
        option = entry = sl_o = t1_o = t2_o = None
        conf   = 0

    rr = "N/A"
    if entry and sl_o and entry != sl_o and t1_o:
        risk   = abs(entry - sl_o)
        reward = abs(t1_o - entry)
        rr     = f"1:{round(reward/risk,1)}" if risk > 0 else "N/A"

    return {
        "action": action, "option": option, "confidence": conf,
        "entry": round(entry, 2) if entry else None,
        "sl_opt": sl_o, "t1_opt": t1_o, "t2_opt": t2_o,
        "sl_spot": round(zones["imm_sup"] - (price * 0.002), 2),
        "t1_spot": round(zones["imm_res"], 2),
        "t2_spot": round(zones["r2"], 2),
        "rr": rr, "sb": sb, "sb_bear": sb_bear,
        "signals": signals, "vwap": vwap, "change": change_pct,
        "atm_ce": atm_ce, "atm_pe": atm_pe, "atm": atm,
        "ema_data": ema_data, "fib_data": fib_data,
    }

# =========================
# FORMAT OUTPUT
# =========================
def format_output(res, market, oc_data, zones, rsi, rsi_sig, vix_data, asset_key):
    asset  = ASSETS[asset_key]
    price  = market["price"]
    chg    = res["change"]
    arrow  = "📈" if chg >= 0 else "📉"
    ae     = "🟢" if "CALL" in res["action"] else ("🔴" if "PUT" in res["action"] else "⚪")
    trend  = "BULLISH" if res["sb"] > res["sb_bear"] else ("BEARISH" if res["sb_bear"] > res["sb"] else "NEUTRAL")
    t_e    = "📈" if trend == "BULLISH" else ("📉" if trend == "BEARISH" else "➡️")
    unit   = asset["unit"]
    atm    = res["atm"]

    vix_str = f"{vix_data['vix']} ({vix_data['level']})" if vix_data else "N/A"
    rsi_str = f"{rsi} — {rsi_sig}" if rsi else "N/A"
    pcr_str = str(oc_data["pcr"]) if oc_data and oc_data.get("pcr") else "N/A"

    sup_z = f"{unit}{zones['demand_zones'][0]} — {unit}{zones['demand_zones'][1]}" if len(zones['demand_zones']) >= 2 else f"{unit}{zones['imm_sup']}"
    res_z = f"{unit}{zones['supply_zones'][0]} — {unit}{zones['supply_zones'][1]}" if len(zones['supply_zones']) >= 2 else f"{unit}{zones['imm_res']}"

    ce = res["atm_ce"]
    pe = res["atm_pe"]

    if "CALL" in res["action"]:
        call_entry = f"Demand zone\n{unit}{zones['demand_zones'][0]} — {unit}{zones['demand_zones'][1]}" if len(zones['demand_zones'])>=2 else f"{unit}{zones['imm_sup']}"
        call_t     = f"{unit}{zones['imm_res']}"
        call_sl    = f"{unit}{res['sl_spot']} todi ki exit"
        put_entry  = "Supply zone\nN/A"
        put_t      = f"{unit}{atm} strike"
        put_sl     = f"{unit}{zones['imm_res']} todi ki exit"
    elif "PUT" in res["action"]:
        put_entry  = f"Supply zone\n{unit}{zones['supply_zones'][0]} — {unit}{zones['supply_zones'][1]}" if len(zones['supply_zones'])>=2 else f"{unit}{zones['imm_res']}"
        put_t      = f"{unit}{zones['imm_sup']}"
        put_sl     = f"{unit}{zones['imm_res']} todi ki exit"
        call_entry = "Demand zone\nN/A"
        call_t     = f"{unit}{atm} strike"
        call_sl    = f"{unit}{zones['imm_sup']} todi ki exit"
    else:
        call_entry = f"{unit}{zones['imm_sup']}"
        call_t     = f"{unit}{zones['imm_res']}"
        call_sl    = f"{unit}{zones['s1']}"
        put_entry  = f"{unit}{zones['imm_res']}"
        put_t      = f"{unit}{zones['imm_sup']}"
        put_sl     = f"{unit}{zones['r1']}"

    rr_str = res["rr"] if res["rr"] != "N/A" else "1:2"

    prefix = "NIFTY" if asset_key == "nifty" else ("SENSEX" if asset_key == "sensex" else asset["name"].split()[0].upper())

    # EMA display
    ema_data = res.get("ema_data")
    fib_data = res.get("fib_data")

    ema_str = "N/A"
    if ema_data:
        ema_str = f"EMA9:`{ema_data['ema9']}` EMA14:`{ema_data['ema14']}` — {ema_data['signal']}"

    fib_str = "N/A"
    fib_levels_str = ""
    if fib_data:
        fib_str = fib_data['zone']
        lvls = fib_data['levels']
        fib_levels_str = f"\n📐 **Fibonacci:** 23.6%:`{lvls['23.6']}` 38.2%:`{lvls['38.2']}` 50%:`{lvls['50.0']}` 61.8%:`{lvls['61.8']}`"

    msg = f"""📊 **{asset['name']} — Options Analysis**
**{asset['name']}:** {unit}{price} {arrow} {chg:+.2f}%
**Signal:** {t_e} {trend} — {res['action']}
**ATM Strike:** {atm}

🟢 **Demand Zone** — 🔴 **Supply Zone** — ✅ **R:R**
{sup_z} — {res_z} — {rr_str}

⭐ **India VIX:** {vix_str}
📉 **RSI(14):** {rsi_str}
📊 **EMA:** {ema_str}
🔢 **Fib Zone:** {fib_str}{fib_levels_str}
**PCR:** {pcr_str} | **Pivot:** {zones['pivot']} | **Bull/Bear:** {res['sb']}/{res['sb_bear']}

━━━━━━━━━━━━━━━━
📗 **CALL BUY ({atm} CE)**
```
Entry: {call_entry}
Target: {call_t}
SL: {call_sl}
```
📕 **PUT BUY ({atm} PE)**
```
Entry: {put_entry}
Target: {put_t}
SL: {put_sl}
```
⚡ **Index Entry/Target/SL**
Entry: {unit}{zones['imm_sup']} | T1: {unit}{zones['imm_res']} | T2: {unit}{zones['r2']} | SL: {unit}{zones['s1']}

{ae} **Final Signal: {res['action']}** | Confidence: {res['confidence']}%
*He financial advice nahi. •* {datetime.now().strftime("%d %b %H:%M")}"""

    return msg

# =========================
# EQUITY FORMAT
# =========================
def format_equity(stocks):
    if not stocks:
        return "❌ Top stocks data unavailable."

    msg = f"📊 **Top Volume Stocks** | {datetime.now().strftime('%d %b %H:%M')}\n\n"
    for s in stocks[:5]:
        chg   = s.get("change", 0)
        arrow = "📈" if chg >= 0 else "📉"
        price = s["price"]
        high  = s["high"]
        low   = s["low"]
        prev  = s["prev_close"]

        pivot  = round((high + low + prev) / 3, 2) if high and low and prev else 0
        r1     = round((2 * pivot) - low, 2) if pivot else 0
        s1     = round((2 * pivot) - high, 2) if pivot else 0

        trend  = "BULLISH" if price > pivot else "BEARISH"
        t_e    = "📈" if trend == "BULLISH" else "📉"

        msg += f"""**{s['symbol']}** {arrow} ₹{price} ({chg:+.2f}%)
{t_e} {trend} | H:₹{high} L:₹{low}
Entry: ₹{s1 if trend=='BULLISH' else r1} | T1: ₹{r1 if trend=='BULLISH' else s1} | SL: ₹{low if trend=='BULLISH' else high}
━━━━━━━━━━━━━━━━
"""
    msg += "*He financial advice nahi.*"
    return msg

# =========================
# AI ANALYSIS
# =========================
def _get_ai(market, oc_data, zones, rsi, vix_data, res, asset_key):
    asset    = ASSETS[asset_key]
    oc_str   = f"PCR:{oc_data['pcr']}, Call Wall:{oc_data['max_c_strike']}, Put Wall:{oc_data['max_p_strike']}" if oc_data else "N/A"
    ce       = res.get("atm_ce", {}) or {}
    pe       = res.get("atm_pe", {}) or {}
    ce_ltp   = ce.get("ltp", 0)
    pe_ltp   = pe.get("ltp", 0)
    if (not ce_ltp or not pe_ltp) and oc_data and oc_data.get("atm_data"):
        ad     = oc_data["atm_data"]
        ce_ltp = ce_ltp or ad.get("c_ltp", 0)
        pe_ltp = pe_ltp or ad.get("p_ltp", 0)
    ltp_info = f"CE LTP=₹{ce_ltp}, PE LTP=₹{pe_ltp}" if (ce_ltp or pe_ltp) else "LTP unavailable — use CMP"

    ema_data = res.get("ema_data")
    fib_data = res.get("fib_data")
    ema_str  = f"EMA9={ema_data['ema9']}, EMA14={ema_data['ema14']}, Signal={ema_data['signal']}" if ema_data else "N/A"
    fib_str  = f"Zone={fib_data['zone']}, NearSup={fib_data['nearest_sup']}, NearRes={fib_data['nearest_res']}" if fib_data else "N/A"

    prompt = f"""Expert {asset['name']} intraday options trader. Give precise trade call.

Asset: {asset['name']} | Price={market['price']} Change={res['change']}% VWAP={res['vwap']}
O={market['open']} H={market['high']} L={market['low']}
RSI={rsi or 'N/A'} | VIX={vix_data['vix'] if vix_data else 'N/A'}
EMA: {ema_str}
Fibonacci: {fib_str}
Bull:{res['sb']}/9 Bear:{res['sb_bear']}/9
ATM={res['atm']} | {ltp_info}
Resistance={zones['imm_res']} Support={zones['imm_sup']}
Pivot={zones['pivot']} R1={zones['r1']} S1={zones['s1']}
OI: {oc_str}
System: {res['action']} ({res['confidence']}%)

Consider EMA crossover and Fibonacci zones in analysis.
IMPORTANT: Use actual LTP for entry price. If unavailable write "Use CMP".
Min 65% confidence. NO TRADE if unclear.

**Signal:** BULLISH/BEARISH — CALL BUY/PUT BUY/NO TRADE
**Option:** {asset['name']} XXXXX CE/PE
**Entry:** ₹XX | **SL:** ₹XX | **T1:** ₹XX | **T2:** ₹XX
**Confidence:** XX%
**Reason:** (1 line)
**Risk:** (1 line)"""

    try:
        r = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return r.content[0].text
    except Exception as e:
        return f"AI Error: {str(e)}"

# =========================
# MAIN TRADE HANDLER
# =========================
async def handle_trade(message, asset_key):
    asset = ASSETS[asset_key]

    if asset_key == "equity":
        await message.channel.send("⏳ Fetching top volume stocks...")
        stocks = await run_in_thread(_get_top_stocks, timeout=15)
        await message.channel.send(format_equity(stocks))
        return

    await message.channel.send(f"⏳ Fetching {asset['name']} data...")

    # Fetch all data in parallel
    oc_index = "NIFTY" if asset_key == "nifty" else ("BANKNIFTY" if asset_key == "banknifty" else None)

    market, vix_data, rsi_result, ema_result = await asyncio.gather(
        get_market_data(asset_key),
        run_in_thread(_get_vix, timeout=10),
        run_in_thread(_get_rsi, asset.get("yahoo_symbol", "^NSEI"), asset.get("fyers_symbol", ""), timeout=15),
        run_in_thread(_get_ema_data, asset.get("yahoo_symbol", "^NSEI"), asset.get("fyers_symbol", ""), timeout=15),
    )

    if not market:
        await message.channel.send(f"❌ {asset['name']} data unavailable. Markets may be closed.")
        return

    oc_data = None
    if asset_key in ["nifty", "sensex"] and oc_index:
        oc_data = await run_in_thread(_get_oc_nse, oc_index, timeout=15)

    rsi, rsi_sig, _   = rsi_result if rsi_result else (None, None, None)
    ema_data, _, prices = ema_result if ema_result else (None, None, None)

    # Fibonacci from day high/low
    fib_data = None
    if market.get("high") and market.get("low"):
        fib_data = _calc_fibonacci(market["high"], market["low"], market["price"])

    src_info = f"✅ **{asset['name']}** | Source: {market.get('all_sources', market['source'])}"
    if not oc_data and asset_key in ["nifty", "sensex"]:
        src_info += " | ⚠️ OI unavailable"
    await message.channel.send(src_info)

    greeks_result = await run_in_thread(_get_greeks, market["price"], asset_key, oc_data, timeout=10)
    greeks, atm, expiry_str = greeks_result if greeks_result else ({}, round(market["price"] / asset.get("strike_gap", 50)) * asset.get("strike_gap", 50), "")

    zones = get_zones(market)
    res   = master_engine(market, oc_data, greeks, atm, expiry_str, zones, rsi, vix_data, asset_key, ema_data, fib_data)
    out   = format_output(res, market, oc_data, zones, rsi, rsi_sig, vix_data, asset_key)

    await message.channel.send(out)

    await message.channel.send("🤖 AI analysis...")
    ai = await run_in_thread(_get_ai, market, oc_data, zones, rsi, vix_data, res, asset_key, timeout=30)
    if ai:
        await message.channel.send(f"🤖 **AI Analysis** | {datetime.now().strftime('%H:%M')}\n\n{ai}\n\n*He financial advice nahi.*")

# =========================
# AUTO SIGNAL CONFIG
# =========================
SIGNAL_CHANNEL_ID = 1484099393714917387
SIGNAL_THRESHOLD  = 7
last_auto_action  = None
dm_usage          = {}  # {user_id_date: count} — daily DM limit tracker

# =========================
# AUTO SIGNAL LOOP (har 5 min)
# =========================
async def run_auto_signal():
    global last_auto_action
    await client.wait_until_ready()
    signal_ch = client.get_channel(SIGNAL_CHANNEL_ID)
    if not signal_ch:
        print("Signal channel not found!")
        return

    while not client.is_closed():
        try:
            # Market hours check (IST 9:15 to 15:30 = UTC 3:45 to 10:00)
            now     = datetime.utcnow()
            in_mkt  = (
                now.weekday() < 5 and
                ((now.hour == 3 and now.minute >= 45) or
                 (4 <= now.hour <= 9) or
                 (now.hour == 10 and now.minute == 0))
            )

            if in_mkt:
                asset = ASSETS["nifty"]
                market, vix_data, rsi_result, ema_result = await asyncio.gather(
                    get_market_data("nifty"),
                    run_in_thread(_get_vix, timeout=10),
                    run_in_thread(_get_rsi, asset.get("yahoo_symbol","^NSEI"), asset.get("fyers_symbol",""), timeout=15),
                    run_in_thread(_get_ema_data, asset.get("yahoo_symbol","^NSEI"), asset.get("fyers_symbol",""), timeout=15),
                )

                if market:
                    rsi, rsi_sig, _     = rsi_result if rsi_result else (None, None, None)
                    ema_data, _, _      = ema_result if ema_result else (None, None, None)
                    fib_data            = _calc_fibonacci(market["high"], market["low"], market["price"]) if market.get("high") and market.get("low") else None
                    greeks_r            = await run_in_thread(_get_greeks, market["price"], "nifty", None, timeout=10)
                    greeks, atm, expst  = greeks_r if greeks_r else ({}, round(market["price"]/50)*50, "")
                    zones               = get_zones(market)
                    res                 = master_engine(market, None, greeks, atm, expst, zones, rsi, vix_data, "nifty", ema_data, fib_data)

                    action = res["action"]
                    score  = max(res["sb"], res["sb_bear"])

                    if score >= SIGNAL_THRESHOLD and action != "NO TRADE" and action != last_auto_action:
                        last_auto_action = action
                        ae      = "🟢" if "CALL" in action else "🔴"
                        ema_str = f"EMA9:`{ema_data['ema9']}` EMA14:`{ema_data['ema14']}` — {ema_data['signal']}" if ema_data else "N/A"
                        fib_str = fib_data['zone'] if fib_data else "N/A"

                        auto_msg = f"""🚨 **NIFTY AUTO SIGNAL** | {datetime.now().strftime('%d %b %H:%M')}
━━━━━━━━━━━━━━━━━━━━
{ae} **{action}** | Confidence: `{res['confidence']}%` | Score: `{score}/9`

💹 Price: `₹{market['price']}` | VWAP: `{res['vwap']}`
📉 RSI: `{rsi}` | 📊 {ema_str}
🔢 Fib: {fib_str}
🏗️ Support: `{zones['imm_sup']}` | Resistance: `{zones['imm_res']}`
⭐ VIX: `{vix_data['vix'] if vix_data else 'N/A'}`

**Option:** `NIFTY {atm} {'CE' if 'CALL' in action else 'PE'}`
**Entry:** Demand/Supply zone | **SL:** `{zones['s1'] if 'CALL' in action else zones['r1']}`
**T1:** `{zones['imm_res'] if 'CALL' in action else zones['imm_sup']}` | **T2:** `{zones['r2'] if 'CALL' in action else zones['s2']}`

_Type `trade!` here for full personal analysis in DM_
⚠️ *He financial advice nahi.*"""

                        await signal_ch.send(auto_msg)
                        print(f"Auto signal sent: {action} at {datetime.now()}")

        except Exception as e:
            print(f"Auto signal error: {e}")

        await asyncio.sleep(SIGNAL_INTERVAL * 60)

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    print(f"Bot ready: {client.user}")
    asyncio.ensure_future(run_auto_signal())

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
        await message.channel.send("⏳ Updating token...")
        ok, msg = await run_in_thread(_update_token, tok, timeout=10)
        await message.channel.send(msg)
        return

    cmd = message.content.lower().strip()

    # PREMIUM SIGNAL CHANNEL — trade! likhne pe DM mein bhejo (5/day per user)
    if message.channel.id == SIGNAL_CHANNEL_ID:
        if cmd == "trade!":
            try:
                await message.delete()
            except:
                pass

            user_id   = message.author.id
            today_str = datetime.now().strftime("%Y-%m-%d")
            key       = f"{user_id}_{today_str}"

            # Check daily limit
            user_count = dm_usage.get(key, 0)
            if user_count >= 5:
                try:
                    await message.author.send(
                        f"⚠️ **Daily limit khatam!**\n"
                        f"Aaj ke 5 free analyses use ho gaye.\n"
                        f"Kal subah reset hoga! 🌅"
                    )
                except:
                    pass
                return

            # Update count
            dm_usage[key] = user_count + 1
            remaining     = 5 - dm_usage[key]

            try:
                await message.author.send(
                    f"⏳ Tumhara personal Nifty analysis aa raha hai...\n"
                    f"_(Aaj ke {remaining} analyses baaki hain)_"
                )
                class DMMessage:
                    def __init__(self, author, channel):
                        self.author  = author
                        self.channel = channel
                        self.content = "trade!nifty"
                dm_ch  = message.author.dm_channel or await message.author.create_dm()
                dm_msg = DMMessage(message.author, dm_ch)
                await handle_trade(dm_msg, "nifty")
            except discord.Forbidden:
                await message.channel.send(
                    f"{message.author.mention} DM enable karo pehle!",
                    delete_after=10
                )
            except Exception as e:
                print(f"DM error: {e}")
        return
    if cmd == "trade!nifty":
        await handle_trade(message, "nifty")
    elif cmd == "trade!sensex":
        await handle_trade(message, "sensex")
    elif cmd == "trade!gold":
        await handle_trade(message, "gold")
    elif cmd == "trade!oil":
        await handle_trade(message, "oil")
    elif cmd == "trade!equity":
        await handle_trade(message, "equity")

    elif cmd == "oi!":
        oc = await run_in_thread(_get_oc_nse, "NIFTY", timeout=15)
        if not oc:
            await message.channel.send("❌ OI unavailable"); return
        rows = "```\nStrike  | CE OI     | PE OI     |C.IV|P.IV\n"
        for s in oc["near"][:8]:
            mk = "◄" if s["strike"] == oc["atm"] else " "
            rows += f"{s['strike']}{mk}| {s['c_oi']:>9,} | {s['p_oi']:>9,} |{s['c_iv']:>4}%|{s['p_iv']:>4}%\n"
        rows += "```"
        await message.channel.send(f"📈 **OI** | {oc['expiry']} | Spot:`{oc['spot']}` ATM:`{oc['atm']}` PCR:`{oc['pcr']}`\nCall Wall:`{oc['max_c_strike']}` | Put Wall:`{oc['max_p_strike']}`\n{rows}")

    elif cmd == "vix!":
        v = await run_in_thread(_get_vix, timeout=10)
        if not v: await message.channel.send("❌ VIX unavailable"); return
        await message.channel.send(f"⚡ **India VIX:** `{v['vix']}` ({v['chg']:+.2f}%)\n{v['level']}")

    elif cmd == "help!":
        await message.channel.send("""📋 **COMMANDS**

`trade!nifty`  — Nifty50 Options Analysis
`trade!sensex` — Sensex Options Analysis
`trade!gold`   — MCX Gold Analysis
`trade!oil`    — MCX Crude Oil Analysis
`trade!equity` — Top Volume Stocks

`oi!`   — Nifty Option Chain
`vix!`  — India VIX
`help!` — This menu

🔐 Token channel mein naya token paste karo directly""")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Bot starting...")
    client.run(DISCORD_TOKEN)
