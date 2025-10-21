// MQL5/Experts/TradeCopier_Source.mq5
//
// اکسپرت سمت منبع (Master).
// وظیفه: شناسایی رویدادهای معاملاتی (Open, Close, Modify) و ارسال
// آن‌ها به سرور هسته پایتون از طریق ZMQ PUSH.

#property copyright "TradeCopier Professional (Refactored)"
#property version "2.5" // نسخه نهایی (کامپایل شده با Define دستی)
#property strict

// هدرهای استاندارد الزامی
#include <Trade/Trade.mqh>
#include <Arrays/ArrayObj.mqh>

// هدرهای پروژه
#include <Include/ZMQ/zmq.mqh>
#include <Include/Common/json.mqh>

// [اصلاح نهایی] تعریف دستی ثابت‌های استاندارد (Workaround)
#ifndef TRADE_TRANSACTION_MODIFY
#define TRADE_TRANSACTION_MODIFY 5 // نوع تراکنش برای اصلاح پوزیشن (SL/TP)
#endif
#ifndef DEAL_ENTRY_IN_OUT
#define DEAL_ENTRY_IN_OUT 2        // نوع ورودی/خروجی دیل (برای بستن بخشی)
#endif

//--- ورودی‌های اکسپرت
input string InpServerAddress      = "127.0.0.1"; // آدرس سرور هسته
input int    InpSignalPort         = 5555;        // پورت PUSH سیگنال‌ها
input string InpSourceIDStr        = "S1";        // شناسه منحصر به فرد این منبع
input ulong  InpMagicNumber        = 0;           // مجیک نامبر (0 = همه معاملات)
input bool   InpEnableDebugLogging = false;       // فعال‌سازی لاگ‌های سطح INFO

//--- متغیرهای گلوبال ZMQ
int g_zmq_context;
int g_zmq_socket_push;
CJsonBuilder g_json_builder;

//--- ساختار صف تلاش مجدد (امن)
#define MAX_MSG_SIZE 2048
struct RetryMessage
{
    int    attempts;
    int    data_len;
    uchar  json_data[MAX_MSG_SIZE];
};

//--- متغیرهای گلوبال مدیریت وضعیت
RetryMessage g_retry_queue[];
int          g_ping_fails        = 0;
int          g_timer_count       = 0;
int          g_log_file_handle   = INVALID_HANDLE;

//+------------------------------------------------------------------+
//| تابع مقداردهی اولیه اکسپرت
//+------------------------------------------------------------------+
int OnInit()
{
    string log_file_name = "SourceEA_" + InpSourceIDStr + ".log";
    g_log_file_handle = FileOpen(log_file_name, FILE_WRITE|FILE_ANSI|FILE_TXT|FILE_SHARE_READ);
    if(g_log_file_handle == INVALID_HANDLE)
    {
        Print("CRITICAL: [SourceEA] Could not open log file: " + log_file_name);
    }

    if(StringLen(InpSourceIDStr) == 0)
    {
        LogEvent("ERROR", "Invalid InpSourceIDStr: empty");
        return(INIT_PARAMETERS_INCORRECT);
    }

    g_zmq_context = ZmqContextNew(1);
    if(g_zmq_context == 0)
    {
        LogEvent("ERROR", "Failed to create ZMQ context", ZmqErrno());
        return(INIT_FAILED);
    }

    g_zmq_socket_push = ZmqSocketNew(g_zmq_context, ZMQ_PUSH);
    if(g_zmq_socket_push == 0)
    {
        LogEvent("ERROR", "Failed to create ZMQ PUSH socket", ZmqErrno());
        ZmqContextTerm(g_zmq_context);
        return(INIT_FAILED);
    }

    ZmqSocketSet(g_zmq_socket_push, ZMQ_SNDHWM, 1000);

    string address = "tcp://" + InpServerAddress + ":" + (string)InpSignalPort;
    if(ZmqConnect(g_zmq_socket_push, address) != 0)
    {
        LogEvent("ERROR", "Failed to connect to ZMQ server at " + address, ZmqErrno());
        ZmqClose(g_zmq_socket_push);
        ZmqContextTerm(g_zmq_context);
        return(INIT_FAILED);
    }

    EventSetTimer(5);

    LogEvent("INFO", "Source EA successfully connected to ZMQ server at " + address);
    LogEvent("INFO", "--- Source EA Initialized ---");
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| تابع پایان کار اکسپرت
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    LogEvent("INFO", "Deinitializing Source EA...");
    EventKillTimer();

    if(g_zmq_socket_push != 0)
        ZmqClose(g_zmq_socket_push);

    if(g_zmq_context != 0)
        ZmqContextTerm(g_zmq_context);

    if(g_log_file_handle != INVALID_HANDLE)
    {
        FileClose(g_log_file_handle);
    }

    LogEvent("INFO", "Source EA Deinitialized.");
}

