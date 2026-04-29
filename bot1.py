import os, asyncio, math
from datetime import datetime
import discord
import yfinance as yf
import pandas as pd
import aiohttp

# ============== CONFIG ==============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = "1498261283584217219"

# Optional keys
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")  # optional
NEWS_API_KEY = os.getenv("NEWS_API_KEY")      # optional

# ============== DISCORD ==============
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ============== HELPERS ==============
async def safe_await(coro, timeout=6):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except:
        return None

def rsi(series: pd.Series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def ema(series: pd.Series, span):
    return series.ewm(span=span, adjust=False).mean()

def round_to(x, base=50):
    return int(base * round(float(x)/base))

# ============== YAHOO (PRICE + EMA + RSI + VIX) ==============
async def get_yahoo_pack():
    def fetch():
        df = yf.download("^NSEI", period="1d", interval="1m", progress=False)
        if df is None or df.empty:
            return None

        df = df.dropna()
        close = df["Close"]
        latest = df.iloc[-1]

        df["EMA9"] = ema(close, 9)
        df["EMA14"] = ema(close, 14)
        df["RSI14"] = rsi(close, 14)

        vix_df = yf.download("^INDIAVIX", period="1d", interval="1m", progress=False)
        vix = float(vix_df["Close"].iloc[-1]) if vix_df is not None and not vix_df.empty else None

        return {
            "price": float(latest["Close"]),
            "high": float(df["High"].max()),
            "low": float(df["Low"].min()),
            "open": float(df["Open"].iloc[0]),
            "ema9": float(df["EMA9"].iloc[-1]),
            "ema14": float(df["EMA14"].iloc[-1]),
            "rsi": float(df["RSI14"].iloc[-1]),
            "vix": vix
        }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch)

