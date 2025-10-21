#property copyright "TradeCopier Professional"
#property version "1.0"
#property strict

#include <Trade/Trade.mqh>
#include <Include/ZMQ/zmq.mqh>
#include <Include/Common/json.mqh>

input string InpServerAddress = "127.0.0.1";
input int InpConfigPort = 5557;
input int InpPublishPort = 5556;
input int InpSignalPort = 5555;
input string InpCopyIDStr = "C_A";
input ulong InpMagicNumber = 12345;
input bool InpEnableDebugLogging = false;




struct sSourceConfig {
    string SourceTopicID;
    string CopyMode;
    string AllowedSymbols;
    string VolumeType;
    double VolumeValue;
    double MaxLotSize;
    int MaxConcurrentTrades;
    double SourceDrawdownLimit;
};

struct sGlobalConfig {
    double DailyDrawdownPercent;
    double AlertDrawdownPercent;
    bool ResetDDFlag;
};

struct PositionMap {
    ulong ticket;
    long source_pos_id;
    string source_id_str;
};

struct RetryMessage
{
    int    attempts;    // تعداد تلاش‌ها
    int    data_len;    // طول واقعی داده
    uchar  json_data[MAX_MSG_SIZE]; // داده به صورت بایت (نه آبجکت string)
};


CTrade g_trade;
CJsonBuilder g_json_builder;

int g_zmq_context;
int g_zmq_socket_req;
int g_zmq_socket_sub;
int g_zmq_socket_push;

sSourceConfig g_source_configs[];
sGlobalConfig g_global_config;

PositionMap g_position_map[];
RetryMessage g_retry_queue[];
int g_ping_fails = 0;
int g_timer_count = 0;
double g_daily_dd = 0.0;
datetime g_last_dd_reset = 0;
datetime g_last_recv_time = 0;
string g_prev_topics[];
int g_log_file_handle = INVALID_HANDLE;
bool g_trading_stopped_by_dd = false;





/******************************************************************
 * مقداردهی اولیه اکسپرت، باز کردن فایل لاگ، و اتصال به ZMQ
 ******************************************************************/
int OnInit() {
    // [جدید] باز کردن فایل لاگ در اولین قدم
    string log_file_name = "CopyEA_" + InpCopyIDStr + ".log";
    g_log_file_handle = FileOpen(log_file_name, FILE_WRITE|FILE_ANSI|FILE_TXT|FILE_SHARE_READ);
    if(g_log_file_handle == INVALID_HANDLE)
       {
        Print("CRITICAL: [CopyEA] Could not open log file: " + log_file_name);
       }

    if (StringLen(InpCopyIDStr) == 0) {
        LogEvent("ERROR", "Invalid InpCopyIDStr: empty", 0);
        return(INIT_PARAMETERS_INCORRECT);
    }
    if (InpMagicNumber == 0) {
        LogEvent("ERROR", "Invalid InpMagicNumber: zero");
        return(INIT_PARAMETERS_INCORRECT);
    }
    if (InpConfigPort <= 0 || InpPublishPort <= 0 || InpSignalPort <= 0) {
        LogEvent("ERROR", "Invalid port numbers");
        return(INIT_PARAMETERS_INCORRECT);
    }

    g_trade.SetExpertMagicNumber(InpMagicNumber);
    g_trade.SetMarginMode();

    g_zmq_context = ZmqContextNew(2);
    if (g_zmq_context == 0) {
        LogEvent("ERROR", "Failed to create ZMQ context", ZmqErrno());
        return(INIT_FAILED);
    }

    g_zmq_socket_push = ZmqSocketNew(g_zmq_context, ZMQ_PUSH);
    string push_address = "tcp://" + InpServerAddress + ":" + (string)InpSignalPort;
    if (ZmqConnect(g_zmq_socket_push, push_address) != 0) {
        LogEvent("ERROR", "Failed to connect ZMQ PUSH socket to " + push_address, ZmqErrno());
    } else {
        LogEvent("INFO", "PUSH socket connected to " + push_address);
    }
    ZmqSocketSet(g_zmq_socket_push, ZMQ_SNDHWM, 100);

    if (!FetchConfiguration()) {
        LogEvent("ERROR", "Failed to fetch configuration from server");
        CleanupZMQ();
        return(INIT_FAILED);
    }

    g_zmq_socket_sub = ZmqSocketNew(g_zmq_context, ZMQ_SUB);
    string sub_address = "tcp://" + InpServerAddress + ":" + (string)InpPublishPort;
    if (ZmqConnect(g_zmq_socket_sub, sub_address) != 0) {
        LogEvent("ERROR", "Failed to connect ZMQ SUB socket to " + sub_address, ZmqErrno());
        CleanupZMQ();
        return(INIT_FAILED);
    }
    ZmqSocketSet(g_zmq_socket_sub, ZMQ_RCVHWM, 100);

    UpdateSubscriptions();

    EventSetMillisecondTimer(100);

    LogEvent("INFO", "--- Copy EA Initialized Successfully ---");
    return(INIT_SUCCEEDED);
}




//+------------------------------------------------------------------+
//| توابع کمکی برای پارس کردن دستی JSON
//+------------------------------------------------------------------+

// تابع برای استخراج مقدار رشته‌ای از JSON
string JsonGetString(const string& json, const string& key)
{
    string key_pattern = "\"" + key + "\":\"";
    int start_pos = StringFind(json, key_pattern);
    if(start_pos < 0) return ""; // کلید یافت نشد

    start_pos += StringLen(key_pattern);
    int end_pos = StringFind(json, "\"", start_pos);
    if(end_pos < 0) return ""; // خطای فرمت

    // استخراج و حذف بک‌اسلش‌های escape شده
    string value = StringSubstr(json, start_pos, end_pos - start_pos);
    StringReplace(value, "\\\\", "\\"); // \\ -> \
    StringReplace(value, "\\\"", "\""); // \" -> "
    // ... (می‌توان موارد دیگر مانند \n, \t را نیز اضافه کرد)
    return value;
}

