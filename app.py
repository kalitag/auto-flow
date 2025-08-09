import os
import re
import logging
from typing import Optional, Dict, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import json

# Web framework
from flask import Flask, request, Response

# Telegram bot
import telegram
from telegram import Update, Bot

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
BOT_TOKEN = os.environ.get('BOT_TOKEN', "8465346144:AAGSHC77UkXVZZTUscbYItvJxgQbBxmFcWo")
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', "https://auto-flow-k6sb.onrender.com")

# Initialize bot
bot = Bot(token=BOT_TOKEN)

class LinkProcessor:
    """Handles link detection, unshortening, and cleaning"""
    
    SHORTENERS = [
        'cutt.ly', 'spoo.me', 'amzn.to', 'amzn.in', 'fkrt.cc', 
        'bitli.in', 'da.gd', 'wishlink.com', 'bit.ly', 'tinyurl.com',
        'goo.gl', 'ow.ly', 'is.gd', 'buff.ly', 'shorturl.at'
    ]
    
    AFFILIATE_PARAMS = [
        'tag', 'ref', 'affid', 'affiliate_id', 'utm_source', 
        'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
        'fbclid', 'gclid', 'msclkid', 'wickedid', 'ascsubtag'
    ]
    
    @classmethod
    def extract_links(cls, text: str) -> List[str]:
        """Extract all URLs from text"""
        if not text:
            return []
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        return re.findall(url_pattern, text)
    
    @classmethod
    def is_shortened(cls, url: str) -> bool:
        """Check if URL is from a shortener service"""
        try:
            domain = urlparse(url).netloc.lower()
            return any(shortener in domain for shortener in cls.SHORTENERS)
        except:
            return False
    
    @classmethod
    def unshorten(cls, url: str, max_redirects: int = 10) -> str:
        """Follow redirects to get final URL"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            session = requests.Session()
            session.max_redirects = max_redirects
            resp = session.head(url, allow_redirects=True, timeout=5, headers=headers)
            final_url = resp.url
            
            # Double check if still shortened
            if cls.is_shortened(final_url) and final_url != url:
                resp = requests.get(final_url, allow_redirects=True, timeout=5, headers=headers)
                final_url = resp.url
            
            return final_url
        except Exception as e:
            logger.error(f"Error unshortening URL: {e}")
            return url
    
    @classmethod
    def clean_url(cls, url: str) -> str:
        """Remove affiliate and tracking parameters"""
        try:
            parsed = urlparse(url)
            query_dict = parse_qs(parsed.query)
            
            # Remove affiliate parameters
            cleaned_query = {k: v for k, v in query_dict.items() 
                            if k.lower() not in cls.AFFILIATE_PARAMS}
            
            # Rebuild URL
            new_query = urlencode(cleaned_query, doseq=True)
            cleaned_url = urlunparse(parsed._replace(query=new_query))
            
            # Remove trailing ? if no parameters
            if cleaned_url.endswith('?'):
                cleaned_url = cleaned_url[:-1]
                
            return cleaned_url
        except:
            return url

class ProductScraper:
    """Scrapes product information from various e-commerce sites"""
    
    @staticmethod
    def detect_site(url: str) -> str:
        """Detect which e-commerce site the URL is from"""
        try:
            domain = urlparse(url).netloc.lower()
            if 'amazon' in domain:
                return 'amazon'
            elif 'flipkart' in domain:
                return 'flipkart'
            elif 'meesho' in domain:
                return 'meesho'
            elif 'myntra' in domain:
                return 'myntra'
            elif 'ajio' in domain:
                return 'ajio'
            else:
                return 'generic'
        except:
            return 'generic'
    
    @staticmethod
    def scrape_page(url: str) -> Optional[BeautifulSoup]:
        """Fetch and parse webpage"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        try:
            response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            response.raise_for_status()
            return BeautifulSoup(response.content, 'html.parser')
        except Exception as e:
            logger.error(f"Scraping error for {url}: {e}")
            return None
    
    @classmethod
    def extract_title(cls, soup: BeautifulSoup, site: str) -> str:
        """Extract product title based on site"""
        title = ""
        
        try:
            # Try meta tags first
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                title = og_title['content']
            
            # Site-specific selectors
            if not title:
                if site == 'amazon':
                    elem = soup.find('span', id='productTitle') or \
                           soup.find('h1', class_='a-size-large') or \
                           soup.find('h1', id='title')
                    if elem:
                        title = elem.text.strip()
                elif site == 'flipkart':
                    elem = soup.find('span', class_='B_NuCI') or \
                           soup.find('h1', class_='yhB1nd') or \
                           soup.find('h1')
                    if elem:
                        title = elem.text.strip()
                elif site == 'meesho':
                    elem = soup.find('span', {'data-testid': 'product-title'}) or \
                           soup.find('p', class_='Text__StyledText-sc-oo0kvp-0') or \
                           soup.find('h1')
                    if elem:
                        title = elem.text.strip()
                elif site == 'myntra':
                    elem = soup.find('h1', class_='pdp-title') or \
                           soup.find('h1', class_='pdp-name') or \
                           soup.find('h1')
                    if elem:
                        title = elem.text.strip()
            
            # Fallback to page title
            if not title:
                page_title = soup.find('title')
                if page_title:
                    title = page_title.text.strip()
            
            return cls.clean_title(title)
        except:
            return "Product"
    
    @staticmethod
    def clean_title(title: str) -> str:
        """Clean and shorten title"""
        if not title:
            return "Product"
            
        # Remove common unnecessary patterns
        patterns = [
            r'Buy\s+.*?online.*',
            r'Online Shopping.*',
            r'\|.*',
            r' - Buy.*',
            r'Free Shipping.*',
            r'Cash on Delivery.*',
            r'at Best Price.*',
            r'with Offers.*'
        ]
        
        for pattern in patterns:
            title = re.sub(pattern, '', title, flags=re.IGNORECASE)
        
        # Clean extra spaces
        title = ' '.join(title.split())
        
        # Limit length
        words = title.split()[:8]
        result = ' '.join(words).strip()
        
        return result if result else "Product"
    
    @classmethod
    def extract_price(cls, soup: BeautifulSoup, site: str) -> Optional[str]:
        """Extract product price"""
        price_text = ""
        
        try:
            if site == 'amazon':
                # Amazon price selectors
                selectors = [
                    ('span', {'class': 'a-price-whole'}),
                    ('span', {'class': 'a-price-range'}),
                    ('span', {'class': 'a-price'}),
                    ('span', {'class': 'a-color-price'})
                ]
                for tag, attrs in selectors:
                    elem = soup.find(tag, attrs)
                    if elem:
                        price_text = elem.text
                        break
                        
            elif site == 'flipkart':
                elem = soup.find('div', class_='_30jeq3') or \
                       soup.find('div', class_='_1vC4OE') or \
                       soup.find('div', class_='_25b18c')
                if elem:
                    price_text = elem.text
                    
            elif site == 'meesho':
                elem = soup.find('span', class_='Text__StyledText-sc-oo0kvp-0') or \
                       soup.find('h4')
                if elem and '₹' in elem.text:
                    price_text = elem.text
                    
            elif site == 'myntra':
                elem = soup.find('span', class_='pdp-price') or \
                       soup.find('strong', class_='pdp-price')
                if elem:
                    price_text = elem.text
            
            # Generic price search if not found
            if not price_text:
                # Look for price in meta tags
                price_meta = soup.find('meta', {'property': 'product:price:amount'})
                if price_meta:
                    price_text = price_meta.get('content', '')
                else:
                    # Search for price pattern in text
                    price_pattern = r'[₹Rs\.]\s*[\d,]+(?:\.\d{2})?'
                    for elem in soup.find_all(text=re.compile(price_pattern)):
                        if '₹' in elem or 'Rs' in elem:
                            price_text = elem
                            break
            
            # Extract digits only
            if price_text:
                # Remove commas and extract numbers
                price_text = price_text.replace(',', '')
                digits = re.findall(r'\d+', price_text)
                if digits:
                    return digits[0]
        except Exception as e:
            logger.error(f"Price extraction error: {e}")
        
        return None
    
    @classmethod
    def extract_sizes(cls, soup: BeautifulSoup) -> str:
        """Extract available sizes"""
        try:
            size_labels = ['S', 'M', 'L', 'XL', 'XXL', 'XXXL', '28', '30', '32', '34', '36', '38', '40']
            found_sizes = []
            
            # Look for size containers
            size_containers = soup.find_all(['div', 'ul'], class_=re.compile('size|Size'))
            
            for container in size_containers:
                size_elements = container.find_all(['span', 'div', 'button', 'li'])
                for elem in size_elements:
                    text = elem.text.strip().upper()
                    if text in size_labels and text not in found_sizes:
                        found_sizes.append(text)
            
            if len(found_sizes) >= 4:
                return "All"
            elif found_sizes:
                return ', '.join(found_sizes[:5])  # Limit to 5 sizes
        except:
            pass
        
        return ""
    
    @classmethod
    def extract_gender(cls, text: str) -> str:
        """Detect gender from text"""
        if not text:
            return ""
            
        text_lower = text.lower()
        
        # Check for explicit gender mentions
        if any(word in text_lower for word in ['women', 'woman', 'girl', 'ladies', 'female', 'her']):
            return "Women"
        elif any(word in text_lower for word in ['men', 'man', 'boy', 'male', 'gents', 'his']):
            return "Men"
        elif any(word in text_lower for word in ['kid', 'child', 'baby', 'infant', 'toddler']):
            return "Kids"
        elif 'unisex' in text_lower:
            return "Unisex"
        
        return ""
    
    @classmethod
    def extract_quantity(cls, text: str) -> str:
        """Extract quantity information"""
        if not text:
            return ""
            
        patterns = [
            (r'pack\s*of\s*(\d+)', 'Pack of {}'),
            (r'set\s*of\s*(\d+)', 'Set of {}'),
            (r'combo\s*of\s*(\d+)', 'Combo of {}'),
            (r'(\d+)\s*pcs', '{} pcs'),
            (r'(\d+)\s*pieces', '{} pieces'),
            (r'(\d+)\s*kg\b', '{}kg'),
            (r'(\d+)\s*g\b', '{}g'),
            (r'(\d+)\s*ml\b', '{}ml'),
            (r'(\d+)\s*l\b', '{}L')
        ]
        
        text_lower = text.lower()
        for pattern, format_str in patterns:
            match = re.search(pattern, text_lower)
            if match:
                return format_str.format(match.group(1))
        
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
    def process_message(text: str, caption: str = "") -> str:
        """Process message and extract deal information"""
        try:
            # Combine text and caption
            full_text = f"{text or ''} {caption or ''}".strip()
            
            if not full_text:
                return "⚠️ Please send a product link."
            
            # Extract links
            links = LinkProcessor.extract_links(full_text)
            if not links:
                return "⚠️ No product link detected. Please share a product link from Amazon, Flipkart, Meesho, Myntra, etc."
            
            # Process first link
            url = links[0]
            logger.info(f"Processing URL: {url}")
            
            # Unshorten if needed
            if LinkProcessor.is_shortened(url):
                logger.info("Unshortening URL...")
                url = LinkProcessor.unshorten(url)
                logger.info(f"Unshortened to: {url}")
            
            # Clean URL
            clean_url = LinkProcessor.clean_url(url)
            
            # Detect site
            site = ProductScraper.detect_site(clean_url)
            logger.info(f"Detected site: {site}")
            
            # Scrape product info
            soup = ProductScraper.scrape_page(clean_url)
            if not soup:
                return f"❌ Unable to fetch product details from the link.\n\nLink: {clean_url}\n\n@reviewcheckk"
            
            # Extract information
            title = ProductScraper.extract_title(soup, site)
            price = ProductScraper.extract_price(soup, site)
            sizes = ProductScraper.extract_sizes(soup)
            
            # Extract from full text (includes caption)
            full_content = full_text + " " + title
            gender = ProductScraper.extract_gender(full_content)
            quantity = ProductScraper.extract_quantity(full_content)
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
            
            title_line = ' '.join(title_parts)
            if price:
                response_parts.append(f"{title_line} @{price} rs")
            else:
                response_parts.append(title_line)
            
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
            
        except Exception as e:
            logger.error(f"Error in process_message: {e}")
            return "❌ An error occurred while processing the link. Please try again."

