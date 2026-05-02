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
        "name": "Moneycontrol Markets",
        "url":  "https://www.moneycontrol.com/rss/marketreports.xml",
    },
    {
        "name": "NSE India",
        "url":  "https://www.nseindia.com/api/corporate-announcements?index=equities",
    },
    {
        "name": "Business Standard Markets",
        "url":  "https://www.business-standard.com/rss/markets-106.rss",
    },
    {
        "name": "LiveMint Markets",
        "url":  "https://www.livemint.com/rss/markets",
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

    prompt = f"""You are an Indian stock market news filter. 

From the following news items, select ONLY those that are:
1. Directly related to Indian stock market (NSE, BSE, Nifty, Sensex)
2. About Indian companies (earnings, results, mergers, acquisitions)
3. RBI/SEBI policy decisions affecting markets
4. Global events that SIGNIFICANTLY impact Indian markets (Fed rate, crude oil major moves)
5. IPOs, FII/DII data, bulk deals

EXCLUDE:
- General world news with minimal market impact
- Sports, entertainment, politics (unless directly market related)
- Duplicate or very similar news

For each selected news, provide:
- Relevance score (1-10)
- Market impact: BULLISH / BEARISH / NEUTRAL
- One line summary in simple Hindi/English

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
                if score < 6:
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
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    print(f"✅ News Bot ready: {client.user}")
    asyncio.ensure_future(news_loop())

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
                if ai_info.get("score", 0) < 6:
                    continue
                msg = format_news_message(news_item, ai_info)
                await message.channel.send(msg)
                posted_hashes.add(get_news_hash(news_item["title"]))
                posted += 1
                await asyncio.sleep(2)

            if posted == 0:
                await message.channel.send("📭 Abhi koi naya relevant market news nahi hai.")

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
