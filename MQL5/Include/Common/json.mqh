#property strict

class CJsonBuilder
{
protected:
    string m_json_string;
    bool   m_first_pair;

public:
    void Init(void)
    {
        m_json_string = "{";
        m_first_pair = true;
    }

    // --- توابع Add برای افزودن جفت‌های Key:Value ---
    
    // افزودن رشته: "key": "value"
    void Add(string key, string value)
    {
        if(!m_first_pair)
            m_json_string += ",";
        m_json_string += "\"" + key + "\":\"" + EscapeString(value) + "\"";
        m_first_pair = false;
    }

    // افزودن عدد صحیح: "key": 123
    void Add(string key, long value)
    {
        if(!m_first_pair)
            m_json_string += ",";
        m_json_string += "\"" + key + "\":" + (string)value;
        m_first_pair = false;
    }

    // افزودن عدد اعشاری: "key": 123.45
    void Add(string key, double value, int digits = 5)
    {
        if(!m_first_pair)
            m_json_string += ",";
        m_json_string += "\"" + key + "\":" + DoubleToString(value, digits);
        m_first_pair = false;
    }

    // برگرداندن رشته نهایی JSON
    string ToString(void)
    {
        return m_json_string + "}";
    }

protected:
    // تابع کمکی برای مدیریت کاراکترهای خاص در رشته
    string EscapeString(string value)
    {
        StringReplace(value, "\\", "\\\\"); // \ -> \\
        StringReplace(value, "\"", "\\\""); // " -> \"
        StringReplace(value, "\n", "\\n"); // Newline
        StringReplace(value, "\r", "\\r"); // Return
        StringReplace(value, "\t", "\\t"); // Tab
        return value;
    }
};