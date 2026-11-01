// Runtime for Fortran READ and file I/O, embedded verbatim into translated C++ files (see fortran_to_cpp._IO_RUNTIME).
// Needs fortran_format_runtime.hpp (ffmt::parse) and <fstream>, <iostream>, <map>, <memory>, <sstream>, <cstdio>.
// Mirrors fortran_io_runtime.py: units 5 (stdin) and 6 (stdout); a READ statement is one FReader whose get(x) fills the next
// item and whose finish() returns IOSTAT (0 ok, -1 end of file, > 0 error).
struct FUnit {
    std::unique_ptr<std::fstream> file;
    std::istream* in = nullptr;
    std::ostream* out = nullptr;
    std::string name;
    bool readable = true, writable = true;
};

inline std::map<int, FUnit>& fortran_units() {
    static std::map<int, FUnit> units;
    return units;
}

inline FUnit* fortran_unit(int number) {
    auto& units = fortran_units();
    if (number == 5 && !units.count(5)) { FUnit u; u.in = &std::cin; u.writable = false; units.emplace(5, std::move(u)); }
    if (number == 6 && !units.count(6)) { FUnit u; u.out = &std::cout; u.readable = false; units.emplace(6, std::move(u)); }
    auto it = units.find(number);
    return it == units.end() ? nullptr : &it->second;
}

inline std::string fortran_lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

inline std::string fortran_strip(const std::string& s) {
    std::size_t b = s.find_first_not_of(" \t"), e = s.find_last_not_of(" \t");
    return b == std::string::npos ? "" : s.substr(b, e - b + 1);
}

inline int fortran_open(int number, const std::string& file, std::string status = "unknown", std::string action = "readwrite",
                        std::string position = "asis") {
    status = fortran_lower(fortran_strip(status)); action = fortran_lower(fortran_strip(action)); position = fortran_lower(fortran_strip(position));
    auto& units = fortran_units();
    if (units.count(number) && number != 5 && number != 6) return fortran_strip(file) == units[number].name ? 0 : 1;
    FUnit u;
    u.name = fortran_strip(file);
    bool exists = static_cast<bool>(std::ifstream(u.name));
    if (status == "old" && !exists) return 29;
    if (status == "new" && exists) return 17;
    u.file.reset(new std::fstream());
    std::ios::openmode mode = std::ios::in | std::ios::out;
    if (action == "read") mode = std::ios::in;
    else if (!exists || status == "replace" || status == "new" || status == "scratch") mode |= std::ios::trunc;
    if (position == "append") mode |= std::ios::app;
    if (status == "scratch") u.name = "fortran_scratch_" + std::to_string(number) + ".tmp";
    u.file->open(u.name, mode);
    if (!u.file->is_open()) return 5;
    u.in = u.file.get(); u.out = u.file.get();
    u.readable = action != "write"; u.writable = action != "read";
    units[number] = std::move(u);
    return 0;
}

inline int fortran_close(int number, const std::string& status = "keep") {
    auto& units = fortran_units();
    auto it = units.find(number);
    if (it == units.end()) return 0;
    std::string name = it->second.name;
    if (it->second.file) it->second.file->close();
    units.erase(it);
    if (number != 5 && number != 6 && fortran_lower(fortran_strip(status)) == "delete") std::remove(name.c_str());
    return 0;
}

inline int fortran_rewind(int number) {
    FUnit* u = fortran_unit(number);
    if (!u || !u->file) return 1;
    u->file->clear();
    u->file->seekg(0);
    u->file->seekp(0);
    return 0;
}

inline int fortran_write_unit(int number, const std::string& text) {
    FUnit* u = fortran_unit(number);
    if (!u || !u->out || !u->writable) return 5;
    *u->out << text;
    u->out->flush();
    return 0;
}

inline bool fortran_exists(const std::string& file) { return static_cast<bool>(std::ifstream(fortran_strip(file))); }

class FReader {
public:
    bool ok = true;
    int iostat = 0;

