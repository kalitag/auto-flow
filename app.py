"""
Fixed and simplified Auto-Flow Telegram bot suitable for webhook deployment (Render/Heroku-like).
- Uses Flask to receive Telegram webhook updates
- Avoids complicated telegram-ext asynchronous integration by replying via Telegram HTTP API (requests)
- Robust URL unshortening, affiliate stripping, product parsing heuristics

Usage:
- Set environment variables: TELEGRAM_BOT_TOKEN and RENDER_BASE_URL (e.g. https://your-app.onrender.com)
- Deploy to Render (or any host), set service port (Render provides PORT env)
- Visit / to check health
- Telegram webhook will be set at startup automatically

Requirements:
flask
requests
beautifulsoup4

Save as `auto_flow_bot.py` and run with `python auto_flow_bot.py` (in production Render will run it for you).
"""

import os
import re
import logging
import requests
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from urllib.parse import urlsplit, urlunsplit

# ---------- Configuration ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8465346144:AAGSHC77UkXVZZTUscbYItvJxgQbBxmFcWo"
RENDER_BASE = os.environ.get("RENDER_BASE_URL") or os.environ.get("RENDER_URL") or "https://auto-flow-k6sb.onrender.com"
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_BASE.rstrip('/')}{WEBHOOK_PATH}"
PORT = int(os.environ.get("PORT", 10000))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------- Constants / Heuristics ----------
SHORTENERS = ["cutt.ly", "spoo.me", "amzn-to.co", "fkrt.cc", "bitli.in", "da.gd", "wishlink.com"]
AFFILIATE_TAGS = ["tag=", "affid=", "utm_", "ref=", "linkCode=", "ascsubtag=", "affsource=", "affExtParam1="]
SIZE_LABELS = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
GENDER_KEYWORDS = ["men", "women", "kids", "unisex"]
QUANTITY_PATTERNS = [r"pack of \d+", r"set of \d+", r"\d+\s?pcs", r"\d+\s?kg", r"\d+\s?ml", r"\d+\s?g", r"quantity \d+"]

app = Flask(__name__)

# ---------- Helper functions ----------

def safe_request_get(url, timeout=8, allow_redirects=True, headers=None):
    headers = headers or {"User-Agent": "Mozilla/5.0 (compatible; AutoFlow/1.0)"}
    try:
        return requests.get(url, timeout=timeout, allow_redirects=allow_redirects, headers=headers)
    except requests.RequestException as e:
        logging.debug(f"GET request failed for {url}: {e}")
        return None


def unshorten_link(url):
    try:
        parsed = urlsplit(url)
        netloc = parsed.netloc.lower()
        if any(s in netloc for s in SHORTENERS):
            # follow redirects with GET (some shorteners don't respond to HEAD)
            r = safe_request_get(url, timeout=8, allow_redirects=True)
            if r and r.url:
                return r.url
        return url
    except Exception:
        return url


def strip_affiliate(url):
    try:
        if "?" not in url:
            return url
        base, query = url.split("?", 1)
        parts = [p for p in query.split("&") if not any(tag in p for tag in AFFILIATE_TAGS)]
        return base + ("?" + "&".join(parts) if parts else "")
    except Exception:
        return url


def extract_title(soup, fallback="T-shirt"):
    try:
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            return og["content"].strip()
    except Exception:
        pass
    return fallback


def clean_title(title):
    extra_words = r"(?i)\b(buy|best price|online|deal|discount|offer|brand new|free shipping)\b"
    t = re.sub(extra_words, "", title)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def detect_gender(title):
    tl = title.lower()
    for g in GENDER_KEYWORDS:
        if g in tl:
            return g.capitalize()
    return ""


def detect_quantity(title):
    tl = title.lower()
    for p in QUANTITY_PATTERNS:
        m = re.search(p, tl)
        if m:
            return m.group(0)
    return ""


def extract_price(text):
    # Accept variants like ₹599, Rs 599, INR 599
    m = re.search(r"(?:₹|Rs\.?|INR)\s?(?P<p>\d{2,7})", text)
    if m:
        return m.group("p")
    # fallback default
    return "599"


def extract_sizes(soup, text):
    sizes = set()
    # Check common size spans/buttons
    for el in soup.find_all(text=True):
        txt = el.strip()
        if txt in SIZE_LABELS:
            sizes.add(txt)
    # Check in page text as fallback
    for s in SIZE_LABELS:
        if re.search(fr"\b{s}\b", text):
            sizes.add(s)
    if not sizes:
        return ["S", "M"]
    return sorted(sizes)


