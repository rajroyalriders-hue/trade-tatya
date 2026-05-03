import discord
import asyncio
import requests
import anthropic
import os
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime

# =========================
# CONFIG
# =========================
DISCORD_TOKEN    = os.getenv("NEWS_DISCORD_TOKEN")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")
NEWS_CHANNEL_ID  = 1500013603229798400
CHECK_INTERVAL   = 30  # minutes

# =========================
# INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True
client           = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# Posted news hashes — repeat avoid karne ke liye
posted_hashes = set()

# =========================
# NEWS SOURCES (RSS Feeds)
# =========================
NEWS_SOURCES = [
    {
        "name": "Economic Times Markets",
        "url":  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    },
    {
        "name": "Economic Times News",
        "url":  "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
    },
    {
        "name": "Moneycontrol via Google News",
        "url":  "https://news.google.com/rss/search?q=moneycontrol+stock+market&hl=en-IN&gl=IN&ceid=IN:en",
    },
    {
        "name": "Moneycontrol Markets via Google",
        "url":  "https://news.google.com/rss/search?q=site:moneycontrol.com+market&hl=en-IN&gl=IN&ceid=IN:en",
    },
    {
        "name": "Business Standard Markets",
        "url":  "https://www.business-standard.com/rss/markets-106.rss",
    },
    {
        "name": "Business Standard Economy",
        "url":  "https://www.business-standard.com/rss/economy-policy-101.rss",
    },
    {
        "name": "LiveMint Markets",
        "url":  "https://www.livemint.com/rss/markets",
    },
    {
        "name": "Reuters Business",
        "url":  "https://feeds.reuters.com/reuters/businessNews",
    },
    {
        "name": "CNBC World Markets",
        "url":  "https://www.cnbc.com/id/15839069/device/rss/rss.html",
    },
    {
        "name": "Al Jazeera Business",
        "url":  "https://www.aljazeera.com/xml/rss/all.xml",
    },
]

# =========================
# FETCH RSS NEWS
# =========================
def fetch_rss(url, source_name):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml",
        }
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return []

        root = ET.fromstring(r.content)
        items = []

        for item in root.findall(".//item")[:10]:
            title = item.findtext("title", "").strip()
            desc  = item.findtext("description", "").strip()
            link  = item.findtext("link", "").strip()
            pub   = item.findtext("pubDate", "").strip()

            if title:
                items.append({
                    "title":   title,
                    "desc":    desc[:300] if desc else "",
                    "link":    link,
                    "pub":     pub,
                    "source":  source_name,
                })

        return items
    except Exception as e:
        print(f"RSS error {source_name}: {e}")
        return []

# =========================
# FETCH ALL NEWS
# =========================
def fetch_all_news():
    all_news = []
    for source in NEWS_SOURCES:
        items = fetch_rss(source["url"], source["name"])
        all_news.extend(items)
        print(f"Fetched {len(items)} from {source['name']}")
    return all_news

# =========================
# NEWS HASH — duplicate check
# =========================
def get_news_hash(title):
    # Normalize title for better duplicate detection
    clean = title.lower().strip()
    return hashlib.md5(clean.encode()).hexdigest()

