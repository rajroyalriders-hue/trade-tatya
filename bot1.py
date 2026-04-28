import discord
import time
import requests
import json
from datetime import datetime
import anthropic
from fyers_apiv3 import fyersModel
import os

# =========================
# CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CHANNEL_ID = 1498261283584217219

FYERS_APP_ID = os.getenv("FYERS_APP_ID")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN")

COOLDOWN_SECONDS = 60
last_run_time = 0

# =========================
# CLIENTS
# =========================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

fyers = fyersModel.FyersModel(
    client_id=FYERS_APP_ID,
    token=FYERS_ACCESS_TOKEN
)

# =========================
# FETCH NIFTY SPOT DATA (FYERS)
# =========================
def get_market_data():
    try:
        data = {"symbols": "NSE:NIFTY50-INDEX"}
        response = fyers.quotes(data=data)
        print("FYERS RESPONSE:", response)

        if "d" not in response or not response["d"]:
            return None

        v = response["d"][0].get("v")
        if not v or "lp" not in v:
            return None

        return {
            "price": v.get("lp"),
            "high": v.get("high_price"),
            "low": v.get("low_price"),
            "open": v.get("open_price"),
            "prev_close": v.get("prev_close_price") or v.get("prev_close")
        }
    except Exception as e:
        print(f"Market data error: {e}")
        return None

# =========================
# FETCH OPTION CHAIN (NSE)
# =========================
def get_option_chain():
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.nseindia.com/option-chain",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        response = session.get(
            "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
            headers=headers,
            timeout=10
        )
        data = response.json()

        records = data.get("records", {})
        filtered = data.get("filtered", {})
        spot_price = records.get("underlyingValue", 0)
        expiry_dates = records.get("expiryDates", [])
        nearest_expiry = expiry_dates[0] if expiry_dates else None

        oi_data = []
        total_call_oi = 0
        total_put_oi = 0
        max_call_oi = 0
        max_put_oi = 0
        max_call_strike = 0
        max_put_strike = 0

        for item in records.get("data", []):
            if item.get("expiryDate") != nearest_expiry:
                continue

            strike = item.get("strikePrice", 0)
            ce = item.get("CE", {})
            pe = item.get("PE", {})

            ce_oi = ce.get("openInterest", 0) or 0
            pe_oi = pe.get("openInterest", 0) or 0
            ce_coi = ce.get("changeinOpenInterest", 0) or 0
            pe_coi = pe.get("changeinOpenInterest", 0) or 0
            ce_iv = ce.get("impliedVolatility", 0) or 0
            pe_iv = pe.get("impliedVolatility", 0) or 0
            ce_ltp = ce.get("lastPrice", 0) or 0
            pe_ltp = pe.get("lastPrice", 0) or 0

            total_call_oi += ce_oi
            total_put_oi += pe_oi

            if ce_oi > max_call_oi:
                max_call_oi = ce_oi
                max_call_strike = strike

            if pe_oi > max_put_oi:
                max_put_oi = pe_oi
                max_put_strike = strike

            oi_data.append({
                "strike": strike,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_coi": ce_coi,
                "pe_coi": pe_coi,
                "ce_iv": ce_iv,
                "pe_iv": pe_iv,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
            })

        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0

        # ATM strike
        atm_strike = round(spot_price / 50) * 50
        atm_data = next((x for x in oi_data if x["strike"] == atm_strike), None)

        # Top 5 strikes near ATM
        near_strikes = sorted(oi_data, key=lambda x: abs(x["strike"] - spot_price))[:10]

        return {
            "spot": spot_price,
            "expiry": nearest_expiry,
            "pcr": pcr,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "max_call_strike": max_call_strike,
            "max_call_oi": max_call_oi,
            "max_put_strike": max_put_strike,
            "max_put_oi": max_put_oi,
            "atm_strike": atm_strike,
            "atm_data": atm_data,
            "near_strikes": near_strikes,
        }

    except Exception as e:
        print(f"Option chain error: {e}")
        return None