//+------------------------------------------------------------------+
//| تابع مدیریت رویدادهای تایمر (صف تلاش مجدد و پینگ)
//+------------------------------------------------------------------+
void OnTimer()
{
    // --- پردازش صف تلاش مجدد ---
    if(ArraySize(g_retry_queue) > 0)
    {
        RetryMessage msg;
        msg = g_retry_queue[0];

        if(ZmqSend(g_zmq_socket_push, msg.json_data, msg.data_len, ZMQ_DONTWAIT) != -1)
        {
            ArrayRemove(g_retry_queue, 0);
        }
        else
        {
            int err = ZmqErrno();
            LogEvent("WARN", "Retry send failed", err);
            g_retry_queue[0].attempts++;

            if(g_retry_queue[0].attempts >= 3)
            {
                LogEvent("ERROR", "Max retries reached, discarding message");
                ArrayRemove(g_retry_queue, 0);
            }
        }
    }

    // --- ارسال پینگ ---
    g_timer_count++;
    if(g_timer_count % 6 == 0)
    {
        g_json_builder.Init();
        g_json_builder.Add("event", "PING");
        g_json_builder.Add("source_id_str", InpSourceIDStr);
        g_json_builder.Add("timestamp", (long)TimeCurrent());
        string ping_msg = g_json_builder.ToString();

        if(ZmqSendString(g_zmq_socket_push, ping_msg, ZMQ_DONTWAIT) == -1)
        {
            LogEvent("WARN", "Ping send failed", ZmqErrno());
            g_ping_fails++;

            if(g_ping_fails >= 3)
            {
                LogEvent("ERROR", "Multiple ping fails, attempting reconnect", g_ping_fails);
                string address = "tcp://" + InpServerAddress + ":" + (string)InpSignalPort;
                ZmqDisconnect(g_zmq_socket_push, address);

                if(ZmqConnect(g_zmq_socket_push, address) == 0)
                {
                    LogEvent("INFO", "Reconnect successful");
                    g_ping_fails = 0;
                }
                else
                {
                    LogEvent("CRITICAL", "Reconnect failed", ZmqErrno());
                }
            }
        }
        else
        {
            g_ping_fails = 0;
        }
    }
}

//+------------------------------------------------------------------+
//| تابع مدیریت تراکنش‌های معاملاتی (شناسایی دقیق رویدادها)
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
    string event_type = "";
    ulong  deal_ticket = 0; // تیکت دیل (معمولاً دیل ورودی)

    // --- مدیریت رویدادهای مبتنی بر دیل ---
    // [اصلاح شده] استفاده از ثابت تعریف شده TRADE_TRANSACTION_DEAL_ADD
    if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
    {
        if(!HistoryDealSelect(trans.deal))
        {
            LogEvent("ERROR", "Error selecting deal", trans.deal);
            return;
        }
        deal_ticket = trans.deal;

        if(InpMagicNumber > 0 && HistoryDealGetInteger(deal_ticket, DEAL_MAGIC) != InpMagicNumber)
             return;

        ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);

        if(entry == DEAL_ENTRY_IN)
        {
            event_type = "TRADE_OPEN";
        }
        else if(entry == DEAL_ENTRY_OUT)
        {
            event_type = "TRADE_CLOSE_MASTER";
        }
        else if(entry == DEAL_ENTRY_IN_OUT) // [اصلاح شده] استفاده از ثابت تعریف شده
        {
            event_type = "TRADE_PARTIAL_CLOSE_MASTER";
        }
        else
        {
            return;
        }

        SendTradeEvent(deal_ticket, event_type);
    }
    // --- مدیریت رویدادهای اصلاح پوزیشن ---
    // [اصلاح شده] استفاده از ثابت تعریف شده TRADE_TRANSACTION_MODIFY
    else if(trans.type == TRADE_TRANSACTION_MODIFY)
    {
        ulong pos_ticket = trans.position;
        if(pos_ticket == 0)
            return;

        if(PositionSelectByTicket(pos_ticket))
        {
            if(InpMagicNumber > 0 && PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
                 return;

            if(HistorySelectByPosition(pos_ticket))
            {
                 deal_ticket = HistoryDealGetTicket(0);
                 if(deal_ticket == 0)
                 {
                    LogEvent("WARN", "Could not find entry deal for modified position", pos_ticket);
                    return;
                 }

                 if(!HistoryDealSelect(deal_ticket))
                 {
                     LogEvent("ERROR", "Error selecting entry deal for modify event", deal_ticket);
                     return;
                 }

                 event_type = "TRADE_MODIFY";
                 SendTradeEvent(deal_ticket, event_type);
            }
            else
            {
                 LogEvent("WARN", "Could not find history for modified position", pos_ticket);
            }
        }
         else
         {
             LogEvent("WARN", "Could not select modified position by ticket", pos_ticket);
         }
    }
}