# =========================
# AI FILTER & FORMAT
# =========================
def ai_filter_and_format(news_items):
    """AI se filter karo — sirf Indian market relevant news"""
    if not news_items:
        return []

    # Prepare news list for AI
    news_text = ""
    for i, n in enumerate(news_items[:20]):
        news_text += f"{i+1}. [{n['source']}] {n['title']}\n   {n['desc'][:150]}\n\n"

    prompt = f"""You are an Indian stock market news filter and analyst.

From the following news items, select those that could impact Indian markets including:
1. Indian stock market news (NSE, BSE, Nifty, Sensex movements)
2. Indian company news (earnings, results, mergers, acquisitions, management changes)
3. RBI/SEBI/Government policy decisions
4. US Fed, ECB rate decisions and statements
5. Trump tweets/statements about trade, tariffs, India, China, oil
6. Crude oil, gold, dollar index major moves
7. US-China trade war updates
8. Global recession fears or growth data
9. FII/DII flows, IPOs, bulk deals
10. Any geopolitical event affecting global markets

Be INCLUSIVE — even small news that could cause 0.5% move in Nifty is relevant.

EXCLUDE only:
- Pure sports, entertainment with zero market relevance
- Highly local political news with no market impact

For each selected news:
- Relevance score (1-10) — be generous, score 4+ for anything with even small market impact
- Market impact: BULLISH / BEARISH / NEUTRAL
- One line summary mentioning WHY it affects Indian market

News items:
{news_text}

Respond in JSON format ONLY (no markdown, no extra text):
[
  {{
    "index": 1,
    "score": 8,
    "impact": "BULLISH",
    "summary": "Short summary here"
  }}
]

If no relevant news, return empty array: []"""

    try:
        r = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = r.content[0].text.strip()

        # Parse JSON
        import json
        # Remove any markdown if present
        text = text.replace("```json", "").replace("```", "").strip()
        filtered = json.loads(text)
        return filtered
    except Exception as e:
        print(f"AI filter error: {e}")
        return []

# =========================
# FORMAT NEWS MESSAGE
# =========================
def format_news_message(news_item, ai_info):
    impact    = ai_info.get("impact", "NEUTRAL")
    summary   = ai_info.get("summary", news_item["title"])
    score     = ai_info.get("score", 5)
    source    = news_item["source"]
    link      = news_item["link"]
    title     = news_item["title"]

    # Impact emoji
    if impact == "BULLISH":
        imp_emoji = "🟢 BULLISH"
    elif impact == "BEARISH":
        imp_emoji = "🔴 BEARISH"
    else:
        imp_emoji = "🟡 NEUTRAL"

    # Relevance bar
    bar = "█" * score + "░" * (10 - score)

    msg = f"""📰 **MARKET NEWS UPDATE**
━━━━━━━━━━━━━━━━━━━━
**{title}**

💡 **AI Summary:** {summary}

📊 **Market Impact:** {imp_emoji}
📈 **Relevance:** [{bar}] {score}/10
📡 **Source:** {source}
🕐 **Time:** {datetime.now().strftime("%d %b %Y %H:%M")}

🔗 [Full Article]({link})
━━━━━━━━━━━━━━━━━━━━
*📢 Sirf educational purpose ke liye | SEBI registered nahi hain*"""

    return msg

