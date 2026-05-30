#!/usr/bin/env python3
"""
docktail.py — Docker log viewer with level filtering and live counters.

NO COMPOSE FILE NEEDED.  The script talks to the Docker daemon directly via
'docker ps' and 'docker logs', so it works with every container regardless of
how it was started — compose stack, Unraid WebUI, plain 'docker run', etc.

Usage:
    python3 docktail.py                      # follow ALL running containers
    python3 docktail.py <name> [<name> ...]  # specific container(s)
    python3 docktail.py --project mystack    # all containers in a compose project
    docker compose logs -f    | python3 docktail.py --stdin
    docker-compose logs -f    | python3 docktail.py --stdin

Options:
    --tail N        Lines of history per container on start (default: 50)
    --show LEVELS   Comma-separated levels to display, e.g. WARN,ERROR
                    Default: ERROR,WARN,INFO   (all levels are always counted)
    --project NAME  Filter to containers belonging to a specific compose project
                    (matches the com.docker.compose.project label)
    --skip NAME...  Exclude specific containers when watching all (no names given).
                    Example: --skip traefik crowdsec watchtower

Keyboard (live):
    e / w / i / d   Toggle ERROR / WARN / INFO / DEBUG display
    p               Pause / resume     c  Clear counters     q / Ctrl+C  Quit
"""

import sys, os, re, threading, subprocess, time, signal, argparse, atexit, heapq, shutil
from collections import deque
from datetime import datetime, timezone

# ── Docker binary detection ───────────────────────────────────────────────────

def _find_docker() -> list:
    """Return ['docker'] or exit with a helpful message."""
    if shutil.which('docker'):
        return ['docker']
    sys.exit(
        "Error: 'docker' not found in PATH.\n"
        "Make sure Docker is installed and the 'docker' binary is on your PATH."
    )

def _find_compose() -> list | None:
    """Return the compose command as a list, or None if neither variant is found.

    Tries 'docker compose' (v2 plugin) first, then 'docker-compose' (v1 standalone).
    Used only for informational output — the script itself never calls compose directly.
    """
    try:
        r = subprocess.run(['docker', 'compose', 'version'],
                           capture_output=True, timeout=3)
        if r.returncode == 0:
            return ['docker', 'compose']
    except Exception:
        pass
    if shutil.which('docker-compose'):
        return ['docker-compose']
    return None

# Resolved once at startup, reused everywhere
_DOCKER = _find_docker()

# ── Terminal helpers ──────────────────────────────────────────────────────────

_term_lock    = threading.Lock()
_orig_termios = None

def _save_termios():
    global _orig_termios
    try:
        import termios
        _orig_termios = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

def _restore_termios():
    if _orig_termios is not None:
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _orig_termios)
        except Exception:
            pass

def term_size():
    try:
        s = os.get_terminal_size()
        return s.lines, s.columns
    except Exception:
        return 24, 80

# ── ANSI ──────────────────────────────────────────────────────────────────────

_ANSI_RE   = re.compile(r'\x1b(?:\[[0-9;]*[a-zA-Z]|[()][0-9A-Za-z])')
strip_ansi = lambda s: _ANSI_RE.sub('', s)
def csi(*c): return f"\033[{';'.join(str(x) for x in c)}m"
RESET = csi(0)

BADGE = {
    "ERROR": f"{csi(41,97,1)} ERR {RESET}",
    "WARN":  f"{csi(43,30,1)} WRN {RESET}",
    "INFO":  f"{csi(44,97  )} INF {RESET}",
    "DEBUG": f"{csi(100,37 )} DBG {RESET}",
}
LEVEL_FG = {
    "ERROR": csi(31,1), "WARN": csi(33,1),
    "INFO":  csi(34),   "DEBUG": csi(90),
}
LEVEL_RANK = {
    "ERROR":0, "WARN":1, "WARNING":1, "INFO":2, "DEBUG":3, "TRACE":3,
}

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ── Container colour palette ──────────────────────────────────────────────────

_CONTAINER_COLORS = [
    csi(38,5,75),   # sky blue
    csi(38,5,214),  # amber
    csi(38,5,114),  # sage green
    csi(38,5,204),  # coral
    csi(38,5,141),  # lavender
    csi(38,5,80),   # teal
    csi(38,5,222),  # pale gold
    csi(38,5,210),  # rose
    csi(38,5,107),  # olive green
    csi(38,5,147),  # periwinkle
    csi(38,5,173),  # terracotta
    csi(38,5,87),   # cyan-mint
]