# Webhook handler
def handle_update(update_json):
    """Handle telegram update"""
    try:
        update = Update.de_json(update_json, bot)
        
        # Handle message
        if update.message:
            chat_id = update.message.chat_id
            
            # Get text and caption
            text = update.message.text or ""
            caption = update.message.caption or ""
            
            # Check for /start command
            if text.startswith('/start'):
                response = (
                    "👋 Welcome to Deal Bot!\n\n"
                    "Send me any product link and I'll extract the deal information for you.\n"
                    "I work with:\n"
                    "• Amazon\n"
                    "• Flipkart\n"
                    "• Meesho\n"
                    "• Myntra\n"
                    "• And more!\n\n"
                    "Just forward or send any product link!"
                )
            else:
                # Process the message
                response = DealBot.process_message(text, caption)
            
            # Send response
            bot.send_message(
                chat_id=chat_id,
                text=response,
                disable_web_page_preview=True,
                parse_mode='HTML'
            )
            
    except Exception as e:
        logger.error(f"Error handling update: {e}")

# Flask routes
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Handle webhook requests"""
    try:
        if request.method == "POST":
            update_json = request.get_json(force=True)
            handle_update(update_json)
            return Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status=200)  # Return 200 to prevent telegram retry

@app.route('/', methods=['GET', 'POST'])
def index():
    """Health check endpoint"""
    return "✅ Deal Bot is running! 🤖"

@app.route('/setwebhook', methods=['GET'])
def set_webhook():
    """Set webhook URL"""
    webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    try:
        result = bot.set_webhook(webhook_url)
        if result:
            return f"✅ Webhook set successfully to: {webhook_url}"
        else:
            return "❌ Failed to set webhook"
    except Exception as e:
        return f"❌ Error setting webhook: {e}"

@app.route('/health', methods=['GET'])
def health():
    """Health check for monitoring"""
    return {"status": "healthy", "bot": "running"}

# Initialize webhook on startup
def initialize():
    """Initialize bot and set webhook"""
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        bot.delete_webhook()  # Clear any existing webhook
        result = bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {result} - URL: {webhook_url}")
        
        # Get webhook info
        webhook_info = bot.get_webhook_info()
        logger.info(f"Webhook info: {webhook_info}")
    except Exception as e:
        logger.error(f"Initialization error: {e}")

# Run initialization
initialize()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