# ============== NSE OPTION CHAIN (OI / PCR) ==============
async def get_nse_oi_pack():
    url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    headers = {
        "user-agent": "Mozilla/5.0",
        "accept-language": "en-US,en;q=0.9"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            # warm-up cookie
            async with s.get("https://www.nseindia.com") as _:
                pass
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()

        records = data.get("records", {})
        expiry = records.get("expiryDates", [None])[0]
        chain = records.get("data", [])

        total_ce = total_pe = 0
        max_ce = (0, 0)  # (OI, strike)
        max_pe = (0, 0)

        for item in chain:
            if item.get("expiryDate") != expiry:
                continue
            ce = item.get("CE")
            pe = item.get("PE")

            if ce:
                oi = ce.get("openInterest", 0) or 0
                total_ce += oi
                if oi > max_ce[0]:
                    max_ce = (oi, item.get("strikePrice", 0))

            if pe:
                oi = pe.get("openInterest", 0) or 0
                total_pe += oi
                if oi > max_pe[0]:
                    max_pe = (oi, item.get("strikePrice", 0))

        pcr = (total_pe / total_ce) if total_ce else 0

        return {
            "pcr": round(pcr, 2),
            "max_call_oi": max_ce[0],
            "max_call_strike": max_ce[1],
            "max_put_oi": max_pe[0],
            "max_put_strike": max_pe[1],
            "total_call_oi": total_ce,
            "total_put_oi": total_pe,
            "expiry": expiry
        }
    except:
        return None

# ============== SUPPLY / DEMAND (simple zones) ==============
def get_zones(high, low):
    rng = high - low
    supply = high - 0.2 * rng
    demand = low + 0.2 * rng
    return round(supply, 2), round(demand, 2)

# ============== NEWS SENTIMENT (optional) ==============
async def get_news_sentiment():
    if not NEWS_API_KEY:
        return None
    url = f"https://newsapi.org/v2/everything?q=NIFTY%20OR%20India%20markets&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        titles = " ".join([a.get("title","") for a in data.get("articles", [])])
        # very naive sentiment
        pos_words = ["up", "gain", "surge", "bull", "positive"]
        neg_words = ["fall", "drop", "bear", "negative", "crash"]
        score = sum(w in titles.lower() for w in pos_words) - sum(w in titles.lower() for w in neg_words)
        if score > 0:
            return "BULLISH"
        elif score < 0:
            return "BEARISH"
        return "NEUTRAL"
    except:
        return None

# ============== STRATEGY (EMA + RSI + OI + VIX + Zones) ==============
def decide(mkt, oi):
    price = mkt["price"]
    ema9 = mkt["ema9"]
    ema14 = mkt["ema14"]
    rsi_v = mkt["rsi"]
    vix = mkt["vix"]

    supply, demand = get_zones(mkt["high"], mkt["low"])

    # basic trend
    trend = "BULLISH" if ema9 > ema14 else "BEARISH"

    # filters
    if vix and vix > 22:
        return {"signal": "NO TRADE", "reason": "High VIX", "entry": None, "sl": None, "t1": None, "t2": None,
                "zones": (supply, demand), "trend": trend}

    if not oi or oi.get("total_call_oi", 0) == 0:
        return {"signal": "NO TRADE", "reason": "No OI data", "entry": None, "sl": None, "t1": None, "t2": None,
                "zones": (supply, demand), "trend": trend}

    pcr = oi["pcr"]

    # rules
    if trend == "BULLISH" and rsi_v > 50 and price > ema9 and pcr > 1 and price > demand:
        entry = round(price, 2)
        sl = round(demand - 10, 2)
        t1 = round(price + 40, 2)
        t2 = round(price + 80, 2)
        return {"signal": "CALL BUY", "reason": "EMA+RSI+PCR bullish", "entry": entry, "sl": sl, "t1": t1, "t2": t2,
                "zones": (supply, demand), "trend": trend}

    if trend == "BEARISH" and rsi_v < 50 and price < ema9 and pcr < 1 and price < supply:
        entry = round(price, 2)
        sl = round(supply + 10, 2)
        t1 = round(price - 40, 2)
        t2 = round(price - 80, 2)
        return {"signal": "PUT BUY", "reason": "EMA+RSI+PCR bearish", "entry": entry, "sl": sl, "t1": t1, "t2": t2,
                "zones": (supply, demand), "trend": trend}

    return {"signal": "NO TRADE", "reason": "No confluence", "entry": None, "sl": None, "t1": None, "t2": None,
            "zones": (supply, demand), "trend": trend}

# ============== AI SUMMARY (optional Claude) ==============
async def ai_summary(mkt, oi, decision, news):
    if not CLAUDE_API_KEY:
        return "AI not configured"
    try:
        import anthropic
        client_ai = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        prompt = f"""
Price:{mkt['price']} EMA9:{mkt['ema9']} EMA14:{mkt['ema14']} RSI:{mkt['rsi']} VIX:{mkt['vix']}
PCR:{oi.get('pcr') if oi else None}
Decision:{decision['signal']} Reason:{decision['reason']}
News:{news}
Give short trader explanation + confidence%.
"""
        resp = client_ai.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text
    except:
        return "AI error"

# ============== FORMAT ==============
def fmt(mkt, oi, dec, news, ai):
    supply, demand = dec["zones"]
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━
📊 PRO TRADE SIGNAL | {datetime.now().strftime("%H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━━━

💹 MARKET
Price: {mkt['price']} | Open: {mkt['open']}
High: {mkt['high']} | Low: {mkt['low']}

📈 EMA / RSI / VIX
EMA9: {mkt['ema9']} | EMA14: {mkt['ema14']}
RSI14: {round(mkt['rsi'],2)} | VIX: {mkt['vix']}

🏗️ ZONES
Supply: {supply} | Demand: {demand}

📊 OI
PCR: {oi.get('pcr') if oi else None}
Max CE: {oi.get('max_call_strike') if oi else None}
Max PE: {oi.get('max_put_strike') if oi else None}

📰 NEWS: {news}

🎯 SIGNAL: {dec['signal']}
Reason: {dec['reason']}

Entry: {dec['entry']} | SL: {dec['sl']}
Target1: {dec['t1']} | Target2: {dec['t2']}

🤖 AI:
{ai}
"""

# ============== MAIN COMMAND ==============
@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.lower() == "trade":

        mkt = await safe_await(get_yahoo_pack())
        oi = await safe_await(get_nse_oi_pack())
        news = await safe_await(get_news_sentiment())

        if not mkt:
            await message.channel.send("⚠️ NO DATA (price)")
            return

        decision = decide(mkt, oi)

        # strict filter
        if decision["signal"] == "NO TRADE":
            await message.channel.send("⚠️ NO TRADE - insufficient confluence")
            return

        ai = await safe_await(ai_summary(mkt, oi or {}, decision, news), timeout=10)
        msg = fmt(mkt, oi or {}, decision, news, ai)
        await message.channel.send(msg)

@client.event
async def on_ready():
    print("Bot Live 🔥")

client.run(DISCORD_TOKEN)
