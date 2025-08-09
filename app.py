# main.py
"""
Deal-bot (Flask webhook + python-telegram-bot v20.6)

How to use:
 - Do NOT commit a real BOT_TOKEN to git.
 - On Render set env var BOT_TOKEN to your bot token.
 - Optionally set WEBHOOK_BASE to your render domain (default below).
 - Start command (Render): gunicorn main:app -w 2 -b 0.0.0.0:$PORT
"""

import os
import re
import asyncio
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ---------- Configuration ----------
# DO NOT hardcode token here. Set BOT_TOKEN in Render environment variables.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Your provided webhook base (safe to include); can be overridden via env var.
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "https://auto-flow-k6sb.onrender.com")
PORT = int(os.environ.get("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required. Set it in Render.")

# create Flask app
app = Flask(__name__)

# Create the PTB Application (we will NOT call run_polling; we'll process updates via Flask webhook)
application = ApplicationBuilder().token(BOT_TOKEN).build()

# ---------- Utilities ----------
URL_REGEX = re.compile(r"(https?://[^\s\)\]\}]+)")
SHORTENER_DOMAINS = {
    "cutt.ly", "spoo.me", "amzn-to.co", "fkrt.cc", "bitli.in", "da.gd", "wishlink.com",
    "bit.ly", "t.co", "tinyurl.com"
}

def find_urls(text: str):
    if not text:
        return []
    return URL_REGEX.findall(text)

def unshorten_url(url: str, timeout=8):
    try:
        # Prefer HEAD then GET fallback
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code in (200, 301, 302, 303, 307, 308):
            return r.url
    except Exception:
        pass
    try:
        r = requests.get(url, allow_redirects=True, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        return r.url
    except Exception:
        return url

def clean_title(raw: str):
    if not raw:
        return ""
    t = re.sub(r"[\n\r\t]+", " ", raw)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.split(r"\s[\|\-–:]\s", t)[0]
    return t.strip()

def extract_price(text):
    if not text:
        return None
    m = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9\.,]+)", text, flags=re.IGNORECASE)
    if m:
        return re.sub(r"[^\d]", "", m.group(1))
    m2 = re.search(r"([0-9][0-9,\.]{2,})", text)
    if m2:
        return re.sub(r"[^\d]", "", m2.group(1))
    return None

def extract_sizes(soup):
    text = (soup.get_text(separator=" ") or "").upper()
    found = []
    for s in ["XS","S","M","L","XL","XXL","XXXL"]:
        if re.search(r"\b" + re.escape(s) + r"\b", text):
            found.append(s)
    if found:
        order = ["XS","S","M","L","XL","XXL","XXXL"]
        out = [o for o in order if o in found]
        return ", ".join(out)
    # try select/option patterns
    selects = soup.find_all("select")
    for sel in selects:
        opts = [o.get_text(strip=True).upper() for o in sel.find_all("option")]
        sizes = [o for o in opts if re.fullmatch(r"^(XS|S|M|L|XL|XXL|XXXL)$", o)]
        if sizes:
            return ", ".join(sizes)
    return None

def extract_pin_from_text(text):
    if not text:
        return None
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1)
    return None

# ---------- Bot logic ----------
async def format_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "") + " " + (msg.caption or "")
    text = text.strip()
    # Collect URLs from text and entities
    urls = find_urls(text)
    if not urls and msg.entities:
        for ent in msg.entities:
            if ent.type in ("url", "text_link"):
                if ent.type == "text_link":
                    urls.append(ent.url)
                else:
                    urls.append(text[ent.offset:ent.offset+ent.length])
    if not urls:
        await msg.reply_text("⚠️ No product link detected.")
        return

    raw_url = urls[0]
    full_url = unshorten_url(raw_url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Deal-bot/1.0)"}
    try:
        resp = requests.get(full_url, headers=headers, timeout=10)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        await msg.reply_text("❌ Unable to fetch the product page.")
        return

    # Title: prefer OG, then <title>, then caption
    title = ""
    og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name":"og:title"})
    if og and og.get("content"):
        title = clean_title(og["content"])
    elif soup.title and soup.title.string:
        title = clean_title(soup.title.string)
    if msg.caption:
        caption_candidate = msg.caption.strip()
        if 6 < len(caption_candidate) < 200:
            title = clean_title(caption_candidate)

    # Price detection
    price = None
    meta_price = soup.find("meta", attrs={"property":"product:price:amount"}) or soup.find("meta", attrs={"name":"price"})
    if meta_price and meta_price.get("content"):
        price = re.sub(r"[^\d]", "", meta_price["content"])
    if not price:
        price = extract_price(soup.get_text())

    # Sizes & pin
    sizes = extract_sizes(soup)
    pin = None
    # per your spec: for meesho links prefer pin extraction, else not required
    if "meesho" in full_url.lower():
        pin = extract_pin_from_text(text) or extract_pin_from_text(soup.get_text()) or "110001"
    else:
        pin = extract_pin_from_text(text)

    # gender and quantity heuristics
    low = (title + " " + text).lower()
    gender = ""
    if any(k in low for k in [" men ", " for men", "mens", "boy ", "boys "]):
        gender = "men"
    elif any(k in low for k in [" women ", " for women", "ladies", "girls"]):
        gender = "women"
    elif any(k in low for k in [" kids ", "child", "children"]):
        gender = "kids"

    quantity = ""
    qmatch = re.search(r"\b(pack of|set of|(\d+)\s?pcs|(\d+)\s?pack)\b", low)
    if qmatch:
        quantity = qmatch.group(0)

    # assemble first line
    first_parts = []
    if gender:
        first_parts.append(gender)
    if quantity:
        first_parts.append(quantity)
    if title:
        first_parts.append(title)
    first_line = " ".join(first_parts).strip()
    if price:
        first_line = f"{first_line} @{price} rs".strip()

    lines = []
    if first_line:
        lines.append(first_line)
    else:
        lines.append(title or "Deal")
    lines.append(full_url)
    lines.append("")  # blank line
    if sizes:
        lines.append(f"Size - {sizes}")
    if pin:
        lines.append(f"Pin - {pin}")
    lines.append("")  # blank
    lines.append("@reviewcheckk")

    final_text = "\n".join([ln for ln in lines if ln is not None and ln != ""])
    final_text = final_text.replace("₹", "")  # ensure no ₹ symbol

    # Send reply
    try:
        await msg.reply_text(final_text, disable_web_page_preview=False)
    except Exception:
        # fallback: simple text
        await msg.reply_text("❌ Failed to send formatted message. Check bot permissions.")

# Register handler
product_filter = filters.TEXT | filters.Caption() | filters.Entity("url") | filters.Entity("text_link")
application.add_handler(MessageHandler(product_filter, format_and_reply))

# ---------- Flask webhook endpoint ----------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook_listener():
    data = request.get_json(force=True)
    bot = application.bot
    update = Update.de_json(data, bot)
    # schedule processing (async)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.create_task(application.process_update(update))
    return ("", 200)

@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "service": "Deal-bot"}, 200

# Helper to set webhook on startup (best-effort)
def set_telegram_webhook():
    webhook_url = f"{WEBHOOK_BASE.rstrip('/')}/{BOT_TOKEN}"
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    try:
        r = requests.post(api, data={"url": webhook_url}, timeout=12)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

# When run directly, try to set webhook (for dev)
if __name__ == "__main__":
    print("Setting webhook to:", f"{WEBHOOK_BASE}/{BOT_TOKEN}")
    print("setWebhook response:", set_telegram_webhook())
    app.run(host="0.0.0.0", port=PORT)
