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

struct RetryMessage {
    string json;
    int attempts;
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

int OnInit() {
    if (StringLen(InpCopyIDStr) == 0) {
        LogEvent("ERROR", "Invalid InpCopyIDStr: empty");
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

bool FetchConfiguration() {
    LogEvent("INFO", "Attempting to fetch configuration for '" + InpCopyIDStr + "'");

    g_zmq_socket_req = ZmqSocketNew(g_zmq_context, ZMQ_REQ);
    string req_address = "tcp://" + InpServerAddress + ":" + (string)InpConfigPort;

    if (ZmqConnect(g_zmq_socket_req, req_address) != 0) {
        LogEvent("ERROR", "Failed to connect ZMQ REQ socket to " + req_address, ZmqErrno());
        return false;
    }

    g_json_builder.Init();
    g_json_builder.Add("command", "GET_CONFIG");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);

    if (ZmqSendString(g_zmq_socket_req, g_json_builder.ToString(), 0) == -1) {
        LogEvent("ERROR", "Failed to send config request", ZmqErrno());
        ZmqClose(g_zmq_socket_req);
        return false;
    }

    uchar recv_buffer[65536];
    int recv_len = ZmqRecv(g_zmq_socket_req, recv_buffer, 65536, 0);
    ZmqClose(g_zmq_socket_req);

    if (recv_len <= 0) {
        LogEvent("ERROR", "Failed to receive config response", ZmqErrno());
        return false;
    }

    string json_response = CharArrayToString(recv_buffer, 0, recv_len);

    CJAVal json_data;
    if (!json_data.Deserialize(json_response)) {
        LogEvent("ERROR", "Failed to deserialize JSON response: " + json_response);
        return false;
    }

    if (json_data["status"].ToStr() != "OK") {
        LogEvent("ERROR", "Server returned an error: " + json_data["message"].ToStr());
        return false;
    }

    CJAVal config = json_data["config"];

    CJAVal global_settings = config["global_settings"];
    g_global_config.DailyDrawdownPercent = global_settings["daily_drawdown_percent"].ToDbl();
    g_global_config.AlertDrawdownPercent = global_settings["alert_drawdown_percent"].ToDbl();
    g_global_config.ResetDDFlag = global_settings["reset_dd_flag"].ToBool();

    LogEvent("INFO", "Global settings loaded: DD=" + DoubleToString(g_global_config.DailyDrawdownPercent, 2) + "%, Alert=" + DoubleToString(g_global_config.AlertDrawdownPercent, 2) + "%");

    if (g_global_config.ResetDDFlag) {
        g_daily_dd = 0.0;
        g_last_dd_reset = TimeCurrent();
        g_global_config.ResetDDFlag = false;
        LogEvent("INFO", "DD reset triggered on config fetch");
    }

    CJAVal mappings = config["mappings"];
    int mappings_count = mappings.Size();
    ArrayResize(g_source_configs, mappings_count);

    for (int i = 0; i < mappings_count; i++) {
        CJAVal m = mappings[i];
        g_source_configs[i].SourceTopicID = m["source_topic_id"].ToStr();
        g_source_configs[i].CopyMode = m["copy_mode"].ToStr();
        g_source_configs[i].AllowedSymbols = m["allowed_symbols"].ToStr();
        g_source_configs[i].VolumeType = m["volume_type"].ToStr();
        g_source_configs[i].VolumeValue = m["volume_value"].ToDbl();
        g_source_configs[i].MaxLotSize = m["max_lot_size"].ToDbl();
        g_source_configs[i].MaxConcurrentTrades = (int)m["max_concurrent_trades"].ToInt();
        g_source_configs[i].SourceDrawdownLimit = m["source_drawdown_limit"].ToDbl();

        LogEvent("INFO", "Mapping #" + IntegerToString(i) + " loaded: Source=" + g_source_configs[i].SourceTopicID + ", Vol=" + DoubleToString(g_source_configs[i].VolumeValue, 2) + " (" + g_source_configs[i].VolumeType + ")");
    }

    return true;
}

void OnDeinit(const int reason) {
    LogEvent("INFO", "Deinitializing Copy EA...");

    EventKillTimer();
    CleanupZMQ();

    LogEvent("INFO", "Copy EA Deinitialized.");
}

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
}

void OnTimer() {
    if (ArraySize(g_retry_queue) > 0) {
        RetryMessage msg = g_retry_queue[0];
        if (ZmqSendString(g_zmq_socket_push, msg.json, ZMQ_DONTWAIT) != -1) {
            LogEvent("INFO", "Retry report send successful");
            ArrayCopy(g_retry_queue, g_retry_queue, 0, 1);
            ArrayResize(g_retry_queue, ArraySize(g_retry_queue) - 1);
        } else {
            int err = ZmqErrno();
            LogEvent("WARN", "Retry report send failed", err);
            g_retry_queue[0].attempts++;
            if (g_retry_queue[0].attempts >= 3) {
                LogEvent("ERROR", "Max retries reached for report, discarding");
                ArrayCopy(g_retry_queue, g_retry_queue, 0, 1);
                ArrayResize(g_retry_queue, ArraySize(g_retry_queue) - 1);
            }
        }
    }

    g_timer_count++;
    if (g_timer_count % 300 == 0) {
        if (!FetchConfiguration()) {
            LogEvent("WARN", "Config refresh failed");
        } else {
            UpdateSubscriptions();
            LogEvent("INFO", "Config refreshed successfully");
        }
    }

    g_timer_count++;
    if (g_timer_count % 300 == 0) {
        g_json_builder.Init();
        g_json_builder.Add("event", "PING_COPY");
        g_json_builder.Add("copy_id_str", InpCopyIDStr);
        g_json_builder.Add("timestamp", (long)TimeCurrent());
        string ping_msg = g_json_builder.ToString();

        if (ZmqSendString(g_zmq_socket_push, ping_msg, ZMQ_DONTWAIT) == -1) {
            LogEvent("WARN", "Ping send failed", ZmqErrno());
            g_ping_fails++;
            if (g_ping_fails >= 3) {
                LogEvent("ERROR", "Multiple ping fails, attempting reconnect", g_ping_fails);
                ReconnectSockets();
            }
        } else {
            g_ping_fails = 0;
            LogEvent("INFO", "Ping sent successfully");
        }
    }

    if (TimeCurrent() - g_last_recv_time < 0.05) return;

    CheckSignalSocket();
}

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

void UpdateSubscriptions() {
    for (int i = 0; i < ArraySize(g_prev_topics); i++) {
        uchar topic_array[];
        StringToCharArray(g_prev_topics[i], topic_array);
        ZmqSocketSet(g_zmq_socket_sub, ZMQ_UNSUBSCRIBE, topic_array);
        LogEvent("INFO", "Unsubscribed from old topic: " + g_prev_topics[i]);
    }

    ArrayResize(g_prev_topics, ArraySize(g_source_configs));

    int mappings_count = ArraySize(g_source_configs);
    if (mappings_count == 0) {
        LogEvent("WARN", "No active mappings, nothing to copy");
    }

    for (int i = 0; i < mappings_count; i++) {
        string topic = g_source_configs[i].SourceTopicID;
        uchar topic_array[];
        StringToCharArray(topic, topic_array);
        if (ZmqSocketSet(g_zmq_socket_sub, ZMQ_SUBSCRIBE, topic_array) != 0) {
            LogEvent("ERROR", "Failed to subscribe to topic '" + topic + "'", ZmqErrno());
        } else {
            LogEvent("INFO", "Subscribed to topic: '" + topic + "'");
            g_prev_topics[i] = topic;
        }
    }
}

void CheckSignalSocket() {
    if (g_zmq_socket_sub == 0) return;

    uchar recv_buffer[4096];
    string topic_received;
    string json_message;

    while (true) {
        int topic_len = ZmqRecv(g_zmq_socket_sub, recv_buffer, 4096, ZMQ_DONTWAIT);
        if (topic_len <= 0) break;

        topic_received = CharArrayToString(recv_buffer, 0, topic_len);

        int rcvmore = 0;
        ZmqSocketGet(g_zmq_socket_sub, ZMQ_RCVMORE, rcvmore);

        if (!rcvmore) {
            LogEvent("WARN", "Received topic '" + topic_received + "' but no message body");
            continue;
        }

        int msg_len = ZmqRecv(g_zmq_socket_sub, recv_buffer, 4096, ZMQ_DONTWAIT);
        if (msg_len <= 0) {
            LogEvent("WARN", "Message body read failed for topic '" + topic_received + "'", ZmqErrno());
            continue;
        }

        json_message = CharArrayToString(recv_buffer, 0, msg_len);

        LogEvent("INFO", "Received signal on topic '" + topic_received + "'");
        g_last_recv_time = TimeCurrent();

        ProcessSignal(topic_received, json_message);
    }
}




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





//+------------------------------------------------------------------+
//| پردازش سیگنال دریافت شده از سرور                                  |
//+------------------------------------------------------------------+
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

