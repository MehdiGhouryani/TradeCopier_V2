// MQL5/Experts/TradeCopier_Copy.mq5
//
// اکسپرت اسلیو (Copy) - معماری جدید مبتنی بر ZMQ
// وظیفه: دریافت تنظیمات، دریافت آنی سیگنال‌ها، اجرای معاملات و گزارش نتایج.

#property copyright "TradeCopier Professional"
#property version   "1.0"
#property strict

// --- فراخوانی کتابخانه‌ها ---
#include <Trade/Trade.mqh>           // کتابخانه استاندارد ترید
#include <Include/ZMQ/zmq.mqh>       // کتابخانه اصلی ZMQ
#include <Include/Common/json.mqh>  // کتابخانه کمکی ساخت JSON (برای گزارش‌دهی)

// --- ورودی‌های اکسپرت ---
input string InpServerAddress  = "127.0.0.1";  // آدرس IP سرور پایتون
input int    InpConfigPort   = 5557;         // پورت REQ تنظیمات
input int    InpPublishPort  = 5556;         // پورت SUB سیگنال
input int    InpSignalPort   = 5555;         // پورت PUSH گزارش‌دهی
input string InpCopyIDStr    = "C_A";          // شناسه منحصر به فرد این اسلیو (باید با دیتابیس مطابقت داشته باشد)
input ulong  InpMagicNumber  = 12345;        // مجیک نامبر برای تمام معاملات کپی شده

// --- ساختارهای داده برای نگهداری تنظیمات ---
// این ساختار، تنظیمات یک اتصال (Mapping) را در حافظه نگه می‌دارد
struct sSourceConfig
{
    string SourceTopicID;       // شناسه مستر برای دنبال کردن (مثال: "S1")
    string CopyMode;            // "ALL", "GOLD_ONLY", "SYMBOLS"
    string AllowedSymbols;      // "EURUSD;GBPUSD"
    string VolumeType;          // "MULTIPLIER", "FIXED"
    double VolumeValue;         // 1.0
    double MaxLotSize;          // 0.0 =
    int    MaxConcurrentTrades; // 0 =
    double SourceDrawdownLimit; // 0.0 =
};

// این ساختار، تنظیمات کلی این اسلیو را نگه می‌دارد
struct sGlobalConfig
{
    double DailyDrawdownPercent;
    double AlertDrawdownPercent;
    bool   ResetDDFlag;
};

// --- متغیرهای سراسری ---
CTrade       g_trade;                 // نمونه کلاس ترید
CJsonBuilder g_json_builder;    // ابزار ساخت پیام JSON (برای گزارش)

// کانتکست ZMQ
int          g_zmq_context;

// سوکت‌ها
int          g_zmq_socket_req;    // سوکت REQ (درخواست تنظیمات)
int          g_zmq_socket_sub;    // سوکت SUB (دریافت سیگنال)
int          g_zmq_socket_push;   // سوکت PUSH (ارسال گزارش)

