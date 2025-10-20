import logging
import logging.handlers
import json
import sys

class JsonFormatter(logging.Formatter):
    """فرمت‌کننده لاگ برای خروجی JSON فشرده."""
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'name': record.name,
            'message': record.getMessage(),
            'funcName': record.funcName,
            'lineno': record.lineno,
        }
        extra_keys = [
            'user_id', 'username', 'callback_data', 'command', 
            'input_for', 'action_attempt', 'status', 'entity_id', 
            'details', 'error'
        ]
        for key in extra_keys:
            if hasattr(record, key):
                log_data[key] = getattr(record, key)
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        return json.dumps(log_data, separators=(',', ':'), ensure_ascii=False)

def setup_logging():
    """پیکربندی سیستم لاگ‌نویسی برنامه."""
    json_formatter = JsonFormatter()
    file_handler = logging.handlers.RotatingFileHandler(
        filename="trade_copier.log",
        maxBytes=5*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(json_formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(json_formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.info("Logging system initialized successfully in JSON format.")
