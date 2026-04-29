import discord
import time
import requests
import json
import threading
import asyncio
from datetime import datetime
from flask import Flask, request
import anthropic
from fyers_apiv3 import fyersModel
import os

# =========================
# CONFIG
# =========================
DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN")
CLAUDE_API_KEY      = os.getenv("CLAUDE_API_KEY")
CHANNEL_ID          = 1498261283584217219

# Token update channel (private)
TOKEN_UPDATE_CHANNEL_ID = 1498884238496239626
ALLOWED_USER_ID         = 1158032451659120732

# Fyers
FYERS_APP_ID        = os.getenv("FYERS_APP_ID")
FYERS_SECRET_KEY    = os.getenv("FYERS_SECRET_KEY")
FYERS_ACCESS_TOKEN  = os.getenv("FYERS_ACCESS_TOKEN")
FYERS_REDIRECT_URI  = os.getenv("FYERS_REDIRECT_URI")  # Railway URL + /callback
# Example: https://your-app.up.railway.app/callback

# Railway
RAILWAY_API_TOKEN   = os.getenv("RAILWAY_API_TOKEN")
RAILWAY_PROJECT_ID  = os.getenv("RAILWAY_PROJECT_ID")
RAILWAY_SERVICE_ID  = os.getenv("RAILWAY_SERVICE_ID")

COOLDOWN_SECONDS = 60
last_run_time = 0

# =========================
# DISCORD CLIENT
# =========================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

fyers = fyersModel.FyersModel(
    client_id=FYERS_APP_ID,
    token=FYERS_ACCESS_TOKEN
)

# Discord channel reference (Flask callback ke liye)
discord_loop = None
token_channel = None

# =========================
# FLASK APP
# =========================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running!", 200

@flask_app.route("/callback")
def fyers_callback():
    """
    Fyers login ke baad yahan redirect aata hai.
    URL mein auth_code hota hai — usse access token banate hain.
    """
    auth_code = request.args.get("auth_code")
    state     = request.args.get("state", "")

    if not auth_code:
        return "<h2>❌ Auth code nahi mila. Dobara try karo.</h2>", 400

    try:
        import hashlib
        app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
        token_resp = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": auth_code
            },
            timeout=15
        )
        response = token_resp.json()

        if "access_token" not in response:
            error_msg = response.get("message", "Unknown error")
            # Discord pe error bhejo
            asyncio.run_coroutine_threadsafe(
                send_discord_message(f"❌ Token generate fail: {error_msg}"),
                discord_loop
            )
            return f"<h2>❌ Token generate fail: {error_msg}</h2>", 400

        new_token = response["access_token"]

        # Railway update karo
        success, msg = update_railway_token(new_token)

        # Discord pe result bhejo
        asyncio.run_coroutine_threadsafe(
            send_discord_message(
                f"{'✅' if success else '❌'} **Fyers Token Update**\n{msg}"
            ),
            discord_loop
        )

        if success:
            return """
            <html><body style='font-family:sans-serif;text-align:center;padding:50px'>
            <h2>✅ Token generate aur update ho gaya!</h2>
            <p>Discord channel check karo. Bot ready hai.</p>
            <p>Yeh tab band kar sakte ho.</p>
            </body></html>
            """, 200
        else:
            return f"<h2>⚠️ Token mila par Railway update fail: {msg}</h2>", 500

    except Exception as e:
        asyncio.run_coroutine_threadsafe(
            send_discord_message(f"❌ Callback error: {str(e)}"),
            discord_loop
        )
        return f"<h2>❌ Error: {str(e)}</h2>", 500


async def send_discord_message(msg: str):
    """Flask thread se Discord mein message bhejne ke liye"""
    global token_channel
    if token_channel:
        await token_channel.send(msg)


