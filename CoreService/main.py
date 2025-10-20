import asyncio
import logging
import platform
from core import database
from core import server
from core import telegram_bot
from core.logging_config import setup_logging

logger = logging.getLogger(__name__)

async def main():
    """راه‌اندازی و اجرای برنامه اصلی."""
    setup_logging()
    logger.info("Application starting...")
    try:
        database.init_db()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"FATAL: Database initialization failed: {e}")
        return
    alert_queue = asyncio.Queue()
    zmq_server = server.ZMQServer(alert_queue=alert_queue)
    server_task = None
    telegram_task = None
    try:
        logger.info("Starting ZMQ Server task...")
        server_task = asyncio.create_task(zmq_server.run())
        logger.info("Starting Telegram Bot task...")
        telegram_task = asyncio.create_task(telegram_bot.run(queue=alert_queue))
        await asyncio.gather(server_task, telegram_task)
    except KeyboardInterrupt:
        logger.info("Application shutting down by user request (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Application encountered a fatal error: {e}", exc_info=True)
    finally:
        if telegram_task and not telegram_task.done():
            logger.info("Cancelling Telegram Bot task...")
            telegram_task.cancel()
        if server_task and not server_task.done():
            logger.info("Cancelling ZMQ Server task...")
            server_task.cancel()
        await asyncio.sleep(1)
        logger.info("Application shutdown complete.")

if __name__ == "__main__":
    """اجرای برنامه با تنظیمات حلقه asyncio."""
    if platform.system() == "Windows":
        from asyncio import WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