_container_color_map: dict = {}
_color_assign_lock         = threading.Lock()

def container_color(name: str) -> str:
    with _color_assign_lock:
        if name not in _container_color_map:
            idx = len(_container_color_map) % len(_CONTAINER_COLORS)
            _container_color_map[name] = _CONTAINER_COLORS[idx]
        return _container_color_map[name]

# ── Log parser ────────────────────────────────────────────────────────────────
#
# Strategy: always strip the docker timestamp prefix first, then try a series
# of format patterns on the remainder.  The fallback scans for a level keyword
# in the remainder (not the full raw line, which avoids repeating the docker
# timestamp in the message column).
#
# Supported formats (tried in order on the remainder after docker-ts is stripped):
#   A  Rust/tracing    ts2  LEVEL  target: message  kv=val
#   B  NestJS          [Nest] pid - date time  LEVEL  [Context]  message
#   C  bracket         [LEVEL] message  or  [LEVEL]: message
#   D  Python logging  YYYY-MM-DD HH:MM:SS[,ms] … LEVEL … message
#   E  level-first     LEVEL message  (e.g. crowdsec INF, gluetun WRN)
#   F  bare-scan       search for level keyword anywhere (last resort)

# All level keywords including short forms used by various apps
_LEVEL_PAT = r'(?P<level>ERROR|ERR|CRITICAL|WARN(?:ING)?|WRN|INFO|INF|DEBUG|DBG|TRACE)'

# Step 1 patterns — strip outer container prefix + docker timestamp
#
# IMPORTANT: service uses [^|:\s]+? (excludes colon and pipe) so it never
# accidentally matches an ISO timestamp like 2026-05-30T19:15:29Z, which
# contains colons.  Docker container names never contain colons.
# rest uses \s*(.*)$ (not .+) to handle blank lines that are just a timestamp.
_COMPOSE_PRE = re.compile(
    r'^(?P<service>[^|:\s]+?)\s+\|\s+(?P<ts1>\S+)\s*(?P<rest>.*)$', re.DOTALL)
# Plain docker logs --timestamps:  ts1  <remainder>  (rest may be empty)
_DOCKER_PRE  = re.compile(
    r'^(?P<ts1>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*)\s*(?P<rest>.*)$', re.DOTALL)

# Step 2 patterns — applied to <remainder> after docker-ts is stripped
# A: Rust tracing-subscriber:  ts2  LEVEL  target::path: message
_P_RUST = re.compile(
    r'^(?P<ts2>\d{4}-\d{2}-\d{2}T[^\s]+)\s+' + _LEVEL_PAT + r'\s+'
    r'(?P<target>[^\s:]+(?:::[^\s:]+)*):\s*(?P<message>.+)$', re.DOTALL)

# B: NestJS:  [Nest] pid - MM/DD/YYYY, HH:MM:SS AM  LEVEL  [Context]  message
_P_NEST = re.compile(
    r'^\[Nest\]\s+\d+\s+-\s+.{5,35}?\s+' + _LEVEL_PAT + r'\s+'
    r'\[(?P<target>[^\]]+)\]\s+(?P<message>.+)$', re.DOTALL)

# C: bracket level:  [LEVEL] message  or  [LEVEL]: message
_P_BRACKET = re.compile(
    r'^\[' + _LEVEL_PAT + r'\]\s*:?\s*(?P<message>.+)$', re.DOTALL)

# C2: date+bracket level (hotio/linuxserver.io containers):
#     [YYYY-MM-DD HH:MM:SS] [LEVEL] message
_P_BRACKET_TS = re.compile(
    r'^\[\d{4}-\d{2}-\d{2}[^\]]+\]\s+\[' + _LEVEL_PAT + r'\]\s*(?P<message>.+)$',
    re.DOTALL)

# D: Python logging:  YYYY-MM-DD HH:MM:SS[,ms]  <optional context>  LEVEL  message
#    Handles tautulli, icloudpd, bazarr, etc.  The .{0,60}? skips over any
#    logger name / request-id between the timestamp and the level keyword.
_P_PYTHON = re.compile(
    r'^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)'
    r'.{0,60}?\b' + _LEVEL_PAT + r'\b'
    r'[\s:*]+(?P<message>.+)$', re.DOTALL)