# =========================
# FETCH GREEKS (FYERS)
# =========================
def get_greeks(spot_price):
    try:
        atm = round(spot_price / 50) * 50
        otm_call = atm + 50
        itm_call = atm - 50

        # Get expiry
        today = datetime.now()
        # Try current week Thursday
        days_ahead = 3 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        expiry = today.replace(
            day=today.day + days_ahead,
            hour=0, minute=0, second=0, microsecond=0
        )
        expiry_str = expiry.strftime("%d%b%y").upper()

        symbols = [
            f"NSE:NIFTY{expiry_str}{atm}CE",
            f"NSE:NIFTY{expiry_str}{atm}PE",
            f"NSE:NIFTY{expiry_str}{otm_call}CE",
            f"NSE:NIFTY{expiry_str}{itm_call}CE",
        ]

        data = {"symbols": ",".join(symbols)}
        response = fyers.quotes(data=data)

        greeks_data = {}
        if "d" in response and response["d"]:
            for item in response["d"]:
                sym = item.get("n", "")
                v = item.get("v", {})
                greeks_data[sym] = {
                    "ltp": v.get("lp", 0),
                    "delta": v.get("delta", 0),
                    "gamma": v.get("gamma", 0),
                    "theta": v.get("theta", 0),
                    "vega": v.get("vega", 0),
                    "iv": v.get("iv", 0),
                    "oi": v.get("oi", 0),
                }

        return greeks_data, atm, expiry_str

    except Exception as e:
        print(f"Greeks error: {e}")
        return {}, 0, ""

# =========================
# SUPPLY & DEMAND ZONES
# =========================
def get_supply_demand_zones(oc_data, spot):
    if not oc_data:
        return None

    # Major resistance = max call OI strike (supply)
    # Major support = max put OI strike (demand)
    supply_zone = oc_data["max_call_strike"]
    demand_zone = oc_data["max_put_strike"]

    # Secondary zones from near strikes
    high_ce_oi = sorted(oc_data["near_strikes"], key=lambda x: x["ce_oi"], reverse=True)[:3]
    high_pe_oi = sorted(oc_data["near_strikes"], key=lambda x: x["pe_oi"], reverse=True)[:3]

    resistance_levels = [x["strike"] for x in high_ce_oi]
    support_levels = [x["strike"] for x in high_pe_oi]

    # Immediate S/R
    above = [x["strike"] for x in oc_data["near_strikes"] if x["strike"] > spot]
    below = [x["strike"] for x in oc_data["near_strikes"] if x["strike"] < spot]

    immediate_resistance = min(above) if above else supply_zone
    immediate_support = max(below) if below else demand_zone

    return {
        "major_supply": supply_zone,
        "major_demand": demand_zone,
        "resistance_levels": resistance_levels,
        "support_levels": support_levels,
        "immediate_resistance": immediate_resistance,
        "immediate_support": immediate_support,
    }

# =========================
# TECHNICAL LEVELS
# =========================
def calculate_levels(m):
    high, low = m["high"], m["low"]
    close = m.get("prev_close") or m["open"]
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    r2 = pivot + (high - low)
    s1 = (2 * pivot) - high
    s2 = pivot - (high - low)
    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
    }

def calc_vwap(m):
    return round((m["high"] + m["low"] + m["price"]) / 3, 2)