# =========================
# RAILWAY TOKEN UPDATE
# =========================
def update_railway_token(new_token: str) -> tuple[bool, str]:
    try:
        api_token  = RAILWAY_API_TOKEN
        project_id = RAILWAY_PROJECT_ID
        service_id = RAILWAY_SERVICE_ID

        if not all([api_token, project_id, service_id]):
            return False, "Railway config missing (RAILWAY_API_TOKEN / PROJECT_ID / SERVICE_ID)"

        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

        # Step 1: Environment ID lo
        env_query = """
        query { project(id: "%s") { environments { edges { node { id name } } } } }
        """ % project_id

        env_resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": env_query},
            headers=headers,
            timeout=15
        )
        env_data = env_resp.json()
        environments = env_data["data"]["project"]["environments"]["edges"]

        env_id = None
        for e in environments:
            if e["node"]["name"].lower() == "production":
                env_id = e["node"]["id"]
                break
        if not env_id:
            env_id = environments[0]["node"]["id"]

        # Step 2: Variable update karo
        upsert_mutation = """
        mutation variableUpsert($input: VariableUpsertInput!) {
            variableUpsert(input: $input)
        }
        """
        variables = {
            "input": {
                "projectId": project_id,
                "environmentId": env_id,
                "serviceId": service_id,
                "name": "FYERS_ACCESS_TOKEN",
                "value": new_token
            }
        }
        upsert_resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={"query": upsert_mutation, "variables": variables},
            headers=headers,
            timeout=15
        )
        upsert_data = upsert_resp.json()

        if "errors" in upsert_data:
            return False, f"Railway error: {upsert_data['errors'][0]['message']}"

        # Step 3: Redeploy karo
        redeploy_mutation = """
        mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """
        requests.post(
            "https://backboard.railway.app/graphql/v2",
            json={
                "query": redeploy_mutation,
                "variables": {"serviceId": service_id, "environmentId": env_id}
            },
            headers=headers,
            timeout=15
        )

        # Step 4: Current session update karo
        global fyers, FYERS_ACCESS_TOKEN
        FYERS_ACCESS_TOKEN = new_token
        fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=new_token)

        return True, "Token save + Railway redeploy ho gaya! Bot 1-2 min mein restart hoga. 🚀"

    except Exception as e:
        return False, f"Error: {str(e)}"


# =========================
# FYERS LOGIN LINK GENERATOR
# =========================
def get_fyers_login_url() -> str:
    try:
        import urllib.parse
        params = {
            "client_id": FYERS_APP_ID,
            "redirect_uri": FYERS_REDIRECT_URI,
            "response_type": "code",
            "state": "discord_bot"
        }
        return "https://api-t1.fyers.in/api/v3/generate-authcode?" + urllib.parse.urlencode(params)
    except Exception as e:
        return f"ERROR:{str(e)}"
        return f"ERROR:{str(e)}"


# =========================
# MARKET DATA FUNCTIONS
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


def get_option_chain_fyers(spot_price):
    try:
        # ATM calculate
        atm = round(spot_price / 50) * 50

        # Nearby strikes (adjust kar sakta hai)
        strikes = [atm - 200, atm - 150, atm - 100, atm - 50,
                   atm, atm + 50, atm + 100, atm + 150, atm + 200]

        # Expiry calculate (Thursday)
        today = datetime.now()
        days_ahead = 3 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        expiry = today.replace(day=today.day + days_ahead)
        expiry_str = expiry.strftime("%y%b").upper()

        symbols = []
        for s in strikes:
            symbols.append(f"NSE:NIFTY{expiry_str}{s}CE")
            symbols.append(f"NSE:NIFTY{expiry_str}{s}PE")

        data = {"symbols": ",".join(symbols)}
        response = fyers.quotes(data=data)

        total_call_oi = 0
        total_put_oi = 0
        max_call_oi = 0
        max_put_oi = 0
        max_call_strike = 0
        max_put_strike = 0

        oi_data = []

        if "d" in response:
            for item in response["d"]:
                sym = item.get("n", "")
                v = item.get("v", {})

                oi = v.get("oi", 0)
                ltp = v.get("lp", 0)

                # strike extract
                import re
                match = re.search(r'(\d{5})(CE|PE)$', sym)
               if not match:
                   continue
               strike = int(match.group(1))

                if sym.endswith("CE"):
                    total_call_oi += oi
                    if oi > max_call_oi:
                        max_call_oi = oi
                        max_call_strike = strike
                    oi_data.append({"strike": strike, "ce_oi": oi, "ce_ltp": ltp, "pe_oi": 0, "pe_ltp": 0})

                elif sym.endswith("PE"):
                    total_put_oi += oi
                    if oi > max_put_oi:
                        max_put_oi = oi
                        max_put_strike = strike
                    oi_data.append({"strike": strike, "ce_oi": 0, "ce_ltp": 0, "pe_oi": oi, "pe_ltp": ltp})

        # Merge CE & PE
        merged = {}
        for item in oi_data:
            s = item["strike"]
            if s not in merged:
                merged[s] = {"strike": s, "ce_oi": 0, "pe_oi": 0}
            merged[s]["ce_oi"] += item["ce_oi"]
            merged[s]["pe_oi"] += item["pe_oi"]

        merged_list = list(merged.values())

        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0

        return {
            "spot": spot_price,
            "expiry": expiry_str,
            "pcr": pcr,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "max_call_strike": max_call_strike,
            "max_call_oi": max_call_oi,
            "max_put_strike": max_put_strike,
            "max_put_oi": max_put_oi,
            "atm_strike": atm,
            "near_strikes": merged_list
        }

    except Exception as e:
        print("Fyers OI Error:", e)
        return None


