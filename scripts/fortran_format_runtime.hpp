// Runtime for Fortran formatted output, embedded verbatim into translated C++ files (see fortran_to_cpp._FORMAT_RUNTIME).
// fortran_write(fmt, items) formats a list of values with a Fortran format string and returns the text of the records,
// each ending in a newline. The logic mirrors fortran_format_runtime.py; both are checked against gfortran's output.
//
// Implemented: Iw[.m], Fw.d, Ew.d[Ee], Dw.d, ESw.d, Gw.d, Lw, A[w], nX, '/', quoted strings, repeat counts and nested
// groups, ':' and format reversion. Rounding is that of the exact decimal expansion (printf), as gfortran's.
struct FItem {
    enum Kind { INT, REAL, LOGICAL, TEXT, CPLX } kind;
    long long i = 0;
    double r = 0, im = 0;
    bool l = false;
    std::string a;
    FItem(int v) : kind(INT), i(v) {}
    FItem(long long v) : kind(INT), i(v) {}
    FItem(float v) : kind(REAL), r(v) {}
    FItem(double v) : kind(REAL), r(v) {}
    FItem(bool v) : kind(LOGICAL), l(v) {}
    FItem(const char* v) : kind(TEXT), a(v) {}
    FItem(const std::string& v) : kind(TEXT), a(v) {}
    FItem(std::complex<float> v) : kind(CPLX), r(v.real()), im(v.imag()) {}
    FItem(std::complex<double> v) : kind(CPLX), r(v.real()), im(v.imag()) {}
};

namespace ffmt {

struct Op {
    enum Kind { LIT, X, NL, COLON, DATA, GROUP } kind = LIT;
    std::string text;       // LIT text, or the descriptor letter(s)
    int n = 0;              // X count
    int w = -1, d = -1, e = -1, rep = 1;
    std::vector<Op> sub;    // GROUP
};

inline std::string stars(int w) { return std::string(w, '*'); }
inline std::string rjust(const std::string& s, int w, char fill = ' ') {
    return static_cast<int>(s.size()) >= w ? s : std::string(w - s.size(), fill) + s;
}

inline std::vector<Op> parse(const std::string& text, std::size_t& pos, bool inner) {
    std::vector<Op> ops;
    while (pos < text.size()) {
        char ch = text[pos];
        if (ch == ' ' || ch == ',') { ++pos; continue; }
        if (ch == ')') {
            if (!inner) throw std::runtime_error("unbalanced parenthesis in format");
            ++pos;
            return ops;
        }
        if (ch == '\'' || ch == '"') {
            std::size_t end = pos + 1;
            std::string chunk;
            while (end < text.size()) {
                if (text[end] == ch) {
                    if (end + 1 < text.size() && text[end + 1] == ch) { chunk += ch; end += 2; continue; }
                    break;
                }
                chunk += text[end++];
            }
            Op op; op.kind = Op::LIT; op.text = chunk;
            ops.push_back(op);
            pos = end + 1;
            continue;
        }
        if (ch == '/') { Op op; op.kind = Op::NL; ops.push_back(op); ++pos; continue; }
        if (ch == ':') { Op op; op.kind = Op::COLON; ops.push_back(op); ++pos; continue; }
        int rep = -1;
        if (std::isdigit(static_cast<unsigned char>(ch))) {
            rep = 0;
            while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) rep = rep * 10 + (text[pos++] - '0');
            while (pos < text.size() && text[pos] == ' ') ++pos;
        }
        if (pos < text.size() && text[pos] == '(') {
            ++pos;
            Op op; op.kind = Op::GROUP; op.rep = rep < 0 ? 1 : rep; op.sub = parse(text, pos, true);
            ops.push_back(op);
            continue;
        }
        std::string letter;
        while (pos < text.size() && std::isalpha(static_cast<unsigned char>(text[pos]))) letter += static_cast<char>(std::toupper(text[pos++]));
        if (letter == "X") { Op op; op.kind = Op::X; op.n = rep < 0 ? 1 : rep; ops.push_back(op); continue; }
        if (letter != "I" && letter != "F" && letter != "E" && letter != "D" && letter != "ES" && letter != "G" &&
            letter != "L" && letter != "A")
            throw std::runtime_error("unsupported edit descriptor " + letter);
        Op op; op.kind = Op::DATA; op.text = letter; op.rep = rep < 0 ? 1 : rep;
        auto number = [&](int& out) {
            if (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) {
                out = 0;
                while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) out = out * 10 + (text[pos++] - '0');
            }
        };
        number(op.w);
        if (pos < text.size() && text[pos] == '.') { ++pos; number(op.d); }
        if (pos < text.size() && (text[pos] == 'E' || text[pos] == 'e')) { ++pos; number(op.e); }
        ops.push_back(op);
    }
    return ops;
}