# =========================
# MASTER TRADE ENGINE
# =========================
def master_trade_engine(market, oc_data, greeks_data, atm, expiry_str):
    price = market["price"]
    levels = calculate_levels(market)
    vwap = calc_vwap(market)
    zones = get_supply_demand_zones(oc_data, price) if oc_data else None

    # ATM greeks
    atm_ce_key = f"NSE:NIFTY{expiry_str}{atm}CE"
    atm_pe_key = f"NSE:NIFTY{expiry_str}{atm}PE"
    atm_ce = greeks_data.get(atm_ce_key, {})
    atm_pe = greeks_data.get(atm_pe_key, {})

    # Signals
    signals = []

    # 1. Trend signal
    bullish = price > vwap
    bearish = price < vwap
    trend = "BULLISH" if bullish else "BEARISH"

    # 2. OI signal
    oi_signal = "NEUTRAL"
    if oc_data:
        pcr = oc_data["pcr"]
        if pcr > 1.3:
            oi_signal = "BULLISH"  # More puts = support
        elif pcr < 0.7:
            oi_signal = "BEARISH"  # More calls = resistance
        else:
            oi_signal = "NEUTRAL"

    # 3. Greeks signal
    greek_signal = "NEUTRAL"
    if atm_ce and atm_pe:
        ce_delta = abs(atm_ce.get("delta", 0))
        pe_delta = abs(atm_pe.get("delta", 0))
        if ce_delta > pe_delta:
            greek_signal = "BULLISH"
        elif pe_delta > ce_delta:
            greek_signal = "BEARISH"

    # 4. Zone signal
    zone_signal = "NEUTRAL"
    if zones:
        if price <= zones["immediate_support"] + 30:
            zone_signal = "BULLISH"
        elif price >= zones["immediate_resistance"] - 30:
            zone_signal = "BEARISH"

    # Count bullish/bearish signals
    bull_count = sum([
        trend == "BULLISH",
        oi_signal == "BULLISH",
        greek_signal == "BULLISH",
        zone_signal == "BULLISH"
    ])
    bear_count = sum([
        trend == "BEARISH",
        oi_signal == "BEARISH",
        greek_signal == "BEARISH",
        zone_signal == "BEARISH"
    ])

    # Final signal
    if bull_count >= 3:
        final_signal = "CALL BUY"
        strike = atm
        option_sym = f"NIFTY {strike} CE"
        entry = atm_ce.get("ltp", price) if atm_ce else price
        sl = zones["immediate_support"] - 20 if zones else levels["s1"]
        t1 = zones["immediate_resistance"] if zones else levels["r1"]
        t2 = levels["r1"] if zones else levels["r2"]
        confidence = int((bull_count / 4) * 100)

    elif bear_count >= 3:
        final_signal = "PUT BUY"
        strike = atm
        option_sym = f"NIFTY {strike} PE"
        entry = atm_pe.get("ltp", price) if atm_pe else price
        sl = zones["immediate_resistance"] + 20 if zones else levels["r1"]
        t1 = zones["immediate_support"] if zones else levels["s1"]
        t2 = levels["s1"] if zones else levels["s2"]
        confidence = int((bear_count / 4) * 100)

    else:
        final_signal = "NO TRADE"
        option_sym = None
        entry = sl = t1 = t2 = None
        confidence = 0

    return {
        "signal": final_signal,
        "option": option_sym,
        "entry": round(entry, 2) if entry else None,
        "sl": round(sl, 2) if sl else None,
        "t1": round(t1, 2) if t1 else None,
        "t2": round(t2, 2) if t2 else None,
        "confidence": confidence,
        "trend": trend,
        "oi_signal": oi_signal,
        "greek_signal": greek_signal,
        "zone_signal": zone_signal,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "levels": levels,
        "vwap": vwap,
        "atm_ce": atm_ce,
        "atm_pe": atm_pe,
        "zones": zones,
        "oc_data": oc_data,
    }

