// MQL5/Include/ZMQ/zmq.mqh
//
// فایل هدر اصلی ZMQ برای MQL5
// این فایل، فایل‌های هدر زمینه (Context) و سوکت (Socket) را فراخوانی می‌کند.
#property strict

#include "zmq_context.mqh"
#include "zmq_socket.mqh"

//--- ZMQ constants
#define ZMQ_VERSION_MAJOR         (4)
#define ZMQ_VERSION_MINOR         (3)
#define ZMQ_VERSION_PATCH         (4)

#define ZMQ_VERSION               ZMQ_MAKE_VERSION(ZMQ_VERSION_MAJOR, ZMQ_VERSION_MINOR, ZMQ_VERSION_PATCH)
#define ZMQ_MAKE_VERSION(major, minor, patch) \
    ((major) * 10000 + (minor) * 100 + (patch))

//--- ZMQ errors
#define ZMQ_HAUSNUMERO            (156384712)
#define ZMQ_EMSGSIZE              (ZMQ_HAUSNUMERO + 10)
#define ZMQ_ECONNABORTED          (ZMQ_HAUSNUMERO + 53)
#define ZMQ_ECONNRESET            (ZMQ_HAUSNUMERO + 54)
#define ZMQ_ETIMEDOUT             (ZMQ_HAUSNUMERO + 60)

//--- ZMQ options
#define ZMQ_SUBSCRIBE             (6)
#define ZMQ_UNSUBSCRIBE           (7)
#define ZMQ_TYPE                  (16)
#define ZMQ_RCVMORE               (13)
// [جدید] افزودن ثابت‌های High Water Mark
#define ZMQ_SNDHWM                (23) // Send High Water Mark
#define ZMQ_RCVHWM                (24) // Receive High Water Mark


//--- ZMQ message
#define ZMQ_DONTWAIT              (1)
#define ZMQ_SNDMORE               (2)

//--- ZMQ socket types
#define ZMQ_PAIR                  (0)
#define ZMQ_PUB                   (1)
#define ZMQ_SUB                   (2)
#define ZMQ_REQ                   (3)
#define ZMQ_REP                   (4)
#define ZMQ_DEALER                (5)
#define ZMQ_ROUTER                (6)
#define ZMQ_PULL                  (7)
#define ZMQ_PUSH                  (8)
#define ZMQ_XPUB                  (9)
#define ZMQ_XSUB                  (10)

//--- ZMQ functions
#import "libzmq.dll"
int ZmqVersion(int& major, int& minor, int& patch);
int ZmqErrno(void);
string ZmqStrError(int errnum);
#import