        CheckDailyDrawdown();

        double point_value = SymbolInfoDouble(symbol, SYMBOL_POINT) * SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
        double projected_profit = final_volume * point_value * (position_type == 0 ? (tp - price) : (price - tp));
        if(g_daily_dd + projected_profit < -AccountEquity() * (g_global_config.DailyDrawdownPercent / 100.0))
        {
            LogEvent("WARN", "Trade rejected: exceeds daily DD limit", g_daily_dd + projected_profit);
            return;
        }

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
            LogEvent("INFO", "Removed position from map", ticket);
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
    
    LogEvent("INFO", "Deal " + (string)trans.deal + " closed. Profit: " + DoubleToString(profit, 2) + ". New Daily DD total: " + DoubleToString(g_daily_dd, 2));

    SendCloseReport(trans.deal);
}





void SendCloseReport(ulong deal_ticket) {
    if (g_zmq_socket_push == 0) return;

    if (!HistoryDealSelect(deal_ticket)) {
        LogEvent("ERROR", "Failed to select deal for report", deal_ticket);
        return;
    }

    string comment = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
    string parts[];
    string source_id_str = "UNKNOWN";
    long source_ticket = 0;

    if (StringSplit(comment, '|', parts) >= 2) {
        source_ticket = (long)StringToInteger(parts[0]);
        source_id_str = parts[1];
    }

    g_json_builder.Init();
    g_json_builder.Add("event", "TRADE_CLOSED_COPY");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);
    g_json_builder.Add("source_id_str", source_id_str);
    g_json_builder.Add("source_ticket", source_ticket);
    g_json_builder.Add("symbol", HistoryDealGetString(deal_ticket, DEAL_SYMBOL));
    g_json_builder.Add("profit", HistoryDealGetDouble(deal_ticket, DEAL_PROFIT));
    g_json_builder.Add("volume", HistoryDealGetDouble(deal_ticket, DEAL_VOLUME));
    g_json_builder.Add("deal_ticket", (long)deal_ticket);

    string json_message = g_json_builder.ToString();

    if (ZmqSendString(g_zmq_socket_push, json_message, ZMQ_DONTWAIT) == -1) {
        LogEvent("ERROR", "Failed to send close report", ZmqErrno());
        int size = ArraySize(g_retry_queue);
        ArrayResize(g_retry_queue, size + 1);
        g_retry_queue[size].json = json_message;
        g_retry_queue[size].attempts = 0;
    } else {
        LogEvent("INFO", "Close report sent successfully");
    }
}