// آرایه‌های نگهداری تنظیمات (دریافتی از سرور)
sSourceConfig g_source_configs[]; // لیست تمام اتصالات مجاز
sGlobalConfig g_global_config;    // تنظیمات کلی

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    Print("Initializing Copy EA (ZMQ v1.0)...");
    g_trade.SetExpertMagicNumber(InpMagicNumber);
    g_trade.SetMarginMode();
    
    // --- ۱. راه‌اندازی ZMQ ---
    g_zmq_context = ZmqContextNew(1);
    if(g_zmq_context == 0)
    {
        Print("Failed to create ZMQ context. Error: ", ZmqErrno());
        return(INIT_FAILED);
    }
    
    // --- ۲. اتصال به سوکت PUSH (برای ارسال گزارش) ---
    // (این سوکت ساده است و نیازی به پاسخ ندارد)
    g_zmq_socket_push = ZmqSocketNew(g_zmq_context, ZMQ_PUSH);
    string push_address = "tcp://" + InpServerAddress + ":" + (string)InpSignalPort;
    if(ZmqConnect(g_zmq_socket_push, push_address) != 0)
    {
        Print("Failed to connect ZMQ PUSH socket to ", push_address, ". Error: ", ZmqErrno());
        // (ادامه می‌دهیم، شاید ارتباط بعداً برقرار شود)
    }
    else
    {
        Print("PUSH socket connected to ", push_address);
    }

    // --- ۳. دریافت تنظیمات از سرور (مهم‌ترین بخش) ---
    if(!FetchConfiguration())
    {
        Print("Failed to fetch configuration from server. Halting initialization.");
        CleanupZMQ();
        return(INIT_FAILED);
    }
    
    // --- ۴. اتصال به سوکت SUB (برای دریافت سیگنال) ---
    g_zmq_socket_sub = ZmqSocketNew(g_zmq_context, ZMQ_SUB);
    string sub_address = "tcp://" + InpServerAddress + ":" + (string)InpPublishPort;
    if(ZmqConnect(g_zmq_socket_sub, sub_address) != 0)
    {
        Print("Failed to connect ZMQ SUB socket to ", sub_address, ". Error: ", ZmqErrno());
        CleanupZMQ();
        return(INIT_FAILED);
    }
    
    // --- ۵. ثبت‌نام (Subscribe) برای تاپیک‌های مجاز ---
    // (بر اساس تنظیماتی که در مرحله ۳ دریافت کردیم)
    int mappings_count = ArraySize(g_source_configs);
    if(mappings_count == 0)
    {
        Print("Warning: No active mappings received from server. EA will run but copy nothing.");
    }
    
    for(int i = 0; i < mappings_count; i++)
    {
        string topic = g_source_configs[i].SourceTopicID;
        // ZmqSocketSetString(g_zmq_socket_sub, ZMQ_SUBSCRIBE, topic); // (در برخی نسخه‌های کتابخانه ZMQ، این روش کار نمی‌کند)
        
        // روش مطمئن‌تر با ZmqSocketSet:
        uchar topic_array[];
        StringToCharArray(topic, topic_array);
        if(ZmqSocketSet(g_zmq_socket_sub, ZMQ_SUBSCRIBE, topic_array) != 0)
        {
             Print("Failed to subscribe to topic '", topic, "'. Error: ", ZmqErrno());
        }
        else
        {
             Print("Subscribed to topic: '", topic, "'");
        }
    }
    
    Print("SUB socket connected to ", sub_address);
    Print("--- Copy EA Initialized Successfully ---");
    EventSetTimer(1); // تایمر ۱ ثانیه‌ای برای چک کردن سیگنال‌ها
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| تابع کمکی: دریافت تنظیمات از سرور پایتون
//+------------------------------------------------------------------+
bool FetchConfiguration()
{
    Print("Attempting to fetch configuration for '", InpCopyIDStr, "'...");
    
    // ۱. ایجاد سوکت REQ
    g_zmq_socket_req = ZmqSocketNew(g_zmq_context, ZMQ_REQ);
    string req_address = "tcp://" + InpServerAddress + ":" + (string)InpConfigPort;
    
    if(ZmqConnect(g_zmq_socket_req, req_address) != 0)
    {
        Print("Failed to connect ZMQ REQ socket to ", req_address, ". Error: ", ZmqErrno());
        return false;
    }
    
    // ۲. ساخت پیام درخواست JSON
    g_json_builder.Init();
    g_json_builder.Add("command", "GET_CONFIG");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);
    
    // ۳. ارسال درخواست
    if(ZmqSendString(g_zmq_socket_req, g_json_builder.ToString()) == -1)
    {
        Print("Failed to send config request. Error: ", ZmqErrno());
        ZmqClose(g_zmq_socket_req);
        return false;
    }
    
    // ۴. دریافت پاسخ (با انتظار محدود)
    // (این بخش حیاتی است - باید منتظر پاسخ بمانیم)
    // (در MQL5، دریافت مسدودکننده (blocking) در OnInit می‌تواند مشکل‌ساز باشد،
    // اما برای تنظیمات اولیه ضروری است. از یک تایم‌اوت ساده استفاده می‌کنیم)
    
    // TODO: پیاده‌سازی تایم‌اوت صحیح با ZMQ_POLL
    
    uchar recv_buffer[65536]; // بافر 64KB برای دریافت تنظیمات
    int recv_len = ZmqRecv(g_zmq_socket_req, recv_buffer, 65536, 0); // 0 = پرچم مسدود کننده
    ZmqClose(g_zmq_socket_req); // بلافاصله پس از دریافت، سوکت REQ را می‌بندیم
    
    if(recv_len <= 0)
    {
        Print("Failed to receive config response. Error: ", ZmqErrno());
        return false;
    }
    
    string json_response = CharArrayToString(recv_buffer, 0, recv_len);
    
    // ۵. پارس کردن پاسخ JSON
    CJAVal json_data;
    if(!json_data.Deserialize(json_response))
    {
        Print("Failed to deserialize JSON response: ", json_response);
        return false;
    }
    
    if(json_data["status"].ToStr() != "OK")
    {
        Print("Server returned an error: ", json_data["message"].ToStr());
        return false;
    }
    
    // ۶. بارگذاری تنظیمات در متغیرهای سراسری
    CJAVal config = json_data["config"];
    
    // بارگذاری تنظیمات کلی
    CJAVal global_settings = config["global_settings"];
    g_global_config.DailyDrawdownPercent = global_settings["daily_drawdown_percent"].ToDouble();
    g_global_config.AlertDrawdownPercent = global_settings["alert_drawdown_percent"].ToDouble();
    g_global_config.ResetDDFlag           = global_settings["reset_dd_flag"].ToBool();
    
    PrintFormat("Global settings loaded: DD=%.2f%%, Alert=%.2f%%",
                g_global_config.DailyDrawdownPercent, g_global_config.AlertDrawdownPercent);

    // بارگذاری تنظیمات اتصالات (Mappings)
    CJAVal mappings = config["mappings"];
    int mappings_count = mappings.Size();
    ArrayResize(g_source_configs, mappings_count);
    
    for(int i = 0; i < mappings_count; i++)
    {
        CJAVal m = mappings[i];
        g_source_configs[i].SourceTopicID       = m["source_topic_id"].ToStr();
        g_source_configs[i].CopyMode            = m["copy_mode"].ToStr();
        g_source_configs[i].AllowedSymbols      = m["allowed_symbols"].ToStr();
        g_source_configs[i].VolumeType          = m["volume_type"].ToStr();
        g_source_configs[i].VolumeValue         = m["volume_value"].ToDouble();
        g_source_configs[i].MaxLotSize          = m["max_lot_size"].ToDouble();
        g_source_configs[i].MaxConcurrentTrades = (int)m["max_concurrent_trades"].ToInteger();
        g_source_configs[i].SourceDrawdownLimit = m["source_drawdown_limit"].ToDouble();
        
        PrintFormat("Mapping #%d loaded: Source=%s, Vol=%.2f (%s)",
                    i, g_source_configs[i].SourceTopicID, g_source_configs[i].VolumeValue, g_source_configs[i].VolumeType);
    }

    return true;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    Print("Deinitializing Copy EA...");
    EventKillTimer();
    CleanupZMQ();
    Print("Copy EA Deinitialized.");
}

