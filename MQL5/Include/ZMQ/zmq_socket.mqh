// MQL5/Include/ZMQ/zmq_socket.mqh
//
// [اصلاح شده] مدیریت سوکت‌های ZMQ (اتصال، ارسال، دریافت، تنظیم گزینه‌ها)

#property strict

#import "libzmq.dll"
int zmq_socket(int context, int type); // تابع اصلی برای ایجاد سوکت
int zmq_close(int socket);
// [جدید] تابع اصلی DLL برای تنظیم گزینه‌ها (همه انواع)
int zmq_setsockopt(int socket, int option, const uchar &value[], int valuesize);
// [جدید] تابع اصلی DLL برای دریافت گزینه‌ها (فقط int)
int zmq_getsockopt(int socket, int option, int& value, int& valuesize);
int zmq_bind(int socket, string addr);
int zmq_connect(int socket, string addr);
int zmq_unbind(int socket, string addr);
int zmq_disconnect(int socket, string addr);
int zmq_send(int socket, uchar& data[], int len, int flags = 0);
int zmq_recv(int socket, uchar& data[], int len, int flags = 0);
int zmq_socket_monitor(int socket, string addr, int events);
#import

//--- توابع کمکی MQL5 ---

// تابع ایجاد سوکت (نام ساده‌تر)
int ZmqSocketNew(int context, int type)
{
    return zmq_socket(context, type);
}

// تابع بستن سوکت (نام قبلی)
int ZmqClose(int socket)
{
    return zmq_close(socket);
}

// [اصلاح شده] تابع تنظیم گزینه‌های عددی (int)
int ZmqSocketSet(int socket, int option, int value)
{
    uchar val_arr[sizeof(int)];
    // تبدیل int به آرایه بایت
    ArrayCopy(val_arr, 0, 0, (uchar)value);
    // فراخوانی تابع اصلی DLL
    return zmq_setsockopt(socket, option, val_arr, sizeof(int));
}

// [جدید] تابع کمکی برای تنظیم گزینه‌های بایتی (uchar[]) مانند Subscribe
int ZmqSocketSetBytes(int socket, int option, uchar &value[], int valuesize)
{
    // فراخوانی مستقیم تابع اصلی DLL
    return zmq_setsockopt(socket, option, value, valuesize);
}

// [اصلاح شده] تابع دریافت گزینه‌های عددی (int)
int ZmqSocketGet(int socket, int option, int& value) // پارامتر سوم به صورت مرجع
{
    int valuesize = sizeof(int);
    // فراخوانی تابع اصلی DLL
    return zmq_getsockopt(socket, option, value, valuesize);
}

// توابع اتصال (نام قبلی)
int ZmqBind(int socket, string addr) { return zmq_bind(socket, addr); }
int ZmqConnect(int socket, string addr) { return zmq_connect(socket, addr); }
int ZmqUnbind(int socket, string addr) { return zmq_unbind(socket, addr); }
int ZmqDisconnect(int socket, string addr) { return zmq_disconnect(socket, addr); }

// توابع ارسال/دریافت بایت (نام قبلی)
int ZmqSend(int socket, uchar& data[], int len, int flags = 0) { return zmq_send(socket, data, len, flags); }
int ZmqRecv(int socket, uchar& data[], int len, int flags = 0) { return zmq_recv(socket, data, len, flags); }

// تابع مانیتور سوکت (نام قبلی)
int ZmqSocketMonitor(int socket, string addr, int events) { return zmq_socket_monitor(socket, addr, events); }


//--- توابع کمکی برای رشته‌ها (بدون تغییر) ---
int ZmqSendString(int socket, string data, int flags = 0, uint codepage = CP_UTF8)
{
    uchar arr[];
    int len = StringToCharArray(data, arr, 0, -1, codepage) - 1;
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

// [حذف شده] تابع ZmqSocketSetString حذف شد چون با ZmqSocketSetBytes جایگزین می‌شود
// int ZmqSocketSetString(int socket, int option, string value) ...