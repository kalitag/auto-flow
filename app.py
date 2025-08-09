# app.py
import os
import re
import asyncio
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from playwright.async_api import async_playwright, Playwright
from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

# --- Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN") # Get from Render environment variables
WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL_BASE") # Get from Render environment variables

# Ensure BOT_TOKEN is set before proceeding
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is not set. Bot will not function correctly.")
    # Exit or handle this more gracefully depending on desired behavior.
    # For a web app, it's fine if it launches, but bot functions will fail.

WEBHOOK_PATH = f"/{BOT_TOKEN}" if BOT_TOKEN else "/dummy_path" # Use dummy if token missing

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0") 

SUPPORTED_SHORTENERS = [
    "cutt.ly", "spoo.me", "amzn-to.co", "fkrt.cc", "bitli.in", "da.gd", "wishlink.com"
]

AFFILIATE_PARAMS = [
    "tag", "ref", "aff_id", "partner_id", "linkCode", "camp", "creative",
    "creativeASIN", "ascsubtag", "utm_source", "utm_medium", "utm_campaign",
    "fbclid", "_encoding", "psc", "coliid", "colid", "sr_p_7"
]

PRICE_PATTERN = re.compile(r'[₹Rs]{1,2}\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', re.IGNORECASE)

GENDER_TAGS = ["men", "women", "kids", "unisex"]
QUANTITY_TAGS = ["pack of", "set of", "pcs", "kg", "ml", "g", "quantity"]

MEESHO_FALLBACK_PIN = "110001"

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Celery App Initialization ---
celery_app = Celery(
    "deal_bot_tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=['app']
)

playwright_context = {}

@worker_process_init.connect
def init_playwright(**kwargs):
    """Initializes Playwright browser context when a Celery worker process starts."""
    global playwright_context
    logger.info("Initializing Playwright in Celery worker process.")
    # Celery workers already have an event loop, so we run the async part in a new task.
    # This prevents blocking the worker's startup.
    async def _init_playwright_async_wrapper():
        try:
            pw = await async_playwright().start()
            playwright_context['pw'] = pw
            playwright_context['browser'] = await pw.chromium.launch(headless=True)
            logger.info("Playwright browser launched successfully.")
        except Exception as e:
            logger.error(f"Failed to launch Playwright browser in worker: {e}", exc_info=True)
            # Depending on severity, might re-raise or set a flag to skip Playwright tasks
            raise # Re-raise to ensure worker doesn't start in a broken state

    asyncio.get_event_loop().run_until_complete(_init_playwright_async_wrapper())


@worker_process_shutdown.connect
def close_playwright(**kwargs):
    """Closes Playwright browser context when a Celery worker process shuts down."""
    global playwright_context
    if 'browser' in playwright_context and playwright_context['browser']:
        logger.info("Closing Playwright browser in Celery worker process.")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(playwright_context['browser'].close())
        loop.run_until_complete(playwright_context['pw'].stop())
        logger.info("Playwright browser closed.")

# --- Telegram Bot Application Initialization ---
# Initialize with the bot token. This `application` instance is used by both Flask and Celery.
application = Application.builder().token(BOT_TOKEN).arbitrary_callback_data(True).build()


# --- Utility Functions ---

def extract_product_link(message_text: str) -> str | None:
    url_pattern = re.compile(
        r'(https?://(?:www\.)?|www\.)?'
        r'(?:[a-zA-Z0-9-]+\.)+'
        r'[a-zA-Z]{2,6}'
        r'(?:/[^\s]*)?'
    )
    match = url_pattern.search(message_text)
    if match:
        url = match.group(0)
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url if url.startswith('www.') else 'https://' + url
        logger.info(f"Extracted initial URL: {url}")
        return url
    return None