//+------------------------------------------------------------------+
//| تابع ساخت و ارسال پیام JSON رویداد معاملاتی
//+------------------------------------------------------------------+
void SendTradeEvent(ulong deal_ticket, string event_type)
{
    g_json_builder.Init();
    g_json_builder.Add("event", event_type);
    g_json_builder.Add("source_id_str", InpSourceIDStr);
    g_json_builder.Add("timestamp_ms", (long)HistoryDealGetInteger(deal_ticket, DEAL_TIME_MSC));

    long position_id = HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);

    g_json_builder.Add("deal_ticket", (long)deal_ticket);
    g_json_builder.Add("order_ticket", (long)HistoryDealGetInteger(deal_ticket, DEAL_ORDER));
    g_json_builder.Add("position_id", position_id);
    g_json_builder.Add("symbol", HistoryDealGetString(deal_ticket, DEAL_SYMBOL));
    g_json_builder.Add("magic", (long)HistoryDealGetInteger(deal_ticket, DEAL_MAGIC));
    g_json_builder.Add("volume", HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    g_json_builder.Add("price", HistoryDealGetDouble(deal_ticket, DEAL_PRICE));
    g_json_builder.Add("profit", HistoryDealGetDouble(deal_ticket, DEAL_PROFIT));

    if(PositionSelectByTicket(position_id))
    {
        g_json_builder.Add("position_sl", PositionGetDouble(POSITION_SL));
        g_json_builder.Add("position_tp", PositionGetDouble(POSITION_TP));
        g_json_builder.Add("position_type", (long)PositionGetInteger(POSITION_TYPE));
    }
    else
    {
        g_json_builder.Add("position_sl", HistoryDealGetDouble(deal_ticket, DEAL_SL));
        g_json_builder.Add("position_tp", HistoryDealGetDouble(deal_ticket, DEAL_TP));
        g_json_builder.Add("position_type", (long)HistoryDealGetInteger(deal_ticket, DEAL_TYPE));
    }

    if(event_type == "TRADE_MODIFY")
    {
        g_json_builder.Add("modified_fields", "sl_tp");
    }
    else if(event_type == "TRADE_PARTIAL_CLOSE_MASTER")
    {
        g_json_builder.Add("volume_closed", HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    }

    string json_message = g_json_builder.ToString();

    uchar json_data[];
    int data_len = StringToCharArray(json_message, json_data, 0, -1, CP_UTF8) - 1;

    if(data_len >= MAX_MSG_SIZE)
    {
        LogEvent("ERROR", "JSON message size exceeds MAX_MSG_SIZE", data_len);
        return;
    }

    if(ZmqSend(g_zmq_socket_push, json_data, data_len, ZMQ_DONTWAIT) == -1)
    {
        LogEvent("ERROR", "Failed to send ZMQ message", ZmqErrno());

        int size = ArraySize(g_retry_queue);
        ArrayResize(g_retry_queue, size + 1);

        g_retry_queue[size].attempts = 0;
        g_retry_queue[size].data_len = data_len;
        ArrayCopy(g_retry_queue[size].json_data, json_data, 0, 0, data_len);
    }
    else
    {
        LogEvent("INFO", "Event sent: " + event_type + " (PosID: " + IntegerToString(position_id) + ")");
    }
}

//+------------------------------------------------------------------+
//| تابع لاگ‌نویسی هوشمند
//+------------------------------------------------------------------+
void LogEvent(string level, string msg, long extra = -1)
{
    if(level == "INFO" && !InpEnableDebugLogging)
    {
        return;
    }

    datetime ts = TimeCurrent();
    string log_str = TimeToString(ts, TIME_DATE|TIME_MINUTES|TIME_SECONDS) +
                     " [" + level + "] [SourceEA-" + InpSourceIDStr + "]: " + msg;

    if(extra != -1)
    {
        log_str += " {extra:" + IntegerToString((int)extra) + "}";
    }

    if(level == "WARN" || level == "ERROR" || level == "CRITICAL")
    {
        Print(log_str);
    }

    if(g_log_file_handle != INVALID_HANDLE)
    {
        FileSeek(g_log_file_handle, 0, SEEK_END);
        FileWrite(g_log_file_handle, log_str + "\r\n");
    }

    if(level == "CRITICAL")
    {
        ExpertRemove();
    }
}
//+------------------------------------------------------------------+