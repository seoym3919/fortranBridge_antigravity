r"""Runtime for Fortran formatted output, embedded verbatim into translated Python modules (see fortran_to_python._HELPERS).

Only function definitions live here (the generated module imports math and re). `_fwrite(fmt, items)` formats a
list of values with a Fortran format string and returns the text of the records, each ending in a newline.

Implemented: Iw[.m], Fw.d, Ew.d[Ee], Dw.d, ESw.d, Gw.d, Lw, A[w], nX, '/', quoted strings, repeat counts and nested
groups, ':' and format reversion. Rounding is that of the exact decimal expansion of the binary value, as gfortran's.
"""


def _fparse(fmt):
    """Format string -> list of ops: ('lit', text) ('x', n) ('nl',) ('colon',) ('d', letter, w, d, e) ('group', rep, ops)."""
    text = fmt.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    pos = 0

    def parse(stop_at_paren):
        nonlocal pos
        ops = []
        while pos < len(text):
            ch = text[pos]
            if ch in " ,":
                pos += 1
            elif ch == ")":
                if not stop_at_paren:
                    raise ValueError("unbalanced parenthesis in format")
                pos += 1
                return ops
            elif ch in "'\"":
                end = pos + 1
                chunk = ""
                while end < len(text):
                    if text[end] == ch:
                        if end + 1 < len(text) and text[end + 1] == ch:
                            chunk += ch
                            end += 2
                            continue
                        break
                    chunk += text[end]
                    end += 1
                ops.append(("lit", chunk))
                pos = end + 1
            elif ch == "/":
                ops.append(("nl",))
                pos += 1
            elif ch == ":":
                ops.append(("colon",))
                pos += 1
            else:
                m = re.match(r"(\d*)\s*(\(|[A-Za-z]+)", text[pos:])
                if not m:
                    raise ValueError("bad format near " + text[pos:pos + 10])
                rep = int(m.group(1)) if m.group(1) else None
                if m.group(2) == "(":
                    pos += m.end()
                    ops.append(("group", rep or 1, parse(True)))
                    continue
                letter = m.group(2).upper()
                pos += m.end()
                if letter == "X":
                    ops.append(("x", rep if rep is not None else 1))
                    continue
                if letter not in ("I", "F", "E", "D", "ES", "G", "L", "A", "EN"):
                    raise ValueError("unsupported edit descriptor " + letter)
                spec = re.match(r"(\d+)?(?:\.(\d+))?(?:E(\d+))?", text[pos:], re.IGNORECASE)
                width = int(spec.group(1)) if spec.group(1) else None
                digits = int(spec.group(2)) if spec.group(2) else None
                expw = int(spec.group(3)) if spec.group(3) else None
                pos += spec.end()
                ops.append(("d", letter, width, digits, expw, rep or 1))
        return ops

    return parse(False)


def _fstars(w):
    return "*" * w


def _fint(v, w, m):
    v = int(v)
    s = str(abs(v))
    if m is not None:
        s = "" if (m == 0 and v == 0) else s.rjust(m, "0")
    s = ("-" if v < 0 else "") + s
    if w is None or w == 0:
        return s
    return _fstars(w) if len(s) > w else s.rjust(w)


def _fspecial(v, w):
    """Text for NaN or an infinity, or None for a finite value."""
    if v != v:
        s = "NaN"
    elif v in (math.inf, -math.inf):
        s = ("-" if v < 0 else "") + ("Infinity" if (w or 8) >= (9 if v < 0 else 8) else "Inf")
    else:
        return None
    return s.rjust(w) if w and len(s) <= w else (_fstars(w) if w else s)


def _ffixed(v, w, d):
    v = float(v)
    special = _fspecial(v, w)
    if special is not None:
        return special
    s = f"{v:.{d}f}"
    if d == 0:
        s += "."                                    # F10.0 prints "2." : the decimal point is always written
    if w is None or w == 0:
        return s[1:] if s.startswith("0.") and d == 0 else (s[1:] if s.startswith("0.") else s.replace("-0.", "-.", 1))
    if len(s) > w:                                  # drop the optional leading zero before a fraction
        s = s[1:] if s.startswith("0.") else s.replace("-0.", "-.", 1)
    return _fstars(w) if len(s) > w else s.rjust(w)


def _fexp_parts(v, digits, scientific):
    """(sign, digit string, decimal exponent) of |v| rounded to `digits` significant digits."""
    if v == 0:
        return "-" if math.copysign(1.0, v) < 0 else "", "0" * digits, 0
    mant = f"{abs(v):.{digits - 1}e}"
    digs, exp = mant.split("e")
    digs = digs.replace(".", "")
    exp = int(exp)
    return ("-" if v < 0 else ""), digs, (exp if scientific else exp + 1)


