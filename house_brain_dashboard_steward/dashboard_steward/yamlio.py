"""Strict Lovelace YAML subset: a parser that refuses anything ambiguous and a canonical emitter.

Why our own: House Brain Apps ship with zero third-party Python packages (no PyYAML), and the YAML that
Dashboard Steward handles comes from two untrusted places, a model and a live dashboard. A strict subset is
a safety feature: anything Home Assistant's YAML 1.1 loader could read differently from a human (``on``,
``yes``, ``10:30``, ``2026-11-08``, ``0123``) or that carries hidden structure (tags, anchors, aliases, merge
keys, several documents) is refused with a reason code instead of being guessed at. Nothing is silently
stripped to make parsing pass.

Supported: block mappings and sequences, ``- key: value`` items, single/double quoted scalars on one line,
literal/folded block scalars with clip (``|``, ``>``) or strip (``|-``, ``>-``) chomping, the empty
collections ``[]`` / ``{}``, one-line flow sequences of scalars, ``#`` comments, one optional leading ``---``.

``emit`` writes a canonical form that this parser and a YAML 1.1 safe loader both read back to the same
value (tested differentially against PyYAML in the test suite, which is a test-only oracle).
"""
from __future__ import annotations

import math
import re

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64


class YamlRefused(ValueError):
    """The text is outside the strict subset. ``code`` is a stable reason code, ``line`` is 1-based."""

    def __init__(self, code: str, line: int = 0, detail: str = ""):
        self.code, self.line, self.detail = code, line, detail
        super().__init__(f"{code} at line {line}: {detail}" if line else f"{code}: {detail}")


# ---------------------------------------------------------------- scalar resolution (YAML 1.1 aware)
_NULL = {"", "~", "null", "Null", "NULL"}
_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}
# YAML 1.1 (Home Assistant's loader) reads these as booleans; a human usually means a word. Must be quoted.
_AMBIGUOUS_BOOL = {"yes", "Yes", "YES", "no", "No", "NO", "on", "On", "ON", "off", "Off", "OFF",
                   "y", "Y", "n", "N"}
_RE_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)\Z")
_RE_FLOAT = re.compile(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+][0-9]+)?\Z")
# Things a YAML 1.1 loader turns into a non-string that a reader would not expect: octal/hex/binary ints,
# underscores, sexagesimal times, timestamps, special floats, exponents without a dot or exponent sign.
_RE_AMBIGUOUS = [
    re.compile(r"[-+]?0[0-9_]+\Z"), re.compile(r"[-+]?0[xXoObB][0-9a-fA-F_]+\Z"),
    re.compile(r"[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?\Z"),  # underscores / plain digits handled above first
    re.compile(r"[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?\Z"),
    re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"),
    re.compile(r"[-+]?\.(?:inf|Inf|INF|nan|NaN|NAN)\Z"),
    re.compile(r"[-+]?[0-9.]+[eE][-+]?[0-9]+\Z"),
    re.compile(r"=\Z"), re.compile(r"<<\Z"),
]


def resolve_plain(text: str, line: int = 0):
    """Resolve a plain (unquoted) scalar exactly as both this parser and a YAML 1.1 safe loader would."""
    if text in _NULL:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if text in _AMBIGUOUS_BOOL:
        raise YamlRefused("YAML_AMBIGUOUS_BOOLEAN", line, f"quote {text!r}")
    if _RE_INT.match(text):
        return int(text)
    if _RE_FLOAT.match(text):
        return float(text)
    for rx in _RE_AMBIGUOUS:
        if rx.match(text):
            raise YamlRefused("YAML_AMBIGUOUS_SCALAR", line, f"quote {text[:40]!r}")
    if text[0] in "!&*":
        raise YamlRefused({"!": "YAML_TAG_REFUSED", "&": "YAML_ANCHOR_REFUSED", "*": "YAML_ALIAS_REFUSED"}[text[0]],
                          line, text[:40])
    if text[0] in "[]{}>|%@`\"'," or text.startswith(("- ", "? ", ": ")) or text in ("-", "?", ":"):
        raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", line, text[:40])
    if ": " in text or text.endswith(":") or " #" in text or "\t" in text:
        raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", line, text[:40])
    return text


