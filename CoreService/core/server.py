import asyncio
import zmq
import zmq.asyncio
import json
import logging
from . import database

CONFIG_PORT = "5557"
SIGNAL_PORT = "5555"
PUBLISH_PORT = "5556"

logger = logging.getLogger(__name__)
telegram_alert_queue: asyncio.Queue = None

class ZMQServer:
    """مدیریت سرور ZMQ برای ارتباط با اکسپرت‌ها."""
    def __init__(self, alert_queue: asyncio.Queue):
        self.context = zmq.asyncio.Context()
        self.publish_queue = asyncio.Queue(maxsize=1000)
        self.processing_queue = asyncio.Queue(maxsize=1000)
        global telegram_alert_queue
        telegram_alert_queue = alert_queue
        logger.info("سرور ZMQ با صف هشدار تلگرام مقداردهی شد.")



    async def start_config_responder(self):
        """
        پاسخگویی غیرمسدود (non-blocking) به درخواست‌های تنظیمات (Config)
        این تابع به درخواست‌های ZMQ.REP گوش می‌دهد و با فراخوانی
        تابع دیتابیس در یک ترد جداگانه، تنظیمات را برای اکسپرت کپی ارسال می‌کند.
        """
        socket = self.context.socket(zmq.REP)
        socket.bind(f"tcp://*:{CONFIG_PORT}")
        logger.info(f"Config Responder (REP) listening on port {CONFIG_PORT}...")
        
        while True:
            log_extra = {}
            request_data = {}
            try:
                request_raw = await socket.recv_string()
                request_data = json.loads(request_raw)
                copy_id_str = request_data.get("copy_id_str")

                log_extra = {
                    "component": "ConfigResponder",
                    "command": request_data.get("command"),
                    "copy_id": copy_id_str
                }
                
                logger.info(f"Config request received.", extra=log_extra)

                if request_data.get("command") == "GET_CONFIG":
                    if not copy_id_str:
                        raise ValueError("copy_id_str is missing")
                    
                    # [بهبود عملکرد] فراخوانی مسدودکننده دیتابیس به یک ترد جداگانه منتقل شد
                    config_data = await asyncio.to_thread(
                        database.get_config_for_copy_ea, 
                        copy_id_str
                    )
                    
                    response = {"status": "OK", "config": config_data}
                    logger.info(f"Sending config for {copy_id_str} (non-blocking).", extra=log_extra)
                else:
                    raise ValueError("Unknown command")
            
            except Exception as e:
                log_extra["error"] = str(e)
                log_extra["raw_request"] = request_data # ثبت درخواست کامل در صورت خطا
                logger.error(f"Config request failed: {e}", extra=log_extra)
                response = {"status": "ERROR", "message": str(e)}
            
            await socket.send_json(response)




    async def start_signal_collector(self):
        """جمع‌آوری سیگنال‌ها و گزارش‌ها از اکسپرت‌ها."""
        socket = self.context.socket(zmq.PULL)
        socket.bind(f"tcp://*:{SIGNAL_PORT}")
        logger.info(f"Signal Collector (PULL) listening on port {SIGNAL_PORT}...")
        while True:
            try:
                signal_raw = await socket.recv_string()
                signal_data = json.loads(signal_raw)
                await self.processing_queue.put(signal_data)
            except Exception as e:
                logger.error(f"Error receiving signal: {e}")




    async def start_signal_processor(self):
        """
        پردازشگر اصلی سیگنال‌ها از صف داخلی.
        این تابع سیگنال‌ها را از صف برداشته، نوع آن‌ها را تشخیص داده،
        در صورت نیاز به صف انتشار (publish_queue) یا صف تلگرام (telegram_alert_queue) ارسال کرده
        و گزارش‌های دریافتی را به صورت غیرمسدود (non-blocking) در دیتابیس ذخیره می‌کند.
        """
        logger.info("Signal Processor task started. Waiting for signals...")

        while True:
            signal_data = None
            log_extra = {} 

            try:
                signal_data = await self.processing_queue.get()
                event_type = signal_data.get("event", "UNKNOWN_EVENT")

                log_extra = {
                    "event_type": event_type,
                    "source_id": signal_data.get("source_id_str"),
                    "copy_id": signal_data.get("copy_id_str"),
                    "position_id": signal_data.get("position_id"),
                    "symbol": signal_data.get("symbol"),
                    "ea_id": signal_data.get("ea_id", signal_data.get("source_id_str", signal_data.get("copy_id_str", "EA"))),
                }

                logger.debug("Received signal from queue.", extra=log_extra)

                if event_type == "PING" or event_type == "PING_COPY":
                    ea_type = "SourceEA" if event_type == "PING" else "CopyEA"
                    logger.info(f"{ea_type} ({log_extra['ea_id']}) is alive (PING received).", extra=log_extra)

                elif event_type in ["TRADE_OPEN", "TRADE_MODIFY", "TRADE_CLOSE_MASTER", "TRADE_PARTIAL_CLOSE_MASTER"]:
                    logger.info(f"Processing Master signal: {event_type}", extra=log_extra)
                    await self.publish_queue.put(signal_data)
                    logger.debug(f"Signal {event_type} put on publish_queue.", extra=log_extra)

                    msg = None
                    if event_type == "TRADE_OPEN":
                        msg = (
                            f"✅ *سیگنال باز شدن*\n\n"
                            f"▫️ *سورس:* `{log_extra['source_id']}`\n"
                            f"▫️ *نماد:* `{log_extra['symbol']}`\n"
                            f"▫️ *نوع:* `{'BUY' if signal_data.get('position_type') == 0 else 'SELL'}`\n"
                            f"▫️ *تیکت سورس:* `{log_extra['position_id']}`"
                        )
                    elif event_type == "TRADE_MODIFY":
                        msg = (
                            f"🔄 *سیگنال اصلاح شد*\n\n"
                            f"▫️ *سورس:* `{log_extra['source_id']}`\n"
                            f"▫️ *نماد:* `{log_extra['symbol']}`\n"
                            f"▫️ *تیکت سورس:* `{log_extra['position_id']}`\n"
                            f"▫️ *SL جدید:* `{signal_data.get('position_sl', 0.0):.5f}`\n"
                            f"▫️ *TP جدید:* `{signal_data.get('position_tp', 0.0):.5f}`"
                        )
                    elif event_type == "TRADE_CLOSE_MASTER":
                        msg = (
                            f"☑️ *بسته شدن (توسط مستر)*\n\n"
                            f"▫️ *سورس:* `{log_extra['source_id']}`\n"
                            f"▫️ *نماد:* `{log_extra['symbol']}`\n"
                            f"▫️ *سود:* `{signal_data.get('profit', 0.0):.2f}`\n"
                            f"▫️ *تیکت سورس:* `{log_extra['position_id']}`"
                        )
                    elif event_type == "TRADE_PARTIAL_CLOSE_MASTER":
                        msg = (
                            f"✂️ *بسته شدن بخشی (توسط مستر)*\n\n"
                            f"▫️ *سورس:* `{log_extra['source_id']}`\n"
                            f"▫️ *نماد:* `{log_extra['symbol']}`\n"
                            f"▫️ *حجم بسته شده:* `{signal_data.get('volume_closed', 0.0):.2f}`\n"
                            f"▫️ *سود:* `{signal_data.get('profit', 0.0):.2f}`\n"
                            f"▫️ *تیکت سورس:* `{log_extra['position_id']}`"
                        )
                        
                    if msg and telegram_alert_queue:
                        await telegram_alert_queue.put(msg)
                        logger.debug(f"Telegram alert sent for {event_type}.", extra=log_extra)

                elif event_type == "TRADE_CLOSED_COPY":
                    profit = signal_data.get("profit", 0.0)
                    source_ticket = signal_data.get("source_ticket")
                    
                    log_extra.update({
                        "profit": profit,
                        "source_ticket": source_ticket
                    })
                    
                    logger.info(f"Processing Copy close report.", extra=log_extra)
                    
                    try:
                        # [بهبود عملکرد] فراخوانی مسدودکننده دیتابیس به یک ترد جداگانه منتقل شد
                        await asyncio.to_thread(
                            database.save_trade_history,
                            copy_id_str=log_extra['copy_id'],
                            source_id_str=log_extra['source_id'],
                            symbol=log_extra['symbol'],
                            profit=profit,
                            source_ticket=source_ticket
                        )
                        logger.info("Trade history saved to DB (non-blocking).", extra=log_extra)
                        
                    except ValueError as ve: 
                        logger.error(f"Failed to save trade history: {ve}", extra=log_extra)
                        if telegram_alert_queue:
                            await telegram_alert_queue.put(f"⚠️ *خطای ذخیره تاریخچه*\n\n{escape_markdown(str(ve), 2)}")
                    except Exception as db_e: 
                        logger.error(f"Critical DB error saving trade history: {db_e}", exc_info=True, extra=log_extra)
                        if telegram_alert_queue:
                            await telegram_alert_queue.put(f"🚨 *خطای شدید دیتابیس*\n\n عدم موفقیت در ذخیره تاریخچه معامله `{log_extra['copy_id']}` از سورس `{log_extra['source_id']}`. جزئیات در لاگ سرور.")

                    emoji = "🔻" if profit < 0 else "✅"
                    msg = (
                        f"{emoji} *معامله کپی شده بسته شد*\n\n"
                        f"▫️ *حساب کپی:* `{log_extra['copy_id']}`\n"
                        f"▫️ *سورس:* `{log_extra['source_id']}`\n"
                        f"▫️ *نماد:* `{log_extra['symbol']}`\n"
                        f"▫️ *سود/زیان:* `{profit:.2f}`\n"
                        f"▫️ *تیکت سورس:* `{source_ticket}`"
                    )
                    if telegram_alert_queue:
                        await telegram_alert_queue.put(msg)
                        logger.debug("Telegram alert sent for TRADE_CLOSED_COPY.", extra=log_extra)

                elif event_type == "EA_ERROR":
                    error_message = signal_data.get('message', 'No details provided.')
                    log_extra['error_message'] = error_message
                    logger.warning(f"EA Error Reported from {log_extra['ea_id']}.", extra=log_extra)
                    msg = (
                        f"🚨 *خطای اکسپرت*\n\n"
                        f"*{log_extra['ea_id']}*:\n"
                        f"`{escape_markdown(error_message, 2)}`"
                    )
                    if telegram_alert_queue:
                        await telegram_alert_queue.put(msg)
                        logger.debug("Telegram alert sent for EA_ERROR.", extra=log_extra)

                else:
                    log_extra['raw_signal'] = signal_data
                    logger.warning(f"Unknown event type received.", extra=log_extra)
                    if telegram_alert_queue:
                         await telegram_alert_queue.put(f"⚠️ *رویداد ناشناخته*\n\n سرور یک پیام با نوع `{event_type}` دریافت کرد که قادر به پردازش آن نیست. جزئیات در لاگ سرور.")


            except json.JSONDecodeError as json_err:
                log_extra['error'] = str(json_err)
                log_extra['raw_signal_on_error'] = signal_data
                logger.error("Failed to decode JSON signal.", extra=log_extra)
                if telegram_alert_queue:
                     await telegram_alert_queue.put("🚨 *خطای JSON*\n\n سرور پیامی دریافت کرد که قابل پارس کردن به عنوان JSON نبود. پیام خام در لاگ سرور ثبت شد.")
            
            except Exception as e:
                log_extra['error'] = str(e)
                log_extra['raw_signal_on_error'] = signal_data
                logger.critical(f"Critical unhandled error in signal processing task: {e}", exc_info=True, extra=log_extra)
                if telegram_alert_queue:
                     await telegram_alert_queue.put(f"🆘 *خطای بحرانی در پردازشگر سیگنال*\n\n خطای پیش‌بینی نشده: `{escape_markdown(str(e), 2)}`. لطفاً لاگ‌های سرور را فوراً بررسی کنید.")

            finally:
                if self.processing_queue:
                     self.processing_queue.task_done()




    async def start_signal_publisher(self):
        """انتشار سیگنال‌ها برای اکسپرت‌های اسلیو."""
        socket = self.context.socket(zmq.PUB)
        socket.bind(f"tcp://*:{PUBLISH_PORT}")
        logger.info(f"Signal Publisher (PUB) listening on port {PUBLISH_PORT}...")
        while True:
            try:
                signal_data = await self.publish_queue.get()
                topic = signal_data.get("source_id_str")
                if not topic:
                    logger.warning(f"Signal has no 'source_id_str' to use as topic: {signal_data}")
                    continue
                logger.info(f"Publishing on topic '{topic}': {signal_data}")
                await socket.send_string(topic, flags=zmq.SNDMORE)
                await socket.send_json(signal_data)
            except Exception as e:
                logger.error(f"Error publishing signal: {e}")
            finally:
                self.publish_queue.task_done()

    async def run(self):
        """اجرای همزمان تسک‌های سرور ZMQ."""
        logger.info("ZMQ Core Service Starting...")
        try:
            await asyncio.gather(
                self.start_config_responder(),
                self.start_signal_collector(),
                self.start_signal_processor(),
                self.start_signal_publisher()
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("ZMQ Core Service Shutting Down...")
        finally:
            self.context.term()