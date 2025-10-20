// MQL5/Include/ZMQ/zmq_context.mqh
//
// مدیریت کانتکست ZMQ (ایجاد و خاتمه دادن)

#property strict

#import "libzmq.dll"
int ZmqContextNew(int io_threads = 1);
int ZmqContextTerm(int context);
int ZmqContextShutdown(int context);
int ZmqContextSet(int context, int option, int value);
int ZmqContextGet(int context, int option);
#import

//--- Wrapper class for ZMQ context
class CZmqContext
{
protected:
    int m_context;

public:
    CZmqContext(int io_threads = 1) : m_context(ZmqContextNew(io_threads))
    {
    }

    ~CZmqContext(void)
    {
        if(m_context != 0)
            ZmqContextTerm(m_context);
    }

    int Context(void) const { return m_context; }
};