def get_greeks(spot_price):
    try:
        atm = round(spot_price / 50) * 50
        otm_call = atm + 50
        itm_call = atm - 50

        today = datetime.now()
        days_ahead = 3 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        expiry = today.replace(day=today.day + days_ahead, hour=0, minute=0, second=0, microsecond=0)
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
                v   = item.get("v", {})
                greeks_data[sym] = {
                    "ltp": v.get("lp", 0), "delta": v.get("delta", 0),
                    "gamma": v.get("gamma", 0), "theta": v.get("theta", 0),
                    "vega": v.get("vega", 0), "iv": v.get("iv", 0), "oi": v.get("oi", 0),
                }

        return greeks_data, atm, expiry_str
    except Exception as e:
        print(f"Greeks error: {e}")
        return {}, 0, ""


def get_supply_demand_zones(oc_data, spot):
    if not oc_data:
        return None
    supply_zone = oc_data["max_call_strike"]
    demand_zone = oc_data["max_put_strike"]

    high_ce_oi = sorted(oc_data["near_strikes"], key=lambda x: x["ce_oi",0], reverse=True)[:3]
    high_pe_oi = sorted(oc_data["near_strikes"], key=lambda x: x["pe_oi",0], reverse=True)[:3]

    above = [x["strike"] for x in oc_data["near_strikes"] if x["strike"] > spot]
    below = [x["strike"] for x in oc_data["near_strikes"] if x["strike"] < spot]

    return {
        "major_supply": supply_zone, "major_demand": demand_zone,
        "resistance_levels": [x["strike"] for x in high_ce_oi],
        "support_levels": [x["strike"] for x in high_pe_oi],
        "immediate_resistance": min(above) if above else supply_zone,
        "immediate_support": max(below) if below else demand_zone,
    }


def calculate_levels(m):
    high, low = m["high"], m["low"]
    close = m.get("prev_close") or m["open"]
    pivot = (high + low + close) / 3
    return {
        "pivot": round(pivot, 2),
        "r1": round((2 * pivot) - low, 2),
        "r2": round(pivot + (high - low), 2),
        "s1": round((2 * pivot) - high, 2),
        "s2": round(pivot - (high - low), 2),
    }


def calc_vwap(m):
    return round((m["high"] + m["low"] + m["price"]) / 3, 2)