// تابع برای استخراج مقدار عددی (double) از JSON
double JsonGetDouble(const string& json, const string& key)
{
    string key_pattern = "\"" + key + "\":";
    int start_pos = StringFind(json, key_pattern);
    if(start_pos < 0) return 0.0; // کلید یافت نشد

    start_pos += StringLen(key_pattern);
    // پیدا کردن پایان عدد (ویرگول یا آکولاد بسته)
    int end_pos1 = StringFind(json, ",", start_pos);
    int end_pos2 = StringFind(json, "}", start_pos);
    int end_pos = -1;

    if(end_pos1 >= 0 && end_pos2 >= 0) end_pos = MathMin(end_pos1, end_pos2);
    else if(end_pos1 >= 0) end_pos = end_pos1;
    else if(end_pos2 >= 0) end_pos = end_pos2;

    if(end_pos < 0) return 0.0; // خطای فرمت

    string value_str = StringSubstr(json, start_pos, end_pos - start_pos);
    return StringToDouble(value_str);
}

// تابع برای استخراج مقدار عددی (long) از JSON
long JsonGetLong(const string& json, const string& key)
{
    // مشابه JsonGetDouble اما با StringToInteger
    string key_pattern = "\"" + key + "\":";
    int start_pos = StringFind(json, key_pattern);
    if(start_pos < 0) return 0;

    start_pos += StringLen(key_pattern);
    int end_pos1 = StringFind(json, ",", start_pos);
    int end_pos2 = StringFind(json, "}", start_pos);
    int end_pos = -1;

    if(end_pos1 >= 0 && end_pos2 >= 0) end_pos = MathMin(end_pos1, end_pos2);
    else if(end_pos1 >= 0) end_pos = end_pos1;
    else if(end_pos2 >= 0) end_pos = end_pos2;

    if(end_pos < 0) return 0;

    string value_str = StringSubstr(json, start_pos, end_pos - start_pos);
    return StringToInteger(value_str);
}


// تابع برای استخراج مقدار بولی (boolean) از JSON
bool JsonGetBool(const string& json, const string& key)
{
    string key_pattern = "\"" + key + "\":";
    int start_pos = StringFind(json, key_pattern);
    if(start_pos < 0) return false;

    start_pos += StringLen(key_pattern);
    // پیدا کردن پایان مقدار بولی (true یا false)
    int end_pos1 = StringFind(json, ",", start_pos);
    int end_pos2 = StringFind(json, "}", start_pos);
    int end_pos = -1;

    if(end_pos1 >= 0 && end_pos2 >= 0) end_pos = MathMin(end_pos1, end_pos2);
    else if(end_pos1 >= 0) end_pos = end_pos1;
    else if(end_pos2 >= 0) end_pos = end_pos2;

    if(end_pos < 0) return false;

    string value_str = StringSubstr(json, start_pos, end_pos - start_pos);
    StringTrim(value_str); // حذف فضاهای احتمالی
    return (value_str == "true");
}