def _fexpo(letter, exp, expw):
    sign = "-" if exp < 0 else "+"
    mag = abs(exp)
    if expw is None:
        if mag < 100:
            return f"{letter}{sign}{mag:02d}"
        return f"{sign}{mag:03d}"
    if len(str(mag)) > expw:
        return None
    return f"{letter}{sign}{str(mag).rjust(expw, '0')}"


def _fexp(v, w, d, expw, letter, scientific):
    v = float(v)
    special = _fspecial(v, w)
    if special is not None:
        return special
    if d is None:
        d = 0
    digits = d + 1 if scientific else d
    if digits == 0:
        digits = 1
    sign, digs, exp = _fexp_parts(v, max(digits, 1), scientific)
    tail = _fexpo(letter, exp, expw)
    if tail is None:
        return _fstars(w or 1)
    body = (digs[0] + "." + digs[1:]) if scientific else ("0." + digs if d > 0 else "0.")
    s = sign + body + tail
    if w and len(s) > w and not scientific and s[len(sign):].startswith("0."):
        s = sign + s[len(sign) + 1:]
    if w is None or w == 0:
        return s
    return _fstars(w) if len(s) > w else s.rjust(w)


def _fgen(v, w, d, expw):
    v = float(v)
    special = _fspecial(v, w)
    if special is not None:
        return special
    if d is None:
        d = 0
    a = abs(v)
    if v == 0:
        pad = 4 if expw is None else expw + 2
        return (_ffixed(0.0, (w or 0) - pad if w else None, max(d - 1, 0)) + " " * pad).rjust(w or 0)
    # decimal exponent of the value as it would print with d significant digits
    exp = int(f"{a:.{max(d, 1) - 1}e}".split("e")[1]) + 1
    if 0 <= exp <= d:
        pad = 4 if expw is None else expw + 2
        return (_ffixed(v, (w - pad) if w else None, d - exp) + " " * pad).rjust(w or 0)
    return _fexp(v, w, d, expw, "E", False)


def _fwrite(fmt, items):
    ops = _fparse(fmt)
    values = []
    for v in items:                                  # a complex value takes two real edit descriptors
        if isinstance(v, complex):
            values += [v.real, v.imag]
        elif hasattr(v, "real") and hasattr(v, "imag") and not isinstance(v, (int, float, bool)) and getattr(v, "dtype", None) is not None \
                and v.dtype.kind == "c":
            values += [float(v.real), float(v.imag)]
        else:
            values.append(v)
    state = {"i": 0, "cur": [], "lines": [], "pend": 0}

    def have_more():
        return state["i"] < len(values)

    def emit(text):
        if state["pend"]:                            # nX only moves the position: the blanks appear if something follows
            state["cur"].append(" " * state["pend"])
            state["pend"] = 0
        state["cur"].append(text)

    def newline():
        state["lines"].append("".join(state["cur"]))
        state["cur"] = []
        state["pend"] = 0

    class Stop(Exception):
        pass

    def run(seq):
        for op in seq:
            kind = op[0]
            if kind == "lit":
                emit(op[1])
            elif kind == "x":
                state["pend"] += op[1]
            elif kind == "nl":
                newline()
            elif kind == "colon":
                if not have_more():
                    raise Stop
            elif kind == "group":
                for _ in range(op[1]):
                    run(op[2])
            else:
                _, letter, w, d, e, rep = op
                for _ in range(rep):
                    if not have_more():
                        raise Stop
                    v = values[state["i"]]
                    state["i"] += 1
                    if letter == "I":
                        emit(_fint(v, w, d))
                    elif letter == "F":
                        emit(_ffixed(v, w, d if d is not None else 0))
                    elif letter in ("E", "D"):
                        emit(_fexp(v, w, d, e, letter, False))
                    elif letter == "ES":
                        emit(_fexp(v, w, d, e, "E", True))
                    elif letter == "G":
                        if isinstance(v, (int, bool)) and not isinstance(v, float):
                            emit(_fint(v, w, None))
                        else:
                            emit(_fgen(v, w, d, e))
                    elif letter == "L":
                        emit(("T" if v else "F").rjust(w or 1))
                    elif letter == "A":
                        s = str(v)
                        emit(s if not w else (s[:w] if len(s) >= w else s.rjust(w)))
                    else:
                        raise ValueError("unsupported edit descriptor " + letter)

    tail_group = None
    for k in range(len(ops) - 1, -1, -1):
        if ops[k][0] == "group":
            tail_group = k
            break
    try:
        run(ops)
        # format reversion: more items than descriptors start a new record at the last top-level group
        while have_more():
            consumed = state["i"]
            newline()
            run(ops[tail_group:] if tail_group is not None else ops)
            if state["i"] == consumed:
                break
    except Stop:
        pass
    state["lines"].append("".join(state["cur"]))
    return "\n".join(state["lines"]) + "\n"
