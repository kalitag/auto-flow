import os
import re
import logging
import asyncio
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Web framework
from flask import Flask, request, Response

# Telegram bot
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Web scraping
import requests
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app for webhook
app = Flask(__name__)

# Bot configuration
BOT_TOKEN = "8465346144:AAGSHC77UkXVZZTUscbYItvJxgQbBxmFcWo"
WEBHOOK_URL = "https://auto-flow-k6sb.onrender.com"

# Initialize bot
bot = Bot(token=BOT_TOKEN)
application = None

class LinkProcessor:
    """Handles link detection, unshortening, and cleaning"""
    
    SHORTENERS = [
        'cutt.ly', 'spoo.me', 'amzn.to', 'amzn.in', 'fkrt.cc', 
        'bitli.in', 'da.gd', 'wishlink.com', 'bit.ly', 'tinyurl.com',
        'goo.gl', 'ow.ly', 'is.gd', 'buff.ly'
    ]
    
    AFFILIATE_PARAMS = [
        'tag', 'ref', 'affid', 'affiliate_id', 'utm_source', 
        'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
        'fbclid', 'gclid', 'msclkid'
    ]
    
    @classmethod
    def extract_links(cls, text: str) -> List[str]:
        """Extract all URLs from text"""
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        return re.findall(url_pattern, text)
    
    @classmethod
    def is_shortened(cls, url: str) -> bool:
        """Check if URL is from a shortener service"""
        domain = urlparse(url).netloc.lower()
        return any(shortener in domain for shortener in cls.SHORTENERS)
    
    @classmethod
    def unshorten(cls, url: str, max_redirects: int = 10) -> str:
        """Follow redirects to get final URL"""
        try:
            session = requests.Session()
            session.max_redirects = max_redirects
            resp = session.head(url, allow_redirects=True, timeout=5)
            return resp.url
        except:
            try:
                resp = requests.get(url, allow_redirects=True, timeout=5)
                return resp.url
            except:
                return url
    
    @classmethod
    def clean_url(cls, url: str) -> str:
        """Remove affiliate and tracking parameters"""
        parsed = urlparse(url)
        query_dict = parse_qs(parsed.query)
        
        # Remove affiliate parameters
        cleaned_query = {k: v for k, v in query_dict.items() 
                        if k.lower() not in cls.AFFILIATE_PARAMS}
        
        # Rebuild URL
        new_query = urlencode(cleaned_query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

class ProductScraper:
    """Scrapes product information from various e-commerce sites"""
    
    @staticmethod
    def detect_site(url: str) -> str:
        """Detect which e-commerce site the URL is from"""
        domain = urlparse(url).netloc.lower()
        if 'amazon' in domain:
            return 'amazon'
        elif 'flipkart' in domain:
            return 'flipkart'
        elif 'meesho' in domain:
            return 'meesho'
        elif 'myntra' in domain:
            return 'myntra'
        else:
            return 'generic'
    
    @staticmethod
    def scrape_page(url: str) -> Optional[BeautifulSoup]:
        """Fetch and parse webpage"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return BeautifulSoup(response.content, 'html.parser')
        except Exception as e:
            logger.error(f"Scraping error: {e}")
            return None
    
    @classmethod
    def extract_title(cls, soup: BeautifulSoup, site: str) -> str:
        """Extract product title based on site"""
        title = ""
        
        # Try meta tags first
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content']
        
        # Site-specific selectors
        if not title:
            if site == 'amazon':
                elem = soup.find('span', id='productTitle')
                if elem:
                    title = elem.text.strip()
            elif site == 'flipkart':
                elem = soup.find('span', class_='B_NuCI') or soup.find('h1', class_='yhB1nd')
                if elem:
                    title = elem.text.strip()
            elif site == 'meesho':
                elem = soup.find('h1') or soup.find('p', class_='Text__StyledText-sc-oo0kvp-0')
                if elem:
                    title = elem.text.strip()
            elif site == 'myntra':
                elem = soup.find('h1', class_='pdp-title') or soup.find('h1', class_='pdp-name')
                if elem:
                    title = elem.text.strip()
        
        # Fallback to page title
        if not title:
            page_title = soup.find('title')
            if page_title:
                title = page_title.text.strip()
        
        return cls.clean_title(title)
    
    @staticmethod
    def clean_title(title: str) -> str:
        """Clean and shorten title"""
        # Remove common unnecessary patterns
        patterns = [
            r'Buy.*?online at.*?prices',
            r'Online Shopping.*',
            r'\|.*',
            r' - .*',
            r'Free Shipping.*',
            r'Cash on Delivery.*'
        ]
        
        for pattern in patterns:
            title = re.sub(pattern, '', title, flags=re.IGNORECASE)
        
        # Limit length
        words = title.split()[:10]
        return ' '.join(words).strip()
    
    @classmethod
    def extract_price(cls, soup: BeautifulSoup, site: str) -> Optional[str]:
        """Extract product price"""
        price_text = ""
        
        if site == 'amazon':
            elem = soup.find('span', class_='a-price-whole') or \
                   soup.find('span', class_='a-price-range')
            if elem:
                price_text = elem.text
        elif site == 'flipkart':
            elem = soup.find('div', class_='_30jeq3') or \
                   soup.find('div', class_='_1vC4OE')
            if elem:
                price_text = elem.text
        elif site == 'meesho':
            elem = soup.find('h4') or \
                   soup.find('span', class_='Text__StyledText-sc-oo0kvp-0')
            if elem and '₹' in elem.text:
                price_text = elem.text
        elif site == 'myntra':
            elem = soup.find('span', class_='pdp-price') or \
                   soup.find('strong', class_='pdp-price')
            if elem:
                price_text = elem.text
        
        # Generic price search
        if not price_text:
            price_pattern = r'[₹Rs\.]\s*(\d+(?:,\d+)*(?:\.\d+)?)'
            for text in soup.stripped_strings:
                match = re.search(price_pattern, text)
                if match:
                    price_text = match.group(0)
                    break
        
        # Extract digits only
        if price_text:
            digits = re.findall(r'\d+', price_text.replace(',', ''))
            if digits:
                return digits[0]
        
        return None
    
    @classmethod
    def extract_sizes(cls, soup: BeautifulSoup) -> str:
        """Extract available sizes"""
        size_labels = ['S', 'M', 'L', 'XL', 'XXL', 'XXXL']
        found_sizes = []
        
        # Look for size elements
        size_elements = soup.find_all(['span', 'div', 'button'], 
                                     text=re.compile(r'^(S|M|L|XL|XXL|XXXL)$'))
        
        for elem in size_elements:
            size = elem.text.strip().upper()
            if size in size_labels and size not in found_sizes:
                found_sizes.append(size)
        
        if len(found_sizes) >= 4:
            return "All"
        elif found_sizes:
            return ', '.join(found_sizes)
        
        return ""
    
    @classmethod
    def extract_gender(cls, text: str) -> str:
        """Detect gender from text"""
        text_lower = text.lower()
        if any(word in text_lower for word in ['women', 'woman', 'girl', 'ladies', 'female']):
            return "Women"
        elif any(word in text_lower for word in ['men', 'man', 'boy', 'male', 'gents']):
            return "Men"
        elif any(word in text_lower for word in ['kid', 'child', 'baby', 'infant']):
            return "Kids"
        elif 'unisex' in text_lower:
            return "Unisex"
        return ""
    
    @classmethod
    def extract_quantity(cls, text: str) -> str:
        """Extract quantity information"""
        patterns = [
            r'pack\s*of\s*(\d+)',
            r'set\s*of\s*(\d+)',
            r'(\d+)\s*pcs',
            r'(\d+)\s*pieces',
            r'(\d+)\s*kg',
            r'(\d+)\s*ml',
            r'(\d+)\s*g\b'
        ]
        
        text_lower = text.lower()
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                if 'pack' in pattern or 'set' in pattern:
                    return f"Pack of {match.group(1)}"
                elif 'pcs' in pattern or 'pieces' in pattern:
                    return f"{match.group(1)} pcs"
                else:
                    unit = pattern.split('\\s*')[1].replace('\\b', '')
                    return f"{match.group(1)}{unit}"
        
        return ""
    
    @classmethod
    def extract_pincode(cls, text: str, site: str) -> str:
        """Extract pincode (mainly for Meesho)"""
        if site != 'meesho':
            return ""
        
        # Look for 6-digit pincode
        match = re.search(r'\b\d{6}\b', text)
        if match:
            return match.group(0)
        
        # Default pincode for Meesho
        return "110001"

class DealBot:
    """Main bot logic"""
    
    @staticmethod
    async def process_message(text: str, caption: str = "") -> str:
        """Process message and extract deal information"""
        # Combine text and caption
        full_text = f"{text} {caption}".strip()
        
        # Extract links
        links = LinkProcessor.extract_links(full_text)
        if not links:
            return "⚠️ No product link detected."
        
        # Process first link
        url = links[0]
        
        # Unshorten if needed
        if LinkProcessor.is_shortened(url):
            url = LinkProcessor.unshorten(url)
        
        # Clean URL
        clean_url = LinkProcessor.clean_url(url)
        
        # Scrape product info
        soup = ProductScraper.scrape_page(clean_url)
        if not soup:
            return "❌ Unable to extract product info."
        
        # Detect site
        site = ProductScraper.detect_site(clean_url)
        
        # Extract information
        title = ProductScraper.extract_title(soup, site)
        price = ProductScraper.extract_price(soup, site)
        sizes = ProductScraper.extract_sizes(soup)
        
        # Extract from full text (includes caption)
        gender = ProductScraper.extract_gender(full_text + " " + title)
        quantity = ProductScraper.extract_quantity(full_text + " " + title)
        pincode = ProductScraper.extract_pincode(full_text, site)
        
        # Build response
        response_parts = []
        
        # Title line with gender and quantity
        title_parts = []
        if gender:
            title_parts.append(gender)
        if quantity:
            title_parts.append(quantity)
        title_parts.append(title)
        
        if price:
            response_parts.append(f"{' '.join(title_parts)} @{price} rs")
        else:
            response_parts.append(' '.join(title_parts))
        
        # Clean URL
        response_parts.append(clean_url)
        response_parts.append("")  # Empty line
        
        # Size information
        if sizes:
            response_parts.append(f"Size - {sizes}")
        
        # Pincode (for Meesho)
        if pincode:
            response_parts.append(f"Pin - {pincode}")
        
        # Footer
        response_parts.append("")
        response_parts.append("@reviewcheckk")
        
        return '\n'.join(response_parts)

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    await update.message.reply_text(
        "👋 Welcome to Deal Bot!\n\n"
        "Send me any product link and I'll extract the deal information for you.\n"
        "I work with Amazon, Flipkart, Meesho, Myntra and more!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages"""
    try:
        # Get message text and caption
        text = update.message.text or ""
        caption = update.message.caption or ""
        
        # Process message
        response = await DealBot.process_message(text, caption)
        
        # Send response
        await update.message.reply_text(response, disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request.")

# Webhook route
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Handle webhook requests"""
    try:
        if request.method == "POST":
            update = Update.de_json(request.get_json(force=True), bot)
            
            # Process update asynchronously
            asyncio.run(application.process_update(update))
            
            return Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status=500)

@app.route('/', methods=['GET'])
def index():
    """Health check endpoint"""
    return "Deal Bot is running! 🤖"

def setup_application():
    """Setup the telegram application"""
    global application
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    
    # Initialize application
    asyncio.run(application.initialize())

async def set_webhook():
    """Set webhook URL"""
    webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    success = await bot.set_webhook(webhook_url)
    if success:
        logger.info(f"Webhook set to: {webhook_url}")
    else:
        logger.error("Failed to set webhook")
    return success

if __name__ == '__main__':
    # Setup application
    setup_application()
    
    # Set webhook
    asyncio.run(set_webhook())
    
    # Run Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