    FReader(int number, const std::string* fmt, bool die) : die_(die), unit_(fortran_unit(number)) {
        if (fmt) { std::string text = fortran_strip(*fmt); if (text.size() >= 2 && text.front() == '(' && text.back() == ')') text = text.substr(1, text.size() - 2); std::size_t pos = 0; ops_ = ffmt::parse(text, pos, false); formatted_ = true; }
        if (!unit_ || !unit_->in || !unit_->readable) fail(5);
    }

    void get(int& v) { long long t = v; get(t); v = static_cast<int>(t); }
    void get(long long& v) {
        std::string s;
        if (!field('i', s)) return;
        try { std::size_t used = 0; long long r = s.empty() ? 0 : std::stoll(s, &used); if (used != s.size()) { fail(5010); return; } v = r; } catch (...) { fail(5010); }
    }
    void get(float& v) { double t = v; get(t); v = static_cast<float>(t); }
    void get(double& v) {
        std::string s;
        if (!field('r', s)) return;
        double r;
        if (!real(s, r, decimals_)) { fail(5010); return; }
        v = r;
    }
    void get(bool& v) {
        std::string s;
        if (!field('l', s)) return;
        std::string t = fortran_lower(s);
        while (!t.empty() && t[0] == '.') t.erase(0, 1);
        if (formatted_) { v = !t.empty() && t[0] == 't'; return; }
        if (!t.empty() && (t[0] == 't' || t[0] == 'f')) v = t[0] == 't'; else fail(5010);
    }
    void get(std::string& v, int width = -1) {
        std::string s;
        if (!field('a', s, width)) return;
        v = s;
    }
    int finish(int die = -1) {
        if (ok && !formatted_ && !have_line_ && tokens_.empty()) record();
        if (!formatted_ && tokens_.size() == 1 && tokens_[0].kind == Tok::SLASH) tokens_.clear();
        if ((die < 0 ? die_ : die != 0) && !ok) { std::fprintf(stderr, "Fortran runtime error: end of file or bad input (iostat=%d)\n", iostat); std::exit(2); }
        return iostat;
    }

private:
    struct Tok { enum Kind { VALUE, NUL, SLASH, STR } kind = VALUE; std::string text; };
    bool die_, formatted_ = false, have_line_ = false;
    FUnit* unit_;
    std::vector<ffmt::Op> ops_;
    std::vector<Tok> tokens_;
    std::vector<ffmt::Op> queue_;
    bool queue_started_ = false;
    std::string line_;
    std::size_t col_ = 0;
    int decimals_ = -1;

    void fail(int code) { if (ok) { ok = false; iostat = code; } }

    bool record() {
        if (!ok) return false;
        std::string line;
        if (!std::getline(*unit_->in, line)) { fail(-1); return false; }
        if (!line.empty() && line.back() == '\r') line.pop_back();
        line_ = line; col_ = 0; have_line_ = true;
        return true;
    }

    static void split(const std::string& line, std::vector<Tok>& out) {
        std::size_t i = 0, n = line.size();
        bool need_value = true;
        while (i < n) {
            char ch = line[i];
            if (ch == ' ' || ch == '\t') { ++i; continue; }
            if (ch == ',') { if (need_value) out.push_back(Tok{Tok::NUL, ""}); need_value = true; ++i; continue; }
            if (ch == '/') { out.push_back(Tok{Tok::SLASH, ""}); break; }
            if (ch == '\'' || ch == '"') {
                std::size_t j = i + 1; std::string chunk;
                while (j < n) { if (line[j] == ch) { if (j + 1 < n && line[j + 1] == ch) { chunk += ch; j += 2; continue; } break; } chunk += line[j++]; }
                out.push_back(Tok{Tok::STR, chunk});
                i = j + 1;
            } else {
                std::size_t j = i;
                while (j < n && line[j] != ' ' && line[j] != '\t' && line[j] != ',' && line[j] != '/') ++j;
                std::string token = line.substr(i, j - i);
                std::size_t star = token.find('*');
                if (star != std::string::npos && star > 0 && token.find_first_not_of("0123456789") == star) {
                    int count = std::atoi(token.substr(0, star).c_str());
                    std::string rest = token.substr(star + 1);
                    for (int k = 0; k < count; ++k) out.push_back(rest.empty() ? Tok{Tok::NUL, ""} : Tok{Tok::VALUE, rest});
                } else out.push_back(Tok{Tok::VALUE, token});
                i = j;
            }
            need_value = false;
        }
        if (need_value && !out.empty()) { std::size_t last = line.find_last_not_of(" \t"); if (last != std::string::npos && line[last] == ',') out.push_back(Tok{Tok::NUL, ""}); }
    }

