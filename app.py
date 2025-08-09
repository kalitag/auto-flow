# main.py
import os
import re
import json
import requests
import asyncio
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from telegram import Update, Bot, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# CONFIG (from env)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "CHANGE_ME")
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "https://auto-flow-k6sb.onrender.com")
PORT = int(os.environ.get("PORT", "8080"))

if BOT_TOKEN == "CHANGE_ME":
    raise RuntimeError("Set BOT_TOKEN env var before running")

app = Flask(__name__)

# build PTB application (we don't run polling, we only process updates)
application = ApplicationBuilder().token(BOT_TOKEN).build()

# ------------ Utilities ------------
URL_REGEX = re.compile(r"(https?://[^\s]+)")
SHORTENER_DOMAINS = [
    "cutt.ly", "spoo.me", "amzn-to.co", "fkrt.cc", "bitli.in", "da.gd", "wishlink.com",
    "bit.ly", "t.co", "tinyurl.com"
]

def find_urls(text: str):
    if not text:
        return []
    return URL_REGEX.findall(text)

def unshorten_url(url: str, timeout=8):
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        return r.url
    except Exception:
        try:
            r = requests.get(url, allow_redirects=True, timeout=timeout)
            return r.url
        except Exception:
            return url

def clean_title(raw: str):
    if not raw:
        return ""
    # take og:title or <title> fallback and strip site suffixes like " - Flipkart"
    t = re.sub(r"[\n\r\t]+", " ", raw).strip()
    t = re.sub(r"\s{2,}", " ", t)
    # drop site names after common separators
    t = re.split(r"\s[\|\-–:]\s", t)[0]
    return t.strip()

def extract_price(text):
    if not text:
        return None
    # look for ₹ or Rs or INR followed by numbers and commas
    m = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9\.,]+)", text)
    if m:
        raw = m.group(1)
        digits = re.sub(r"[^\d]", "", raw)
        return digits if digits else None
    # fallback: any 3+ digit number with commas
    m2 = re.search(r"([0-9,]{3,})", text)
    if m2:
        return re.sub(r"[^\d]", "", m2.group(1))
    return None

def extract_sizes(soup):
    # very heuristic: look for common size strings in text content or select options
    text = soup.get_text(separator=" ").upper()
    found = []
    for s in ["XS","S","M","L","XL","XXL","XXXL"]:
        if re.search(r"\b" + re.escape(s) + r"\b", text):
            found.append(s)
    if found:
        # unique and maintain SML order as in labels
        labels = ["XS","S","M","L","XL","XXL","XXXL"]
        out = [l for l in labels if l in found]
        return ", ".join(out)
    return None

def extract_pin_from_text(text):
    if not text:
        return None
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1)
    return None

async def format_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    urls = find_urls(text)
    # if message contains multiple URLs, pick one (first)
    if not urls and msg.entities:
        # try message entities for url
        for ent in msg.entities:
            if ent.type in ("url","text_link"):
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
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        await msg.reply_text("❌ Unable to fetch the product page.")
        return

    # title: prefer og:title, then <title>
    og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name":"og:title"})
    if og and og.get("content"):
        title = clean_title(og["content"])
    else:
        ttag = soup.title.string if soup.title else ""
        title = clean_title(ttag)

    # sometimes forwarded image caption contains title — prefer message caption if available
    if msg.forward_from or msg.caption:
        # use caption as a candidate
        caption_candidate = msg.caption or ""
        # prefer if caption length between 6 and 200 chars
        if 6 < len(caption_candidate) < 200:
            title = clean_title(caption_candidate)

    # price: search in page text and meta tags
    price = None
    # try meta: og:price:amount
    meta_price = soup.find("meta", attrs={"property":"product:price:amount"}) or soup.find("meta", attrs={"name":"price"})
    if meta_price and meta_price.get("content"):
        price = re.sub(r"[^\d]", "", meta_price["content"])
    if not price:
        price = extract_price(soup.get_text())

    sizes = extract_sizes(soup)
    # pin: only for meesho links per your spec (best-effort)
    pin = None
    if "meesho" in full_url:
        pin = extract_pin_from_text(text) or extract_pin_from_text(soup.get_text()) or "110001"
    else:
        pin = extract_pin_from_text(text) or None

    # build structured output
    # detect gender and quantity heuristics from text/title
    gender = ""
    low = (title + " " + (text or "")).lower()
    if any(k in low for k in ["men ", "for men", "mens", "boy"]):
        gender = "men"
    elif any(k in low for k in ["women", "for women", "ladies", "girls"]):
        gender = "women"
    elif any(k in low for k in ["kids","child","children"]):
        gender = "kids"
    else:
        gender = ""

    quantity = ""
    qmatch = re.search(r"\b(pack of|set of|(\d+)\s?pcs|(\d+)\s?pack)\b", low)
    if qmatch:
        quantity = qmatch.group(0)
    # assemble first line
    first_line_parts = []
    if gender:
        first_line_parts.append(gender)
    if quantity:
        first_line_parts.append(quantity)
    if title:
        first_line_parts.append(title)
    first_line = " ".join(first_line_parts).strip()
    if price:
        first_line = f"{first_line} @{price} rs".strip()
    else:
        first_line = f"{first_line}".strip()

    sizes_line = f"Size - {sizes}" if sizes else ""
    pin_line = f"Pin - {pin}" if pin else ""

    out_lines = [first_line, full_url, ""]
    if sizes_line:
        out_lines.append(sizes_line)
    if pin_line:
        out_lines.append(pin_line)
    out_lines.append("")
    out_lines.append("@reviewcheckk")

    final_text = "\n".join([ln for ln in out_lines if ln is not None and ln != ""])

    # final cleanup rules
    final_text = final_text.replace("₹", "")  # ensure no ₹
    await msg.reply_text(final_text, disable_web_page_preview=False)

# register handler
product_filter = filters.TEXT | filters.Caption() | filters.Entity("url") | filters.Entity("text_link")
application.add_handler(MessageHandler(product_filter, lambda u,c: asyncio.create_task(format_and_reply(u,c))))

# ------------- Flask webhook endpoint -------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        # Telegram sends JSON
        pass
    data = request.get_json(force=True)
    bot = application.bot
    update = Update.de_json(data, bot)
    # schedule PTB to process the update asynchronously
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.create_task(application.process_update(update))
    return ("", 200)

# Health check for Render
@app.route("/", methods=["GET"])
def index():
    return {"status": "ok", "service": "Deal-bot"}, 200

# helper to set webhook once (call during container start)
def set_telegram_webhook():
    webhook_url = f"{WEBHOOK_BASE}/{BOT_TOKEN}"
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    r = requests.post(api, data={"url": webhook_url}, timeout=10)
    try:
        result = r.json()
    except Exception:
        result = {"ok": False, "status_code": r.status_code, "text": r.text}
    print("setWebhook response:", result)
    return result

if __name__ == "__main__":
    # set webhook (best-effort). On Render, this will run on container start.
    print("Setting webhook to:", f"{WEBHOOK_BASE}/{BOT_TOKEN}")
    try:
        set_telegram_webhook()
    except Exception as e:
        print("Could not set webhook:", e)
    # run Flask (Render will use gunicorn; this is for local dev)
    app.run(host="0.0.0.0", port=PORT)