# ---------------------------------------------------------------- quoted scalars
_ESC = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
        "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ",
        "P": " "}
_HEXLEN = {"x": 2, "u": 4, "U": 8}


def _double_quoted(s: str, i: int, line: int) -> tuple[str, int]:
    """Parse a double-quoted scalar starting at s[i] == '"'. Returns (value, index after closing quote)."""
    out = []
    i += 1
    while i < len(s):
        c = s[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            i += 1
            if i >= len(s):
                break
            e = s[i]
            if e in _ESC:
                out.append(_ESC[e])
                i += 1
            elif e in _HEXLEN:
                n = _HEXLEN[e]
                h = s[i + 1:i + 1 + n]
                if len(h) != n or not re.fullmatch(r"[0-9a-fA-F]+", h):
                    raise YamlRefused("YAML_BAD_ESCAPE", line, h)
                cp = int(h, 16)
                if 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
                    raise YamlRefused("YAML_BAD_ESCAPE", line, h)
                out.append(chr(cp))
                i += 1 + n
            else:
                raise YamlRefused("YAML_BAD_ESCAPE", line, e)
        else:
            out.append(c)
            i += 1
    raise YamlRefused("YAML_MULTILINE_QUOTED_REFUSED", line, "unterminated or multi-line quoted scalar")


def _single_quoted(s: str, i: int, line: int) -> tuple[str, int]:
    out = []
    i += 1
    while i < len(s):
        c = s[i]
        if c == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise YamlRefused("YAML_MULTILINE_QUOTED_REFUSED", line, "unterminated or multi-line quoted scalar")


def _strip_comment(rest: str, line: int) -> str:
    """Strip a trailing comment from text that does not start with a quote. '#' must follow whitespace."""
    m = re.search(r"(^|\s)#", rest)
    return (rest[:m.start()] if m else rest).rstrip()


def _inline_value(rest: str, line: int):
    """Parse an inline (same-line) value that is not a block scalar indicator."""
    if not rest:
        return None
    c = rest[0]
    if c in "\"'":
        val, j = (_double_quoted if c == '"' else _single_quoted)(rest, 0, line)
        tail = rest[j:]
        if tail.strip() and not re.match(r"\s+#", tail):
            raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", line, "text after a quoted scalar")
        return val
    if c in "!&*":
        code = {"!": "YAML_TAG_REFUSED", "&": "YAML_ANCHOR_REFUSED", "*": "YAML_ALIAS_REFUSED"}[c]
        raise YamlRefused(code, line, rest[:40])
    text = _strip_comment(rest, line)
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if text.startswith("["):
        return _flow_sequence(text, line)
    if text.startswith("{"):
        raise YamlRefused("YAML_FLOW_MAPPING_REFUSED", line, text[:40])
    return resolve_plain(text, line)


def _flow_sequence(text: str, line: int) -> list:
    if not text.endswith("]"):
        raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", line, "flow sequence must close on its line")
    body = text[1:-1]
    items, i = [], 0
    while True:
        while i < len(body) and body[i] == " ":
            i += 1
        if i >= len(body):
            break
        c = body[i]
        if c in "\"'":
            val, i = (_double_quoted if c == '"' else _single_quoted)(body, i, line)
        elif c in "[]{}!&*":
            raise YamlRefused("YAML_NESTED_FLOW_REFUSED", line, text[:40])
        else:
            j = body.find(",", i)
            j = len(body) if j < 0 else j
            val = resolve_plain(body[i:j].strip(), line)
            i = j
        items.append(val)
        while i < len(body) and body[i] == " ":
            i += 1
        if i < len(body):
            if body[i] != ",":
                raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", line, text[:40])
            i += 1
    return items


# ---------------------------------------------------------------- block parser
class _Lines:
    def __init__(self, text: str):
        self.raw = text.split("\n")
        self.i = 0

    def skip_blank(self):
        while self.i < len(self.raw):
            s = self.raw[self.i]
            st = s.strip()
            if st and not st.startswith("#"):
                return
            self.i += 1

    def peek(self):
        """(indent, content, line_no) of the next meaningful line, or None."""
        self.skip_blank()
        if self.i >= len(self.raw):
            return None
        s = self.raw[self.i]
        body = s.lstrip(" ")
        if body.startswith("\t") or ("\t" in s[:len(s) - len(body)]):
            raise YamlRefused("YAML_TAB_INDENT", self.i + 1)
        return len(s) - len(body), body.rstrip(), self.i + 1


_RE_PLAIN_KEY = re.compile(r"([^\s#'\"\[\]{}!&*?|>%@`,-][^:#]*?|-[^\s:#][^:#]*?)\s*:(?:\s|$)")


def _split_key(body: str, line: int):
    """Return (key, rest) when ``body`` is a mapping entry, else None."""
    if body[0] in "\"'":
        key, j = (_double_quoted if body[0] == '"' else _single_quoted)(body, 0, line)
        m = re.match(r"\s*:(?:\s|$)", body[j:])
        if not m:
            return None
        return key, body[j + m.end():].strip()
    if body.startswith("? "):
        raise YamlRefused("YAML_COMPLEX_KEY_REFUSED", line)
    m = _RE_PLAIN_KEY.match(body)
    if not m:
        return None
    key = m.group(1).rstrip()
    if key == "<<":
        raise YamlRefused("YAML_MERGE_KEY_REFUSED", line)
    if key[0] in "!&*":
        raise YamlRefused("YAML_TAG_REFUSED" if key[0] == "!" else "YAML_ANCHOR_REFUSED", line, key)
    return key, body[m.end():].strip()


class _Parser:
    def __init__(self, text: str):
        self.L = _Lines(text)
        self.depth = 0

    def node(self, indent: int, override=None):
        """Parse the node whose first line is at column ``indent`` (override = (col, text, line) for '- ' items)."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise YamlRefused("YAML_TOO_DEEP", 0)
        try:
            first = override or self.L.peek()
            col, body, ln = first
            if body == "-" or body.startswith("- "):
                return self.sequence(col, override)
            if _split_key(body, ln) is not None:
                return self.mapping(col, override)
            if override is None:
                self.L.i += 1
            if body.startswith(("|", ">")):
                raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", ln, "block scalar without a key")
            value = _inline_value(body, ln)
            nxt = self.L.peek()
            if nxt is not None and nxt[0] > col and override is None:
                raise YamlRefused("YAML_MULTILINE_PLAIN_REFUSED", nxt[2])
            return value
        finally:
            self.depth -= 1

    def sequence(self, col: int, override=None):
        out = []
        cur = override
        while True:
            if cur is None:
                p = self.L.peek()
                if p is None or p[0] != col or not (p[1] == "-" or p[1].startswith("- ")):
                    if p is not None and p[0] > col:
                        raise YamlRefused("YAML_BAD_INDENT", p[2])
                    return out
                cur = p
            _, body, ln = cur
            self.L.i = ln  # consume the line
            rest = body[1:].lstrip(" ")
            if not rest or rest.startswith("#"):
                p = self.L.peek()
                out.append(self.node(p[0]) if p is not None and p[0] > col else None)
            else:
                inner_col = col + (len(body) - len(rest))
                if rest.startswith(("|", ">")):
                    out.append(self.block_scalar(rest, col, ln))
                elif rest == "-" or rest.startswith("- ") or _split_key(rest, ln) is not None:
                    out.append(self.node(inner_col, (inner_col, rest, ln)))
                else:
                    out.append(_inline_value(rest, ln))
                    p = self.L.peek()
                    if p is not None and p[0] > col:
                        raise YamlRefused("YAML_MULTILINE_PLAIN_REFUSED", p[2])
            cur = None

    def mapping(self, col: int, override=None):
        out: dict = {}
        cur = override
        while True:
            if cur is None:
                p = self.L.peek()
                if p is None or p[0] != col:
                    if p is not None and p[0] > col:
                        raise YamlRefused("YAML_BAD_INDENT", p[2])
                    return out
                if p[1] == "-" or p[1].startswith("- "):
                    raise YamlRefused("YAML_BAD_INDENT", p[2], "sequence item inside a mapping")
                cur = p
            _, body, ln = cur
            kv = _split_key(body, ln)
            if kv is None:
                raise YamlRefused("YAML_UNSUPPORTED_SYNTAX", ln, body[:40])
            key, rest = kv
            if key in out:
                raise YamlRefused("YAML_DUPLICATE_KEY", ln, key)
            self.L.i = ln
            if rest.startswith(("|", ">")):
                out[key] = self.block_scalar(rest, col, ln)
            elif rest and not rest.startswith("#"):
                out[key] = _inline_value(rest, ln)
                p = self.L.peek()
                if p is not None and p[0] > col:
                    raise YamlRefused("YAML_MULTILINE_PLAIN_REFUSED", p[2])
            else:
                p = self.L.peek()
                if p is not None and p[0] > col:
                    out[key] = self.node(p[0])
                elif p is not None and p[0] == col and (p[1] == "-" or p[1].startswith("- ")):
                    out[key] = self.sequence(col)
                else:
                    out[key] = None
            cur = None

    def block_scalar(self, header: str, parent_col: int, ln: int) -> str:
        h = _strip_comment(header, ln)
        if h not in ("|", "|-", ">", ">-"):
            raise YamlRefused("YAML_BLOCK_HEADER_REFUSED", ln, h)
        literal, strip = h[0] == "|", h.endswith("-")
        raw = self.L.raw
        i = self.L.i
        lines = []
        ind = None
        while i < len(raw):
            s = raw[i]
            if not s.strip():
                lines.append("")
                i += 1
                continue
            n = len(s) - len(s.lstrip(" "))
            if s[n:n + 1] == "\t" and n <= parent_col:
                raise YamlRefused("YAML_TAB_INDENT", i + 1)
            if ind is None:
                if n <= parent_col:
                    break
                ind = n
            if n < ind:
                if n > parent_col:
                    raise YamlRefused("YAML_BAD_INDENT", i + 1, "block scalar line less indented than its first")
                break
            lines.append(s[ind:])
            i += 1
        # trailing blank lines belong to chomping (clip/strip both drop them), not to the following node
        while lines and lines[-1] == "":
            lines.pop()
        self.L.i = i
        if not lines:
            return ""
        if literal:
            text = "\n".join(lines)
        else:
            # YAML folding: a break between two "text" lines becomes a space (or, with b blank lines between
            # them, b newlines); breaks next to a more-indented ("spaced") line are all kept (b + 1 newlines).
            # Leading blank lines are kept as newlines.
            text, prev, blanks = "", None, 0
            for l in lines:
                if l == "":
                    blanks += 1
                    continue
                spaced = l.startswith((" ", "\t"))
                if prev is None:
                    text = "\n" * blanks + l
                elif not spaced and not prev:
                    text += (" " if blanks == 0 else "\n" * blanks) + l
                else:
                    text += "\n" * (blanks + 1) + l
                prev, blanks = spaced, 0
        return text if strip else text + "\n"


def loads(text: str):
    """Parse the strict subset. Raises YamlRefused with a reason code on anything outside it."""
    if isinstance(text, bytes):
        if len(text) > MAX_INPUT_BYTES:
            raise YamlRefused("YAML_TOO_LARGE")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise YamlRefused("YAML_NOT_UTF8") from None
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise YamlRefused("YAML_TOO_LARGE")
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if "\r" in text:
        raise YamlRefused("YAML_BARE_CR")
    for k, s in enumerate(text.split("\n"), 1):
        if s.startswith("%"):
            raise YamlRefused("YAML_DIRECTIVE_REFUSED", k)
        if re.match(r"(---|\.\.\.)(\s|$)", s) and not (k == 1 and s.strip() == "---"):
            raise YamlRefused("YAML_MULTI_DOCUMENT_REFUSED", k)
    if text.startswith("---"):
        text = "\n" + text.split("\n", 1)[1] if "\n" in text else ""
    p = _Parser(text)
    first = p.L.peek()
    if first is None:
        return None
    value = p.node(first[0])
    rest = p.L.peek()
    if rest is not None:
        raise YamlRefused("YAML_TRAILING_CONTENT", rest[2])
    return value


# ---------------------------------------------------------------- canonical emitter
_RE_SAFE_PLAIN = re.compile(r"[A-Za-z_/$][A-Za-z0-9_ ./()+=$-]*\Z")


def _plain_ok(s: str) -> bool:
    if not s or s != s.strip() or not _RE_SAFE_PLAIN.match(s) or s.endswith(":"):
        return False
    try:
        return resolve_plain(s) == s
    except YamlRefused:
        return False


def _quote(s: str) -> str:
    out = ['"']
    for ch in s:
        cp = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif cp < 0x20 or cp == 0x7F or 0x80 <= cp <= 0x9F or cp in (0x2028, 0x2029, 0xFEFF):
            out.append(f"\\u{cp:04x}")
        elif 0xD800 <= cp <= 0xDFFF:
            raise ValueError("lone surrogate cannot be emitted")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _block_ok(s: str) -> bool:
    """A literal block keeps the text exactly; only use it when that is guaranteed."""
    if "\n" not in s or s.endswith("\n\n") or s.startswith((" ", "\n")) or "\r" in s:
        return False
    body = s[:-1] if s.endswith("\n") else s
    for l in body.split("\n"):
        if l != l.rstrip(" \t") or "\t" in l:
            return False
        if any(0 <= ord(c) < 0x20 or c in "\x7f  ﻿\x85" or 0x80 <= ord(c) <= 0x9f for c in l):
            return False
    return True


def _scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite float")
        r = repr(v)
        if "e" in r:
            mant, exp = r.split("e")
            if "." not in mant:
                mant += ".0"
            if exp[0] not in "+-":
                exp = "+" + exp
            r = f"{mant}e{exp}"
        return r
    if isinstance(v, str):
        return v if _plain_ok(v) else _quote(v)
    raise TypeError(f"cannot emit {type(v).__name__}")


def _key(k) -> str:
    if not isinstance(k, str):
        raise TypeError("mapping keys must be strings")
    return k if _plain_ok(k) and k != "-" and not k.startswith("- ") else _quote(k)


def _emit(v, ind: int, out: list, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ValueError("too deep")
    pad = " " * ind
    if isinstance(v, dict):
        for k, x in v.items():
            _emit_entry(_key(k) + ":", x, ind, out, depth)
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, dict) and x:
                first = True
                for k, y in x.items():
                    prefix = (pad + "- ") if first else (pad + "  ")
                    _emit_entry(_key(k) + ":", y, ind + 2, out, depth + 1, prefix=prefix)
                    first = False
            elif isinstance(x, list) and x:
                out.append(pad + "-")
                _emit(x, ind + 2, out, depth + 1)
            else:
                _emit_entry("-", x, ind, out, depth)
    else:
        out.append(pad + _scalar(v))


def _emit_entry(head: str, x, ind: int, out: list, depth: int, prefix: str | None = None):
    lead = (prefix if prefix is not None else " " * ind) + head
    if isinstance(x, dict) and x:
        out.append(lead)
        _emit(x, ind + 2, out, depth + 1)
    elif isinstance(x, list) and x:
        out.append(lead)
        _emit(x, ind + 2 if head != "-" else ind + 2, out, depth + 1)
    elif isinstance(x, dict):
        out.append(lead + " {}")
    elif isinstance(x, list):
        out.append(lead + " []")
    elif isinstance(x, str) and _block_ok(x):
        out.append(lead + (" |" if x.endswith("\n") else " |-"))
        body = x[:-1] if x.endswith("\n") else x
        for l in body.split("\n"):
            out.append((" " * (ind + 2) + l) if l else "")
    else:
        out.append(lead + " " + _scalar(x))


def dumps(value) -> str:
    """Canonical YAML for a JSON-like value (dict/list/str/int/float/bool/None)."""
    if not isinstance(value, (dict, list)) or not value:
        return _scalar(value) + "\n" if not isinstance(value, (dict, list)) else ("{}\n" if isinstance(value, dict) else "[]\n")
    out: list[str] = []
    _emit(value, 0, out)
    return "\n".join(out) + "\n"