# =========================
# MAIN NEWS LOOP
# =========================
async def news_loop():
    await client.wait_until_ready()
    channel = client.get_channel(NEWS_CHANNEL_ID)

    if not channel:
        print(f"❌ News channel not found: {NEWS_CHANNEL_ID}")
        return

    print(f"✅ News bot started | Channel: {channel.name}")

    while not client.is_closed():
        try:
            print(f"\n🔍 Fetching news... {datetime.now().strftime('%H:%M:%S')}")

            # Fetch all news
            all_news = await asyncio.get_event_loop().run_in_executor(None, fetch_all_news)

            if not all_news:
                print("No news fetched")
                await asyncio.sleep(CHECK_INTERVAL * 60)
                continue

            # Filter new (not posted) news
            new_items = []
            for item in all_news:
                h = get_news_hash(item["title"])
                if h not in posted_hashes:
                    new_items.append(item)

            print(f"New items: {len(new_items)} / Total: {len(all_news)}")

            if not new_items:
                print("No new news to post")
                await asyncio.sleep(CHECK_INTERVAL * 60)
                continue

            # AI filter
            filtered = await asyncio.get_event_loop().run_in_executor(
                None, ai_filter_and_format, new_items
            )

            print(f"AI selected: {len(filtered)} relevant news")

            # Post filtered news (max 5 per cycle)
            posted = 0
            for ai_info in filtered[:5]:
                idx = ai_info.get("index", 1) - 1
                if idx < 0 or idx >= len(new_items):
                    continue

                news_item = new_items[idx]
                score     = ai_info.get("score", 0)

                # Only post if relevance >= 6
                if score < 4:
                    continue

                msg = format_news_message(news_item, ai_info)
                await channel.send(msg)

                # Mark as posted
                posted_hashes.add(get_news_hash(news_item["title"]))
                posted += 1

                # Small delay between posts
                await asyncio.sleep(3)

            print(f"Posted: {posted} news items")

            # Keep hash set size manageable
            if len(posted_hashes) > 500:
                # Remove oldest entries (convert to list, keep last 300)
                recent = list(posted_hashes)[-300:]
                posted_hashes.clear()
                posted_hashes.update(recent)

        except Exception as e:
            print(f"News loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL * 60)


# =========================
# FII/DII DATA — NSE
# =========================
def fetch_fii_dii():
    """Fetch FII/DII data — Pro, Client, DII Pro, DII Client all 4"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Referer": "https://www.nseindia.com",
        }
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=8)
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None

        result = {
            "fii_pro":    {"buy": 0, "sell": 0, "net": 0},
            "fii_client": {"buy": 0, "sell": 0, "net": 0},
            "dii_pro":    {"buy": 0, "sell": 0, "net": 0},
            "dii_client": {"buy": 0, "sell": 0, "net": 0},
            "date": datetime.now().strftime("%d %b %Y"),
        }

        for item in data:
            cat  = item.get("category", "").upper()
            buy  = float(item.get("buyValue", 0) or 0)
            sell = float(item.get("sellValue", 0) or 0)
            net  = float(item.get("netValue", 0) or 0)

            if "FII" in cat or "FPI" in cat:
                if "PRO" in cat:
                    result["fii_pro"]    = {"buy": buy, "sell": sell, "net": net}
                elif "CLIENT" in cat:
                    result["fii_client"] = {"buy": buy, "sell": sell, "net": net}
                else:
                    # If no sub-category, put in pro
                    result["fii_pro"]    = {"buy": buy, "sell": sell, "net": net}
            elif "DII" in cat:
                if "PRO" in cat:
                    result["dii_pro"]    = {"buy": buy, "sell": sell, "net": net}
                elif "CLIENT" in cat:
                    result["dii_client"] = {"buy": buy, "sell": sell, "net": net}
                else:
                    result["dii_pro"]    = {"buy": buy, "sell": sell, "net": net}

        # Check if any data came
        total_buy = sum([
            result["fii_pro"]["buy"], result["fii_client"]["buy"],
            result["dii_pro"]["buy"], result["dii_client"]["buy"]
        ])
        if total_buy == 0:
            return None

        return result
    except Exception as e:
        print(f"FII/DII error: {e}")
        return None

def format_fii_dii(data):
    fp  = data["fii_pro"]
    fc  = data["fii_client"]
    dp  = data["dii_pro"]
    dc  = data["dii_client"]

    fii_total_net = round(fp["net"] + fc["net"], 2)
    dii_total_net = round(dp["net"] + dc["net"], 2)
    grand_total   = round(fii_total_net + dii_total_net, 2)

    def e(v): return "🟢" if v >= 0 else "🔴"
    def tag(v): return "Buying 💰" if v >= 0 else "Selling 🚨"

    sentiment = "BULLISH 📈" if grand_total >= 0 else "BEARISH 📉"

    return f"""📊 **FII / DII TRADE DATA** | {data['date']}
━━━━━━━━━━━━━━━━━━━━
🏦 **FII / FPI**
{e(fp['net'])} Pro — Buy: `₹{fp['buy']:,.2f} Cr` | Sell: `₹{fp['sell']:,.2f} Cr` | **Net: `₹{fp['net']:,.2f} Cr`** ({tag(fp['net'])})
{e(fc['net'])} Client — Buy: `₹{fc['buy']:,.2f} Cr` | Sell: `₹{fc['sell']:,.2f} Cr` | **Net: `₹{fc['net']:,.2f} Cr`** ({tag(fc['net'])})
{e(fii_total_net)} **FII Total Net: `₹{fii_total_net:,.2f} Cr`**

🏛️ **DII**
{e(dp['net'])} Pro — Buy: `₹{dp['buy']:,.2f} Cr` | Sell: `₹{dp['sell']:,.2f} Cr` | **Net: `₹{dp['net']:,.2f} Cr`** ({tag(dp['net'])})
{e(dc['net'])} Client — Buy: `₹{dc['buy']:,.2f} Cr` | Sell: `₹{dc['sell']:,.2f} Cr` | **Net: `₹{dc['net']:,.2f} Cr`** ({tag(dc['net'])})
{e(dii_total_net)} **DII Total Net: `₹{dii_total_net:,.2f} Cr`**

━━━━━━━━━━━━━━━━━━━━
{e(grand_total)} **Grand Total Net Flow: `₹{grand_total:,.2f} Cr`**
📌 **Overall Sentiment: {sentiment}**
━━━━━━━━━━━━━━━━━━━━
*📢 Source: NSE India | Sirf educational purpose ke liye*"""

# =========================
# FII/DII AUTO POST LOOP
# =========================
fii_posted_date = None  # Track karo aaj ka data post hua ya nahi

async def fii_dii_loop():
    global fii_posted_date
    await client.wait_until_ready()
    channel = client.get_channel(NEWS_CHANNEL_ID)
    if not channel:
        return

    print("FII/DII loop started!")

    while not client.is_closed():
        try:
            now      = datetime.now()
            today    = now.strftime("%Y-%m-%d")
            weekday  = now.weekday()
            hour     = now.hour
            minute   = now.minute

            # Only on weekdays, between 6 PM and 8 PM IST
            is_time  = (
                weekday < 5 and
                18 <= hour <= 20 and
                fii_posted_date != today
            )

            if is_time:
                print(f"Trying FII/DII fetch at {now.strftime('%H:%M')}")
                data = await asyncio.get_event_loop().run_in_executor(None, fetch_fii_dii)

                if data:
                    msg = format_fii_dii(data)
                    await channel.send(msg)
                    fii_posted_date = today
                    print(f"FII/DII posted for {today}")
                else:
                    print(f"FII/DII data not available yet at {now.strftime('%H:%M')} — will retry in 30 min")

        except Exception as e:
            print(f"FII/DII loop error: {e}")

        # Check every 30 minutes
        await asyncio.sleep(30 * 60)

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    print(f"✅ News Bot ready: {client.user}")
    asyncio.ensure_future(news_loop())
    asyncio.ensure_future(fii_dii_loop())

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.channel.id == NEWS_CHANNEL_ID:
        cmd = message.content.lower().strip()

        if cmd == "news!":
            await message.channel.send("🔍 Fetching latest market news...")
            all_news = await asyncio.get_event_loop().run_in_executor(None, fetch_all_news)
            new_items = [n for n in all_news if get_news_hash(n["title"]) not in posted_hashes]

            if not new_items:
                await message.channel.send("✅ Sab latest news already posted hai!")
                return

            filtered = await asyncio.get_event_loop().run_in_executor(None, ai_filter_and_format, new_items)

            posted = 0
            for ai_info in filtered[:3]:
                idx = ai_info.get("index", 1) - 1
                if idx < 0 or idx >= len(new_items):
                    continue
                news_item = new_items[idx]
                if ai_info.get("score", 0) < 4:
                    continue
                msg = format_news_message(news_item, ai_info)
                await message.channel.send(msg)
                posted_hashes.add(get_news_hash(news_item["title"]))
                posted += 1
                await asyncio.sleep(2)

            if posted == 0:
                await message.channel.send("📭 Abhi koi naya relevant market news nahi hai.")

        elif cmd == "fii!":
            await message.channel.send("⏳ Fetching FII/DII data from NSE...")
            data = await asyncio.get_event_loop().run_in_executor(None, fetch_fii_dii)
            if data:
                await message.channel.send(format_fii_dii(data))
            else:
                await message.channel.send("❌ FII/DII data abhi available nahi hai. NSE usually 6 PM ke baad deta hai!")

        elif cmd == "help!":
            await message.channel.send("""📋 **NEWS BOT COMMANDS**
`news!` — Abhi latest market news fetch karo
`help!` — Yeh menu

🤖 Auto news har 30 min mein aata hai!
📢 *Sirf educational purpose ke liye*""")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("Starting News Bot...")
    client.run(DISCORD_TOKEN)