//+------------------------------------------------------------------+
//| [بازنویسی شده] دریافت و پارس کردن تنظیمات از سرور (بدون CJAVal)
//+------------------------------------------------------------------+
bool FetchConfiguration()
{
    LogEvent("INFO", "Attempting to fetch configuration for '" + InpCopyIDStr + "'", 0);
    g_zmq_socket_req = ZmqSocketNew(g_zmq_context, ZMQ_REQ);
    string req_address = "tcp://" + InpServerAddress + ":" + (string)InpConfigPort;
    if (ZmqConnect(g_zmq_socket_req, req_address) != 0)
    {
        LogEvent("ERROR", "Failed to connect ZMQ REQ socket to " + req_address, ZmqErrno());
        return false;
    }

    // ارسال درخواست
    g_json_builder.Init();
    g_json_builder.Add("command", "GET_CONFIG");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);
    if (ZmqSendString(g_zmq_socket_req, g_json_builder.ToString(), 0) == -1)
    {
        LogEvent("ERROR", "Failed to send config request", ZmqErrno());
        ZmqClose(g_zmq_socket_req);
        return false;
    }

    // دریافت پاسخ
    uchar recv_buffer[65536]; // بافر بزرگ برای تنظیمات حجیم
    int recv_len = ZmqRecv(g_zmq_socket_req, recv_buffer, 65536, 0);
    ZmqClose(g_zmq_socket_req); // بستن سوکت REQ بلافاصله پس از دریافت
    if (recv_len <= 0)
    {
        LogEvent("ERROR", "Failed to receive config response", ZmqErrno());
        return false;
    }

    string json_response = CharArrayToString(recv_buffer, 0, recv_len, CP_UTF8);

    // --- [بازنویسی شده] پارس کردن دستی JSON ---
    LogEvent("DEBUG", "Received config JSON: " + json_response, 0); // لاگ دیباگ (اختیاری)

    // 1. بررسی وضعیت کلی
    string status = JsonGetString(json_response, "status");
    if (status != "OK")
    {
        string message = JsonGetString(json_response, "message");
        LogEvent("ERROR", "Server returned an error: " + message, 0);
        return false;
    }

    // 2. پیدا کردن آبجکت "config"
    int config_start = StringFind(json_response, "\"config\":{");
    if (config_start < 0)
    {
        LogEvent("ERROR", "Could not find 'config' object in JSON response", 0);
        return false;
    }
    config_start += StringLen("\"config\":{"); // رفتن به ابتدای محتوای آبجکت
    // پیدا کردن آکولاد بسته متناظر (این بخش ساده‌سازی شده و ممکن است برای JSONهای پیچیده خطا دهد)
    int config_end = StringFind(json_response, "}}", config_start); // فرض می‌کنیم config آخرین بخش است
     if (config_end < 0) config_end = StringLen(json_response) -1; // اگر آخرین بخش نبود
     else config_end +=1; // شامل } دوم هم بشود
     
    string config_json = StringSubstr(json_response, config_start, config_end - config_start );
     if (StringLen(config_json) > 0 && StringGetCharacter(config_json, StringLen(config_json)-1) != '}') // افزودن } پایانی اگر لازم بود
     {
      config_json = StringSubstr(json_response, config_start, config_end - config_start +1); // یک کاراکتر بیشتر بخوان
     }
      


    // 3. پارس کردن "global_settings"
    int gs_start = StringFind(config_json, "\"global_settings\":{");
    if (gs_start < 0)
    {
        LogEvent("ERROR", "Could not find 'global_settings' in config JSON", 0);
        return false;
    }
    gs_start += StringLen("\"global_settings\":{");
    int gs_end = StringFind(config_json, "}", gs_start);
    if (gs_end < 0)
    {
        LogEvent("ERROR", "Could not find end of 'global_settings' in config JSON", 0);
        return false;
    }
    string gs_json = StringSubstr(config_json, gs_start, gs_end - gs_start);

    g_global_config.DailyDrawdownPercent = JsonGetDouble(gs_json, "daily_drawdown_percent");
    g_global_config.AlertDrawdownPercent = JsonGetDouble(gs_json, "alert_drawdown_percent");
    g_global_config.ResetDDFlag = JsonGetBool(gs_json, "reset_dd_flag");

    LogEvent("INFO", "Global settings loaded: DD=" + DoubleToString(g_global_config.DailyDrawdownPercent, 2) + "%, Alert=" + DoubleToString(g_global_config.AlertDrawdownPercent, 2) + "%", 0);

    // اعمال منطق ریست کردن DD
    if (g_global_config.ResetDDFlag)
    {
        g_daily_dd = 0.0;
        g_last_dd_reset = TimeCurrent();
        g_trading_stopped_by_dd = false;
        g_global_config.ResetDDFlag = false; // فلگ را مصرف کن
        LogEvent("INFO", "DD reset triggered by admin. Trading re-enabled.", 0);
        // نکته: برای ذخیره دائمی False شدن فلگ، نیاز به ارسال پاسخ به سرور است (در این پیاده‌سازی نیست)
    }

    // 4. پارس کردن آرایه "mappings"
    int mappings_start = StringFind(config_json, "\"mappings\":[");
    if (mappings_start < 0)
    {
        LogEvent("WARN", "No 'mappings' array found in config JSON", 0);
        ArrayResize(g_source_configs, 0); // اگر مپینگ نبود، آرایه را خالی کن
        return true; // عدم وجود مپینگ لزوما خطا نیست
    }
    mappings_start += StringLen("\"mappings\":[");
    int mappings_end = StringFind(config_json, "]", mappings_start);
    if (mappings_end < 0)
    {
        LogEvent("ERROR", "Could not find end of 'mappings' array in config JSON", 0);
        return false;
    }
    string mappings_content = StringSubstr(config_json, mappings_start, mappings_end - mappings_start);

    // شمردن تعداد آبجکت‌های مپینگ (بر اساس '{')
    int current_pos = 0;
    int mappings_count = 0;
    while(StringFind(mappings_content, "{", current_pos) >= 0)
    {
        mappings_count++;
        current_pos = StringFind(mappings_content, "{", current_pos) + 1;
    }

    ArrayResize(g_source_configs, mappings_count); // تغییر اندازه آرایه گلوبال
    current_pos = 0; // ریست کردن پوزیشن برای حلقه اصلی

    for (int i = 0; i < mappings_count; i++)
    {
        int map_obj_start = StringFind(mappings_content, "{", current_pos);
        if (map_obj_start < 0) break; // آبجکت دیگری یافت نشد
        int map_obj_end = StringFind(mappings_content, "}", map_obj_start);
        if (map_obj_end < 0) break; // خطای فرمت

        string map_json = StringSubstr(mappings_content, map_obj_start + 1, map_obj_end - map_obj_start - 1);

        // استخراج مقادیر برای هر مپینگ
        g_source_configs[i].SourceTopicID = JsonGetString(map_json, "source_topic_id");
        g_source_configs[i].CopyMode = JsonGetString(map_json, "copy_mode");
        // برای allowed_symbols که ممکن است null باشد، JsonGetString مقدار "" برمی‌گرداند که اوکی است
        g_source_configs[i].AllowedSymbols = JsonGetString(map_json, "allowed_symbols");
        g_source_configs[i].VolumeType = JsonGetString(map_json, "volume_type");
        g_source_configs[i].VolumeValue = JsonGetDouble(map_json, "volume_value");
        g_source_configs[i].MaxLotSize = JsonGetDouble(map_json, "max_lot_size");
        g_source_configs[i].MaxConcurrentTrades = (int)JsonGetLong(map_json, "max_concurrent_trades"); // تبدیل long به int
        g_source_configs[i].SourceDrawdownLimit = JsonGetDouble(map_json, "source_drawdown_limit");

        // اعتبارسنجی ساده (مثلاً VolumeType باید معتبر باشد)
        if(g_source_configs[i].VolumeType != "MULTIPLIER" && g_source_configs[i].VolumeType != "FIXED")
        {
             LogEvent("WARN", "Invalid VolumeType '" + g_source_configs[i].VolumeType + "' for mapping " + g_source_configs[i].SourceTopicID + ". Defaulting to MULTIPLIER.", 0);
             g_source_configs[i].VolumeType = "MULTIPLIER";
             g_source_configs[i].VolumeValue = 1.0;
        }


        LogEvent("INFO", "Mapping #" + IntegerToString(i) + " loaded: Source=" + g_source_configs[i].SourceTopicID + ", Vol=" + DoubleToString(g_source_configs[i].VolumeValue, 2) + " (" + g_source_configs[i].VolumeType + ")", 0);

        current_pos = map_obj_end + 1; // برو به بعد از آبجکت فعلی
    }

    return true; // موفقیت‌آمیز بود
}
//+------------------------------------------------------------------+




/******************************************************************
 * پاکسازی نهایی اکسپرت هنگام خروج
 ******************************************************************/
void OnDeinit(const int reason) {
    LogEvent("INFO", "Deinitializing Copy EA...");

    EventKillTimer();
    CleanupZMQ();
    LogEvent("INFO", "Copy EA Deinitialized.");
}



/******************************************************************
 * پاکسازی تمام سوکت‌های ZMQ و بستن فایل لاگ
 ******************************************************************/
void CleanupZMQ() {
    if (g_zmq_socket_req != 0)
        ZmqClose(g_zmq_socket_req);
    if (g_zmq_socket_sub != 0)
        ZmqClose(g_zmq_socket_sub);
    if (g_zmq_socket_push != 0)
        ZmqClose(g_zmq_socket_push);
    if (g_zmq_context != 0)
        ZmqContextTerm(g_zmq_context);

    g_zmq_socket_req = 0;
    g_zmq_socket_sub = 0;
    g_zmq_socket_push = 0;
    g_zmq_context = 0;

    // [جدید] بستن فایل لاگ
    if(g_log_file_handle != INVALID_HANDLE)
       {
        FileClose(g_log_file_handle);
        g_log_file_handle = INVALID_HANDLE;
       }
}







/******************************************************************
 * [بازنویسی شده] مدیریت رویدادهای تایمر
 * 1. پردازش صف تلاش مجدد (با struct امن uchar[])
 * 2. رفرش تنظیمات دوره‌ای
 * 3. ارسال پینگ دوره‌ای
 * 4. بررسی سوکت سیگنال‌ها
 ******************************************************************/
