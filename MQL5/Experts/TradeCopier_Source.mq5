// MQL5/Experts/TradeCopier_Source.mq5
//
// اکسپرت مستر (Source) - معماری جدید مبتنی بر ZMQ
// وظیفه: ارسال آنی سیگنال‌های باز/بسته شدن معامله به سرور مرکزی پایتون.
// این اکسپرت جایگزین مکانیزم نوشتن فایل .txt شده است.

#property copyright "TradeCopier Professional"
#property version   "1.0"
#property strict

// --- فراخوانی کتابخانه‌ها ---
#include <Include/ZMQ/zmq.mqh>      // کتابخانه اصلی ZMQ
#include <Include/Common/json.mqh> // کتابخانه کمکی ساخت JSON

// --- ورودی‌های اکسپرت ---
input string InpServerAddress = "127.0.0.1";  // آدرس IP سرور پایتون
input int    InpSignalPort  = 5555;         // پورت PULL سرور (که در server.py تنظیم کردیم)
input string InpSourceIDStr = "S1";          // شناسه منحصر به فرد این مستر (باید با دیتابیس مطابقت داشته باشد)
input ulong  InpMagicNumber = 0;           // (اختیاری) فیلتر کردن معاملات بر اساس مجیک نامبر

// --- متغیرهای سراسری ---
int    g_zmq_context;           // کانتکست ZMQ
int    g_zmq_socket_push;       // سوکت PUSH برای ارسال سیگنال
CJsonBuilder g_json_builder;    // ابزار ساخت پیام JSON

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    Print("Initializing Source EA (ZMQ v1.0)...");
    
    // ۱. ایجاد کانتکست ZMQ
    g_zmq_context = ZmqContextNew(1); // 1 thread
    if(g_zmq_context == 0)
    {
        Print("Failed to create ZMQ context. Error: ", ZmqErrno());
        return(INIT_FAILED);
    }
    
    // ۲. ایجاد سوکت PUSH
    g_zmq_socket_push = ZmqSocketNew(g_zmq_context, ZMQ_PUSH);
    if(g_zmq_socket_push == 0)
    {
        Print("Failed to create ZMQ PUSH socket. Error: ", ZmqErrno());
        ZmqContextTerm(g_zmq_context);
        return(INIT_FAILED);
    }
    
    // ۳. اتصال به سرور مرکزی پایتون
    string address = "tcp://" + InpServerAddress + ":" + (string)InpSignalPort;
    if(ZmqConnect(g_zmq_socket_push, address) != 0)
    {
        Print("Failed to connect to ZMQ server at ", address, ". Error: ", ZmqErrno());
        ZmqClose(g_zmq_socket_push);
        ZmqContextTerm(g_zmq_context);
        return(INIT_FAILED);
    }
    
    Print("Source EA successfully connected to ZMQ server at ", address);
    Print("--- Source EA Initialized ---");
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    Print("Deinitializing Source EA...");
    // بستن سوکت و کانتکست
    if(g_zmq_socket_push != 0)
        ZmqClose(g_zmq_socket_push);
    if(g_zmq_context != 0)
        ZmqContextTerm(g_zmq_context);
    Print("Source EA Deinitialized.");
}

