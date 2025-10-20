#property copyright "TradeCopier Professional"
#property version "1.0"
#property strict

#include <Include/ZMQ/zmq.mqh>
#include <Include/Common/json.mqh>

input string InpServerAddress = "127.0.0.1";
input int InpSignalPort = 5555;
input string InpSourceIDStr = "S1";
input ulong InpMagicNumber = 0;

int g_zmq_context;
int g_zmq_socket_push;
CJsonBuilder g_json_builder;

struct RetryMessage {
    string json;
    int attempts;
};

RetryMessage g_retry_queue[];
int g_ping_fails = 0;
int g_timer_count = 0;

int OnInit() {
    if (StringLen(InpSourceIDStr) == 0) {
        LogEvent("ERROR", "Invalid InpSourceIDStr: empty");
        return(INIT_PARAMETERS_INCORRECT);
    }
    if (InpMagicNumber < 0) {
        LogEvent("ERROR", "Invalid InpMagicNumber: negative");
        return(INIT_PARAMETERS_INCORRECT);
    }

    g_zmq_context = ZmqContextNew(1);
    if (g_zmq_context == 0) {
        LogEvent("ERROR", "Failed to create ZMQ context", ZmqErrno());
        return(INIT_FAILED);
    }

    g_zmq_socket_push = ZmqSocketNew(g_zmq_context, ZMQ_PUSH);
    if (g_zmq_socket_push == 0) {
        LogEvent("ERROR", "Failed to create ZMQ PUSH socket", ZmqErrno());
        ZmqContextTerm(g_zmq_context);
        return(INIT_FAILED);
    }

    ZmqSocketSet(g_zmq_socket_push, ZMQ_SNDHWM, 100);

    string address = "tcp://" + InpServerAddress + ":" + (string)InpSignalPort;
    if (ZmqConnect(g_zmq_socket_push, address) != 0) {
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

void OnDeinit(const int reason) {
    LogEvent("INFO", "Deinitializing Source EA...");

    EventKillTimer();

    if (g_zmq_socket_push != 0)
        ZmqClose(g_zmq_socket_push);
    if (g_zmq_context != 0)
        ZmqContextTerm(g_zmq_context);

    LogEvent("INFO", "Source EA Deinitialized.");
}

void OnTimer() {
    if (ArraySize(g_retry_queue) > 0) {
        RetryMessage msg = g_retry_queue[0];
        if (ZmqSendString(g_zmq_socket_push, msg.json, ZMQ_DONTWAIT) != -1) {
            LogEvent("INFO", "Retry send successful");
            ArrayCopy(g_retry_queue, g_retry_queue, 0, 1);
            ArrayResize(g_retry_queue, ArraySize(g_retry_queue) - 1);
        } else {
            int err = ZmqErrno();
            LogEvent("WARN", "Retry send failed", err);
            g_retry_queue[0].attempts++;
            if (g_retry_queue[0].attempts >= 3) {
                LogEvent("ERROR", "Max retries reached, discarding message");
                ArrayCopy(g_retry_queue, g_retry_queue, 0, 1);
                ArrayResize(g_retry_queue, ArraySize(g_retry_queue) - 1);
            }
        }
    }

    g_timer_count++;
    if (g_timer_count % 6 == 0) {
        g_json_builder.Init();
        g_json_builder.Add("event", "PING");
        g_json_builder.Add("source_id_str", InpSourceIDStr);
        g_json_builder.Add("timestamp", (long)TimeCurrent());
        string ping_msg = g_json_builder.ToString();

        if (ZmqSendString(g_zmq_socket_push, ping_msg, ZMQ_DONTWAIT) == -1) {
            LogEvent("WARN", "Ping send failed", ZmqErrno());
            g_ping_fails++;
            if (g_ping_fails >= 3) {
                LogEvent("ERROR", "Multiple ping fails, attempting reconnect", g_ping_fails);
                ZmqDisconnect(g_zmq_socket_push, "tcp://" + InpServerAddress + ":" + (string)InpSignalPort);
                if (ZmqConnect(g_zmq_socket_push, "tcp://" + InpServerAddress + ":" + (string)InpSignalPort) == 0) {
                    LogEvent("INFO", "Reconnect successful");
                    g_ping_fails = 0;
                } else {
                    LogEvent("CRITICAL", "Reconnect failed", ZmqErrno());
                }
            }
        } else {
            g_ping_fails = 0;
        }
    }
}

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result) {
    if (trans.type != TRADE_TRANSACTION_DEAL_ADD && trans.type != TRADE_TRANSACTION_HISTORY_UPDATE && trans.type != TRADE_TRANSACTION_POSITION_UPDATE)
        return;

    if (InpMagicNumber > 0 && trans.magic != InpMagicNumber)
        return;

    if (!HistoryDealSelect(trans.deal)) {
        LogEvent("ERROR", "Error selecting deal", trans.deal);
        return;
    }

    string event_type = "";
    ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);

    if (entry == DEAL_ENTRY_IN) {
        event_type = "TRADE_OPEN";
    } else if (entry == DEAL_ENTRY_OUT) {
        event_type = "TRADE_CLOSE_MASTER";
    } else if (entry == DEAL_ENTRY_IN_OUT) {
        event_type = "TRADE_PARTIAL_CLOSE_MASTER";
    } else {
        ENUM_DEAL_REASON reason = (ENUM_DEAL_REASON)HistoryDealGetInteger(trans.deal, DEAL_REASON);
        if (reason == DEAL_REASON_SL || reason == DEAL_REASON_TP || reason == DEAL_REASON_CLIENT) {
            event_type = "TRADE_MODIFY";
        } else {
            return;
        }
    }

    SendTradeEvent(trans.deal, event_type);
}