void OnTimer()
{
    // --- 1. [بازنویسی شده] پردازش صف تلاش مجدد ---
    if (ArraySize(g_retry_queue) > 0)
    {
        RetryMessage msg;
        // کپی کردن داده‌های struct (امن است چون آبجکت ندارد)
        msg = g_retry_queue[0];

        // ارسال مستقیم آرایه بایت (uchar[])
        if (ZmqSend(g_zmq_socket_push, msg.json_data, msg.data_len, ZMQ_DONTWAIT) != -1)
        {
            // [اصلاح شده] اکنون ArrayRemove روی g_retry_queue کار می‌کند
            ArrayRemove(g_retry_queue, 0);
            // لاگ موفقیت حذف شد برای فشرده‌سازی
        }
        else
        {
            int err = ZmqErrno();
            // فقط در صورت خطا لاگ می‌نویسیم
            LogEvent("WARN", "Retry report send failed", err);
            // تلاش را افزایش می‌دهیم (فقط در متادیتای صف اصلی)
            g_retry_queue[0].attempts++;

            // اگر به حداکثر تلاش رسید، پیام را حذف کن
            if (g_retry_queue[0].attempts >= 3)
            {
                LogEvent("ERROR", "Max retries reached for report, discarding");
                ArrayRemove(g_retry_queue, 0); // حذف پیام از صف
            }
        }
    }

    // --- افزایش شمارنده تایمر ---
    g_timer_count++;

    // --- 2. رفرش کردن تنظیمات (هر 10 دقیقه) ---
    // 100ms * 6000 = 600,000ms = 10 minutes
    if (g_timer_count % 6000 == 0)
    {
        if (!FetchConfiguration())
        {
            // لاگ خطا داخل FetchConfiguration زده می‌شود
            LogEvent("WARN", "Config refresh failed", 0);
        }
        else
        {
            UpdateSubscriptions(); // اشتراک‌ها را بر اساس تنظیمات جدید آپدیت کن
            LogEvent("INFO", "Config refreshed successfully", 0);
        }
    }

    // --- 3. ارسال پینگ (هر 30 ثانیه) ---
    // 100ms * 300 = 30,000ms = 30 seconds
    if (g_timer_count % 300 == 0)
    {
        g_json_builder.Init();
        g_json_builder.Add("event", "PING_COPY");
        g_json_builder.Add("copy_id_str", InpCopyIDStr);
        g_json_builder.Add("timestamp", (long)TimeCurrent());
        string ping_msg = g_json_builder.ToString();

        if (ZmqSendString(g_zmq_socket_push, ping_msg, ZMQ_DONTWAIT) == -1)
        {
            LogEvent("WARN", "Ping send failed", ZmqErrno());
            g_ping_fails++;
            if (g_ping_fails >= 3)
            {
                LogEvent("ERROR", "Multiple ping fails, attempting reconnect", g_ping_fails);
                ReconnectSockets(); // تلاش برای اتصال مجدد
            }
        }
        else
        {
            g_ping_fails = 0; // در صورت موفقیت، شمارنده خطا ریست می‌شود
            // لاگ پینگ موفق حذف شد
        }
    }

    // --- 4. بررسی سیگنال‌های دریافتی ---
    // برای جلوگیری از بار زیاد CPU، فقط اگر مدتی کوتاه از دریافت قبلی گذشته باشد، چک کن
    // (این بخش تغییری نکرده است)
    if (TimeCurrent() - g_last_recv_time < 0.05)
         return; // اگر کمتر از 50 میلی‌ثانیه گذشته، خارج شو

    CheckSignalSocket(); // سوکت SUB را برای پیام‌های جدید چک کن
}
//+------------------------------------------------------------------+





void ReconnectSockets() {
    ZmqDisconnect(g_zmq_socket_sub, "tcp://" + InpServerAddress + ":" + (string)InpPublishPort);
    ZmqDisconnect(g_zmq_socket_push, "tcp://" + InpServerAddress + ":" + (string)InpSignalPort);

    if (ZmqConnect(g_zmq_socket_sub, "tcp://" + InpServerAddress + ":" + (string)InpPublishPort) == 0 &&
        ZmqConnect(g_zmq_socket_push, "tcp://" + InpServerAddress + ":" + (string)InpSignalPort) == 0) {
        LogEvent("INFO", "Reconnect successful");
        g_ping_fails = 0;
        UpdateSubscriptions();
    } else {
        LogEvent("CRITICAL", "Reconnect failed", ZmqErrno());
    }
}





//+------------------------------------------------------------------+
//| [بازنویسی شده] بروزرسانی اشتراک‌های ZMQ بر اساس تنظیمات جدید
//| از ZmqSocketSetBytes برای Subscribe/Unsubscribe استفاده می‌کند
//+------------------------------------------------------------------+
void UpdateSubscriptions()
{
    // اگر سوکت SUB معتبر نیست، خارج شو
    if (g_zmq_socket_sub == 0)
        return;

    // --- لغو اشتراک تاپیک‌های قبلی ---
    for (int i = 0; i < ArraySize(g_prev_topics); i++)
    {
        // تبدیل رشته تاپیک قبلی به بایت
        uchar topic_array[];
        int len = StringToCharArray(g_prev_topics[i], topic_array, 0, -1, CP_UTF8) - 1;

        // [اصلاح شده] استفاده از ZmqSocketSetBytes
        if (ZmqSocketSetBytes(g_zmq_socket_sub, ZMQ_UNSUBSCRIBE, topic_array, len) != 0)
        {
             LogEvent("ERROR", "Failed to unsubscribe from old topic: " + g_prev_topics[i], ZmqErrno());
        }
        else
        {
             LogEvent("INFO", "Unsubscribed from old topic: " + g_prev_topics[i], 0);
        }
    }

    // تغییر اندازه آرایه تاپیک‌های قبلی برای ذخیره تاپیک‌های جدید
    ArrayResize(g_prev_topics, ArraySize(g_source_configs));

    // --- اشتراک تاپیک‌های جدید ---
    int mappings_count = ArraySize(g_source_configs);
    if (mappings_count == 0)
    {
        LogEvent("WARN", "No active mappings found in config, nothing to copy", 0);
    }

    for (int i = 0; i < mappings_count; i++)
    {
        string topic = g_source_configs[i].SourceTopicID;
        // تبدیل رشته تاپیک جدید به بایت
        uchar topic_array[];
        int len = StringToCharArray(topic, topic_array, 0, -1, CP_UTF8) - 1;

        // [اصلاح شده] استفاده از ZmqSocketSetBytes
        if (ZmqSocketSetBytes(g_zmq_socket_sub, ZMQ_SUBSCRIBE, topic_array, len) != 0)
        {
            LogEvent("ERROR", "Failed to subscribe to topic '" + topic + "'", ZmqErrno());
        }
        else
        {
            LogEvent("INFO", "Subscribed to topic: '" + topic + "'", 0);
            g_prev_topics[i] = topic; // ذخیره تاپیک فعلی برای لغو اشتراک در آینده
        }
    }
}
//+------------------------------------------------------------------+



