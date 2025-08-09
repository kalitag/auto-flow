    # set_webhook.py
    import os
    import asyncio
    import logging
    from telegram.ext import Application
    from telegram import Update

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
    )
    logger = logging.getLogger(__name__)

    # Load environment variables (ensure these are set in Render)
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL_BASE")

    WEBHOOK_PATH = f"/{BOT_TOKEN}"

    async def set_webhook_on_startup():
        """Sets the Telegram webhook URL."""
        if not BOT_TOKEN:
            logger.critical("BOT_TOKEN environment variable not set. Cannot set webhook.")
            return
        if not WEBHOOK_URL_BASE:
            logger.critical("WEBHOOK_URL_BASE environment variable not set. Cannot set webhook.")
            return

        application = Application.builder().token(BOT_TOKEN).build()
        
        try:
            full_webhook_url = f"{WEBHOOK_URL_BASE.rstrip('/')}{WEBHOOK_PATH}"
            logger.info(f"Attempting to set webhook to: {full_webhook_url}")
            await application.bot.set_webhook(url=full_webhook_url, allowed_updates=Update.ALL_TYPES)
            logger.info("Webhook set successfully!")
        except Exception as e:
            logger.critical(f"Failed to set webhook on Telegram! Bot will not receive updates. Error: {e}", exc_info=True)

    if __name__ == "__main__":
        asyncio.run(set_webhook_on_startup())
    