//+------------------------------------------------------------------+
//| تابع کمکی: بستن تمام سوکت‌های ZMQ
//+------------------------------------------------------------------+
void CleanupZMQ()
{
    if(g_zmq_socket_req != 0)
        ZmqClose(g_zmq_socket_req);
    if(g_zmq_socket_sub != 0)
        ZmqClose(g_zmq_socket_sub);
    if(g_zmq_socket_push != 0)
        ZmqClose(g_zmq_socket_push);
    if(g_zmq_context != 0)
        ZmqContextTerm(g_zmq_context);
        
    g_zmq_socket_req = 0;
    g_zmq_socket_sub = 0;
    g_zmq_socket_push = 0;
    g_zmq_context = 0;
}

//+------------------------------------------------------------------+
//| Timer function                                                   |
//+------------------------------------------------------------------+
void OnTimer()
{
    // TODO: (در گام‌های بعد)
    // CheckDailyDrawdown();
    
    // چک کردن سوکت سیگنال‌ها برای پیام‌های جدید
    CheckSignalSocket();
}

//+------------------------------------------------------------------+
//| چک کردن سوکت SUB برای سیگنال‌های جدید
//+------------------------------------------------------------------+
void CheckSignalSocket()
{
    if(g_zmq_socket_sub == 0) return;
    
    uchar recv_buffer[4096]; // بافر 4KB
    string topic_received;
    string json_message;
    
    // حلقه برای خواندن تمام پیام‌های موجود در صف، به صورت غیر مسدود
    while(true)
    {
        // ۱. خواندن تاپیک (غیر مسدود)
        int topic_len = ZmqRecv(g_zmq_socket_sub, recv_buffer, 4096, ZMQ_DONTWAIT);
        if(topic_len <= 0)
            break; // هیچ پیامی در صف وجود ندارد
            
        topic_received = CharArrayToString(recv_buffer, 0, topic_len);
        
        // ۲. بررسی اینکه آیا پیام ادامه دارد (RCVMORE)
        int rcvmore = 0;
        ZmqSocketGet(g_zmq_socket_sub, ZMQ_RCVMORE, rcvmore);
        
        if(!rcvmore)
        {
            Print("Warning: Received topic '", topic_received, "' but no message body. Skipping.");
            continue;
        }
        
        // ۳. خواندن بدنه پیام (غیر مسدود)
        int msg_len = ZmqRecv(g_zmq_socket_sub, recv_buffer, 4096, ZMQ_DONTWAIT);
        if(msg_len <= 0)
        {
            Print("Warning: Received topic '", topic_received, "' but message body read failed. Error: ", ZmqErrno());
            continue;
        }
        
        json_message = CharArrayToString(recv_buffer, 0, msg_len);
        
        // ۴. پردازش پیام
        // Print("Received signal on topic '", topic_received, "': ", json_message);
        ExecuteSignal(topic_received, json_message);
    }
}