//+------------------------------------------------------------------+
//| [بازنویسی شده] بررسی سوکت SUB برای سیگنال‌های جدید
//| و اصلاح فراخوانی ZmqSocketGet
//+------------------------------------------------------------------+
void CheckSignalSocket()
{
    // اگر سوکت SUB معتبر نیست، خارج شو
    if (g_zmq_socket_sub == 0)
        return;

    uchar recv_buffer[4096]; // بافر دریافت
    string topic_received;
    string json_message;

    // حلقه برای خواندن تمام پیام‌های موجود در سوکت (non-blocking)
    while (true)
    {
        // 1. خواندن بخش اول: تاپیک
        int topic_len = ZmqRecv(g_zmq_socket_sub, recv_buffer, 4096, ZMQ_DONTWAIT);
        // اگر پیامی نبود یا خطایی رخ داد، از حلقه خارج شو
        if (topic_len <= 0)
            break;

        topic_received = CharArrayToString(recv_buffer, 0, topic_len, CP_UTF8);

        // 2. بررسی اینکه آیا بخش دومی (بدنه پیام) وجود دارد یا خیر
        int rcvmore = 0;
        // [اصلاح شده] فراخوانی صحیح ZmqSocketGet با دو پارامتر
        if (ZmqSocketGet(g_zmq_socket_sub, ZMQ_RCVMORE, rcvmore) != 0)
        {
             // اگر دریافت آپشن ZMQ_RCVMORE با خطا مواجه شد
             LogEvent("ERROR", "Failed to get ZMQ_RCVMORE option", ZmqErrno());
             continue; // برو سراغ پیام بعدی (اگر وجود داشت)
        }

        // اگر بخش دومی نبود (پیام ناقص)، لاگ بنویس و ادامه بده
        if (!rcvmore)
        {
            LogEvent("WARN", "Received topic '" + topic_received + "' but no message body", 0);
            continue;
        }

        // 3. خواندن بخش دوم: بدنه پیام (JSON)
        int msg_len = ZmqRecv(g_zmq_socket_sub, recv_buffer, 4096, ZMQ_DONTWAIT);
        if (msg_len <= 0)
        {
            LogEvent("WARN", "Message body read failed for topic '" + topic_received + "'", ZmqErrno());
            continue;
        }

        json_message = CharArrayToString(recv_buffer, 0, msg_len, CP_UTF8);

        // لاگ دریافت موفق و آپدیت زمان آخرین دریافت
        // LogEvent("INFO", "Received signal on topic '" + topic_received + "'", 0); // لاگ فشرده شد
        g_last_recv_time = TimeCurrent();

        // 4. ارسال پیام JSON برای پردازش
        ProcessSignal(topic_received, json_message);
    }
}
//+------------------------------------------------------------------+



//+------------------------------------------------------------------+
//| محاسبه مجموع سود/زیان شناور برای یک سورس خاص                     |
//+------------------------------------------------------------------+
double GetSourceFloatingProfitLoss(string source_id_str)
{
    double total_profit_loss = 0.0;
    int map_size = ArraySize(g_position_map);
    
    for(int i = 0; i < map_size; i++)
    {
        if(g_position_map[i].source_id_str == source_id_str)
        {
            if(PositionSelectByTicket(g_position_map[i].ticket))
            {
                total_profit_loss += PositionGetDouble(POSITION_PROFIT);
            }
            else
            {
                LogEvent("WARN", "Position ticket not found during DD calculation, removing from map", g_position_map[i].ticket);
                RemoveFromMap(g_position_map[i].ticket);
                map_size--;
                i--;
            }
        }
    }
    
    return total_profit_loss;
}





/******************************************************************
 * محاسبه مجموع سود/زیان شناور تمام معاملات باز این اکسپرت
 ******************************************************************/
double GetTotalFloatingPL()
{
    double total_profit_loss = 0.0;
    int map_size = ArraySize(g_position_map);

    for(int i = 0; i < map_size; i++)
    {
        if(PositionSelectByTicket(g_position_map[i].ticket))
        {
            total_profit_loss += PositionGetDouble(POSITION_PROFIT);
        }
        else
        {
            LogEvent("WARN", "Position ticket not found during DD (float) calculation, removing from map", g_position_map[i].ticket);
            RemoveFromMap(g_position_map[i].ticket);
            map_size--;
            i--;
        }
    }
    
    return total_profit_loss;
}






/******************************************************************
 * پردازش سیگنال دریافت شده از سرور (باز کردن، اصلاح، یا بستن معامله)
 ******************************************************************/
