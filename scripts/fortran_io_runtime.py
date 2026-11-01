r"""Runtime for Fortran READ and file I/O, embedded verbatim into translated Python modules (see fortran_to_python._HELPERS).

Needs fortran_format_runtime.py (for _fparse and _fwrite) and the imports sys, os, math, re, tempfile.

Units: 5 is standard input, 6 standard output; OPEN attaches a file to any other number. A READ statement is one
_FReader: items are fetched with get(kind) ('i' integer, 'r' real, 'l' logical, 'a' character) and finish() ends the
record and returns IOSTAT (0 ok, -1 end of file, > 0 error). List-directed input (fmt None) splits records on blanks, commas
and slashes, supports r*value repeats and null values; formatted input follows the edit descriptors I F E D G L A X /.
"""

_funits = {}


class _FUnit:
    def __init__(self, handle, name=None, writable=False, readable=True):
        self.handle, self.name, self.writable, self.readable = handle, name, writable, readable
        self.pending = None                         # a record already read ahead (BACKSPACE not supported)

    def readline(self):
        line = self.handle.readline()
        return None if line == "" else line.rstrip("\n").rstrip("\r")


def _funit(number):
    if number not in _funits:
        if number == 5:
            _funits[5] = _FUnit(sys.stdin)
        elif number == 6:
            _funits[6] = _FUnit(sys.stdout, writable=True, readable=False)
    return _funits.get(number)


def _fopen(number, file=None, status="unknown", action="readwrite", position="asis"):
    """OPEN: returns IOSTAT."""
    status, action, position = (str(x).strip().lower() for x in (status, action, position))
    if number in _funits and number not in (5, 6):
        return 0 if _funits[number].name == (file and str(file).strip()) else 1
    try:
        if status == "scratch":
            handle = tempfile.TemporaryFile("w+")
            name = None
        else:
            name = str(file).strip()
            exists = os.path.exists(name)
            if status == "old" and not exists:
                return 29
            if status == "new" and exists:
                return 17
            mode = "a+" if position == "append" else ("r+" if exists and status in ("old", "unknown") and action != "write" else "w+")
            if action == "read":
                mode = "r"
            handle = open(name, mode)
            if position == "append":
                handle.seek(0, 2)
        _funits[number] = _FUnit(handle, name, writable=action != "read", readable=action != "write")
        return 0
    except OSError:
        return 5


def _fclose(number, status="keep"):
    unit = _funits.pop(number, None)
    if unit is not None and number not in (5, 6):
        unit.handle.close()
        if str(status).strip().lower() == "delete" and unit.name:
            try:
                os.remove(unit.name)
            except OSError:
                pass
    return 0


def _frewind(number):
    unit = _funit(number)
    if unit is None:
        return 1
    unit.handle.seek(0)
    return 0


def _fwriteunit(number, text):
    """Write already formatted text (records ending in newlines) to a unit; returns IOSTAT."""
    unit = _funit(number)
    if unit is None or not unit.writable:
        return 5
    unit.handle.write(text)
    unit.handle.flush()
    return 0


def _fexists(file):
    return os.path.exists(str(file).strip())