    static bool real(std::string text, double& out, int decimals) {
        std::string s;
        for (char c : text) if (c != ' ') s += (c == 'd' || c == 'D') ? 'e' : c;
        if (s.empty()) { out = 0; return true; }
        bool has_dot = s.find('.') != std::string::npos, has_exp = s.find_first_of("eE") != std::string::npos;
        try {
            if (!has_dot && !has_exp && decimals > 0) { std::size_t used = 0; double v = static_cast<double>(std::stoll(s, &used)); if (used != s.size()) return false; out = v / std::pow(10.0, decimals); return true; }
            // 1.5+3 means 1.5e+3
            std::size_t sign = s.find_last_of("+-");
            if (!has_exp && sign != std::string::npos && sign > 0 && std::isdigit(static_cast<unsigned char>(s[sign - 1]))) s.insert(sign, "e");
            std::size_t used = 0; double v = std::stod(s, &used);
            if (used != s.size()) return false;
            out = v; return true;
        } catch (...) { return false; }
    }

    // next list-directed token or the next formatted field, as text; false when nothing is to be stored
    bool field(char kind, std::string& out, int width = -1) {
        if (!ok) return false;
        if (!formatted_) {
            while (tokens_.empty()) { if (!record()) return false; split(line_, tokens_); }
            Tok t = tokens_.front(); tokens_.erase(tokens_.begin());
            if (t.kind == Tok::SLASH) { tokens_.insert(tokens_.begin(), t); return false; }
            if (t.kind == Tok::NUL) return false;
            out = t.text; (void)kind;
            return true;
        }
        const ffmt::Op* op = next_op();
        if (!op) { if (ok) fail(-1); return false; }
        int w = op->w;
        if (op->text == "A" && w <= 0) w = width > 0 ? width : static_cast<int>(line_.size() - std::min(col_, line_.size()));
        std::string f = col_ < line_.size() ? line_.substr(col_, w > 0 ? static_cast<std::size_t>(w) : std::string::npos) : "";
        col_ += f.size();
        decimals_ = op->d;
        if (kind != 'a') { std::string c; for (char ch : f) if (ch != ' ') c += ch; f = c; }
        out = f;
        return true;
    }

    static void flatten(const std::vector<ffmt::Op>& ops, std::vector<ffmt::Op>& out) {
        for (const auto& op : ops) {
            if (op.kind == ffmt::Op::GROUP) { for (int k = 0; k < op.rep; ++k) flatten(op.sub, out); }
            else if (op.kind == ffmt::Op::DATA) { for (int k = 0; k < op.rep; ++k) out.push_back(op); }
            else out.push_back(op);
        }
    }

    const ffmt::Op* next_op() {
        for (;;) {
            if (!queue_started_) { flatten(ops_, queue_); queue_started_ = true; }
            if (queue_.empty()) {
                std::vector<ffmt::Op> tail;
                for (auto it = ops_.rbegin(); it != ops_.rend(); ++it) if (it->kind == ffmt::Op::GROUP) { tail.push_back(*it); break; }
                flatten(tail.empty() ? ops_ : tail, queue_);
                if (!record() || queue_.empty()) return nullptr;
            }
            if (!have_line_ && !record()) return nullptr;
            current_ = queue_.front();
            queue_.erase(queue_.begin());
            if (current_.kind == ffmt::Op::X) col_ += current_.n;
            else if (current_.kind == ffmt::Op::NL) { if (!record()) return nullptr; }
            else if (current_.kind == ffmt::Op::DATA) return &current_;
        }
    }
    ffmt::Op current_;
};