# =========================
# FORMAT OUTPUT
# =========================
def format_output(res, market):
    oc = res["oc_data"]
    atm_ce = res["atm_ce"]
    atm_pe = res["atm_pe"]
    zones = res["zones"]
    levels = res["levels"]

    signal_emoji = "🟢" if res["signal"] == "CALL BUY" else ("🔴" if res["signal"] == "PUT BUY" else "⚪")

    msg = f"""
━━━━━━━━━━━━━━━━━━━━━━━━
📊 **PRO TRADE SIGNAL** | {datetime.now().strftime("%H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━━━

💹 **MARKET**
Price: `{market['price']}` | VWAP: `{res['vwap']}`
Open: `{market['open']}` | High: `{market['high']}` | Low: `{market['low']}`

📐 **PIVOT LEVELS**
Pivot: `{levels['pivot']}` | R1: `{levels['r1']}` | R2: `{levels['r2']}`
S1: `{levels['s1']}` | S2: `{levels['s2']}`
"""

    if zones:
        msg += f"""
🏗️ **SUPPLY & DEMAND ZONES**
Major Supply (Resistance): `{zones['major_supply']}`
Major Demand (Support): `{zones['major_demand']}`
Immediate Resistance: `{zones['immediate_resistance']}`
Immediate Support: `{zones['immediate_support']}`
"""

    if oc:
        msg += f"""
📈 **OI ANALYSIS** (Expiry: {oc['expiry']})
PCR: `{oc['pcr']}` | ATM Strike: `{oc['atm_strike']}`
Max Call OI: `{oc['max_call_strike']}` ({oc['max_call_oi']:,}) ← **Resistance**
Max Put OI: `{oc['max_put_strike']}` ({oc['max_put_oi']:,}) ← **Support**
Total Call OI: `{oc['total_call_oi']:,}` | Total Put OI: `{oc['total_put_oi']:,}`
"""

    if atm_ce or atm_pe:
        msg += f"""
🔢 **ATM GREEKS**
CE → Delta: `{atm_ce.get('delta','N/A')}` | IV: `{atm_ce.get('iv','N/A')}%` | Theta: `{atm_ce.get('theta','N/A')}` | LTP: `{atm_ce.get('ltp','N/A')}`
PE → Delta: `{atm_pe.get('delta','N/A')}` | IV: `{atm_pe.get('iv','N/A')}%` | Theta: `{atm_pe.get('theta','N/A')}` | LTP: `{atm_pe.get('ltp','N/A')}`
"""

    # Signal confluence
    msg += f"""
🎯 **SIGNAL CONFLUENCE**
Trend: `{res['trend']}` | OI: `{res['oi_signal']}` | Greeks: `{res['greek_signal']}` | Zone: `{res['zone_signal']}`
Bullish Signals: `{res['bull_count']}/4` | Bearish Signals: `{res['bear_count']}/4`

{signal_emoji} **FINAL SIGNAL: {res['signal']}**
"""

    if res["signal"] != "NO TRADE":
        msg += f"""Option: `{res['option']}`
Entry: `{res['entry']}` | SL: `{res['sl']}`
Target 1: `{res['t1']}` | Target 2: `{res['t2']}`
Confidence: `{res['confidence']}%`
"""

    msg += "\n⚠️ *Manual execution only. Not SEBI registered.*"
    return msg