def detect_pin(msg_text, page_text, url):
    if "meesho.com" not in url.lower():
        return ""
    m = re.search(r"\b(\d{6})\b", msg_text) or re.search(r"\b(\d{6})\b", page_text)
    if m:
        return f"Pin - {m.group(1)}"
    return "Pin - 110001"


def get_first_url_from_text(text):
    if not text:
        return None
    urls = re.findall(r"https?://[\w\-./?=&%#:;~]+", text)
    return urls[0] if urls else None


# ---------- Product extraction (network) ----------

def extract_product_info(url, title_hint=None):
    try:
        r = safe_request_get(url, timeout=8)
        if not r or r.status_code != 200:
            return (title_hint or "T-shirt", "599", ["S", "M"], "")
        soup = BeautifulSoup(r.content, "html.parser")
        page_text = r.text
        title = clean_title(extract_title(soup, title_hint or "T-shirt"))
        price = extract_price(page_text)
        sizes = extract_sizes(soup, page_text)
        return (title, price, sizes, page_text)
    except Exception as e:
        logging.debug(f"extract_product_info failed for {url}: {e}")
        return (title_hint or "T-shirt", "599", ["S", "M"], "")


# ---------- Telegram helpers ----------

def telegram_send_message(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=8)
        if not resp.ok:
            logging.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logging.error(f"Failed to send message: {e}")


# ---------- Flask routes ----------

@app.route("/", methods=["GET"])
def health():
    return "Auto-Flow Bot is running.", 200


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    logging.info("Webhook update received")
    data = request.get_json(force=True, silent=True)
    if not data:
        logging.warning("Empty JSON in webhook")
        return jsonify({"ok": False}), 400

    # Minimal safe parsing of incoming update
    msg = data.get("message") or data.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = msg.get("text") or msg.get("caption") or ""

    # If there's no chat or text, just acknowledge
    if not chat_id:
        return jsonify({"ok": True}), 200

    try:
        url = get_first_url_from_text(text)
        if not url:
            telegram_send_message(chat_id, "⚠️ No product link detected. Send a product URL or caption with link.")
            return jsonify({"ok": True}), 200

        unshort = unshorten_link(url)
        clean_url = strip_affiliate(unshort)
        title_hint = msg.get("caption")
        title, price, sizes, page_text = extract_product_info(clean_url, title_hint)

        if not title or not price:
            reply = f"❌ Unable to extract product info." if not title_hint else f"🖼️ {title_hint}\n❌ Unable to extract product info."
            telegram_send_message(chat_id, reply)
            return jsonify({"ok": True}), 200

        gender = detect_gender(title)
        quantity = detect_quantity(title)
        size_line = "Size - All" if len(sizes) >= len(SIZE_LABELS) else f"Size - {', '.join(sizes)}" if sizes else ""
        pin_line = detect_pin(text, page_text, clean_url)

        formatted = f"{gender} {quantity} {title} @{price} rs\n{clean_url}"
        if size_line:
            formatted += f"\n\n{size_line}"
        if pin_line:
            formatted += f"\n{pin_line}"
        formatted += "\n\n@reviewcheckk"

        # tidy whitespace and remove currency symbols to match old behavior
        final_text = re.sub(r"\s+", " ", formatted).strip().replace("₹", "").replace("Rs", "")
        telegram_send_message(chat_id, final_text)
    except Exception as e:
        logging.exception(f"Error handling update: {e}")
        fallback = (
            "Error, falling back: Men Pack of 2 T-shirt @599 rs\nhttps://example.com\n\nSize - S, M\nPin - 110001\n\n@reviewcheckk"
        )
        telegram_send_message(chat_id, fallback)

    return jsonify({"ok": True}), 200


# ---------- Startup: set webhook ----------

def set_webhook():
    try:
        # Preferred: use Telegram Bot API call directly
        r = requests.get(f"{TELEGRAM_API}/setWebhook?url={WEBHOOK_URL}", timeout=8)
        if r.ok:
            logging.info(f"Webhook set to {WEBHOOK_URL}")
        else:
            logging.error(f"Failed to set webhook via API: {r.status_code} {r.text}")
    except Exception as e:
        logging.exception(f"set_webhook failed: {e}")


if __name__ == "__main__":
    logging.info("Starting Auto-Flow Bot")
    set_webhook()
    # Start Flask app
    app.run(host="0.0.0.0", port=PORT, debug=False)