//+------------------------------------------------------------------+
//| تابع اصلی منطق کپی: اجرای سیگنال دریافت شده
//+------------------------------------------------------------------+
void ExecuteSignal(string source_topic, string json_message)
{
    // ۱. پارس کردن JSON سیگنال
    CJAVal signal;
    if(!signal.Deserialize(json_message))
    {
        Print("Failed to deserialize signal JSON: ", json_message);
        return;
    }
    
    // ۲. پیدا کردن تنظیمات مربوط به این سورس
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
        Print("Warning: Received signal from unconfigured source '", source_topic, "'. Ignoring.");
        return;
    }
    
    // ۳. استخراج اطلاعات سیگنال
    string event_type     = signal["event"].ToStr();
    long   source_pos_id  = signal["position_id"].ToInteger();
    string symbol         = signal["symbol"].ToStr();
    double volume         = signal["volume"].ToDouble();
    double price          = signal["price"].ToDouble();
    double sl             = signal["position_sl"].ToDouble();
    double tp             = signal["position_tp"].ToDouble();
    long   position_type  = signal["position_type"].ToInteger(); // 0=BUY, 1=SELL
    
    // TODO: (در گام‌های بعد)
    // - فیلتر کردن نماد (بر اساس config.CopyMode)
    // - محاسبه حجم نهایی (بر اساس config.VolumeType)
    // - بررسی محدودیت‌های ریسک (MaxLotSize, MaxConcurrentTrades)
    
    double final_volume = volume * config.VolumeValue; // فعلاً فقط ضریب ساده
    
    // ساخت کامنت برای شناسایی پوزیشن در آینده
    string comment = (string)source_pos_id + "|" + source_topic;
    
    // ۴. اجرای منطق بر اساس نوع رویداد
    if(event_type == "TRADE_OPEN")
    {
        PrintFormat("EXECUTE OPEN: %s %s %.2f lot (SourceID: %s)",
                    (position_type == 0 ? "BUY" : "SELL"), symbol, final_volume, source_topic);
                    
        bool result = false;
        if(position_type == 0) // BUY
        {
            result = g_trade.Buy(final_volume, symbol, 0, sl, tp, comment);
        }
        else // SELL
        {
            result = g_trade.Sell(final_volume, symbol, 0, sl, tp, comment);
        }
        
        if(!result)
        {
            Print("Failed to open position. Error: ", g_trade.ResultRetcodeDescription());
            // TODO: ارسال خطا به سرور
        }
    }
    else if(event_type == "TRADE_MODIFY")
    {
        // TODO: (در گام‌های بعد)
        // منطق اصلاح SL/TP پوزیشن باز
    }
    else if(event_type == "TRADE_CLOSE_MASTER")
    {
        PrintFormat("EXECUTE CLOSE: %s position %d (SourceID: %s)", symbol, source_pos_id, source_topic);
        
        // پیدا کردن پوزیشن مربوطه در این حساب و بستن آن
        if(PositionSelectByTicket(FindPositionBySourceID(source_pos_id)))
        {
            if(!g_trade.PositionClose(PositionGetInteger(POSITION_TICKET)))
            {
                Print("Failed to close position. Error: ", g_trade.ResultRetcodeDescription());
            }
        }
        else
        {
            Print("Position ", source_pos_id, " not found for closing.");
        }
    }
}