void SendErrorReport(string error_msg) {
    g_json_builder.Init();
    g_json_builder.Add("event", "EA_ERROR");
    g_json_builder.Add("copy_id_str", InpCopyIDStr);
    g_json_builder.Add("message", error_msg);

    string json_message = g_json_builder.ToString();

    if (ZmqSendString(g_zmq_socket_push, json_message, ZMQ_DONTWAIT) == -1) {
        LogEvent("ERROR", "Failed to send error report", ZmqErrno());
    } else {
        LogEvent("INFO", "Error report sent: " + error_msg);
    }
}

void LogEvent(string level, string msg, variant extra = -1) {
    datetime ts = TimeCurrent();
    string log_str = TimeToString(ts, TIME_DATE|TIME_MINUTES|TIME_SECONDS) + " [" + level + "] [CopyEA]: " + msg;
    if (extra != -1) {
        log_str += " {" + VariantToString(extra) + "}";
    }
    Print(log_str);

    if (level == "ERROR" || level == "CRITICAL") {
        int file = FileOpen("CopyEA.log", FILE_WRITE|FILE_ANSI|FILE_TXT|FILE_SHARE_READ);
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

string VariantToString(variant v) {
    if (typename(v) == "long" || typename(v) == "int") {
        return "extra:" + IntegerToString((int)v);
    } else if (typename(v) == "double") {
        return "extra:" + DoubleToString((double)v, 2);
    } else if (typename(v) == "string") {
        return "extra:" + (string)v;
    }
    return "";
}