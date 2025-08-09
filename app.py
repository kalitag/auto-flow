import os
import re
import requests
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, ContextTypes
from bs4 import BeautifulSoup

# Flask app init
app = Flask(__name__)

# Environment variables from Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

bot = Bot(token=BOT_TOKEN)

# Function: unshort any short URL
def unshort_url(url):
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        return resp.url
    except:
        return url

# Function: scrape product details
def scrape_product(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.find("title").text.strip() if soup.find("title") else "N/A"
        price = None
        for tag in soup.find_all(text=re.compile(r"₹\s?\d+")):
            price = tag.strip()
            break

        return {
            "title": title,
            "price": price if price else "N/A",
            "url": url
        }
    except Exception as e:
        return {"error": str(e)}

# Detect gender and quantity
def detect_gender_quantity(text):
    gender = "Unknown"
    quantity = "Unknown"

    if re.search(r"\b(men|male|boy)\b", text, re.I):
        gender = "Male"
    elif re.search(r"\b(women|female|girl)\b", text, re.I):
        gender = "Female"

    qty_match = re.search(r"\b(\d+)\s?(pcs|pieces|item|qty)?\b", text, re.I)
    if qty_match:
        quantity = qty_match.group(1)

    return gender, quantity

# Telegram webhook endpoint
@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    message_text = update.message.text

    # Extract first URL
    urls = re.findall(r'(https?://\S+)', message_text)
    if not urls:
        update.message.reply_text("❌ No URL found in your message.")
        return "ok"

    # Process first URL
    short_url = urls[0]
    final_url = unshort_url(short_url)

    # Scrape
    product_data = scrape_product(final_url)

    # Detect gender & quantity
    gender, quantity = detect_gender_quantity(message_text)

    # Build reply JSON
    reply_data = {
        "gender": gender,
        "quantity": quantity,
        "product": product_data
    }

    update.message.reply_text(f"✅ Data:\n```{reply_data}```", parse_mode="Markdown")

    return "ok"

# Set webhook automatically
@app.before_first_request
def set_webhook():
    bot.delete_webhook()
    bot.set_webhook(f"{WEBHOOK_URL}/webhook")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