# E: level-first (no leading timestamp in remainder):  LEVEL  message
_P_LEVEL_FIRST = re.compile(
    r'^' + _LEVEL_PAT + r'\s+(?P<message>.+)$', re.DOTALL)

# F: logfmt key=value style used by Go apps (logrus, zap, zerolog):
#    time="..." level=warning msg="..." or level=info msg="..."
_P_LOGFMT = re.compile(
    r'\blevel=(?P<level>\w+)\b.*?\bmsg=(?P<message>"[^"]*"|\S+)', re.IGNORECASE)

# G: bare scan — level keyword anywhere (case-insensitive last resort)
_BARE_LEVEL = re.compile(r'\b' + _LEVEL_PAT + r'\b', re.IGNORECASE)

_KV = re.compile(r'\b(\w+?)(?:2m)?=("(?:[^"\\]|\\.)*"|\S+)')

def _fmt_ts(ts: str) -> str:
    """Simple HH:MM:SS.mmm — used only inside parse_line for the ParsedLine.timestamp
    field (kept for fallback display when ts_epoch is unavailable)."""
    try:
        return datetime.fromisoformat(ts.replace('Z','+00:00')).strftime('%H:%M:%S.%f')[:-3]
    except Exception:
        pass
    try:
        return datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').strftime('%H:%M:%S')
    except Exception:
        return ts[11:23] if len(ts) > 11 else ts

def _smart_ts(epoch: float) -> str:
    """Format a Unix epoch as a human-readable timestamp relative to today (local time).

    Same calendar day  →  14:23:45.123          (time + ms, most common case)
    Yesterday          →  yesterday 14:23:45
    Within 6 days      →  Mon 14:23:45           (day-of-week)
    Same year          →  May 28 14:23:45
    Different year     →  2025-05-28 14:23:45
    """
    try:
        now   = datetime.now().astimezone()
        dt    = datetime.fromtimestamp(epoch).astimezone()
        today = now.date()
        d     = dt.date()
        diff  = (today - d).days

        t = dt.strftime('%H:%M:%S')
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

def _ts_epoch(ts: str) -> float:
    """Unix epoch float — used for merge-sort ordering across containers."""
    try:
        return datetime.fromisoformat(ts.replace('Z','+00:00')).timestamp()
    except Exception:
        pass
    try:
        return datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').timestamp()
    except Exception:
        return time.time()

def _norm(lvl: str) -> str:
    """Normalise any level variant to one of ERROR/WARN/INFO/DEBUG."""
    return {
        'WARNING':  'WARN',  'WRN':   'WARN',
        'ERR':      'ERROR', 'CRITICAL': 'ERROR',
        'INF':      'INFO',
        'DBG':      'DEBUG', 'TRACE': 'DEBUG',
    }.get(lvl.upper(), lvl.upper())

class ParsedLine:
    __slots__ = ('raw','service','timestamp','ts_epoch','arrival_mono',
                 'level','target','message')
    def __init__(self, raw='', service='', timestamp='', ts_epoch=0.0,
                 arrival_mono=0.0, level='INFO', target='', message=''):
        self.raw=raw; self.service=service
        self.timestamp=timestamp; self.ts_epoch=ts_epoch
        self.arrival_mono=arrival_mono
        self.level=level; self.target=target; self.message=message

def _make(raw, service, ts1, ts_ep, level, target, message) -> ParsedLine:
    return ParsedLine(raw=raw, service=service,
                      timestamp=_fmt_ts(ts1), ts_epoch=ts_ep,
                      level=_norm(level), target=target, message=message)

