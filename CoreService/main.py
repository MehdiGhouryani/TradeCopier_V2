# CoreService/main.py
#
# --- به‌روزرسانی نهایی ---
# نقطه شروع اصلی که ربات تلگرام و سرور ZMQ را به صورت همزمان اجرا می‌کند
# و صف هشدار (Alert Queue) را بین آن‌ها به اشتراک می‌گذارد.

import asyncio
import logging
from core import database
from core import server
from core import telegram_bot

# راه‌اندازی لاگ‌گیری
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def main():
    """
    تابع اصلی برنامه
    """
    logger.info("Application starting...")
    
    # --- گام ۱: راه‌اندازی دیتابیس ---
    try:
        database.init_db()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"FATAL: Database initialization failed: {e}")
        return

    # --- گام ۲: ایجاد صف ارتباطی مشترک ---
    # این صف برای ارسال هشدار از سرور ZMQ به ربات تلگرام استفاده می‌شود
    alert_queue = asyncio.Queue()

    # --- گام ۳: راه‌اندازی سرور ZMQ ---
    # صف هشدار را به سرور تزریق می‌کنیم
    zmq_server = server.ZMQServer(alert_queue=alert_queue)
    
    # --- گام ۴: راه‌اندازی ربات تلگرام ---
    # ربات تلگرام نیز به همان صف گوش می‌دهد
    # (تابع run ربات تلگرام در فایل telegram_bot.py تعریف شده است)
    
    # --- گام ۵: اجرای همزمان تسک‌ها ---
    server_task = None
    telegram_task = None
    try:
        logger.info("Starting ZMQ Server task...")
        server_task = asyncio.create_task(zmq_server.run())
        
        logger.info("Starting Telegram Bot task...")
        # صف هشدار را به ربات تزریق می‌کنیم
        telegram_task = asyncio.create_task(telegram_bot.run(queue=alert_queue))
        
        # منتظر می‌مانیم تا هر دو تسک اجرا شوند
        await asyncio.gather(
            server_task,
            telegram_task
        )
        
    except KeyboardInterrupt:
        logger.info("Application shutting down by user request (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Application encountered a fatal error: {e}", exc_info=True)
    finally:
        # بستن تسک‌ها به صورت ایمن
        if telegram_task and not telegram_task.done():
            logger.info("Cancelling Telegram Bot task...")
            telegram_task.cancel()
        if server_task and not server_task.done():
            logger.info("Cancelling ZMQ Server task...")
            server_task.cancel()
        
        # منتظر می‌مانیم تا تسک‌ها واقعاً بسته شوند
        await asyncio.sleep(1) 
        logger.info("Application shutdown complete.")


if __name__ == "__main__":
    # اجرای حلقه asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass