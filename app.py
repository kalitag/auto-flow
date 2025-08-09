import os
import re
import requests
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from bs4 import BeautifulSoup

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

bot = Bot(token=BOT_TOKEN)


# --- Short link ko unshort karne ka function ---
def unshorten_url(url):
    try:
        session = requests.Session()
        resp = session.head(url, allow_redirects=True, timeout=10)
        return resp.url
    except Exception:
        return url


# --- Product details scrape karne ka function ---
def scrape_product(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.find("h1")
        price = soup.find(string=re.compile(r"₹"))
        sizes = [s.get_text(strip=True) for s in soup.find_all("span") if re.search(r"\b[XSML\d]", s.get_text())]

        return {
            "title": title.get_text(strip=True) if title else None,
            "price": price.strip() if price else None,
            "sizes": sizes if sizes else None
        }
    except Exception as e:
        return {"error": str(e)}


# --- Gender detect ---
def detect_gender(text):
    text = text.lower()
    if "men" in text or "male" in text:
        return "Men"
    elif "women" in text or "female" in text:
        return "Women"
    return None


# --- Quantity detect ---
def detect_quantity(text):
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else None


# --- Telegram message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Short link detect
    url_match = re.search(r"https?://\S+", text)
    if not url_match:
        await update.message.reply_text("Koi product link nahi mila.")
        return

    short_url = url_match.group(0)
    full_url = unshorten_url(short_url)

    # Scrape product
    product_data = scrape_product(full_url)

    # Extra info
    product_data["gender"] = detect_gender(text)
    product_data["quantity"] = detect_quantity(text)
    product_data["url"] = full_url

    await update.message.reply_text(f"```json\n{product_data}\n```", parse_mode="Markdown")


# --- Flask webhook route ---
@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    application.update_queue.put_nowait(update)
    return "ok"


if __name__ == "__main__":
    # Telegram bot app
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Set webhook
    bot.set_webhook(WEBHOOK_URL)

    # Run Flask
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