def parse_line(raw: str, default_service: str = '') -> ParsedLine:
    raw   = raw.rstrip('\n')
    clean = strip_ansi(raw)

    # ── Step 1: strip outer prefix to get (service, ts1, remainder) ──────────
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

    # ── Step 2: try format patterns on remainder ──────────────────────────────

    # A: Rust tracing (second ISO timestamp + target::path: message)
    m = _P_RUST.match(rest)
    if m:
        return _make(raw, service, m.group('ts2'), _ts_epoch(m.group('ts2')),
                     m.group('level'), m.group('target'), m.group('message'))

    # B: NestJS
    m = _P_NEST.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), m.group('target'), m.group('message'))

    # C: bracket level [LEVEL] message
    m = _P_BRACKET.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    # C2: [date] [LEVEL] message  (hotio / linuxserver.io containers)
    m = _P_BRACKET_TS.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    # D: Python logging (date+time stamp embedded in remainder)
    m = _P_PYTHON.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    # E: level keyword at start of remainder (crowdsec INF, gluetun WRN, etc.)
    m = _P_LEVEL_FIRST.match(rest)
    if m:
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', m.group('message'))

    # F: logfmt (Go apps: level=warning msg="...")
    m = _P_LOGFMT.search(rest)
    if m:
        msg = m.group('message').strip('"')
        return _make(raw, service, ts1, ts_ep,
                     m.group('level'), '', msg)

    # G: bare scan — level anywhere (case-insensitive); use remainder as message
    m = _BARE_LEVEL.search(rest)
    return _make(raw, service, ts1, ts_ep,
                 m.group('level') if m else 'INFO', '', rest)

# ── Shared state ──────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.lock          = threading.Lock()
        self.visible       = {'ERROR', 'WARN', 'INFO'}
        self.counts        = {'ERROR':0, 'WARN':0, 'INFO':0, 'DEBUG':0}
        self.hidden        = 0
        self.paused        = False
        self.running       = True
        self.rate_buf      = deque(maxlen=2000)
        self.last_log_mono = 0.0
        self.spin_idx      = 0
        self.containers    = []
        self.svc_width     = 12

state = State()

def _bucket(lvl):
    if lvl in ('WARN','WARNING'): return 'WARN'
    if lvl in ('DEBUG','TRACE'):  return 'DEBUG'
    return lvl if lvl == 'ERROR' else 'INFO'

def _rate():
    now = time.monotonic()
    while state.rate_buf and state.rate_buf[0] < now - 1.0:
        state.rate_buf.popleft()
    return len(state.rate_buf)

# ── Merge queue ───────────────────────────────────────────────────────────────
#
# Each ingestion thread deposits lines here instead of printing directly.
# A single printer thread drains the heap in ts_epoch order once a line
# has been waiting long enough for out-of-order siblings to arrive.
#
# Two settle windows:
#   SETTLE_STARTUP — used for the first few seconds to absorb the full
#                    tail burst from all containers, which may arrive at
#                    slightly different times from concurrent docker processes.
#   SETTLE_LIVE    — used thereafter; small enough to be imperceptible.

SETTLE_STARTUP   = 1.5   # seconds
SETTLE_LIVE      = 0.15  # seconds
STARTUP_DURATION = 5.0   # switch to live window after this many seconds

_mq_heap  = []           # (ts_epoch, seq, ParsedLine)
_mq_seq   = 0
_mq_lock  = threading.Lock()
_start_mono = 0.0        # set in main() before threads start

def _settle() -> float:
    return (SETTLE_STARTUP
            if time.monotonic() - _start_mono < STARTUP_DURATION
            else SETTLE_LIVE)

def mq_put(p: ParsedLine):
    global _mq_seq
    with _mq_lock:
        heapq.heappush(_mq_heap, (p.ts_epoch, _mq_seq, p))
        _mq_seq += 1

def mq_drain() -> list:
    """Return all lines whose arrival_mono is older than the settle window,
    already in ts_epoch order (heapq guarantees this)."""
    cutoff = time.monotonic() - _settle()
    ready  = []
    with _mq_lock:
        while _mq_heap and _mq_heap[0][2].arrival_mono <= cutoff:
            _, _, p = heapq.heappop(_mq_heap)
            ready.append(p)
    return ready

# ── Rendering ─────────────────────────────────────────────────────────────────
# Row 1          — pinned status bar
# Rows 2..rows   — scroll region for log lines

def setup_display():
    rows, _ = term_size()
    with _term_lock:
        sys.stdout.write(
            "\033[2J"
            f"\033[2;{rows}r"
            f"\033[{rows};1H"
            "\033[?25l"
        )
        sys.stdout.flush()