//+------------------------------------------------------------------+
//| Trade Transaction event handler                                  |
//| این تابع، قلب تپنده و رویدادمحور این اکسپرت است
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
    // ما فقط به تراکنش‌هایی که یک معامله (Deal) جدید به تاریخچه اضافه می‌کنند، علاقه‌مندیم
    if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
        return;
        
    // فیلتر کردن بر اساس مجیک نامبر (اگر تنظیم شده باشد)
    if(InpMagicNumber > 0 && trans.magic != InpMagicNumber)
        return;
        
    // دریافت اطلاعات معامله (Deal)
    if(!HistoryDealSelect(trans.deal))
    {
        Print("Error selecting deal ", trans.deal);
        return;
    }

    // --- تشخیص نوع رویداد ---
    // این معامله می‌تواند باز کردن پوزیشن (IN)، بستن (OUT) یا اصلاح (IN/OUT) باشد
    
    string event_type = "";
    ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
    
    if(entry == DEAL_ENTRY_IN)
    {
        event_type = "TRADE_OPEN"; // یک پوزیشن جدید باز شده است
    }
    else if(entry == DEAL_ENTRY_OUT)
    {
        event_type = "TRADE_CLOSE_MASTER"; // یک پوزیشن موجود بسته شده است
    }
    else
    {
        return; // سایر موارد (مانند In/Out) فعلا نادیده گرفته می‌شوند
    }

    // --- ارسال رویداد به سرور پایتون ---
    SendTradeEvent(trans.deal, event_type);
}

//+------------------------------------------------------------------+
//| تابع کمکی برای ساخت و ارسال پیام JSON
//+------------------------------------------------------------------+
void SendTradeEvent(ulong deal_ticket, string event_type)
{
    // ۱. ساختن پیام JSON
    g_json_builder.Init();
    
    g_json_builder.Add("event",         event_type);
    g_json_builder.Add("source_id_str", InpSourceIDStr); // شناسه مستر
    g_json_builder.Add("timestamp_ms",  (long)HistoryDealGetInteger(deal_ticket, DEAL_TIME_MSC));
    
    // جزئیات معامله
    g_json_builder.Add("deal_ticket",   (long)deal_ticket);
    g_json_builder.Add("order_ticket",  (long)HistoryDealGetInteger(deal_ticket, DEAL_ORDER));
    g_json_builder.Add("position_id",   (long)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID));
    g_json_builder.Add("symbol",        HistoryDealGetString(deal_ticket, DEAL_SYMBOL));
    g_json_builder.Add("magic",         (long)HistoryDealGetInteger(deal_ticket, DEAL_MAGIC));
    g_json_builder.Add("volume",        HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    g_json_builder.Add("price",         HistoryDealGetDouble(deal_ticket, DEAL_PRICE));
    g_json_builder.Add("profit",        HistoryDealGetDouble(deal_ticket, DEAL_PROFIT));
    
    // دریافت اطلاعات پوزیشن مربوط به این معامله (برای SL/TP)
    if(PositionSelectByTicket(HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID)))
    {
        g_json_builder.Add("position_sl", PositionGetDouble(POSITION_SL));
        g_json_builder.Add("position_tp", PositionGetDouble(POSITION_TP));
        g_json_builder.Add("position_type", (long)PositionGetInteger(POSITION_TYPE)); // 0 = BUY, 1 = SELL
    }
    else
    {
        // اگر پوزیشن دیگر وجود ندارد (یعنی رویداد بستن بوده)
        // اطلاعات SL/TP را از خود معامله می‌گیریم (که ممکن است 0 باشد)
        g_json_builder.Add("position_sl", HistoryDealGetDouble(deal_ticket, DEAL_SL));
        g_json_builder.Add("position_tp", HistoryDealGetDouble(deal_ticket, DEAL_TP));
        // نوع معامله را از خود معامله می‌خوانیم
        g_json_builder.Add("position_type", (long)HistoryDealGetInteger(deal_ticket, DEAL_TYPE)); // 0 = BUY, 1 = SELL
    }

    string json_message = g_json_builder.ToString();
    
    // ۲. ارسال پیام JSON از طریق سوکت PUSH
    if(ZmqSendString(g_zmq_socket_push, json_message, ZMQ_DONTWAIT) == -1)
    {
        Print("Failed to send ZMQ message. Error: ", ZmqErrno());
        // (در اینجا می‌توان منطق تلاش مجدد یا ذخیره در صف را اضافه کرد)
    }
    else
    {
        Print("Event sent successfully: ", json_message);
    }
}
//+------------------------------------------------------------------+