def master_trade_engine(market, oc_data, greeks_data, atm, expiry_str):
    price  = market["price"]
    levels = calculate_levels(market)
    vwap   = calc_vwap(market)
    zones  = get_supply_demand_zones(oc_data, price) if oc_data else None

    atm_ce = greeks_data.get(f"NSE:NIFTY{expiry_str}{atm}CE", {})
    atm_pe = greeks_data.get(f"NSE:NIFTY{expiry_str}{atm}PE", {})

    trend = "BULLISH" if price > vwap else "BEARISH"

    oi_signal = "NEUTRAL"
    if oc_data:
        pcr = oc_data["pcr"]
        oi_signal = "BULLISH" if pcr > 1.3 else ("BEARISH" if pcr < 0.7 else "NEUTRAL")

    greek_signal = "NEUTRAL"
    if atm_ce and atm_pe:
        ce_delta = abs(atm_ce.get("delta", 0))
        pe_delta = abs(atm_pe.get("delta", 0))
        greek_signal = "BULLISH" if ce_delta > pe_delta else ("BEARISH" if pe_delta > ce_delta else "NEUTRAL")

    zone_signal = "NEUTRAL"
    if zones:
        if price <= zones["immediate_support"] + 30:
            zone_signal = "BULLISH"
        elif price >= zones["immediate_resistance"] - 30:
            zone_signal = "BEARISH"

    bull_count = sum([trend == "BULLISH", oi_signal == "BULLISH", greek_signal == "BULLISH", zone_signal == "BULLISH"])
    bear_count = sum([trend == "BEARISH", oi_signal == "BEARISH", greek_signal == "BEARISH", zone_signal == "BEARISH"])

    if bull_count >= 3:
        final_signal = "CALL BUY"
        option_sym   = f"NIFTY {atm} CE"
        entry        = atm_ce.get("ltp", price) if atm_ce else price
        sl           = zones["immediate_support"] - 20 if zones else levels["s1"]
        t1           = zones["immediate_resistance"] if zones else levels["r1"]
        t2           = levels["r1"] if zones else levels["r2"]
        confidence   = int((bull_count / 4) * 100)
    elif bear_count >= 3:
        final_signal = "PUT BUY"
        option_sym   = f"NIFTY {atm} PE"
        entry        = atm_pe.get("ltp", price) if atm_pe else price
        sl           = zones["immediate_resistance"] + 20 if zones else levels["r1"]
        t1           = zones["immediate_support"] if zones else levels["s1"]
        t2           = levels["s1"] if zones else levels["s2"]
        confidence   = int((bear_count / 4) * 100)
    else:
        final_signal = "NO TRADE"
        option_sym   = None
        entry = sl = t1 = t2 = None
        confidence   = 0

    return {
        "signal": final_signal, "option": option_sym,
        "entry": round(entry, 2) if entry else None,
        "sl": round(sl, 2) if sl else None,
        "t1": round(t1, 2) if t1 else None,
        "t2": round(t2, 2) if t2 else None,
        "confidence": confidence, "trend": trend,
        "oi_signal": oi_signal, "greek_signal": greek_signal, "zone_signal": zone_signal,
        "bull_count": bull_count, "bear_count": bear_count,
        "levels": levels, "vwap": vwap, "atm_ce": atm_ce, "atm_pe": atm_pe,
        "zones": zones, "oc_data": oc_data,
    }


def format_output(res, market):
    oc     = res["oc_data"]
    atm_ce = res["atm_ce"]
    atm_pe = res["atm_pe"]
    zones  = res["zones"]
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


