// MQL5/Include/ZMQ/zmq_socket.mqh
//
// مدیریت سوکت‌های ZMQ (اتصال، ارسال، دریافت)

#property strict

#import "libzmq.dll"
int ZmqSocketNew(int context, int type);
int ZmqClose(int socket);
int ZmqSocketSet(int socket, int option, int value);
int ZmqSocketGet(int socket, int option);
int ZmqBind(int socket, string addr);
int ZmqConnect(int socket, string addr);
int ZmqUnbind(int socket, string addr);
int ZmqDisconnect(int socket, string addr);
int ZmqSend(int socket, uchar& data[], int len, int flags = 0);
int ZmqRecv(int socket, uchar& data[], int len, int flags = 0);
int ZmqSocketMonitor(int socket, string addr, int events);
#import

//--- Helper functions for strings
int ZmqSendString(int socket, string data, int flags = 0, uint codepage = CP_UTF8)
{
    uchar arr[];
    int len = StringToCharArray(data, arr, 0, -1, codepage) - 1; // -1 to remove null terminator
    return ZmqSend(socket, arr, len, flags);
}

string ZmqRecvString(int socket, int flags = 0, uint codepage = CP_UTF8)
{
    uchar arr[4096]; // 4KB buffer
    int len = ZmqRecv(socket, arr, 4096, flags);
    if(len > 0)
        return CharArrayToString(arr, 0, len, codepage);
    return "";
}

//--- Helper for setting socket options (like SUBSCRIBE)
int ZmqSocketSetString(int socket, int option, string value)
{
    uchar arr[];
    int len = StringToCharArray(value, arr, 0, -1, CP_UTF8) - 1;
    return ZmqSend(socket, arr, len, option); // Note: This uses ZmqSend with option flag, a bit of a hack
}