void ProcessSignal(string source_topic, string json_message)
{
    CJAVal signal;
    if(!signal.Deserialize(json_message))
    {
        LogEvent("ERROR", "Failed to deserialize signal JSON: " + json_message);
        return;
    }

    string event_type = signal["event"].ToStr();
    LogEvent("INFO", "Processing signal event: " + event_type);

    sSourceConfig config;
    bool config_found = false;
    for(int i = 0; i < ArraySize(g_source_configs); i++)
    {
        if(g_source_configs[i].SourceTopicID == source_topic)
        {
            config = g_source_configs[i];
            config_found = true;
            break;
        }
    }

    if(!config_found)
    {
        LogEvent("WARN", "Signal from unconfigured source '" + source_topic + "'");
        return;
    }

    long source_pos_id = signal["position_id"].ToInt();
    string symbol = signal["symbol"].ToStr();
    double volume = signal["volume"].ToDbl();
    double price = signal["price"].ToDbl();
    double sl = signal["position_sl"].ToDbl();
    double tp = signal["position_tp"].ToDbl();
    long position_type = signal["position_type"].ToInt();
    if(symbol == "" || (volume <= 0 && event_type == "TRADE_OPEN"))
    {
        LogEvent("ERROR", "Invalid signal data: symbol or volume", symbol);
        return;
    }

    if(!SymbolSelect(symbol, true))
    {
        LogEvent("ERROR", "Symbol not found: " + symbol);
        return;
    }

    if(config.CopyMode == "ALL")
    {
    }
    else if(config.CopyMode == "GOLD_ONLY")
    {
        if(StringFind(symbol, "XAU") < 0)
        {
            LogEvent("INFO", "Symbol filtered out (GOLD_ONLY): " + symbol);
            return;
        }
    }
    else if(config.CopyMode == "SYMBOLS")
    {
        if(StringFind(config.AllowedSymbols, symbol) < 0)
        {
            LogEvent("INFO", "Symbol filtered out (SYMBOLS): " + symbol);
            return;
        }
    }

    double final_volume;
    if(config.VolumeType == "MULTIPLIER")
    {
        final_volume = volume * config.VolumeValue;
    }
    else if(config.VolumeType == "FIXED")
    {
        final_volume = config.VolumeValue;
    }
    else
    {
        LogEvent("ERROR", "Invalid VolumeType: " + config.VolumeType);
        return;
    }

    double vol_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
    double vol_min = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
    double vol_max = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
    final_volume = NormalizeDouble(final_volume / vol_step, 0) * vol_step;
    if(final_volume < vol_min) final_volume = vol_min;
    if(final_volume > vol_max) final_volume = vol_max;
    if(config.MaxLotSize > 0 && final_volume > config.MaxLotSize)
    {
        LogEvent("WARN", "Volume exceeds MaxLotSize, capping", final_volume);
        final_volume = config.MaxLotSize;
    }

    string short_pos_id = IntegerToString((int)(source_pos_id % 100000000));
    string comment = short_pos_id + "|" + source_topic;
    if(StringLen(comment) > 31) comment = StringSubstr(comment, 0, 31);
    
    if(event_type == "TRADE_OPEN")
    {
        // [جدید] اعمال منطق کامل حد ضرر روزانه
        // 1. ابتدا چک می‌کنیم آیا روز عوض شده تا g_daily_dd ریست شود
        CheckDailyDrawdown(); 
        
        // 2. سپس حد ضرر کامل (شامل شناور) را چک می‌کنیم
        if(CheckDailyDDLimit())
        {
            LogEvent("WARN", "Trade rejected: Daily Drawdown Limit has been reached.");
            return;
        }
        // [پایان بخش جدید]
        
        int concurrent = 0;
        for(int i = 0; i < ArraySize(g_position_map); i++)
        {
            if(g_position_map[i].source_id_str == source_topic) concurrent++;
        }
        if(config.MaxConcurrentTrades > 0 && concurrent >= config.MaxConcurrentTrades)
        {
            LogEvent("WARN", "Max concurrent trades reached for source " + source_topic, concurrent);
            return;
        }

        if(config.SourceDrawdownLimit > 0.0)
        {
            double floating_pl = GetSourceFloatingProfitLoss(source_topic);
            if(floating_pl < -config.SourceDrawdownLimit)
            {
                string err_msg = "SourceDrawdownLimit exceeded for " + source_topic + ", P/L: " + DoubleToString(floating_pl, 2);
                LogEvent("WARN", "Trade rejected: " + err_msg);
                SendErrorReport(err_msg);
                return;
            }
        }

        // [حذف شده] بلاک کد قدیمی و ناقص بررسی DD در اینجا حذف شد
        
        LogEvent("INFO", "Executing OPEN: " + (position_type == 0 ? "BUY" : "SELL") + " " + symbol + " " + DoubleToString(final_volume, 2) + " lots from source " + source_topic);
        MqlTradeRequest req;
        ZeroMemory(req);
        req.action = TRADE_ACTION_DEAL;
        req.symbol = symbol;
        req.volume = final_volume;
        req.type = (position_type == 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL);
        req.deviation = 10;
        req.sl = sl;
        req.tp = tp;
        req.comment = comment;

        MqlTradeResult res;
        if(!g_trade.OrderSend(req, res))
        {
            LogEvent("ERROR", "Open trade failed", res.retcode);
            SendErrorReport("Open failed: " + symbol);
            return;
        }
        else
        {
            LogEvent("INFO", "Open trade successful, deal ticket=" + IntegerToString((int)res.deal) + ", position ticket=" + IntegerToString((int)res.position));
            int size = ArraySize(g_position_map);
            ArrayResize(g_position_map, size + 1);
            g_position_map[size].ticket = res.position;
            g_position_map[size].source_pos_id = source_pos_id;
            g_position_map[size].source_id_str = source_topic;
        }
    }
    else if(event_type == "TRADE_MODIFY")
    {
        LogEvent("INFO", "Executing MODIFY for position " + IntegerToString(source_pos_id) + " from source " + source_topic);
        ulong ticket = FindPositionBySourceID(source_pos_id);
        if(ticket == 0)
        {
            LogEvent("WARN", "Position not found for modify: " + IntegerToString(source_pos_id));
            return;
        }

        MqlTradeRequest req;
        ZeroMemory(req);
        req.action = TRADE_ACTION_SLTP;
        req.position = ticket;
        req.sl = sl;
        req.tp = tp;

        MqlTradeResult res;
        if(!g_trade.RequestExecute(req, res))
        {
            LogEvent("ERROR", "Modify trade failed", res.retcode);
            SendErrorReport("Modify failed: " + symbol);
        }
        else
        {
            LogEvent("INFO", "Modify successful for ticket " + IntegerToString((int)ticket));
        }
    }
    else if(event_type == "TRADE_CLOSE_MASTER")
    {
        LogEvent("INFO", "Executing CLOSE for position " + IntegerToString(source_pos_id) + " from source " + source_topic);
        ulong ticket = FindPositionBySourceID(source_pos_id);
        if(ticket == 0)
        {
            LogEvent("WARN", "Position not found for close: " + IntegerToString(source_pos_id));
            return;
        }

        if(!g_trade.PositionClose(ticket))
        {
            LogEvent("ERROR", "Close trade failed", g_trade.ResultRetcode());
            SendErrorReport("Close failed: " + symbol);
        }
        else
        {
            LogEvent("INFO", "Close successful for ticket " + IntegerToString((int)ticket));
            RemoveFromMap(ticket);
        }
    }
    else if(event_type == "TRADE_PARTIAL_CLOSE_MASTER")
    {
        double volume_closed = signal["volume_closed"].ToDbl();
        LogEvent("INFO", "Executing PARTIAL CLOSE for position " + IntegerToString(source_pos_id) + " volume " + DoubleToString(volume_closed, 2) + " from source " + source_topic);
        ulong ticket = FindPositionBySourceID(source_pos_id);
        if(ticket == 0)
        {
            LogEvent("WARN", "Position not found for partial close: " + IntegerToString(source_pos_id));
            return;
        }

        if(!g_trade.PositionClosePartial(ticket, volume_closed))
        {
            LogEvent("ERROR", "Partial close failed", g_trade.ResultRetcode());
            SendErrorReport("Partial close failed: " + symbol);
        }
        else
        {
            LogEvent("INFO", "Partial close successful for ticket " + IntegerToString((int)ticket));
        }
    }
    else
    {
        LogEvent("WARN", "Unknown event type: " + event_type);
    }
}



