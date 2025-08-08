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

# --- Configuration ---
# Set up logging for better error visibility in your Render logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables for sensitive data.
# For local testing, you can set these in your shell or a .env file.
# In Render, configure them directly in your service settings.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8465346144:AAGSHC77UkXVZZTUscbYItvJxgQbBxmFcWo") # Your Telegram Bot Token
WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL_BASE", "https://deal-bot-255c.onrender.com/") # Your Render app's URL
WEBHOOK_PATH = f"/{BOT_TOKEN}" # Telegram webhook path uses the bot token for security

# Supported URL shorteners as defined in your plan
SUPPORTED_SHORTENERS = [
    "cutt.ly", "spoo.me", "amzn-to.co", "fkrt.cc", "bitli.in", "da.gd", "wishlink.com"
]

# Common affiliate parameters to strip from URLs
AFFILIATE_PARAMS = [
    "tag", "ref", "aff_id", "partner_id", "linkCode", "camp", "creative",
    "creativeASIN", "ascsubtag", "utm_source", "utm_medium", "utm_campaign",
    "fbclid", "_encoding", "psc", "coliid", "colid", "sr_p_7" # Expanded for common e-commerce
]

# Regex for price detection: matches '₹' or 'Rs' followed by digits (with commas/decimals)
PRICE_PATTERN = re.compile(r'[₹Rs]{1,2}\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', re.IGNORECASE)

# Keywords for gender and quantity tags in titles
GENDER_TAGS = ["men", "women", "kids", "unisex"]
QUANTITY_TAGS = ["pack of", "set of", "pcs", "kg", "ml", "g", "quantity"]

# Fallback pin code for meesho.com links if not detected from the page
MEESHO_FALLBACK_PIN = "110001"

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Telegram Bot Application Initialization ---
# This sets up the python-telegram-bot Application instance.
application = Application.builder().token(BOT_TOKEN).arbitrary_callback_data(True).build()

# --- Utility Functions ---

def extract_product_link(message_text: str) -> str | None:
    """
    Extracts the first valid URL from a message text using a robust regex.
    It attempts to add 'https://' if the scheme is missing.
    """
    url_pattern = re.compile(
        r'(https?://(?:www\.)?|www\.)?'  # Matches http(s):// or www. (optional)
        r'(?:[a-zA-Z0-9-]+\.)+'       # Matches domain name (e.g., example.)
        r'[a-zA-Z]{2,6}'             # Matches TLD (e.g., com, org, in)
        r'(?:/[^\s]*)?'              # Matches path and query parameters (optional)
    )
    match = url_pattern.search(message_text)
    if match:
        url = match.group(0)
        # Prepend https if scheme is missing for better reliability
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url if url.startswith('www.') else 'https://' + url
        logger.info(f"Extracted initial URL: {url}")
        return url
    return None

async def unshorten_url(short_url: str) -> str:
    """
    Unshortens a URL by following redirects using an HTTP HEAD request.
    This is efficient as it avoids downloading the full page.
    Only attempts to unshorten if the URL's domain is a known shortener.
    """
    parsed_url = urlparse(short_url)
    if not parsed_url.netloc:
        logger.warning(f"Invalid URL for unshortening (no netloc): {short_url}")
        return short_url

    # Check if the domain is one of the explicitly supported shorteners
    if not any(shortener in parsed_url.netloc for shortener in SUPPORTED_SHORTENERS):
        logger.info(f"URL '{short_url}' is not from a known shortener. Skipping unshorten.")
        return short_url

    try:
        # Using requests.head with allow_redirects=True to get the final URL
        response = requests.head(short_url, allow_redirects=True, timeout=10)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        final_url = response.url
        logger.info(f"Unshortened '{short_url}' to '{final_url}'")
        return final_url
    except requests.exceptions.RequestException as e:
        logger.error(f"Error unshortening URL '{short_url}': {e}")
        return short_url # Return original URL on error to avoid breaking the process

