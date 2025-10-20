# CoreService/core/server.py
#
# --- به‌روزرسانی: افزودن صف هشدار تلگرام ---

import asyncio
import zmq
import zmq.asyncio
import json
import logging
from . import database

# (پورت‌ها مانند قبل)
CONFIG_PORT = "5557"
SIGNAL_PORT = "5555"
PUBLISH_PORT = "5556"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# این صف توسط main.py به سرور و ربات تلگرام تزریق می‌شود
telegram_alert_queue: asyncio.Queue = None 

class ZMQServer:
    def __init__(self, alert_queue: asyncio.Queue): # ورودی جدید
        self.context = zmq.asyncio.Context()
        self.publish_queue = asyncio.Queue(maxsize=1000)
        self.processing_queue = asyncio.Queue(maxsize=1000)
        
        # --- مهم: دریافت صف مشترک ---
        global telegram_alert_queue
        telegram_alert_queue = alert_queue
        logger.info("سرور ZMQ با صف هشدار تلگرام مقداردهی شد.")


    # ... (تسک‌های start_config_responder و start_signal_collector بدون تغییر باقی می‌مانند) ...

    async def start_config_responder(self):
        """
        تسک پاسخگویی به درخواست‌های تنظیمات (Config)
        اکسپرت‌های اسلیو هنگام راه‌اندازی به اینجا متصل می‌شوند.
        """
        socket = self.context.socket(zmq.REP) # REP = Reply (پاسخگو)
        socket.bind(f"tcp://*:{CONFIG_PORT}")
        logger.info(f"Config Responder (REP) listening on port {CONFIG_PORT}...")

        while True:
            try:
                # منتظر دریافت درخواست (در فرمت JSON)
                request_raw = await socket.recv_string()
                request_data = json.loads(request_raw)
                
                logger.info(f"Config request received: {request_data}")

                if request_data.get("command") == "GET_CONFIG":
                    copy_id_str = request_data.get("copy_id_str")
                    if not copy_id_str:
                        raise ValueError("copy_id_str is missing")
                    
                    # دریافت تنظیمات کامل از دیتابیس (فایل database.py)
                    config_data = database.get_config_for_copy_ea(copy_id_str)
                    response = {"status": "OK", "config": config_data}
                    logger.info(f"Sending config for {copy_id_str}...")
                
                else:
                    raise ValueError("Unknown command")

            except Exception as e:
                logger.error(f"Config request failed: {e}")
                response = {"status": "ERROR", "message": str(e)}
            
            # ارسال پاسخ (موفقیت‌آمیز یا خطا) به اکسپرت
            await socket.send_json(response)

    async def start_signal_collector(self):
        """
        تسک جمع‌آوری سیگنال‌ها و گزارش‌ها.
        تمام اکسپرت‌های مستر و اسلیو، داده‌ها را به این پورت PUSH می‌کنند.
        """
        socket = self.context.socket(zmq.PULL) # PULL = کشیدن داده‌ها
        socket.bind(f"tcp://*:{SIGNAL_PORT}")
        logger.info(f"Signal Collector (PULL) listening on port {SIGNAL_PORT}...")

        while True:
            try:
                # دریافت پیام (در فرمت JSON)
                signal_raw = await socket.recv_string()
                signal_data = json.loads(signal_raw)
                
                # قرار دادن پیام در صف پردازش داخلی
                await self.processing_queue.put(signal_data)
                
            except Exception as e:
                logger.error(f"Error receiving signal: {e}")


    async def start_signal_processor(self):
        """
        تسک پردازشگر صف داخلی.
        --- به‌روزرسانی: ارسال هشدار به صف تلگرام ---
        """
        logger.info("Signal Processor task started.")
        while True:
            try:
                signal_data = await self.processing_queue.get()
                event_type = signal_data.get("event")
                
                if event_type in ["TRADE_OPEN", "TRADE_MODIFY", "TRADE_CLOSE_MASTER"]:
                    # سیگنال از مستر -> ارسال برای انتشار
                    await self.publish_queue.put(signal_data)
                    
                    # --- ارسال هشدار باز شدن/بسته شدن برای تلگرام ---
                    if event_type == "TRADE_OPEN":
                        msg = (f"✅ *سیگنال باز شدن*\n\n"
                               f"▫️ *سورس:* `{signal_data.get('source_id_str')}`\n"
                               f"▫️ *نماد:* `{signal_data.get('symbol')}`\n"
                               f"▫️ *نوع:* `{'BUY' if signal_data.get('position_type') == 0 else 'SELL'}`\n"
                               f"▫️ *تیکت سورس:* `{signal_data.get('position_id')}`")
                        await telegram_alert_queue.put(msg)
                    
                    elif event_type == "TRADE_CLOSE_MASTER":
                        msg = (f"☑️ *سیگنال بسته شدن (توسط مستر)*\n\n"
                               f"▫️ *سورس:* `{signal_data.get('source_id_str')}`\n"
                               f"▫️ *نماد:* `{signal_data.get('symbol')}`\n"
                               f"▫️ *سود:* `{signal_data.get('profit', 0.0):.2f}`\n"
                               f"▫️ *تیکت سورس:* `{signal_data.get('position_id')}`")
                        await telegram_alert_queue.put(msg)


                elif event_type == "TRADE_CLOSED_COPY":
                    # گزارش از اسلیو -> ذخیره در دیتابیس و ارسال هشدار
                    logger.info(f"Saving trade history: {signal_data}")
                    database.save_trade_history(
                        copy_id_str=signal_data.get("copy_id_str"),
                        source_id_str=signal_data.get("source_id_str"),
                        symbol=signal_data.get("symbol"),
                        profit=signal_data.get("profit"),
                        source_ticket=signal_data.get("source_ticket")
                    )
                    
                    # --- ارسال هشدار بسته شدن برای تلگرام ---
                    profit = signal_data.get("profit", 0.0)
                    emoji = "🔻" if profit < 0 else "✅"
                    msg = (f"{emoji} *معامله کپی شده بسته شد*\n\n"
                           f"▫️ *حساب کپی:* `{signal_data.get('copy_id_str')}`\n"
                           f"▫️ *سورس:* `{signal_data.get('source_id_str')}`\n"
                           f"▫️ *نماد:* `{signal_data.get('symbol')}`\n"
                           f"▫️ *سود/زیان:* `{profit:.2f}`\n"
                           f"▫️ *تیکت سورس:* `{signal_data.get('source_ticket')}`")
                    await telegram_alert_queue.put(msg)

                elif event_type == "EA_ERROR":
                    # گزارش خطا از اکسپرت -> ارسال هشدار
                    logger.warning(f"EA Error: {signal_data.get('message')}")
                    msg = (f"🚨 *خطای اکسپرت*\n\n"
                           f"*{signal_data.get('ea_id', 'EA')}*:\n"
                           f"`{signal_data.get('message')}`")
                    await telegram_alert_queue.put(msg)
                
                else:
                    logger.warning(f"Unknown event type received: {event_type}")

            except Exception as e:
                logger.error(f"Error processing signal: {e}")
            finally:
                self.processing_queue.task_done()

    # ... (تسک start_signal_publisher بدون تغییر باقی می‌ماند) ...
    async def start_signal_publisher(self):
        """
        تسک انتشار (Publish) سیگنال‌ها.
        پیام‌ها را از صف انتشار خوانده و بر اساس تاپیک (source_id_str)
        برای تمام اسلیوهای مشترک آن تاپیک، ارسال می‌کند.
        """
        socket = self.context.socket(zmq.PUB) # PUB = Publish (انتشار)
        socket.bind(f"tcp://*:{PUBLISH_PORT}")
        logger.info(f"Signal Publisher (PUB) listening on port {PUBLISH_PORT}...")

        while True:
            try:
                # منتظر سیگنالی برای انتشار
                signal_data = await self.publish_queue.get()
                
                # تاپیک پیام، همان شناسه سورس است
                topic = signal_data.get("source_id_str")
                if not topic:
                    logger.warning(f"Signal has no 'source_id_str' to use as topic: {signal_data}")
                    continue
                
                logger.info(f"Publishing on topic '{topic}': {signal_data}")
                
                # ارسال پیام به صورت دو بخشی:
                # 1. تاپیک (برای فیلتر کردن در سمت اسلیو)
                await socket.send_string(topic, flags=zmq.SNDMORE)
                # 2. خود پیام (داده‌های JSON)
                await socket.send_json(signal_data)
                
            except Exception as e:
                logger.error(f"Error publishing signal: {e}")
            finally:
                self.publish_queue.task_done()
                

    async def run(self):
        """
        اجرای همزمان تمام تسک‌های سرور.
        """
        logger.info("--- ZMQ Core Service Starting ---")
        try:
            await asyncio.gather(
                self.start_config_responder(),
                self.start_signal_collector(),
                self.start_signal_processor(),
                self.start_signal_publisher()
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("--- ZMQ Core Service Shutting Down ---")
        finally:
            self.context.term()