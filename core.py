"""
core.py — Shared parsing, timestamp, and level logic for docktail.

Imported by both docktail.py (terminal viewer) and server.py (web UI).
Contains no terminal-specific code (no ANSI rendering, no Docker subprocess
calls, no threading I/O) so it can be imported safely in any context.
"""

import re
import time
import threading
from datetime import datetime

# ── ANSI strip ────────────────────────────────────────────────────────────────
# Needed here because parse_line() strips ANSI codes from raw log lines before
# applying format patterns.  Kept in core so tests can import it directly.

_ANSI_RE = re.compile(r'\x1b(?:\[[0-9;]*[a-zA-Z]|[()][0-9A-Za-z])')


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


# ── Level normalisation ───────────────────────────────────────────────────────

# Maps every level variant to one of the four canonical buckets.
# Used for filtering, counting, and ordering in both the terminal and web UIs.
LEVEL_RANK: dict[str, int] = {
    "ERROR": 0, "WARN": 1, "WARNING": 1, "INFO": 2, "DEBUG": 3, "TRACE": 3,
}


def _norm(lvl: str) -> str:
    """Normalise any level variant to one of ERROR / WARN / INFO / DEBUG."""
    return {
        'WARNING':  'WARN',  'WRN':     'WARN',
        'ERR':      'ERROR', 'CRITICAL': 'ERROR',
        'INF':      'INFO',
        'DBG':      'DEBUG', 'TRACE':   'DEBUG',
    }.get(lvl.upper(), lvl.upper())


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _fmt_ts(ts: str) -> str:
    """Simple HH:MM:SS.mmm — used for the ParsedLine.timestamp display field."""
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).strftime('%H:%M:%S.%f')[:-3]
    except Exception:
        pass
    try:
        return datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').strftime('%H:%M:%S')
    except Exception:
        return ts[11:23] if len(ts) > 11 else ts


def _ts_epoch(ts: str) -> float:
    """Parse an ISO or Python-logging timestamp to a Unix epoch float.

    Used as the merge-sort key so lines from concurrent containers are
    interleaved in correct chronological order.
    """
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
    except Exception:
        pass
    try:
        return datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').timestamp()
    except Exception:
        return time.time()


def _smart_ts(epoch: float) -> str:
    """Format a Unix epoch as a human-readable string relative to today (local time).

    Same calendar day  →  14:23:45.123
    Yesterday          →  yesterday 14:23:45
    Within 6 days      →  Mon 14:23:45
    Same year          →  May 28 14:23:45
    Different year     →  2025-05-28 14:23:45
    """
    try:
        now   = datetime.now().astimezone()
        dt    = datetime.fromtimestamp(epoch).astimezone()
        today = now.date()
        d     = dt.date()
        diff  = (today - d).days
        t     = dt.strftime('%H:%M:%S')
        if diff == 0:
            return f"{t}.{dt.microsecond // 1000:03d}"
        elif diff == 1:
            return f"yesterday {t}"
        elif diff < 7:
            return dt.strftime(f'%a {t}')
        elif d.year == today.year:
            return dt.strftime(f'%b %d {t}')
        else:
            return dt.strftime(f'%Y-%m-%d {t}')
    except Exception:
        return '?'


# ── Container colour index ────────────────────────────────────────────────────
# Assigns a stable 0-based integer index to each container name.
# docktail.py maps indices to ANSI colour codes; server.py maps them to CSS
# colours.  Because both use this function, a given container always gets the
# same colour slot in both the terminal and web UIs.

_container_index_map: dict[str, int] = {}
_container_index_lock = threading.Lock()


def assign_container_index(name: str) -> int:
    """Return a stable 0-based colour index for a container name.

    Indices are assigned in first-seen order and never change during a session.
    Both docktail.py and server.py call this so terminal and web colours stay
    in sync.
    """
    with _container_index_lock:
        if name not in _container_index_map:
            _container_index_map[name] = len(_container_index_map)
        return _container_index_map[name]


# ── ParsedLine ────────────────────────────────────────────────────────────────

class ParsedLine:
    """A single log line with all extracted fields."""
    __slots__ = ('raw', 'service', 'timestamp', 'ts_epoch', 'arrival_mono',
                 'level', 'target', 'message')

    def __init__(self, raw: str = '', service: str = '', timestamp: str = '',
                 ts_epoch: float = 0.0, arrival_mono: float = 0.0,
                 level: str = 'INFO', target: str = '', message: str = ''):
        self.raw          = raw
        self.service      = service
        self.timestamp    = timestamp
        self.ts_epoch     = ts_epoch
        self.arrival_mono = arrival_mono
        self.level        = level
        self.target       = target
        self.message      = message


# ── Log parser ────────────────────────────────────────────────────────────────
#
# Strategy: always strip the docker timestamp prefix first (Step 1), then try
# a series of format patterns on the remainder (Step 2).  The fallback scans
# for a level keyword in the remainder — never in the full raw line — so the
# docker timestamp never bleeds into the message column.
#
# Supported formats (tried in order on the remainder after docker-ts is stripped):
#   A  Rust/tracing    ts2  LEVEL  target: message  kv=val
#   B  NestJS          [Nest] pid - date time  LEVEL  [Context]  message
#   C  bracket         [LEVEL] message  or  [LEVEL]: message
#   C2 bracket+date    [YYYY-MM-DD HH:MM:SS] [LEVEL] message
#   D  Python logging  YYYY-MM-DD HH:MM:SS[,ms] … LEVEL … message
#   E  level-first     LEVEL message  (e.g. short codes: INF, WRN, ERR)
#   F  logfmt          level=warning msg="..."  (Go: logrus, zap, zerolog)
#   G  bare-scan       level keyword anywhere (last resort)