def render_status():
    c   = state.counts
    vis = state.visible

    def pill(name, key):
        count  = c[name]
        active = name in vis
        if active:
            return f"{LEVEL_FG[name]}●{key}:{RESET}{LEVEL_FG[name]}{count:>5}{RESET}"
        else:
            return f"{csi(90)}○{key}:{count:>5}{RESET}"

    rate     = _rate()
    now      = time.monotonic()
    idle_sec = now - state.last_log_mono
    spin     = _SPIN[state.spin_idx % len(_SPIN)]

    if state.last_log_mono == 0:
        activity = f"{csi(90)}{spin} waiting for logs…{RESET}"
    elif rate > 0:
        activity = f"{csi(32)}{spin}{RESET} {rate:.1f} lines/s"
    else:
        secs = int(idle_sec)
        if   secs < 60:    idle_str = f"{secs}s ago"
        elif secs < 3600:  idle_str = f"{secs//60}m {secs%60}s ago"
        else:               idle_str = f"{secs//3600}h {(secs%3600)//60}m ago"
        activity = f"{csi(90)}{spin} idle · last log {idle_str}{RESET}"

    paused = f"  {csi(93)}⏸ PAUSED{RESET}" if state.paused else ""
    div    = f"\033[48;5;235m\033[37m {csi(90)}│{RESET}\033[48;5;235m\033[37m "

    # Show merge queue depth so the user can see when the startup buffer drains
    qlen = len(_mq_heap)
    buf_tag = (f"  {csi(90)}buf:{qlen}{RESET}\033[48;5;235m\033[37m"
               if qlen > 0 else "")

    bar = (
        f"\033[48;5;235m\033[37m "
        + pill('ERROR','E')
        + f"\033[48;5;235m\033[37m  "
        + pill('WARN', 'W')
        + f"\033[48;5;235m\033[37m  "
        + pill('INFO', 'I')
        + f"\033[48;5;235m\033[37m  "
        + pill('DEBUG','D')
        + div
        + f"hidden:{csi(90)}{state.hidden:>5}{RESET}\033[48;5;235m\033[37m"
        + f"  {csi(2)}(filtered){RESET}\033[48;5;235m\033[37m"
        + div
        + activity
        + buf_tag
        + paused
        + div
        + f"{csi(90)}[e w i d  p c q]{RESET}"
        + "\033[48;5;235m\033[K"
        + RESET
    )

    with _term_lock:
        sys.stdout.write(f"\033[s\033[1;1H{bar}\033[u")
        sys.stdout.flush()

def handle_resize(*_):
    setup_display()
    render_status()

def _fmt_line(p: ParsedLine) -> str:
    badge = BADGE.get(p.level, BADGE['INFO'])
    # Use smart timestamp computed at display time (not parse time) so the
    # "yesterday / Mon / May 28" labels are always relative to right now.
    ts_str = _smart_ts(p.ts_epoch) if p.ts_epoch else (p.timestamp or '')
    ts     = f"{csi(90)}{ts_str:<19}{RESET} " if ts_str else ''
    w     = state.svc_width
    svc   = (f"{container_color(p.service)}{p.service:<{w}.{w}}{RESET} "
             if p.service else ' ' * (w + 1))
    if p.target:
        segs  = p.target.split('::')
        short = '::'.join(segs[-2:]) if len(segs) > 2 else p.target
        tgt   = f"{csi(2)}{short:<26}{RESET} "
    else:
        tgt = ''
    msg = _KV.sub(lambda m: f"{csi(2)}{m.group(1)}={m.group(2)}{RESET}",
                  p.message) if p.message else strip_ansi(p.raw)
    return f"{svc}{ts}{badge} {tgt}{msg}"

def print_log_line(p: ParsedLine):
    rows, _ = term_size()
    line = _fmt_line(p)
    with _term_lock:
        sys.stdout.write(f"\033[{rows};1H\n{line}\033[K")
        sys.stdout.flush()

# ── Ingestion ─────────────────────────────────────────────────────────────────

def ingest_stream(stream, default_service: str = ''):
    for raw in stream:
        if not state.running:
            break
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')

        p              = parse_line(raw, default_service)
        p.arrival_mono = time.monotonic()

        with state.lock:
            state.counts[_bucket(p.level)] += 1
            state.rate_buf.append(time.monotonic())
            state.last_log_mono = time.monotonic()
            visible = p.level in state.visible
            if not visible:
                state.hidden += 1

        # Counts are updated immediately; display is deferred to printer_loop
        if visible and not state.paused:
            mq_put(p)

        render_status()

# ── Printer (merge-queue consumer) ───────────────────────────────────────────

