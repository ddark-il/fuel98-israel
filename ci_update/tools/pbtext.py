#!/usr/bin/env python3
"""Minimal protobuf TextFormat parser.

Waze's rt distributor answers with protobuf TextFormat, not binary:

    element {
      response_timestamp {
        timestamp: 1790916608347
        server_hostname: "realtime-frontend-prod-row-v189-xgx4.waze"
      }
    }

This parses that into nested dicts: every field name maps to a LIST of values
(TextFormat repeats fields rather than using arrays), values are int / float /
bool / str / dict.
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r'''
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<str>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')
  | (?P<num>[-+]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)u?l?l?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_.+/\[\]-]*)
  | (?P<open>[{<])
  | (?P<close>[}>])
  | (?P<colon>:)
  | (?P<sep>[,;])
''', re.VERBOSE)


class TextFormatError(ValueError):
    pass


def _tokenize(text: str):
    i, out = 0, []
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise TextFormatError(f'unexpected char {text[i]!r} at {i}')
        i = m.end()
        kind = m.lastgroup
        if kind in ('ws', 'comment', 'sep'):
            continue
        out.append((kind, m.group()))
    return out


def _unescape(lit: str) -> str:
    """Decode a protobuf TextFormat string literal.

    TextFormat escapes non-ASCII as octal bytes (\327\223 ...), so this builds bytes
    and decodes UTF-8 at the end - otherwise Hebrew venue names come out as mojibake.
    """
    s = lit[1:-1]
    buf = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            n = s[i + 1]
            simple = {'n': 0x0A, 't': 0x09, 'r': 0x0D, '"': 0x22, "'": 0x27, '\\': 0x5C}
            if n in simple:
                buf.append(simple[n]); i += 2; continue
            if n in '01234567':
                j = i + 1
                while j < len(s) and j < i + 4 and s[j] in '01234567':
                    j += 1
                buf.append(int(s[i + 1:j], 8) & 0xFF); i = j; continue
            if n == 'x':
                j = i + 2
                while j < len(s) and j < i + 4 and s[j] in '0123456789abcdefABCDEF':
                    j += 1
                buf.append(int(s[i + 2:j], 16) & 0xFF); i = j; continue
            buf.extend(n.encode('utf-8')); i += 2; continue
        buf.extend(c.encode('utf-8')); i += 1
    try:
        return buf.decode('utf-8')
    except UnicodeDecodeError:
        return buf.decode('utf-8', 'replace')


def _value(tok):
    kind, text = tok
    if kind == 'str':
        return _unescape(text)
    if kind == 'num':
        t = text.rstrip('ull').rstrip('UL')
        if t.lower().startswith(('0x', '-0x', '+0x')):
            return int(t, 16)
        if any(ch in t for ch in '.eE'):
            return float(t)
        return int(t)
    if kind == 'name':
        if text == 'true':
            return True
        if text == 'false':
            return False
        return text              # enum name, NaN, inf, ...
    raise TextFormatError(f'unexpected token {tok!r}')


def _parse(tokens, i, stop):
    out: dict[str, list] = {}
    while i < len(tokens):
        kind, text = tokens[i]
        if kind == 'close':
            if stop is None:
                raise TextFormatError('unbalanced }')
            return out, i + 1
        if kind != 'name':
            raise TextFormatError(f'expected field name, got {tokens[i]!r}')
        name = text
        i += 1
        if i < len(tokens) and tokens[i][0] == 'colon':
            i += 1
            if i >= len(tokens):
                raise TextFormatError(f'field {name!r} has no value')
            if tokens[i][0] == 'open':
                sub, i = _parse(tokens, i + 1, tokens[i][1])
                out.setdefault(name, []).append(sub)
            else:
                out.setdefault(name, []).append(_value(tokens[i]))
                i += 1
        elif i < len(tokens) and tokens[i][0] == 'open':
            sub, i = _parse(tokens, i + 1, tokens[i][1])
            out.setdefault(name, []).append(sub)
        else:
            raise TextFormatError(f'field {name!r} needs ":" or a block')
    if stop is not None:
        raise TextFormatError('unterminated block')
    return out, i


def parse(text: str) -> dict:
    """Parse a whole TextFormat document."""
    if not text.strip():
        return {}
    if text.lstrip().startswith('ProtoBase64,'):
        # request-side wrapper, not a response
        return {'ProtoBase64': [text.split(',', 1)[1]]}
    fields, _ = _parse(_tokenize(text), 0, None)
    return fields


def one(node, path: str, default=None):
    """Follow a dotted path through single-valued fields."""
    cur = node
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        vals = cur[part]
        cur = vals[0] if isinstance(vals, list) else vals
    return cur


def all_of(node, path: str) -> list:
    """Follow a dotted path, flattening the last step's list."""
    cur = node
    parts = path.split('.')
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return []
        cur = cur[part][0]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return []
    return cur[parts[-1]]


if __name__ == '__main__':
    import sys, json
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False))