class _FReader:
    def __init__(self, number, fmt, die=True):
        self.unit = _funit(number)
        self.ok, self.iostat, self.die = True, 0, die
        self.line = None
        self.tokens = []
        self.ops = _fparse(fmt) if fmt is not None else None
        self.col = 0
        self.queue = None
        self.fmt_ops = None
        if self.unit is None or not self.unit.readable:
            self._fail(5)

    def _fail(self, code):
        if self.ok:
            self.ok, self.iostat = False, code

    def _record(self):
        if not self.ok:
            return False
        line = self.unit.readline()
        if line is None:
            self._fail(-1)
            return False
        self.line, self.col = line, 0
        return True

    # ---------------------------------------------------------------- list-directed
    @staticmethod
    def _split(line):
        """Tokens of a record: values, None for a null value, '/' to end the statement."""
        out, i, n = [], 0, len(line)
        need_value = True                          # True right after a comma: an empty field there is a null value
        while i < n:
            ch = line[i]
            if ch in " \t":
                i += 1
            elif ch == ",":
                if need_value:
                    out.append(None)
                need_value = True
                i += 1
            elif ch == "/":
                out.append("/")
                break
            else:
                if ch in "'\"":
                    j = i + 1
                    while j < n:
                        if line[j] == ch:
                            if j + 1 < n and line[j + 1] == ch:
                                j += 2
                                continue
                            break
                        j += 1
                    out.append(("str", line[i + 1:j].replace(ch * 2, ch)))
                    i = j + 1
                else:
                    j = i
                    while j < n and line[j] not in " \t,/":
                        j += 1
                    token = line[i:j]
                    m = re.match(r"^(\d+)\*(.*)$", token)
                    if m:
                        out.extend([m.group(2) if m.group(2) else None] * int(m.group(1)))
                    else:
                        out.append(token)
                    i = j
                need_value = False
        if need_value and out and line.rstrip().endswith(","):
            out.append(None)
        return out

    def _next_token(self):
        while not self.tokens:
            if not self._record():
                return "EOF"
            self.tokens = self._split(self.line)
        return self.tokens.pop(0)

    # ---------------------------------------------------------------- formatted
    def _next_field(self):
        """The next data edit descriptor and its text, walking X, / and literals on the way (None at the end)."""
        while True:
            if self.queue is None:
                self.queue = self._flatten(self.ops)
                self.tail_start = None
            if not self.queue:
                # format reversion: a new record from the last top-level group
                self.queue = self._flatten([op for op in self.ops if op[0] == "group"][-1:] or self.ops)
                if not self._record():
                    return None
                if not self.queue:
                    return None
            if self.line is None and not self._record():
                return None
            op = self.queue.pop(0)
            kind = op[0]
            if kind == "x":
                self.col += op[1]
            elif kind == "nl":
                if not self._record():
                    return None
            elif kind == "d":
                return op
            # literals and ':' consume nothing on input

    @staticmethod
    def _flatten(ops):
        out = []
        for op in ops:
            if op[0] == "group":
                for _ in range(op[1]):
                    out.extend(_FReader._flatten(op[2]))
            elif op[0] == "d":
                out.extend([op] * op[5])
            else:
                out.append(op)
        return out

    # ---------------------------------------------------------------- the public interface
    def get(self, kind, width=None):
        if not self.ok:
            return None
        if self.ops is None:
            token = self._next_token()
            if token == "EOF":
                return None
            if token == "/":
                self.tokens = ["/"]
                return None
            if token is None:
                return None
            try:
                return self._convert(kind, token)
            except ValueError:
                self._fail(5010)
                return None
        op = self._next_field()
        if op is None:
            if self.ok:
                self._fail(-1)
            return None
        _, letter, w, d, _e, _rep = op
        if letter == "A":
            w = w or width or len(self.line) - self.col
        field = self.line[self.col:self.col + (w or len(self.line))] if w else self.line[self.col:]
        self.col += len(field)
        try:
            return self._convert_field(kind, letter, field, d)
        except ValueError:
            self._fail(5010)
            return None

    @staticmethod
    def _real(text):
        text = text.strip().replace("d", "e").replace("D", "e")
        if re.match(r"^[+-]?\d*\.?\d*[+-]\d+$", text) and "e" not in text.lower():
            text = re.sub(r"(\d)([+-]\d+)$", r"\1e\2", text)       # 1.5+3 means 1.5e+3
        return float(text)

    def _convert(self, kind, token):
        if isinstance(token, tuple):
            return token[1]
        if kind == "i":
            return int(token)
        if kind == "r":
            return self._real(token)
        if kind == "l":
            body = token.lstrip(".").lower()
            if body[:1] in ("t", "f"):
                return body[0] == "t"
            raise ValueError("bad logical")
        return token

    def _convert_field(self, kind, letter, field, d):
        if kind == "a":
            return field
        compact = field.replace(" ", "")
        if kind == "i":
            return int(compact) if compact else 0
        if kind == "l":
            body = compact.lstrip(".").lower()
            return body[:1] == "t"
        if not compact:
            return 0.0
        if "." not in compact and not re.search(r"[eEdD]", compact) and d:
            return int(compact) / 10 ** d                           # F8.2 reading "12345" is 123.45
        return self._real(compact)

    def finish(self, die=None):
        """End of the statement: the rest of the current record is skipped. Returns IOSTAT."""
        if self.ok and self.line is None and self.ops is None and not self.tokens:
            self._record()                                          # READ with no items still consumes a record
        if self.ops is None and self.tokens == ["/"]:
            self.tokens = []
        if (self.die if die is None else die) and not self.ok:
            raise SystemExit(f"Fortran runtime error: end of file or bad input (iostat={self.iostat})" if self.iostat
                             else "Fortran runtime error")
        return self.iostat


def _freadstart(number, fmt=None, die=True):
    return _FReader(number, fmt, die)


def _flist(items):
    """List-directed output to a file: a leading blank, items separated by blanks (numbers in round-trip precision)."""
    parts = []
    for v in items:
        if isinstance(v, (bool, np.bool_)):
            parts.append("T" if v else "F")
        elif isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (complex, np.complexfloating)):
            parts.append(f"({float(v.real)!r},{float(v.imag)!r})")
        elif isinstance(v, (float, np.floating)):
            parts.append(repr(float(v)))
        else:
            parts.append(str(int(v)))
    return " " + " ".join(parts) + "\n"