def strip_affiliate_tags(url: str) -> str:
    """
    Removes common affiliate tracking parameters from a URL's query string.
    """
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    
    # Filter out parameters whose keys (case-insensitive) are in AFFILIATE_PARAMS
    cleaned_params = {
        key: value for key, value in query_params.items()
        if key.lower() not in AFFILIATE_PARAMS
    }
    
    # Reconstruct the URL with the cleaned query parameters
    cleaned_query = urlencode(cleaned_params, doseq=True)
    cleaned_url = urlunparse(parsed_url._replace(query=cleaned_query))
    logger.info(f"Stripped affiliate tags from '{url}' to '{cleaned_url}'")
    return cleaned_url

async def fetch_page_content(url: str) -> str | None:
    """
    Fetches the HTML content of a given URL.
    This method is suitable for static HTML content.
    For JavaScript-rendered content (common on modern e-commerce sites),
    you would need a headless browser (like Playwright or Selenium).
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching content from '{url}': {e}")
        return None

async def scrape_product_info(url: str, message_caption: str | None = None) -> dict:
    """
    Scrapes product title, price, sizes, and pin code information from the given URL.
    This scraper is designed for speed and basic HTML parsing.
    
    IMPORTANT: For dynamic content (e.g., prices/sizes loaded via JS),
    you would need to integrate a headless browser like Playwright here.
    Example (simplified):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url)
        # Wait for dynamic content to load, e.g., await page.wait_for_selector('div.price')
        html_content = await page.content()
        await browser.close()
    """
    product_info = {
        "title": "N/A",
        "price": "N/A",
        "sizes": "N/A",
        "pin": "N/A",
        "link": url
    }

    # --- Title Extraction ---
    # Prioritize the image caption if available, as per your requirements
    if message_caption:
        product_info["title"] = message_caption
        # Basic cleaning: remove extra spaces
        product_info["title"] = " ".join(product_info["title"].split())
        # Add basic gender/quantity prefixing if keywords are found in the caption
        for tag in GENDER_TAGS:
            if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                if not product_info["title"].lower().startswith(tag): # Avoid double prefixing
                    product_info["title"] = f"{tag.capitalize()} {product_info['title']}"
                break
        for tag in QUANTITY_TAGS:
            if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                if not product_info["title"].lower().startswith(tag): # Avoid double prefixing
                    product_info["title"] = f"{tag.capitalize()} {product_info['title']}"
                break
        logger.info(f"Using message caption for title: '{product_info['title']}'")
    
    html_content = await fetch_page_content(url)
    if not html_content:
        logger.warning(f"Could not fetch content for '{url}'. Cannot scrape further.")
        return product_info

    soup = BeautifulSoup(html_content, 'lxml') # Using lxml parser for speed

    # If title wasn't set from caption, try scraping from the page
    if product_info["title"] == "N/A":
        # Try og:title, then title tag, then h1 for product title
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
            # Basic cleaning for scraped title: remove common website suffixes/noise
            product_info["title"] = re.sub(r'\|\s*Amazon\.in|\s*Online at Best Price|\s*-\s*Buy Online.*', '', product_info["title"], flags=re.IGNORECASE).strip()
            # Ensure gender/quantity tags are added if identified and not already present from scraping
            found_gender = False
            for tag in GENDER_TAGS:
                if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                    product_info["title"] = f"{tag.capitalize()} {product_info['title']}"
                    found_gender = True
                    break # Only add one gender tag
            if found_gender: # If gender was added, remove it from the raw title to avoid duplication later
                for tag in GENDER_TAGS:
                    product_info["title"] = re.sub(r'\b' + re.escape(tag) + r'\b', '', product_info["title"], flags=re.IGNORECASE).strip()

            found_quantity = False
            for tag in QUANTITY_TAGS:
                if re.search(r'\b' + re.escape(tag) + r'\b', product_info["title"], re.IGNORECASE):
                    product_info["title"] = f"{tag.capitalize()} {product_info['title']}"
                    found_quantity = True
                    break # Only add one quantity tag
            if found_quantity: # If quantity was added, remove it from the raw title
                 for tag in QUANTITY_TAGS:
                    product_info["title"] = re.sub(r'\b' + re.escape(tag) + r'\b', '', product_info["title"], flags=re.IGNORECASE).strip()

        logger.info(f"Scraped title: '{product_info['title']}'")

    # --- Price Scraping ---
    # Search for elements containing price patterns or common price class names
    price_candidates = soup.find_all(text=PRICE_PATTERN)
    if not price_candidates: # If regex didn't find, try common price classes
        price_candidates.extend(soup.find_all(class_=re.compile(r'price|product-price|offer-price|final-price|selling-price|current-price', re.IGNORECASE)))
    
    for candidate in price_candidates:
        price_match = PRICE_PATTERN.search(str(candidate))
        if price_match:
            # Extract just the digits and remove commas, as per "Output: Only digits, no ₹ symbol"
            raw_price = price_match.group(1).replace(',', '')
            product_info["price"] = raw_price
            logger.info(f"Scraped price: '{product_info['price']}'")
            break # Found price, stop searching

    # --- Sizes Scraping ---
    # This is often dynamic and highly site-specific.
    # A robust solution requires a headless browser to detect active sizes and stock status.
    # For now, we'll try to find common span elements that might contain sizes.
    available_sizes = []
    # Labels to look for (case-insensitive for comparison, but store original casing)
    size_labels_list = ["S", "M", "L", "XL", "XXL", "XXXL", "Free Size", "One Size"]

    # Look for spans or other elements with common size-related classes or attributes
    size_elements = soup.find_all(lambda tag: tag.name in ['span', 'div', 'li', 'a'] and
                                   any(cls in (tag.get('class', []) or []) for cls in ['size-label', 'product-size-label', 'size-variant', 'selector-item']) or
                                   any(attr in tag.attrs for attr in ['data-size', 'data-value']))

    for element in size_elements:
        text = element.get_text(strip=True)
        if text.upper() in [s.upper() for s in size_labels_list]:
            # Simple stock check: assume available if not explicitly marked as unavailable/disabled.
            # Real stock check would look for specific 'disabled', 'unavailable' classes or AJAX calls.
            if 'unavailable' not in element.get('class', []) and 'disabled' not in element.get('class', []):
                available_sizes.append(text)
    
    if available_sizes:
        # Deduplicate and sort sizes
        unique_sizes = sorted(list(set(available_sizes)), key=lambda x: size_labels_list.index(x.upper()) if x.upper() in [s.upper() for s in size_labels_list] else len(size_labels_list))
        # If all common sizes are found, set to "All" (simplified check)
        if len(unique_sizes) >= len(size_labels_list) - 2: # heuristic check
             product_info["sizes"] = "All"
        else:
            product_info["sizes"] = ", ".join(unique_sizes)
    else:
        product_info["sizes"] = "Not Found" # This will cause the line to be skipped in output

    logger.info(f"Scraped sizes: '{product_info['sizes']}'")

    # --- Pin Code for Meesho ---
    if "meesho.com" in url:
        # As per requirement, use fallback pin for Meesho if not found on page (complex to scrape without JS)
        product_info["pin"] = MEESHO_FALLBACK_PIN
        logger.info(f"Detected Meesho link, applying fallback pin: '{product_info['pin']}'")
    else:
        product_info["pin"] = "N/A" # Not applicable for other sites

    return product_info

# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcoming message when the /start command is issued."""
    await update.message.reply_text(
        "Hello! I'm your Deal-bot. 🤖 Forward me a message with a product link, "
        "or type a message containing one, and I'll try to extract the deal info for you. "
        "For best results, forward messages with images! ✨"
    )