# =========================
# CLAUDE AI ANALYSIS
# =========================
def get_ai_trade(market, oc_data, greeks_data, zones, levels):
    oc_summary = ""
    if oc_data:
        oc_summary = f"""
Option Chain:
- PCR: {oc_data['pcr']}
- Max Call OI Strike (Resistance): {oc_data['max_call_strike']} with {oc_data['max_call_oi']:,} OI
- Max Put OI Strike (Support): {oc_data['max_put_strike']} with {oc_data['max_put_oi']:,} OI
- Total Call OI: {oc_data['total_call_oi']:,}
- Total Put OI: {oc_data['total_put_oi']:,}
"""

    greeks_summary = ""
    if greeks_data:
        for k, v in list(greeks_data.items())[:2]:
            greeks_summary += f"\n{k}: Delta={v.get('delta','N/A')}, IV={v.get('iv','N/A')}%, Theta={v.get('theta','N/A')}, LTP={v.get('ltp','N/A')}"

    zones_summary = ""
    if zones:
        zones_summary = f"""
Supply/Demand:
- Major Supply: {zones['major_supply']}
- Major Demand: {zones['major_demand']}
- Immediate Resistance: {zones['immediate_resistance']}
- Immediate Support: {zones['immediate_support']}
"""

    prompt = f"""You are an expert NIFTY intraday options trader. Analyze this data and give a precise trade recommendation.

Market Data:
- Price: {market['price']}
- Open: {market['open']}, High: {market['high']}, Low: {market['low']}
- Prev Close: {market['prev_close']}
- VWAP: {round((market['high'] + market['low'] + market['price']) / 3, 2)}

Pivot Levels:
- Pivot: {levels['pivot']}, R1: {levels['r1']}, R2: {levels['r2']}
- S1: {levels['s1']}, S2: {levels['s2']}

{oc_summary}
{greeks_summary}
{zones_summary}

Rules:
- Only high probability setups (min 70% confidence)
- If unclear → NO TRADE
- Consider OI, Greeks, Supply/Demand together
- PCR > 1.3 = Bullish, PCR < 0.7 = Bearish

Give structured output:
SIGNAL: [CALL BUY / PUT BUY / NO TRADE]
OPTION: [e.g. NIFTY 24500 CE]
ENTRY: [price]
STOP LOSS: [price]
TARGET 1: [price]
TARGET 2: [price]
CONFIDENCE: [X%]
REASON: [2-3 lines explaining WHY based on OI + Greeks + Zones]
RISK: [key risk to watch]
"""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return f"❌ AI Error: {str(e)}"

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    print(f"✅ Bot logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # FULL ANALYSIS
    if message.content.lower() == "trade!":
        await message.channel.send("⏳ Fetching market data, OI & Greeks...")

        market = get_market_data()
        if not market or market["price"] is None:
            await message.channel.send("❌ Market data fetch failed")
            return

        price = market["price"]

        # Fetch all data
        await message.channel.send("📡 Fetching NSE Option Chain...")
        oc_data = get_option_chain()

        await message.channel.send("🔢 Fetching Greeks...")
        greeks_data, atm, expiry_str = get_greeks(price)

        # Trade engine
        result = master_trade_engine(market, oc_data, greeks_data, atm, expiry_str)
        formatted = format_output(result, market)

        await message.channel.send(formatted)

        # AI Analysis
        await message.channel.send("🤖 Running AI analysis...")
        levels = calculate_levels(market)
        zones = get_supply_demand_zones(oc_data, price) if oc_data else None
        ai_result = get_ai_trade(market, oc_data, greeks_data, zones, levels)

        time_now = datetime.now().strftime("%H:%M:%S")
        ai_msg = f"""
━━━━━━━━━━━━━━━━━━━━━━━━
🤖 **AI DEEP ANALYSIS** | {time_now}
━━━━━━━━━━━━━━━━━━━━━━━━

{ai_result}

⚠️ *Manual execution only. Not SEBI registered.*
"""
        await message.channel.send(ai_msg)

    # QUICK OI CHECK
    elif message.content.lower() == "oi!":
        await message.channel.send("📡 Fetching OI data...")
        oc_data = get_option_chain()
        if not oc_data:
            await message.channel.send("❌ OI fetch failed")
            return

        near = oc_data["near_strikes"][:6]
        table = "Strike | CE OI | PE OI | CE IV | PE IV\n"
        table += "-------|-------|-------|-------|------\n"
        for s in near:
            table += f"{s['strike']} | {s['ce_oi']:,} | {s['pe_oi']:,} | {s['ce_iv']}% | {s['pe_iv']}%\n"

        oi_msg = f"""
📈 **OI SNAPSHOT** | {datetime.now().strftime("%H:%M:%S")}

Spot: `{oc_data['spot']}` | Expiry: `{oc_data['expiry']}`
PCR: `{oc_data['pcr']}` | ATM: `{oc_data['atm_strike']}`

🔴 Max Call OI (Resistance): `{oc_data['max_call_strike']}` — {oc_data['max_call_oi']:,}
🟢 Max Put OI (Support): `{oc_data['max_put_strike']}` — {oc_data['max_put_oi']:,}

```
{table}```
"""
        await message.channel.send(oi_msg)

    # HELP
    elif message.content.lower() == "help!":
        help_msg = """
📋 **BOT COMMANDS**

`trade!` — Full analysis: OI + Greeks + Supply/Demand + AI Signal
`oi!` — Quick OI snapshot with PCR & key levels
`help!` — Show this message
"""
        await message.channel.send(help_msg)

# =========================
# RUN
# =========================
client.run(DISCORD_TOKEN)