inline std::string fint(long long v, int w, int m) {
    unsigned long long mag = v < 0 ? 0ULL - static_cast<unsigned long long>(v) : static_cast<unsigned long long>(v);
    std::string s = std::to_string(mag);
    if (m >= 0) s = (m == 0 && v == 0) ? "" : rjust(s, m, '0');
    if (v < 0) s = "-" + s;
    if (w <= 0) return s;
    return static_cast<int>(s.size()) > w ? stars(w) : rjust(s, w);
}

inline bool special(double v, int w, std::string& out) {
    std::string s;
    if (std::isnan(v)) s = "NaN";
    else if (std::isinf(v)) {
        bool neg = v < 0;
        s = std::string(neg ? "-" : "") + ((w > 0 ? w : 8) >= (neg ? 9 : 8) ? "Infinity" : "Inf");
    } else return false;
    out = w > 0 ? (static_cast<int>(s.size()) <= w ? rjust(s, w) : stars(w)) : s;
    return true;
}

inline std::string printf_string(const char* spec, int precision, double v) {
    int n = std::snprintf(nullptr, 0, spec, precision, v);
    std::vector<char> buf(static_cast<std::size_t>(n) + 1);
    std::snprintf(buf.data(), buf.size(), spec, precision, v);
    return std::string(buf.data());
}

inline std::string drop_zero(const std::string& t) {
    if (t.rfind("0.", 0) == 0) return t.substr(1);
    if (t.rfind("-0.", 0) == 0) return "-" + t.substr(2);
    return t;
}

inline std::string fixed(double v, int w, int d) {
    std::string sp;
    if (special(v, w, sp)) return sp;
    std::string s = printf_string("%.*f", d, v);
    if (d == 0) s += ".";                          // F10.0 prints "2." : the decimal point is always written
    if (w <= 0) return drop_zero(s);
    if (static_cast<int>(s.size()) > w) s = drop_zero(s);
    return static_cast<int>(s.size()) > w ? stars(w) : rjust(s, w);
}

inline std::string expo(char letter, int exp, int expw) {
    std::string sign = exp < 0 ? "-" : "+";
    std::string mag = std::to_string(std::abs(exp));
    if (expw < 0) {
        if (std::abs(exp) < 100) return std::string(1, letter) + sign + rjust(mag, 2, '0');
        return sign + rjust(mag, 3, '0');
    }
    if (static_cast<int>(mag.size()) > expw) return "";
    return std::string(1, letter) + sign + rjust(mag, expw, '0');
}

inline std::string exponent_form(double v, int w, int d, int expw, char letter, bool scientific) {
    std::string sp;
    if (special(v, w, sp)) return sp;
    if (d < 0) d = 0;
    int digits = scientific ? d + 1 : d;
    if (digits == 0) digits = 1;
    std::string sign, digs;
    int exp;
    if (v == 0) {
        sign = std::signbit(v) ? "-" : "";
        digs = std::string(digits, '0');
        exp = 0;
    } else {
        std::string mant = printf_string("%.*e", digits - 1, std::fabs(v));
        std::size_t e = mant.find('e');
        exp = std::atoi(mant.c_str() + e + 1);
        for (std::size_t k = 0; k < e; ++k) if (mant[k] != '.') digs += mant[k];
        sign = v < 0 ? "-" : "";
        if (!scientific) exp += 1;
    }
    std::string tail = expo(letter, exp, expw);
    if (tail.empty()) return stars(w > 0 ? w : 1);
    std::string body = scientific ? digs.substr(0, 1) + "." + digs.substr(1) : (d > 0 ? "0." + digs : std::string("0."));
    std::string s = sign + body + tail;
    if (w > 0 && static_cast<int>(s.size()) > w && !scientific && s.compare(sign.size(), 2, "0.") == 0)
        s = sign + s.substr(sign.size() + 1);
    if (w <= 0) return s;
    return static_cast<int>(s.size()) > w ? stars(w) : rjust(s, w);
}

inline std::string general(double v, int w, int d, int expw) {
    std::string sp;
    if (special(v, w, sp)) return sp;
    if (d < 0) d = 0;
    int pad = expw < 0 ? 4 : expw + 2;
    if (v == 0) return rjust(fixed(0.0, w > 0 ? w - pad : -1, std::max(d - 1, 0)) + std::string(pad, ' '), std::max(w, 0));
    std::string mant = printf_string("%.*e", std::max(d, 1) - 1, std::fabs(v));
    int exp = std::atoi(mant.c_str() + mant.find('e') + 1) + 1;
    if (exp >= 0 && exp <= d)
        return rjust(fixed(v, w > 0 ? w - pad : -1, d - exp) + std::string(pad, ' '), std::max(w, 0));
    return exponent_form(v, w, d, expw, 'E', false);
}