async def process_product_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles incoming messages (text, photos with captions, forwarded messages).
    Extracts links, offloads scraping, and sends the formatted deal.
    """
    message = update.message
    message_text = message.text or message.caption # Get text from message or photo caption

    if not message_text:
        # If no text/caption, but it's a forwarded message, check for image-only case.
        if message.forward_from or message.forward_sender_name:
             if message.photo:
                logger.info("Forwarded message with image detected, but no text/caption. Attempting to extract link from image metadata or external sources if possible (not implemented).")
                await message.reply_text("🔎 Trying to analyze the image... (Note: Image-only link detection is limited.)")
                # For image-only processing without text, you'd need advanced image OCR/recognition here
                # which is out of scope for this basic setup.
                # For now, if no link is found later, it will give "No link detected."
             else:
                await message.reply_text("⚠️ No product link detected in the message.")
                return
        else:
            await message.reply_text("⚠️ No product link detected in the message.")
            return

    # Try to extract product link from message entities (e.g., actual text links, URLs)
    product_link = None
    if message.entities:
        for entity in message.entities:
            if entity.type == 'text_link' and entity.url:
                product_link = entity.url
                break
            elif entity.type == 'url':
                product_link = message_text[entity.offset : entity.offset + entity.length]
                break
    
    # If no link found via entities, try regex on full message text
    if not product_link:
        product_link = extract_product_link(message_text)

    if not product_link:
        await message.reply_text("⚠️ No product link detected.")
        logger.info(f"No product link detected in message from chat ID: {update.effective_chat.id}")
        return

    await message.reply_text("⏳ Processing your link... Please wait a moment. 🚀")
    logger.info(f"Detected link: '{product_link}' from chat ID: {update.effective_chat.id}")

    try:
        # Offload the heavy scraping and formatting work to an asyncio task.
        # This is CRUCIAL to prevent the Flask webhook from timing out (Telegram's limit is 10 seconds).
        # For very high load or extremely long scraping tasks, consider a dedicated
        # message queue system like Celery/RQ with separate worker processes.
        formatted_deal_info = await scrape_and_format_deal(product_link, message.caption)
        await message.reply_text(formatted_deal_info)
    except Exception as e:
        logger.exception(f"❌ Unhandled error processing link '{product_link}': {e}")
        await message.reply_text("❌ Oh no! I hit a snag and couldn't extract the product info. Please try again later. 🚧")

async def scrape_and_format_deal(link: str, caption: str | None = None) -> str:
    """
    Orchestrates the entire process: unshortening, stripping, scraping, and formatting.
    """
    try:
        # 1. Unshorten the URL
        unshortened_link = await unshorten_url(link)
        
        # 2. Strip affiliate tags from the unshortened link
        clean_link = strip_affiliate_tags(unshortened_link)
        
        # 3. Scrape product information from the cleaned link
        product_data = await scrape_product_info(clean_link, caption)

        # 4. Format the output string as per specified structure
        formatted_lines = []

        # [Gender] [Quantity] [Title] @[price] rs
        # Ensure title cleaning matches output rules
        display_title = product_data["title"]
        
        # Remove any bracketed tags used for internal identification by scraper, if any
        display_title = re.sub(r'\[(Men|Women|Kids|Unisex|Pack of|Set of|Pcs|Kg|Ml|G|Quantity)\]\s*', '', display_title, flags=re.IGNORECASE).strip()

        # Re-apply prefixes as standalone words if they were found during scraping.
        # This requires checking the original scraped data or re-deriving.
        # For simplicity, if the scraper already prefixed, it's there.
        # If the title still contains raw gender/quantity words, let's prepend them cleanly.
        final_prefixes = []
        for tag in GENDER_TAGS:
            if re.search(r'\b' + re.escape(tag) + r'\b', display_title, re.IGNORECASE):
                final_prefixes.append(tag.capitalize())
                display_title = re.sub(r'\b' + re.escape(tag) + r'\b', '', display_title, flags=re.IGNORECASE).strip()
                break # Add only one gender tag
        for tag in QUANTITY_TAGS:
            if re.search(r'\b' + re.escape(tag) + r'\b', display_title, re.IGNORECASE):
                final_prefixes.append(tag.capitalize())
                display_title = re.sub(r'\b' + re.escape(tag) + r'\b', '', display_title, flags=re.IGNORECASE).strip()
                break # Add only one quantity tag
        
        # Join prefixes and title
        final_output_title = " ".join(final_prefixes + [display_title.strip()]).strip()

        formatted_lines.append(f"{final_output_title} @{product_data['price']} rs")
        formatted_lines.append(product_data['link'])

        # Size - [sizes] (skip if not found)
        if product_data["sizes"] != "N/A" and product_data["sizes"] != "Not Found":
            formatted_lines.append(f"\nSize - {product_data['sizes']}") # Add newline for separation
        else:
            logger.info("Skipping 'Size' line as no valid sizes were found.")

        # Pin - [pin] (only for meesho.com links)
        if "meesho.com" in clean_link and product_data["pin"] != "N/A":
            formatted_lines.append(f"Pin - {product_data['pin']}") # No extra newline, will follow Size or Title
        else:
            logger.info("Skipping 'Pin' line as it's not a Meesho link or pin is N/A.")
        
        # Always end with @reviewcheckk
        formatted_lines.append("\n@reviewcheckk") # Ensure this is always the last line with a preceding newline

        return "\n".join(formatted_lines)

    except Exception as e:
        logger.exception(f"Error in scrape_and_format_deal for link '{link}': {e}")
        return "❌ Oops! Something went wrong while preparing the deal information. Please try again or with a different link. 🤖"

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any errors and send a generic error message to the user."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update.effective_message:
        await update.effective_message.reply_text(
            "An unexpected error occurred with the bot. We've been notified! Please try again later. 🛠️"
        )

# --- Register Handlers ---
def setup_handlers(app_instance: Application):
    """Register all bot handlers with the Telegram Application instance."""
    app_instance.add_handler(MessageHandler(filters.COMMAND, start)) # Handles /start command
    app_instance.add_handler(MessageHandler(
        # Filters for text messages, photos with captions, and any forwarded messages
        filters.TEXT | filters.PHOTO & filters.FORWARDED | filters.FORWARDED & filters.TEXT | filters.FORWARDED & filters.CAPTION,
        process_product_link
    ))
    app_instance.add_error_handler(error_handler) # Catches all unhandled exceptions

# --- Flask Webhook Route ---
@app.route(WEBHOOK_PATH, methods=["POST"])
async def webhook_handler():
    """
    Receives incoming Telegram updates via POST requests from the webhook.
    This is the entry point for all Telegram messages to your bot.
    """
    if request.method == "POST":
        update_json = request.get_json()
        if not update_json:
            logger.warning("Received empty or invalid JSON from webhook.")
            abort(400) # Bad Request

        try:
            # Create a Telegram Update object from the JSON payload
            update = Update.de_json(update_json, application.bot)
            # Process the update. This is asynchronous, allowing the webhook to return quickly.
            await application.process_update(update)
            return "ok" # Telegram expects a 200 OK response
        except Exception as e:
            logger.error(f"Error processing Telegram update in webhook: {e}", exc_info=True)
            abort(500) # Indicate an internal server error to Telegram, though "ok" is often fine too.
    return "ok" # For GET requests or other methods, just return ok

# --- Main Application Logic (for local testing or Render's initial setup) ---
if __name__ == '__main__':
    # This block is for local development testing or to run a one-time webhook setup.
    # In a Render production deployment, Gunicorn (defined in Procfile) will start the 'app' Flask instance.

    # 1. Set up all Telegram bot handlers
    setup_handlers(application)

    # 2. Set the webhook URL on Telegram's side.
    # This should typically be a one-time operation during deployment or a pre-deploy hook on Render.
    # Running it every time the Flask app starts is generally fine but not strictly necessary after first successful setup.
    async def set_webhook_on_startup():
        try:
            full_webhook_url = f"{WEBHOOK_URL_BASE.rstrip('/')}{WEBHOOK_PATH}"
            logger.info(f"Attempting to set webhook to: {full_webhook_url}")
            # allowed_updates=Update.ALL_TYPES ensures you receive all types of updates
            await application.bot.set_webhook(url=full_webhook_url, allowed_updates=Update.ALL_TYPES)
            logger.info("Webhook set successfully!")
        except Exception as e:
            logger.critical(f"Failed to set webhook on Telegram! Bot will not receive updates. Error: {e}")

    # Run the webhook setup asynchronously
    asyncio.run(set_webhook_on_startup())

    # This line is primarily for local testing where you might run `python app.py`.
    # For Render production, Gunicorn will manage the Flask application process (`gunicorn app:app`).
    # app.run(debug=True, port=8000)