void SendTradeEvent(ulong deal_ticket, string event_type) {
    g_json_builder.Init();

    g_json_builder.Add("event", event_type);
    g_json_builder.Add("source_id_str", InpSourceIDStr);
    g_json_builder.Add("timestamp_ms", (long)HistoryDealGetInteger(deal_ticket, DEAL_TIME_MSC));

    g_json_builder.Add("deal_ticket", (long)deal_ticket);
    g_json_builder.Add("order_ticket", (long)HistoryDealGetInteger(deal_ticket, DEAL_ORDER));
    g_json_builder.Add("position_id", (long)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID));
    g_json_builder.Add("symbol", HistoryDealGetString(deal_ticket, DEAL_SYMBOL));
    g_json_builder.Add("magic", (long)HistoryDealGetInteger(deal_ticket, DEAL_MAGIC));
    g_json_builder.Add("volume", HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    g_json_builder.Add("price", HistoryDealGetDouble(deal_ticket, DEAL_PRICE));
    g_json_builder.Add("profit", HistoryDealGetDouble(deal_ticket, DEAL_PROFIT));

    if (PositionSelectByTicket(HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID))) {
        g_json_builder.Add("position_sl", PositionGetDouble(POSITION_SL));
        g_json_builder.Add("position_tp", PositionGetDouble(POSITION_TP));
        g_json_builder.Add("position_type", (long)PositionGetInteger(POSITION_TYPE));
    } else {
        g_json_builder.Add("position_sl", HistoryDealGetDouble(deal_ticket, DEAL_SL));
        g_json_builder.Add("position_tp", HistoryDealGetDouble(deal_ticket, DEAL_TP));
        g_json_builder.Add("position_type", (long)HistoryDealGetInteger(deal_ticket, DEAL_TYPE));
    }

    if (event_type == "TRADE_MODIFY") {
        g_json_builder.Add("modified_fields", "sl,tp");
    } else if (event_type == "TRADE_PARTIAL_CLOSE_MASTER") {
        g_json_builder.Add("volume_closed", HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    }

    string json_message = g_json_builder.ToString();

    if (ZmqSendString(g_zmq_socket_push, json_message, ZMQ_DONTWAIT) == -1) {
        LogEvent("ERROR", "Failed to send ZMQ message", ZmqErrno());
        int size = ArraySize(g_retry_queue);
        ArrayResize(g_retry_queue, size + 1);
        g_retry_queue[size].json = json_message;
        g_retry_queue[size].attempts = 0;
    } else {
        LogEvent("INFO", "Event sent successfully: " + json_message);
    }
}

void LogEvent(string level, string msg, long extra = -1) {
    datetime ts = TimeCurrent();
    string log_str = TimeToString(ts, TIME_DATE|TIME_MINUTES|TIME_SECONDS) + " [" + level + "] [SourceEA]: " + msg;
    if (extra != -1) {
        log_str += " {extra:" + IntegerToString((int)extra) + "}";
    }
    Print(log_str);

    if (level == "ERROR" || level == "CRITICAL") {
        int file = FileOpen("SourceEA.log", FILE_WRITE|FILE_ANSI|FILE_TXT|FILE_SHARE_READ);
        if (file != INVALID_HANDLE) {
            FileSeek(file, 0, SEEK_END);
            FileWrite(file, log_str);
            FileClose(file);
        }
        if (level == "CRITICAL") {
            ExpertRemove();
        }
    }
}