def _print_date_sep(d) -> None:
    """Print a full-width date separator rule when the log stream crosses a day."""
    _, cols = term_size()
    label   = d.strftime('  %A, %B %-d, %Y  ')   # e.g.  Wednesday, May 28, 2025
    pad     = max(0, cols - len(label) - 4)
    rule    = f"{csi(90)}── {label}{'─' * pad}{RESET}"
    rows, _ = term_size()
    with _term_lock:
        sys.stdout.write(f"\033[{rows};1H\n{rule}\033[K")
        sys.stdout.flush()

_last_printed_date = None   # tracks the calendar date of the last displayed line

def printer_loop():
    """Drain the merge heap in timestamp order at ~25 Hz.
    Lines wait in the heap until their arrival_mono is older than the
    current settle window, ensuring concurrent streams are interleaved
    correctly even when their tail bursts arrive at slightly different times.
    A date-separator rule is injected whenever the log stream crosses midnight."""
    global _last_printed_date
    while state.running:
        lines = mq_drain()
        for p in lines:
            if p.ts_epoch:
                d = datetime.fromtimestamp(p.ts_epoch).date()
                if _last_printed_date is not None and d != _last_printed_date:
                    _print_date_sep(d)
                _last_printed_date = d
            print_log_line(p)
        if lines:
            render_status()
        time.sleep(0.04)  # 25 Hz

    # Flush anything still in the queue on exit
    for _, _, p in sorted(_mq_heap):
        print_log_line(p)

# ── Heartbeat ─────────────────────────────────────────────────────────────────

def heartbeat_loop():
    while state.running:
        with state.lock:
            state.spin_idx += 1
        render_status()
        time.sleep(0.25)

# ── Docker helpers ────────────────────────────────────────────────────────────