struct Stop {};

}  // namespace ffmt

inline std::string fortran_write(const std::string& fmt, const std::vector<FItem>& items) {
    using namespace ffmt;
    std::string text = fmt;
    std::size_t b = text.find_first_not_of(" \t"), e = text.find_last_not_of(" \t");
    text = b == std::string::npos ? "" : text.substr(b, e - b + 1);
    if (text.size() >= 2 && text.front() == '(' && text.back() == ')') text = text.substr(1, text.size() - 2);
    std::size_t pos = 0;
    std::vector<Op> ops = parse(text, pos, false);

    std::vector<FItem> values;                      // a complex value takes two real edit descriptors
    for (const FItem& it : items) {
        if (it.kind == FItem::CPLX) { values.emplace_back(it.r); values.emplace_back(it.im); }
        else values.push_back(it);
    }
    std::size_t next = 0;
    std::vector<std::string> lines;
    std::string cur;
    int pending = 0;
    auto emit = [&](const std::string& s) { if (pending) { cur += std::string(pending, ' '); pending = 0; } cur += s; };
    auto newline = [&]() { lines.push_back(cur); cur.clear(); pending = 0; };

    std::function<void(const std::vector<Op>&)> run = [&](const std::vector<Op>& seq) {
        for (const Op& op : seq) {
            switch (op.kind) {
                case Op::LIT: emit(op.text); break;
                case Op::X: pending += op.n; break;
                case Op::NL: newline(); break;
                case Op::COLON: if (next >= values.size()) throw Stop(); break;
                case Op::GROUP: for (int k = 0; k < op.rep; ++k) run(op.sub); break;
                case Op::DATA:
                    for (int k = 0; k < op.rep; ++k) {
                        if (next >= values.size()) throw Stop();
                        const FItem& v = values[next++];
                        double r = v.kind == FItem::INT ? static_cast<double>(v.i) : v.r;
                        const std::string& L = op.text;
                        if (L == "I") emit(fint(v.kind == FItem::INT ? v.i : static_cast<long long>(v.r), op.w, op.d));
                        else if (L == "F") emit(fixed(r, op.w, op.d < 0 ? 0 : op.d));
                        else if (L == "E" || L == "D") emit(exponent_form(r, op.w, op.d, op.e, L[0], false));
                        else if (L == "ES") emit(exponent_form(r, op.w, op.d, op.e, 'E', true));
                        else if (L == "G") emit(v.kind == FItem::INT ? fint(v.i, op.w, -1) : general(r, op.w, op.d, op.e));
                        else if (L == "L") emit(rjust(v.l ? "T" : "F", op.w > 0 ? op.w : 1));
                        else if (L == "A") {
                            std::string s = v.a;
                            emit(op.w <= 0 ? s : (static_cast<int>(s.size()) >= op.w ? s.substr(0, op.w) : rjust(s, op.w)));
                        }
                    }
                    break;
            }
        }
    };
    int tail_group = -1;
    for (int k = static_cast<int>(ops.size()) - 1; k >= 0; --k)
        if (ops[k].kind == Op::GROUP) { tail_group = k; break; }
    try {
        run(ops);
        while (next < values.size()) {            // format reversion: a new record at the last top-level group
            std::size_t consumed = next;
            newline();
            run(tail_group >= 0 ? std::vector<Op>(ops.begin() + tail_group, ops.end()) : ops);
            if (next == consumed) break;
        }
    } catch (const Stop&) {
    }
    lines.push_back(cur);
    std::string out;
    for (std::size_t k = 0; k < lines.size(); ++k) out += lines[k] + (k + 1 < lines.size() ? "\n" : "");
    return out + "\n";
}

// List-directed output to a file: a leading blank, items separated by blanks (numbers in round-trip precision).
inline std::string fortran_list(const std::vector<FItem>& items) {
    std::string out = " ";
    bool first = true;
    auto number = [](double v) { char buf[64]; std::snprintf(buf, sizeof buf, "%.17g", v); return std::string(buf); };
    for (const FItem& it : items) {
        if (!first) out += " ";
        first = false;
        switch (it.kind) {
            case FItem::INT: out += std::to_string(it.i); break;
            case FItem::REAL: out += number(it.r); break;
            case FItem::LOGICAL: out += it.l ? "T" : "F"; break;
            case FItem::TEXT: out += it.a; break;
            case FItem::CPLX: out += "(" + number(it.r) + "," + number(it.im) + ")"; break;
        }
    }
    return out + "\n";
}
