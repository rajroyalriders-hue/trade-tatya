import discord
import time
from datetime import datetime
import anthropic
from fyers_apiv3 import fyersModel

# =========================
# CONFIG
# =========================
import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # => None
CLAUDE_API_KEY = "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"
CHANNEL_ID = 1498261283584217219

FYERS_APP_ID = "R19GD9BCZH-200"
FYERS_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsiZDoxIiwiZDoyIiwieDowIiwieDoxIiwieDoyIl0sImF0X2hhc2giOiJnQUFBQUFCcDhHT0lVeTVVeGExVzYyS1NBaHRHMGFGNFZjeU5EVTU4Q2s0ZnpMY2d1cFBlRXBsUy15dllhSUM0SXY0MzFnUll3RkxUWGpMdmU0TkszdkI1cUxEUXRpczVwdjdUOXQ1bGdnM3NxM0pTNFNoc2pqTT0iLCJkaXNwbGF5X25hbWUiOiIiLCJvbXMiOiJLMSIsImhzbV9rZXkiOiJiOWQ3OGI2NWM1OTg3NTZmODMyMGJmNzc3OTdkODY2NzA4Y2JhZDY5ODFjMjllMjRlMDQ0ZDZkMCIsImlzRGRwaUVuYWJsZWQiOiJOIiwiaXNNdGZFbmFibGVkIjoiTiIsImZ5X2lkIjoiRkFKNDc3MTkiLCJhcHBUeXBlIjoyMDAsImV4cCI6MTc3NzQyMjYwMCwiaWF0IjoxNzc3MzYxODAwLCJpc3MiOiJhcGkuZnllcnMuaW4iLCJuYmYiOjE3NzczNjE4MDAsInN1YiI6ImFjY2Vzc190b2tlbiJ9.wqhK8AW16nlH2a-_40qjusZTONvRS8DhfMCQ22YAfxI"

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

def calculate_levels(m):
    high, low = m["high"], m["low"]
    close = m.get("prev_close") or m["open"]

    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high

    return {"pivot": round(pivot, 2), "r1": round(r1, 2), "s1": round(s1, 2)}


def calc_vwap(m):
    return round((m["high"] + m["low"] + m["price"]) / 3, 2)


def supply_demand(m):
    return {
        "demand": m["low"] + 25,
        "supply": m["high"] - 25
    }


def nearest_strike(price, step=50):
    return int(round(price / step) * step)


def pro_trade_engine(m):
    price = m["price"]
    levels = calculate_levels(m)
    zones = supply_demand(m)
    vwap = calc_vwap(m)

    bullish = price > vwap
    bearish = price < vwap

    signal = "NO TRADE"
    reason = "No clear edge"
    entry = sl = t1 = t2 = None

    if bullish and price <= zones["demand"] + 10:
        signal = "CALL BUY"
        reason = "Demand bounce"
        entry = price
        sl = zones["demand"] - 20
        t1 = levels["pivot"]
        t2 = levels["r1"]

    elif bearish and price >= zones["supply"] - 10:
        signal = "PUT BUY"
        reason = "Supply rejection"
        entry = price
        sl = zones["supply"] + 20
        t1 = levels["pivot"]
        t2 = levels["s1"]

    elif price > levels["r1"]:
        signal = "CALL BUY"
        reason = "Breakout"
        entry = price
        sl = levels["pivot"]
        t1 = price + 60
        t2 = price + 120

    elif price < levels["s1"]:
        signal = "PUT BUY"
        reason = "Breakdown"
        entry = price
        sl = levels["pivot"]
        t1 = price - 60
        t2 = price - 120

    strike = nearest_strike(price)
    option = f"NIFTY {strike} CE" if signal == "CALL BUY" else f"NIFTY {strike} PE"

    return {
        "signal": signal,
        "reason": reason,
        "entry": entry,
        "sl": sl,
        "t1": t1,
        "t2": t2,
        "levels": levels,
        "zones": zones,
        "vwap": vwap,
        "option": option if signal != "NO TRADE" else None
    }


def format_output(res, m):
    return f"""
📊 PRO TRADE SIGNAL

Price: {m['price']} | VWAP: {res['vwap']}

Pivot: {res['levels']['pivot']}
R1: {res['levels']['r1']} | S1: {res['levels']['s1']}

Demand: {res['zones']['demand']}
Supply: {res['zones']['supply']}

🎯 Signal: {res['signal']}
Reason: {res['reason']}

Entry: {res['entry']}
SL: {res['sl']}
Target: {res['t1']} / {res['t2']}

Option: {res['option']}

⚠️ Manual execution only
"""

# =========================
# FETCH REAL DATA
# =========================
def get_market_data():
    data = {"symbols": "NSE:NIFTY50-INDEX"}  # 👈 CHANGE HERE

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
        "prev_close": v.get("prev_close")
    }
# =========================
# CLAUDE ANALYSIS
# =========================
def get_ai_trade(market):
    prompt = f"""
You are a professional intraday options trader.

Market Data:
Price: {market['price']}
Open: {market['open']}
High: {market['high']}
Low: {market['low']}
Previous Close: {market['prev_close']}

Rules:
- Only high probability trades
- If market unclear → NO TRADE
- Avoid overtrading

Give output:
CALL BUY / PUT BUY / NO TRADE

Also include:
- Reason
- Confidence (%)
"""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",  # ✅ Sahi model
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )

        return response.content[0].text
    except Exception as e:
        return f"❌ AI Error: {str(e)}"

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_message(message):

    if message.author == client.user:
        return

    if message.content.lower() == "trade!":

        await message.channel.send("📊 Fetching REAL market data...")

        market = get_market_data()

        if not market or market["price"] is None:
            await message.channel.send("❌ Data fetch failed")
            return

        result = pro_trade_engine(market)
        final = format_output(result, market)

        await message.channel.send(final)

        market = get_market_data()

        if market is None:
            await message.channel.send("❌ Data fetch error")
            return

        result = get_ai_trade(market)

        time_now = datetime.now().strftime("%H:%M:%S")

        final = f"""
📊 AI TRADE SIGNAL

⏰ Time: {time_now}

{result}

⚠️ Manual execution only
"""

        await message.channel.send(final)

# =========================
# RUN
# =========================
client.run(DISCORD_TOKEN)