//+------------------------------------------------------------------+
//| پیدا کردن تیکت پوزیشن بر اساس شناسه سورس (از کامنت)
//+------------------------------------------------------------------+
ulong FindPositionBySourceID(long source_pos_id)
{
    string source_id_str = (string)source_pos_id;
    
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;
        
        if(PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
        {
            string comment = PositionGetString(POSITION_COMMENT);
            string parts[];
            if(StringSplit(comment, '|', parts) > 0)
            {
                if(parts[0] == source_id_str)
                {
                    return ticket; // پوزیشن پیدا شد
                }
            }
        }
    }
    return 0; // پیدا نشد
}

//+------------------------------------------------------------------+
//| Trade Transaction event handler (برای گزارش‌دهی)
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
    // ما فقط به تراکنش‌هایی که یک معامله بستن (OUT) اضافه می‌کنند، علاقه‌مندیم
    if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
        return;
        
    if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY) != DEAL_ENTRY_OUT)
        return;
        
    // فیلتر کردن بر اساس مجیک نامبر خودمان
    if(trans.magic != InpMagicNumber)
        return;
        
    // ارسال گزارش این معامله بسته شده به سرور پایتون
    SendCloseReport(trans.deal);
}

//+------------------------------------------------------------------+
//| ارسال گزارش بسته شدن معامله به سرور (برای دیتابیس و تلگرام)
//+------------------------------------------------------------------+
void SendCloseReport(ulong deal_ticket)
{
    if(g_zmq_socket_push == 0) return; // اگر سوکت PUSH فعال نیست، ارسال نکن
    
    if(!HistoryDealSelect(deal_ticket)) return;
    
    // استخراج شناسه سورس از کامنت
    string comment = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
    string parts[];
    string source_id_str = "UNKNOWN";
    long source_ticket = 0;
    
    if(StringSplit(comment, '|', parts) >= 2)
    {
        source_ticket = (long)StringToInt(parts[0]);
        source_id_str = parts[1];
    }

    // ساخت پیام JSON
    g_json_builder.Init();
    g_json_builder.Add("event",         "TRADE_CLOSED_COPY");
    g_json_builder.Add("copy_id_str",   InpCopyIDStr);
    g_json_builder.Add("source_id_str", source_id_str);
    g_json_builder.Add("source_ticket", source_ticket);
    g_json_builder.Add("symbol",        HistoryDealGetString(deal_ticket, DEAL_SYMBOL));
    g_json_builder.Add("profit",        HistoryDealGetDouble(deal_ticket, DEAL_PROFIT));
    g_json_builder.Add("volume",        HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    g_json_builder.Add("deal_ticket",   (long)deal_ticket);
    
    string json_message = g_json_builder.ToString();

    // ارسال پیام (بدون انتظار)
    if(ZmqSendString(g_zmq_socket_push, json_message, ZMQ_DONTWAIT) == -1)
    {
        Print("Failed to send close report. Error: ", ZmqErrno());
    }
    else
    {
        Print("Close report sent: ", json_message);
    }
}
//+------------------------------------------------------------------+