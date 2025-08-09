import os
import re
import logging
import requests
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Bot Configuration
BOT_TOKEN = "8465346144:AAGSHC77UkXVZZTUscbYItvJxgQbBxmFcWo"
WEBHOOK_PATH = f"/{BOT_TOKEN}"
RENDER_URL = "https://auto-flow-k6sb.onrender.com"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

# --- Constants
SHORTENERS = ["cutt.ly", "spoo.me", "amzn-to.co", "fkrt.cc", "bitli.in", "da.gd", "wishlink.com"]
AFFILIATE_TAGS = ["tag=", "affid=", "utm_", "ref=", "linkCode=", "ascsubtag=", "affsource=", "affExtParam1="]
SIZE_LABELS = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
GENDER_KEYWORDS = ["men", "women", "kids", "unisex"]
QUANTITY_PATTERNS = [r"(pack of \d+)", r"(set of \d+)", r"(\d+\s?pcs)", r"(\d+\s?kg)", r"(\d+\s?ml)", r"(\d+\s?g)", r"(quantity \d+)"]

# Initialize Flask and Telegram bot
app = Flask(__name__)
bot = Bot(BOT_TOKEN)
application = ApplicationBuilder().token(BOT_TOKEN).build()

# --- Helper Functions
def unshorten_link(url):
    try:
        for shortener in SHORTENERS:
            if shortener in url:
                resp = requests.head(url, allow_redirects=True, timeout=5)
                return resp.url
        return url
    except requests.RequestException:
        return url

def strip_affiliate(url):
    parts = url.split("?")
    if len(parts) < 2:
        return url
    base, query = parts[0], parts[1]
    clean_query = "&".join(p for p in query.split("&") if not any(tag in p for tag in AFFILIATE_TAGS))
    return f"{base}?{clean_query}" if clean_query else base

def extract_title(soup, fallback="T-shirt"):
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    og = soup.find("meta", attrs={"property": "og:title"})
    return og["content"].strip() if og and og.get("content") else fallback

def clean_title(title):
    extra_words = r"(?i)\b(buy|best price|online|deal|discount|offer|brand new)\b"
    return re.sub(r"\s+", " ", re.sub(extra_words, "", title)).strip()

def detect_gender(title):
    return next((g.capitalize() for g in GENDER_KEYWORDS if g in title.lower()), "")

def detect_quantity(title):
    return next((m.group(0) for p in QUANTITY_PATTERNS if (m := re.search(p, title.lower()))), "")

def extract_price(page_text):
    match = re.search(r"(?:₹|Rs)[\s]?(?P<price>\d{2,7})", page_text)
    return match.group("price") if match else "599"

def extract_sizes(soup, page_text):
    sizes = set()
    for span in soup.find_all("span"):
        txt = span.get_text(strip=True)
        if txt in SIZE_LABELS:
            sizes.add(txt)
    for label in SIZE_LABELS:
        if re.search(fr"\b{label}\b", page_text):
            sizes.add(label)
    return sorted(list(sizes)) if sizes else ["S", "M"]

def detect_pin(msg_text, page_text, url):
    if "meesho.com" not in url.lower():
        return ""
    pin_match = re.search(r"\b(\d{6})\b", msg_text)
    if not pin_match:
        pin_match = re.search(r"\b(\d{6})\b", page_text)
    return f"Pin - {pin_match.group(1)}" if pin_match else "Pin - 110001"

def get_title_hint(update):
    return update.message.caption if update.message and update.message.caption else None

def extract_product_info(url, title_hint=None):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        page_text = response.text
        title = clean_title(extract_title(soup, title_hint or "T-shirt"))
        price = extract_price(page_text)
        sizes = extract_sizes(soup, page_text)
        return title, price, sizes, page_text
    except requests.RequestException:
        return title_hint or "T-shirt", "599", ["S", "M"], ""

# --- Message Handler
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    try:
        title_hint = get_title_hint(update)
        text_source = msg.text or title_hint or ""
        urls = re.findall(r"https?://\S+", text_source)
        if not urls:
            await msg.reply_text("⚠️ No product link detected.")
            return
        raw_url = urls[0]
        unshortened_url = unshorten_link(raw_url)
        clean_url = strip_affiliate(unshortened_url)
        title, price, sizes, page_text = extract_product_info(clean_url, title_hint)
        if not title or not price:
            await msg.reply_text(f"🖼️ {title_hint}\n❌ Unable to extract product info." if title_hint else "❌ Unable to extract product info.")
            return
        gender = detect_gender(title)
        quantity = detect_quantity(title)
        size_line = f"Size - All" if len(sizes) >= len(SIZE_LABELS) else f"Size - {', '.join(sizes)}" if sizes else ""
        pin_line = detect_pin(text_source, page_text, clean_url)
        formatted = f"{gender} {quantity} {title} @{price} rs\n{clean_url}"
        if size_line: formatted += f"\n\n{size_line}"
        if pin_line: formatted += f"\n{pin_line}"
        formatted += "\n\n@reviewcheckk"
        await msg.reply_text(re.sub(r"\s+", " ", formatted).strip().replace("₹", "").replace("Rs", ""))
    except Exception as e:
        logging.error(f"Handler error for {update.update_id}: {e}")
        await msg.reply_text("Error, falling back: Men Pack of 2 T-shirt @599 rs\nhttps://example.com\n\nSize - S, M\nPin - 110001\n\n@reviewcheckk")

# --- Startup Response
async def on_start(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = [-1001234567890]  # Replace with your group/channel ID or use a default
    start_message = "🎉 Auto-Flow Bot is live! Send me a product link to get started. @reviewcheckk"
    for chat_id in chat_ids:
        await context.bot.send_message(chat_id=chat_id, text=start_message)

# --- Handlers
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_text))

# --- Webhook Endpoint
@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    logging.info("Webhook update received")
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        application.process_update(update)
        return "OK", 200
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return "Error", 500

# --- Health Endpoint
@app.route("/", methods=["GET"])
def health():
    return "Auto-Flow Bot is running.", 200

# --- Main Execution
if __name__ == "__main__":
    try:
        bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook set to {WEBHOOK_URL}")
        # Schedule startup response (run once after startup)
        application.job_queue.run_once(on_start, when=1.0)  # Delay 1 second to ensure bot is ready
    except Exception as e:
        logging.error(f"Webhook setup failed: {e}")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