/******************************************************************
 * بررسی می‌کند آیا حد ضرر روزانه (DD) فعال شده است یا خیر
 * (شامل ضرر شناور + ضرر معاملات بسته شده)
 ******************************************************************/
bool CheckDailyDDLimit()
{
    // 1. اگر معاملات قبلاً به دلیل DD متوقف شده‌اند، همچنان متوقف بمان
    if(g_trading_stopped_by_dd)
    {
        return true;
    }

    // 2. اگر حد ضرر روزانه در تنظیمات غیرفعال است (0%)، بررسی نکن
    if(g_global_config.DailyDrawdownPercent <= 0.0)
    {
        return false;
    }

    // 3. محاسبه مقادیر
    double current_equity = AccountEquity();
    // محاسبه مبلغ ضرر مجاز (این عدد منفی خواهد بود)
    double dd_limit_amount = -current_equity * (g_global_config.DailyDrawdownPercent / 100.0);

    // 4. محاسبه ضرر کل فعلی
    double floating_pl = GetTotalFloatingPL(); // (از قدم 3.2)
    double total_current_dd = g_daily_dd + floating_pl; // g_daily_dd ضرر معاملات بسته شده است

    // 5. بررسی نهایی
    if(total_current_dd < dd_limit_amount)
    {
        // حد ضرر فعال شد!
        g_trading_stopped_by_dd = true;
        string err_msg = "CRITICAL: Daily Drawdown Limit Hit! Total DD: " + DoubleToString(total_current_dd, 2) +
                         " (Limit: " + DoubleToString(dd_limit_amount, 2) + ")";
                         
        LogEvent("CRITICAL", err_msg);
        SendErrorReport("Daily DD Limit Hit! Trading stopped.");
        
        // **اختیاری:** اگر می‌خواهید تمام معاملات باز فوراً بسته شوند،
        // کد بستن همه معاملات را در اینجا اضافه کنید.
        
        return true;
    }

    // اگر به حد ضرر نرسیده است
    return false;
}




void CheckDailyDrawdown() {
    if (TimeCurrent() - g_last_dd_reset >= 86400) {
        g_daily_dd = 0.0;
        g_last_dd_reset = TimeCurrent();
        LogEvent("INFO", "Daily DD reset on new day");
    }
}





ulong FindPositionBySourceID(long source_pos_id) {
    for (int i = 0; i < ArraySize(g_position_map); i++) {
        if (g_position_map[i].source_pos_id == source_pos_id) {
            return g_position_map[i].ticket;
        }
    }

    string short_search_id = IntegerToString((int)(source_pos_id % 100000000));

    for (int i = PositionsTotal() - 1; i >= 0; i--) {
        ulong ticket = PositionGetTicket(i);
        if (!PositionSelectByTicket(ticket)) continue;

        if (PositionGetInteger(POSITION_MAGIC) == InpMagicNumber) {
            string comment = PositionGetString(POSITION_COMMENT);
            string parts[];
            if (StringSplit(comment, '|', parts) > 0) {
                if (parts[0] == short_search_id) {
                    LogEvent("WARN", "Found position via comment fallback, adding to map", ticket);
                    int size = ArraySize(g_position_map);
                    ArrayResize(g_position_map, size + 1);
                    g_position_map[size].ticket = ticket;
                    g_position_map[size].source_pos_id = source_pos_id;
                    g_position_map[size].source_id_str = parts[1];
                    return ticket;
                }
            }
        }
    }
    return 0;
}





void RemoveFromMap(ulong ticket) {
    for (int i = 0; i < ArraySize(g_position_map); i++) {
        if (g_position_map[i].ticket == ticket) {
            ArrayCopy(g_position_map, g_position_map, i, i + 1);
            ArrayResize(g_position_map, ArraySize(g_position_map) - 1);
            LogEvent("INFO", "Removed position from map", (long)ticket);
            return;
        }
    }
}






//+------------------------------------------------------------------+
//| رویداد تراکنش ترید (برای گزارش‌دهی و محاسبه حد ضرر روزانه)         |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
    if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
        return;
        
    if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY) != DEAL_ENTRY_OUT)
        return;

    if(trans.magic != InpMagicNumber)
        return;

    double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);
    g_daily_dd += profit;
    
    LogEvent("INFO", "Deal closed. Profit: " + DoubleToString(profit, 2) + ". New Daily DD total: " + DoubleToString(g_daily_dd, 2), (long)trans.deal);

    SendCloseReport(trans.deal);
}




