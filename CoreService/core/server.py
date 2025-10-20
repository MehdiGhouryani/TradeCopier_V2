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
        """پاسخگویی به درخواست‌های تنظیمات اکسپرت‌های اسلیو."""
        socket = self.context.socket(zmq.REP)
        socket.bind(f"tcp://*:{CONFIG_PORT}")
        logger.info(f"Config Responder (REP) listening on port {CONFIG_PORT}...")
        while True:
            try:
                request_raw = await socket.recv_string()
                request_data = json.loads(request_raw)
                logger.info(f"Config request received: {request_data}")
                if request_data.get("command") == "GET_CONFIG":
                    copy_id_str = request_data.get("copy_id_str")
                    if not copy_id_str:
                        raise ValueError("copy_id_str is missing")
                    config_data = database.get_config_for_copy_ea(copy_id_str)
                    response = {"status": "OK", "config": config_data}
                    logger.info(f"Sending config for {copy_id_str}...")
                else:
                    raise ValueError("Unknown command")
            except Exception as e:
                logger.error(f"Config request failed: {e}")
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
        """پردازش سیگنال‌های دریافتی و ارسال هشدار به تلگرام."""
        logger.info("Signal Processor task started.")
        while True:
            try:
                signal_data = await self.processing_queue.get()
                event_type = signal_data.get("event")
                if event_type in ["TRADE_OPEN", "TRADE_MODIFY", "TRADE_CLOSE_MASTER"]:
                    await self.publish_queue.put(signal_data)
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
                    logger.info(f"Saving trade history: {signal_data}")
                    database.save_trade_history(
                        copy_id_str=signal_data.get("copy_id_str"),
                        source_id_str=signal_data.get("source_id_str"),
                        symbol=signal_data.get("symbol"),
                        profit=signal_data.get("profit"),
                        source_ticket=signal_data.get("source_ticket")
                    )
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