async def unshorten_url(short_url: str) -> str:
    parsed_url = urlparse(short_url)
    if not parsed_url.netloc:
        logger.warning(f"Invalid URL for unshortening (no netloc): {short_url}")
        return short_url

    if not any(shortener in parsed_url.netloc for shortener in SUPPORTED_SHORTENERS):
        logger.info(f"URL '{short_url}' is not from a known shortener. Skipping unshorten.")
        return short_url

    try:
        response = requests.head(short_url, allow_redirects=True, timeout=10)
        response.raise_for_status()
        final_url = response.url
        logger.info(f"Unshortened '{short_url}' to '{final_url}'")
        return final_url
    except requests.exceptions.RequestException as e:
        logger.error(f"Error unshortening URL '{short_url}': {e}")
        return short_url

    def strip_affiliate_tags(url: str) -> str:
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        cleaned_params = {
            key: value for key, value in query_params.items()
            if key.lower() not in AFFILIATE_PARAMS
        }
        cleaned_query = urlencode(cleaned_params, doseq=True)
        cleaned_url = urlunparse(parsed_url._replace(query=cleaned_query))
        logger.info(f"Stripped affiliate tags from '{url}' to '{cleaned_url}'")
        return cleaned_url

    async def scrape_product_info_playwright(url: str, message_caption: str | None = None) -> dict:
        global playwright_context
        product_info = {
            "title": "N/A",
            "price": "N/A",
            "sizes": "N/A",
            "pin": "N/A",
            "link": url
        }

        if 'browser' not in playwright_context or not playwright_context['browser']:
            logger.error("Playwright browser is not initialized in this worker process. Cannot scrape.")
            return product_info

        page = None # Initialize page to None
        try:
            page = await playwright_context['browser'].new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_selector('body', state='attached', timeout=30000)

            html_content = await page.content()
            soup = BeautifulSoup(html_content, 'lxml')

            if message_caption:
                product_info["title"] = message_caption
                product_info["title"] = " ".join(product_info["title"].split())
                for tag in GENDER_TAGS:
                    if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                        if not product_info["title"].lower().startswith(tag):
                            product_info["title"] = f"{tag.capitalize()} {product_info['title']}"
                        break
                for tag in QUANTITY_TAGS:
                    if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                        if not product_info["title"].lower().startswith(tag):
                            product_info["title"] = f"{tag.capitalize()} {product_info['title']}"
                        break
                logger.info(f"Using message caption for title: '{product_info['title']}'")
            
            if product_info["title"] == "N/A":
                title_meta = soup.find('meta', property='og:title')
                if title_meta and title_meta.get('content'):
                    product_info["title"] = title_meta['content']
                else:
                    title_tag = soup.find('title')
                    if title_tag and title_tag.string:
                        product_info["title"] = title_tag.string
                    else:
                        h1_tag = soup.find('h1')
                        if h1_tag and h1_tag.string:
                            product_info["title"] = h1_tag.string
                
                if product_info["title"] != "N/A":
                    product_info["title"] = re.sub(r'\|\s*Amazon\.in|\s*Online at Best Price|\s*-\s*Buy Online.*', '', product_info["title"], flags=re.IGNORECASE).strip()
                    final_prefixes = []
                    for tag in GENDER_TAGS:
                        if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                            final_prefixes.append(tag.capitalize())
                            product_info["title"] = re.sub(r'\b' + re.escape(tag) + r'\b', '', product_info["title"], flags=re.IGNORECASE).strip()
                            break
                    for tag in QUANTITY_TAGS:
                        if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                            final_prefixes.append(tag.capitalize())
                            product_info["title"] = re.sub(r'\b' + re.escape(tag) + r'\b', '', product_info["title"], flags=re.IGNORECASE).strip()
                            break
                    product_info["title"] = " ".join(final_prefixes + [product_info["title"]]).strip()

            logger.info(f"Scraped title: '{product_info['title']}'")

            price_candidates = soup.find_all(text=PRICE_PATTERN)
            if not price_candidates:
                price_candidates.extend(soup.find_all(class_=re.compile(r'price|product-price|offer-price|final-price|selling-price|current-price', re.IGNORECASE)))
            
            for candidate in price_candidates:
                price_match = PRICE_PATTERN.search(str(candidate))
                if price_match:
                    raw_price = price_match.group(1).replace(',', '')
                    product_info["price"] = raw_price
                    logger.info(f"Scraped price: '{product_info['price']}'")
                    break

            available_sizes = []
            size_labels_list = ["S", "M", "L", "XL", "XXL", "XXXL", "Free Size", "One Size"]

            size_elements = soup.find_all(lambda tag: tag.name in ['span', 'div', 'li', 'a'] and
                                           any(cls in (tag.get('class', []) or []) for cls in ['size-label', 'product-size-label', 'size-variant', 'selector-item']) or
                                           any(attr in tag.attrs for attr in ['data-size', 'data-value']))

            for element in size_elements:
                text = element.get_text(strip=True)
                if text.upper() in [s.upper() for s in size_labels_list]:
                    if 'unavailable' not in element.get('class', []) and 'disabled' not in element.get('class', []):
                        available_sizes.append(text)
            
            if available_sizes:
                unique_sizes = sorted(list(set(available_sizes)), key=lambda x: size_labels_list.index(x.upper()) if x.upper() in [s.upper() for s in size_labels_list] else len(size_labels_list))
                if len(unique_sizes) >= len(size_labels_list) - 2:
                     product_info["sizes"] = "All"
                else:
                    product_info["sizes"] = ", ".join(unique_sizes)
            else:
                product_info["sizes"] = "Not Found"

            logger.info(f"Scraped sizes: '{product_info['sizes']}'")

            if "meesho.com" in url:
                product_info["pin"] = MEESHO_FALLBACK_PIN
                logger.info(f"Detected Meesho link, applying fallback pin: '{product_info['pin']}'")
            else:
                product_info["pin"] = "N/A"

        except Exception as e:
            logger.error(f"Error scraping with Playwright for '{url}': {e}", exc_info=True)
            if page and not page.is_closed():
                await page.close()
            product_info["title"] = "❌ Unable to extract title"
            product_info["price"] = "N/A"
            product_info["sizes"] = "N/A"
            product_info["pin"] = "N/A"
        finally:
            if page and not page.is_closed():
                await page.close() # Ensure page is closed even if an error occurs
        
        return product_info

    # --- Celery Task Definition ---
    @celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
    async def process_deal_task(self, chat_id: int, link: str, message_caption: str | None = None) -> None:
        try:
            unshortened_link = await unshorten_url(link)
            clean_link = strip_affiliate_tags(unshortened_link)
            product_data = await scrape_product_info_playwright(clean_link, message_caption)

            formatted_lines = []
            display_title = product_data["title"]
            display_title = re.sub(r'\[(Men|Women|Kids|Unisex|Pack of|Set of|Pcs|Kg|Ml|G|Quantity)\]\s*', '', display_title, flags=re.IGNORECASE).strip()

            final_prefixes = []
            for tag in GENDER_TAGS:
                if re.search(r'\b' + re.escape(tag) + r'\b', display_title, re.IGNORECASE):
                    final_prefixes.append(tag.capitalize())
                    display_title = re.sub(r'\b' + re.escape(tag) + r'\b', '', display_title, flags=re.IGNORECASE).strip()
                    break
            for tag in QUANTITY_TAGS:
                if re.search(r'\b' + re.escape(tag) + r'\b', display_title, re.IGNORECASE):
                    final_prefixes.append(tag.capitalize())
                    display_title = re.sub(r'\b' + re.escape(tag) + r'\b', '', display_title, flags=re.IGNORECASE).strip()
                    break
            
            final_output_title = " ".join(final_prefixes + [display_title.strip()]).strip()

            formatted_lines.append(f"{final_output_title} @{product_data['price']} rs")
            formatted_lines.append(product_data['link'])

            if product_data["sizes"] != "N/A" and product_data["sizes"] != "Not Found":
                formatted_lines.append(f"\nSize - {product_data['sizes']}")
            else:
                logger.info("Skipping 'Size' line as no valid sizes were found.")

            if "meesho.com" in clean_link and product_data["pin"] != "N/A":
                formatted_lines.append(f"Pin - {product_data['pin']}")
            else:
                logger.info("Skipping 'Pin' line as it's not a Meesho link or pin is N/A.")
            
            formatted_lines.append("\n@reviewcheckk")

            final_response = "\n".join(formatted_lines)
            
            await application.bot.send_message(chat_id=chat_id, text=final_response)

        except Exception as e:
            logger.exception(f"Celery task failed for chat ID {chat_id}, link '{link}': {e}")
            try:
                raise self.retry(exc=e)
            except self.MaxRetriesExceededError:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text="❌ I tried my best, but couldn't process this link after multiple attempts. "
                         "The website might be tricky or there's a temporary issue. Please try another link. 🚧"
                )

    # --- Telegram Handlers ---

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Hello! I'm your Deal-bot. 🤖 Forward me a message with a product link, "
            "or type a message containing one, and I'll try to extract the deal info for you. "
            "For best results, forward messages with images! ✨"
        )

    async def process_product_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        message_text = message.text or message.caption

        if not message_text:
            if message.forward_from or message.forward_sender_name:
                 if message.photo:
                    logger.info("Forwarded message with image detected, but no text/caption. Attempting to extract link from image metadata or external sources if possible (not implemented).")
                    await message.reply_text("🔎 Trying to analyze the image... (Note: Image-only link detection is limited.)")
                 else:
                    await message.reply_text("⚠️ No product link detected in the message.")
                    return
            else:
                await message.reply_text("⚠️ No product link detected in the message.")
                return

        product_link = None
        if message.entities:
            for entity in message.entities:
                if entity.type == 'text_link' and entity.url:
                    product_link = entity.url
                    break
                elif entity.type == 'url':
                    product_link = message_text[entity.offset : entity.offset + entity.length]
                    break
        
        if not product_link:
            product_link = extract_product_link(message_text)

        if not product_link:
            await message.reply_text("⚠️ No product link detected.")
            logger.info(f"No product link detected in message from chat ID: {update.effective_chat.id}")
            return

        await message.reply_text("⏳ Processing your link... This might take a moment. 🚀")
        logger.info(f"Detected link: '{product_link}' from chat ID: {update.effective_chat.id}. Dispatching task.")

        process_deal_task.delay(update.effective_chat.id, product_link, message.caption)

    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(msg="Exception while handling an update:", exc_info=context.error)
        if update.effective_message:
            await update.effective_message.reply_text(
                "An unexpected error occurred with the bot. We've been notified! Please try again later. 🛠️"
            )

    # --- Register Handlers ---
    def setup_handlers(app_instance: Application):
        app_instance.add_handler(MessageHandler(filters.COMMAND, start))
        app_instance.add_handler(MessageHandler(
            filters.TEXT | filters.PHOTO & filters.FORWARDED | filters.FORWARDED & filters.TEXT | filters.FORWARDED & filters.CAPTION,
            process_product_link
        ))
        app_instance.add_error_handler(error_handler)

    # --- Flask Webhook Route ---
    @app.route(WEBHOOK_PATH, methods=["POST"])
    async def webhook_handler():
        if request.method == "POST":
            update_json = request.get_json()
            if not update_json:
                logger.warning("Received empty or invalid JSON from webhook.")
                abort(400)

            try:
                update = Update.de_json(update_json, application.bot)
                await application.process_update(update)
                return "ok"
            except Exception as e:
                logger.error(f"Error processing Telegram update in webhook: {e}", exc_info=True)
                abort(500)
        return "ok"

    # --- Main Application Logic for Local Polling ---
    # This block is for local development and direct execution only (not for Render's Gunicorn).
    if __name__ == '__main__':
        logger.info("Starting bot in local polling mode...")
        # Set up handlers for the application instance
        setup_handlers(application)
        # Run the bot in polling mode (continuously check for updates)
        application.run_polling(poll_interval=1.0, timeout=30)