//+------------------------------------------------------------------+
//| [بازنویسی شده] ارسال گزارش بسته شدن معامله به سرور
//| و مدیریت افزودن به صف تلاش مجدد (uchar[]) در صورت خطا
//+------------------------------------------------------------------+
void SendCloseReport(ulong deal_ticket)
{
    // اگر سوکت PUSH معتبر نیست، خارج شو
    if (g_zmq_socket_push == 0)
        return;

    // انتخاب دیل برای خواندن اطلاعات
    if (!HistoryDealSelect(deal_ticket))
    {
        LogEvent("ERROR", "Failed to select deal for report", (long)deal_ticket);
        return;
    }

    // استخراج اطلاعات source_id_str و source_ticket از کامنت
    string comment = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
    string parts[];
    string source_id_str = "UNKNOWN";
    long source_ticket = 0;
    if (StringSplit(comment, '|', parts) >= 2)
    {
        source_ticket = (long)StringToInteger(parts[0]);
        source_id_str = parts[1];
    }

    // ساخت پیام JSON
    g_json_builder.Init();
    g_json_builder.Add("event", "TRADE_CLOSED_COPY");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);
    g_json_builder.Add("source_id_str", source_id_str);
    g_json_builder.Add("source_ticket", source_ticket);
    g_json_builder.Add("symbol", HistoryDealGetString(deal_ticket, DEAL_SYMBOL));
    g_json_builder.Add("profit", HistoryDealGetDouble(deal_ticket, DEAL_PROFIT));
    g_json_builder.Add("volume", HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    g_json_builder.Add("deal_ticket", (long)deal_ticket); // تیکت دیل کپی شده (برای دیباگ)
    string json_message = g_json_builder.ToString();

    // تبدیل پیام string به آرایه بایت uchar[]
    uchar json_data[];
    int data_len = StringToCharArray(json_message, json_data, 0, -1, CP_UTF8) - 1; // -1 for null terminator

    // بررسی اندازه پیام قبل از ارسال یا افزودن به صف
    if(data_len >= MAX_MSG_SIZE)
    {
        LogEvent("ERROR", "Close report JSON too large for retry queue", data_len);
        return; // پیام خیلی بزرگ است، ارسال یا ذخیره نمی‌شود
    }

    // تلاش برای ارسال مستقیم بایت‌ها
    if (ZmqSend(g_zmq_socket_push, json_data, data_len, ZMQ_DONTWAIT) == -1)
    {
        // اگر ارسال ناموفق بود، به صف تلاش مجدد اضافه کن
        LogEvent("ERROR", "Failed to send close report", ZmqErrno());

        int size = ArraySize(g_retry_queue);
        ArrayResize(g_retry_queue, size + 1);

        // ذخیره اطلاعات در struct جدید
        g_retry_queue[size].attempts = 0;
        g_retry_queue[size].data_len = data_len;
        // کپی کردن داده‌های بایت به صف
        ArrayCopy(g_retry_queue[size].json_data, json_data, 0, 0, data_len);
    }
    else
    {
        // اگر ارسال موفق بود، فقط لاگ کن
        LogEvent("INFO", "Close report sent successfully", 0);
    }
}
//+------------------------------------------------------------------+


//+------------------------------------------------------------------+
//| [بازنویسی شده] ارسال گزارش خطا به سرور
//| و مدیریت افزودن به صف تلاش مجدد (uchar[]) در صورت خطا
//+------------------------------------------------------------------+
void SendErrorReport(string error_msg)
{
    // اگر سوکت PUSH معتبر نیست، خارج شو
    if (g_zmq_socket_push == 0)
        return;

    // ساخت پیام JSON
    g_json_builder.Init();
    g_json_builder.Add("event", "EA_ERROR");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);
    g_json_builder.Add("message", error_msg);
    string json_message = g_json_builder.ToString();

    // تبدیل پیام string به آرایه بایت uchar[]
    uchar json_data[];
    int data_len = StringToCharArray(json_message, json_data, 0, -1, CP_UTF8) - 1; // -1 for null terminator

    // بررسی اندازه پیام قبل از ارسال یا افزودن به صف
    if(data_len >= MAX_MSG_SIZE)
    {
        LogEvent("ERROR", "Error report JSON too large for retry queue", data_len);
        // پیام‌های خطا معمولاً بزرگ نیستند، اما برای اطمینان چک می‌کنیم.
        // اگر خیلی بزرگ بود، فقط لاگ می‌کنیم.
        return;
    }

    // تلاش برای ارسال مستقیم بایت‌ها
    if (ZmqSend(g_zmq_socket_push, json_data, data_len, ZMQ_DONTWAIT) == -1)
    {
        // اگر ارسال ناموفق بود، به صف تلاش مجدد اضافه کن
        LogEvent("ERROR", "Failed to send error report", ZmqErrno());

        int size = ArraySize(g_retry_queue);
        // جلوگیری از پر شدن بیش از حد صف (مثلاً حداکثر ۱۰۰ پیام خطا)
        if(size < 100)
        {
            ArrayResize(g_retry_queue, size + 1);
            // ذخیره اطلاعات در struct جدید
            g_retry_queue[size].attempts = 0;
            g_retry_queue[size].data_len = data_len;
            // کپی کردن داده‌های بایت به صف
            ArrayCopy(g_retry_queue[size].json_data, json_data, 0, 0, data_len);
        }
        else
        {
            LogEvent("WARN", "Retry queue full, discarding new error report", size);
        }
    }
    else
    {
        // اگر ارسال موفق بود، فقط لاگ کن (با پارامتر سوم 0)
        LogEvent("INFO", "Error report sent: " + error_msg, 0);
    }
}
//+------------------------------------------------------------------+





//+------------------------------------------------------------------+
//| [اصلاح شده] تابع لاگ‌نویسی هوشمند
//| لاگ‌های INFO فقط در صورت فعال بودن دیباگ و همه لاگ‌ها در فایل ثبت می‌شوند
//+------------------------------------------------------------------+
void LogEvent(string level, string msg, long extra = -1) // پارامتر سوم اضافه شد
{
    // فیلتر کردن لاگ‌های INFO اگر دیباگ غیرفعال باشد
    if (level == "INFO" && !InpEnableDebugLogging)
    {
        return;
    }

    // ساخت رشته لاگ
    datetime ts = TimeCurrent();
    string log_str = TimeToString(ts, TIME_DATE|TIME_MINUTES|TIME_SECONDS) +
                     " [" + level + "] [CopyEA-" + InpCopyIDStr + "]: " + msg;

    if (extra != -1)
    {
        // تبدیل صریح عدد به رشته
        log_str += " {" + IntegerToString(extra) + "}"; // استفاده از IntegerToString
    }

    // چاپ لاگ‌های مهم در ترمینال اکسپرت
    if (level == "WARN" || level == "ERROR" || level == "CRITICAL")
    {
        Print(log_str);
    }

    // نوشتن تمام لاگ‌های فعال در فایل
    if(g_log_file_handle != INVALID_HANDLE)
    {
        FileSeek(g_log_file_handle, 0, SEEK_END);
        FileWrite(g_log_file_handle, log_str + "\r\n");
    }

    // توقف کامل اکسپرت در صورت بروز خطای بحرانی
    if (level == "CRITICAL")
    {
        ExpertRemove();
    }
}
//+------------------------------------------------------------------+