def get_ai_trade(market, oc_data, greeks_data, zones, levels):
    oc_summary = ""
    if oc_data:
        oc_summary = f"""
Option Chain:
- PCR: {oc_data['pcr']}
- Max Call OI Strike (Resistance): {oc_data['max_call_strike']} with {oc_data['max_call_oi']:,} OI
- Max Put OI Strike (Support): {oc_data['max_put_strike']} with {oc_data['max_put_oi']:,} OI
- Total Call OI: {oc_data['total_call_oi']:,} | Total Put OI: {oc_data['total_put_oi']:,}
"""
    greeks_summary = ""
    if greeks_data:
        for k, v in list(greeks_data.items())[:2]:
            greeks_summary += f"\n{k}: Delta={v.get('delta','N/A')}, IV={v.get('iv','N/A')}%, Theta={v.get('theta','N/A')}, LTP={v.get('ltp','N/A')}"

    zones_summary = ""
    if zones:
        zones_summary = f"""
Supply/Demand:
- Major Supply: {zones['major_supply']} | Major Demand: {zones['major_demand']}
- Immediate Resistance: {zones['immediate_resistance']} | Immediate Support: {zones['immediate_support']}
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

{oc_summary}{greeks_summary}{zones_summary}

Rules:
- Only high probability setups (min 70% confidence)
- If unclear → NO TRADE
- PCR > 1.3 = Bullish, PCR < 0.7 = Bearish

Give structured output:
SIGNAL: [CALL BUY / PUT BUY / NO TRADE]
OPTION: [e.g. NIFTY 24500 CE]
ENTRY: [price]
STOP LOSS: [price]
TARGET 1: [price]
TARGET 2: [price]
CONFIDENCE: [X%]
REASON: [2-3 lines]
RISK: [key risk]
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
    global discord_loop, token_channel
    discord_loop = asyncio.get_event_loop()
    token_channel = client.get_channel(TOKEN_UPDATE_CHANNEL_ID)
    print(f"✅ Bot logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # ===========================================
    # TOKEN CHANNEL — "generate" likhne pe login link aata hai
    # ===========================================
    if message.channel.id == TOKEN_UPDATE_CHANNEL_ID:

        if message.author.id != ALLOWED_USER_ID:
            await message.delete()
            return

        text = message.content.strip().lower()

        if text == "generate":
            await message.delete()
            login_url = get_fyers_login_url()

            if login_url.startswith("ERROR:"):
                await message.channel.send(f"❌ Login URL generate nahi hua: {login_url}")
                return

            await message.channel.send(
                f"🔐 **Fyers Login Link** (sirf tumhare liye)\n\n"
                f"👉 {login_url}\n\n"
                f"_Yeh link kholo → Login karo → Token automatically update ho jayega!_"
            )
        else:
            # Kuch aur likha — delete karo
            await message.delete()
            await message.channel.send("ℹ️ Sirf `generate` likho token ke liye.", delete_after=5)

        return

    # ===========================================
    # NORMAL BOT COMMANDS
    # ===========================================
    if message.content.lower() == "trade!":
        await message.channel.send("⏳ Fetching market data, OI & Greeks...")

        market = get_market_data()
        if not market or market["price"] is None:
            await message.channel.send("❌ Market data fetch failed. Pehle token update karo — token channel mein `generate` likho.")
            return

        price = market["price"]

        await message.channel.send("📡 Fetching Fyers Option Chain...")
        oc_data = get_option_chain_fyers(price)

        await message.channel.send("🔢 Fetching Greeks...")
        greeks_data, atm, expiry_str = get_greeks(price)

        result    = master_trade_engine(market, oc_data, greeks_data, atm, expiry_str)
        formatted = format_output(result, market)
        await message.channel.send(formatted)

        await message.channel.send("🤖 Running AI analysis...")
        levels    = calculate_levels(market)
        zones     = get_supply_demand_zones(oc_data, price) if oc_data else None
        ai_result = get_ai_trade(market, oc_data, greeks_data, zones, levels)

        ai_msg = f"""
━━━━━━━━━━━━━━━━━━━━━━━━
🤖 **AI DEEP ANALYSIS** | {datetime.now().strftime("%H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━━━

{ai_result}

⚠️ *Manual execution only. Not SEBI registered.*
"""
        await message.channel.send(ai_msg)

    elif message.content.lower() == "oi!":
        await message.channel.send("📡 Fetching OI data...")
        market = get_market_data()
        price = market["price"]
        oc_data = get_option_chain_fyers(price)
        if not oc_data:
            await message.channel.send("❌ OI fetch failed")
            return

        near  = oc_data["near_strikes"][:6]
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

    elif message.content.lower() == "help!":
        help_msg = """
📋 **BOT COMMANDS**

`trade!` — Full analysis: OI + Greeks + Supply/Demand + AI Signal
`oi!` — Quick OI snapshot with PCR & key levels
`help!` — Show this message

🔐 **Token Update** (private channel mein)
`generate` — Fyers login link milega → login karo → token auto update!
"""
        await message.channel.send(help_msg)


# =========================
# FLASK THREAD
# =========================
def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# =========================
# MAIN — Flask + Discord dono saath chalao
# =========================
if __name__ == "__main__":
    # Flask alag thread mein
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask server started")

    # Discord bot main thread mein
    client.run(DISCORD_TOKEN)