def discover_containers(project: str = None) -> list:
    """Return names of running containers, optionally filtered by compose project.

    Uses 'docker ps' directly — works for containers started any way
    (compose, Unraid WebUI, plain docker run).  The --project filter matches
    the com.docker.compose.project label set automatically by both
    'docker compose' and 'docker-compose'.
    """
    cmd = _DOCKER + ['ps', '--format', '{{.Names}}']
    if project:
        cmd += ['--filter', f'label=com.docker.compose.project={project}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
        if project and not names:
            # Give a helpful hint listing available projects
            proj_r = subprocess.run(
                _DOCKER + ['ps', '--format', '{{.Label "com.docker.compose.project"}}'],
                capture_output=True, text=True, timeout=5)
            projects = sorted({p.strip() for p in proj_r.stdout.splitlines()
                                if p.strip()})
            hint = (f"  Available projects: {', '.join(projects)}"
                    if projects else "  No compose projects found.")
            sys.exit(f"No running containers found for project '{project}'.\n{hint}")
        return names
    except subprocess.TimeoutExpired:
        sys.exit("Error: 'docker ps' timed out.")

def run_docker_logs(containers: list, tail: int):
    procs = []
    for name in containers:
        p = subprocess.Popen(
            _DOCKER + ['logs', '-f', '--timestamps', f'--tail={tail}', name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        procs.append(p)
        threading.Thread(
            target=ingest_stream, args=(p.stdout, name), daemon=True,
        ).start()
    return procs

# ── Keyboard ──────────────────────────────────────────────────────────────────

def keyboard_loop():
    import tty, termios
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while state.running:
            ch = sys.stdin.read(1)
            if ch in ('\x03', '\x04', 'q', 'Q'):
                state.running = False
                break
            def toggle(lvl):
                if lvl in state.visible: state.visible.discard(lvl)
                else: state.visible.add(lvl)
            with state.lock:
                if   ch == 'e': toggle('ERROR')
                elif ch == 'w': toggle('WARN')
                elif ch == 'i': toggle('INFO')
                elif ch == 'd': toggle('DEBUG')
                elif ch == 'p': state.paused    = not state.paused
                elif ch == 'c':
                    state.counts = {'ERROR':0,'WARN':0,'INFO':0,'DEBUG':0}
                    state.hidden = 0
            render_status()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ── Cleanup ───────────────────────────────────────────────────────────────────

_cleaned = False

def cleanup():
    global _cleaned
    if _cleaned: return
    _cleaned = True
    _restore_termios()
    rows, _ = term_size()
    with _term_lock:
        sys.stdout.write(f"\033[r\033[{rows};1H\n\033[?25h\033[m")
        sys.stdout.flush()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _start_mono

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('containers', nargs='*',
                        help='Container name(s). Omit to watch all running containers.')
    parser.add_argument('--stdin',   action='store_true',
                        help='Read from stdin (pipe docker compose logs -f or '
                             'docker-compose logs -f into this)')
    parser.add_argument('--tail',    type=int, default=50,
                        help='Lines of history per container on start (default: 50)')
    parser.add_argument('--show',    default='ERROR,WARN,INFO', metavar='LEVELS',
                        help='Comma-separated levels to display. Default: ERROR,WARN,INFO')
    parser.add_argument('--project', '-p', metavar='NAME', default=None,
                        help='Filter to containers in a specific compose project. '
                             'Works with both "docker compose" and "docker-compose" stacks, '
                             'and with Unraid WebUI containers that have a project label.')
    parser.add_argument('--skip', nargs='+', metavar='CONTAINER', default=[],
                        help='Exclude these containers when watching all (no name given). '
                             'Ignored when specific container names are passed. '
                             'Example: --skip traefik crowdsec watchtower')
    args = parser.parse_args()

    if args.stdin:
        containers = []
    elif args.containers:
        containers = args.containers
    else:
        containers = discover_containers(project=args.project)
        if not containers and not args.project:
            sys.exit("No running containers found. "
                     "Start a container first, or use --stdin.")
        if args.skip:
            skip_set  = {s.lower() for s in args.skip}
            skipped   = [c for c in containers if c.lower() in skip_set]
            containers = [c for c in containers if c.lower() not in skip_set]
            if not containers:
                sys.exit(
                    f"All containers were skipped ({', '.join(skipped)}). "
                    "Nothing left to watch."
                )

    valid = {'ERROR','WARN','WARNING','INFO','DEBUG','TRACE'}
    parsed_show = {_norm(l.strip()) for l in args.show.upper().split(',')
                   if l.strip() in valid}
    if not parsed_show:
        sys.exit("--show: no valid levels. Use ERROR, WARN, INFO, or DEBUG.")

    state.visible    = parsed_show
    state.containers = containers
    state.svc_width  = min(max((len(n) for n in containers), default=8), 20)
    state.svc_width  = max(state.svc_width, 8)

    # Pre-assign colours in docker-ps order so the startup note and log lines agree
    for name in containers:
        container_color(name)

    _save_termios()
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *_: setattr(state, 'running', False))
    signal.signal(signal.SIGINT,  lambda *_: setattr(state, 'running', False))
    try:
        signal.signal(signal.SIGWINCH, handle_resize)
    except AttributeError:
        pass

    setup_display()
    render_status()

    # Record start time *before* spawning ingest threads so the startup settle
    # window is measured from when the first lines might arrive.
    _start_mono = time.monotonic()

    procs = []
    if args.stdin:
        compose_cmd = _find_compose()
        compose_str = ' '.join(compose_cmd) if compose_cmd else 'docker compose'
        threading.Thread(target=ingest_stream, args=(sys.stdin,''), daemon=True).start()
        rows, _ = term_size()
        with _term_lock:
            sys.stdout.write(
                f"\033[{rows};1H\n"
                f"{csi(90)}reading from stdin  "
                f"tip: {compose_str} logs -f | python3 docktail.py --stdin{RESET}\033[K"
            )
            sys.stdout.flush()
    else:
        colored_names = '  '.join(
            f"{container_color(n)}{n}{RESET}" for n in containers
        )
        project_tag = (f"  {csi(90)}[project: {args.project}]{RESET}"
                       if args.project else "")
        skip_tag = (
            f"  {csi(90)}[skipping: "
            + "  ".join(f"{csi(90,9)}{n}{RESET}{csi(90)}" for n in skipped)
            + f"]{RESET}"
            if args.skip and not args.containers else ""
        )
        rows, _ = term_size()
        with _term_lock:
            note = (f"{csi(90)}watching {len(containers)} container(s):"
                    f"{RESET}  {colored_names}{project_tag}{skip_tag}")
            sys.stdout.write(f"\033[{rows};1H\n{note}\033[K")
            sys.stdout.flush()
        procs = run_docker_logs(containers, args.tail)

    threading.Thread(target=printer_loop,   daemon=True).start()
    threading.Thread(target=keyboard_loop,  daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    try:
        while state.running:
            time.sleep(0.05)
    finally:
        for p in procs:
            try: p.terminate()
            except Exception: pass
        cleanup()
        print("docktail exited.")

if __name__ == '__main__':
    main()