_LEVEL_PAT = r'(?P<level>ERROR|ERR|CRITICAL|WARN(?:ING)?|WRN|INFO|INF|DEBUG|DBG|TRACE)'

# Step 1: strip outer container prefix + docker timestamp.
# service uses [^|:\s]+? (excludes colon and pipe) so it never accidentally
# matches an ISO timestamp — docker container names never contain colons.
# rest uses \s*(.*)$ to handle blank lines that are just a timestamp.
_COMPOSE_PRE = re.compile(
    r'^(?P<service>[^|:\s]+?)\s+\|\s+(?P<ts1>\S+)\s*(?P<rest>.*)$', re.DOTALL)
_DOCKER_PRE  = re.compile(
    r'^(?P<ts1>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*)\s*(?P<rest>.*)$', re.DOTALL)

# Step 2: format patterns applied to the remainder after the docker-ts is stripped.
_P_RUST = re.compile(
    r'^(?P<ts2>\d{4}-\d{2}-\d{2}T[^\s]+)\s+' + _LEVEL_PAT + r'\s+'
    r'(?P<target>[^\s:]+(?:::[^\s:]+)*):\s*(?P<message>.+)$', re.DOTALL)

_P_NEST = re.compile(
    r'^\[Nest\]\s+\d+\s+-\s+.{5,35}?\s+' + _LEVEL_PAT + r'\s+'
    r'\[(?P<target>[^\]]+)\]\s+(?P<message>.+)$', re.DOTALL)

_P_BRACKET = re.compile(
    r'^\[' + _LEVEL_PAT + r'\]\s*:?\s*(?P<message>.+)$', re.DOTALL)

_P_BRACKET_TS = re.compile(
    r'^\[\d{4}-\d{2}-\d{2}[^\]]+\]\s+\[' + _LEVEL_PAT + r'\]\s*(?P<message>.+)$',
    re.DOTALL)

_P_PYTHON = re.compile(
    r'^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)'
    r'.{0,60}?\b' + _LEVEL_PAT + r'\b'
    r'[\s:*]+(?P<message>.+)$', re.DOTALL)

_P_LEVEL_FIRST = re.compile(
    r'^' + _LEVEL_PAT + r'\s+(?P<message>.+)$', re.DOTALL)

_P_LOGFMT = re.compile(
    r'\blevel=(?P<level>\w+)\b.*?\bmsg=(?P<message>"[^"]*"|\S+)', re.IGNORECASE)

_BARE_LEVEL = re.compile(r'\b' + _LEVEL_PAT + r'\b', re.IGNORECASE)

# Matches key=value pairs; also strips the tracing-subscriber "2m" ANSI remnant.
_KV = re.compile(r'\b(\w+?)(?:2m)?=("(?:[^"\\]|\\.)*"|\S+)')


def _make(raw: str, service: str, ts1: str, ts_ep: float,
          level: str, target: str, message: str) -> ParsedLine:
    return ParsedLine(raw=raw, service=service,
                      timestamp=_fmt_ts(ts1), ts_epoch=ts_ep,
                      level=_norm(level), target=target, message=message)


def parse_line(raw: str, default_service: str = '') -> ParsedLine:
    """Parse a raw docker log line into a ParsedLine.

    Works with both 'docker logs --timestamps' output and
    'docker compose logs -f' output (which adds a 'service | ' prefix).
    Never raises — returns a best-effort ParsedLine for any input.
    """
    raw   = raw.rstrip('\n')
    clean = strip_ansi(raw)

    # Step 1: strip outer prefix to get (service, ts1, remainder)
    service = default_service
    ts1     = ''
    ts_ep   = time.time()
    rest    = clean

    m = _COMPOSE_PRE.match(clean)
    if m:
        service = m.group('service')
        ts1     = m.group('ts1')
        ts_ep   = _ts_epoch(ts1)
        rest    = m.group('rest')
    else:
        m = _DOCKER_PRE.match(clean)
        if m:
            ts1   = m.group('ts1')
            ts_ep = _ts_epoch(ts1)
            rest  = m.group('rest')

    # Step 2: try format patterns on remainder
    m = _P_RUST.match(rest)
    if m:
        return _make(raw, service, m.group('ts2'), _ts_epoch(m.group('ts2')),
                     m.group('level'), m.group('target'), m.group('message'))

    m = _P_NEST.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), m.group('target'), m.group('message'))

    m = _P_BRACKET.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    m = _P_BRACKET_TS.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    m = _P_PYTHON.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    m = _P_LEVEL_FIRST.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    m = _P_LOGFMT.search(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message').strip('"'))

    # G: bare scan — level anywhere; use remainder (not full raw) as message
    m = _BARE_LEVEL.search(rest)
    return _make(raw, service, ts1, ts_ep,
                 m.group('level') if m else 'INFO', '', rest)
