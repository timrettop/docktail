"""
server.py — FastAPI + WebSocket backend for the docktail web UI.

Run with:
    uvicorn server:app --host 0.0.0.0 --port 7654 --reload

Endpoints:
    GET  /api/health
    GET  /api/containers[?project=NAME]
    WS   /ws/logs[?containers=a,b&tail=50&project=NAME]
"""

import heapq
import shutil
import subprocess
import threading
import time
from datetime import datetime

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio

from core import (
    ParsedLine,
    _smart_ts,
    assign_container_index,
    parse_line,
)

# ── Container colour palette (CSS) ────────────────────────────────────────────
# Same slot order as _CONTAINER_COLORS in docktail.py so assign_container_index()
# produces the same visual colour for a given container in both UIs.

_CSS_COLORS = [
    "#5bc4f5",  # sky blue
    "#f5a623",  # amber
    "#7bc67e",  # sage green
    "#f57c7c",  # coral
    "#b39ddb",  # lavender
    "#4dd0e1",  # teal
    "#ffd54f",  # pale gold
    "#f48fb1",  # rose
    "#a5c97b",  # olive green
    "#9fa8da",  # periwinkle
    "#e08a6a",  # terracotta
    "#4dd9b6",  # cyan-mint
]


def container_css_color(name: str) -> str:
    """Return a CSS hex colour for a container name.

    Delegates index assignment to core.assign_container_index() so the colour
    slot always matches the ANSI colour used by docktail.py in the terminal.
    """
    return _CSS_COLORS[assign_container_index(name) % len(_CSS_COLORS)]


# ── Docker helpers ─────────────────────────────────────────────────────────────

def _docker_cmd() -> list[str]:
    """Return ['docker'] or raise if the docker binary is not available."""
    if shutil.which('docker'):
        return ['docker']
    raise RuntimeError("'docker' not found in PATH")


def list_containers(project: str | None = None) -> list[dict]:
    """Return running containers as dicts with name, status, and color.

    Optionally filtered by compose project label.
    """
    try:
        cmd = _docker_cmd() + ['ps', '--format', '{{.Names}}\t{{.Status}}']
        if project:
            cmd += ['--filter', f'label=com.docker.compose.project={project}']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        result = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts  = line.split('\t', 1)
            name   = parts[0].strip()
            status = parts[1].strip() if len(parts) > 1 else ''
            result.append({
                "name":   name,
                "status": status,
                "color":  container_css_color(name),
            })
        return result
    except (subprocess.TimeoutExpired, FileNotFoundError, RuntimeError):
        return []


# ── Merge-queue settle windows ────────────────────────────────────────────────
# Mirror the values in docktail.py so ordering behaviour is consistent
# whether the user is watching in the terminal or the web UI.

_SETTLE_STARTUP   = 1.5   # seconds — absorbs concurrent tail bursts on connect
_SETTLE_LIVE      = 0.15  # seconds — ongoing live ordering window
_STARTUP_DURATION = 5.0   # seconds — when to switch from startup to live settle


# ── Log session ────────────────────────────────────────────────────────────────

class LogSession:
    """Manages docker-logs subprocesses for one WebSocket connection.

    One reader thread per container puts parsed ParsedLine objects into a
    shared min-heap keyed by ts_epoch.  The async drain() method yields lines
    in chronological order once they have waited longer than the settle window,
    giving concurrent containers time to interleave correctly.
    """

    def __init__(self, containers: list[str], tail: int):
        self.containers      = containers
        self.tail            = tail
        self._start_mono     = time.monotonic()
        self._heap: list     = []
        self._seq            = 0
        self._lock           = threading.Lock()
        self._procs: list[subprocess.Popen] = []
        self._running        = True
        self._last_sep_date  = None   # date of last separator injected

    def start(self) -> None:
        """Spawn one reader thread per container."""
        for name in self.containers:
            try:
                p = subprocess.Popen(
                    _docker_cmd() + [
                        'logs', '-f', '--timestamps',
                        f'--tail={self.tail}', name,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self._procs.append(p)
                threading.Thread(
                    target=self._reader,
                    args=(p.stdout, name),
                    daemon=True,
                ).start()
            except Exception:
                pass   # container not found — reader just produces no lines

    def _reader(self, stream, default_service: str) -> None:
        try:
            for raw in stream:
                if not self._running:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8', errors='replace')
                p              = parse_line(raw, default_service)
                p.arrival_mono = time.monotonic()
                with self._lock:
                    heapq.heappush(self._heap, (p.ts_epoch, self._seq, p))
                    self._seq += 1
        except Exception:
            pass

    def _settle(self) -> float:
        elapsed = time.monotonic() - self._start_mono
        return _SETTLE_STARTUP if elapsed < _STARTUP_DURATION else _SETTLE_LIVE

    def drain(self) -> list[ParsedLine]:
        """Return all lines whose arrival_mono has exceeded the settle window."""
        cutoff = time.monotonic() - self._settle()
        result = []
        with self._lock:
            while self._heap and self._heap[0][2].arrival_mono <= cutoff:
                _, _, p = heapq.heappop(self._heap)
                result.append(p)
        return result

    def stop(self) -> None:
        """Terminate all subprocesses; reader threads will exit on next iteration."""
        self._running = False
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass


# ── Message serialisation ──────────────────────────────────────────────────────

def _bucket(lvl: str) -> str:
    """Map any level variant to one of the four counter buckets."""
    if lvl in ('WARN', 'WARNING'): return 'WARN'
    if lvl in ('DEBUG', 'TRACE'):  return 'DEBUG'
    return lvl if lvl == 'ERROR' else 'INFO'


def line_to_msg(p: ParsedLine) -> dict:
    """Serialise a ParsedLine to a WebSocket 'line' message."""
    return {
        "type":      "line",
        "service":   p.service,
        "level":     p.level,
        "timestamp": _smart_ts(p.ts_epoch) if p.ts_epoch else p.timestamp,
        "ts_epoch":  p.ts_epoch,
        "target":    p.target,
        "message":   p.message,
        "color":     container_css_color(p.service) if p.service else "#888888",
    }


def date_sep_msg(d) -> dict:
    """Serialise a date boundary to a WebSocket 'date_sep' message."""
    return {
        "type":  "date_sep",
        "label": d.strftime("%A, %B %-d, %Y"),
    }


# ── FastAPI application ────────────────────────────────────────────────────────

app = FastAPI(title="docktail", version="0.1.0")

# Allow the web UI to connect from a different port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/containers")
async def containers_endpoint(project: str | None = None):
    return {"containers": list_containers(project)}


@app.websocket("/ws/logs")
async def ws_logs(
    websocket:  WebSocket,
    containers: str       = Query(default=""),
    tail:       int       = Query(default=50),
    project:    str | None = Query(default=None),
):
    await websocket.accept()

    # Resolve the container list
    if containers:
        container_list = [c.strip() for c in containers.split(',') if c.strip()]
    else:
        container_list = [c['name'] for c in list_containers(project)]

    if not container_list:
        await websocket.send_json({
            "type":    "error",
            "message": "No running containers found.",
        })
        await websocket.close()
        return

    # Inform the client of which containers it will receive and their colours
    await websocket.send_json({
        "type":       "connected",
        "containers": [
            {"name": name, "color": container_css_color(name)}
            for name in container_list
        ],
    })

    session = LogSession(container_list, tail)
    session.start()
    counts  = {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0}

    try:
        while True:
            await asyncio.sleep(0.05)   # drain at 20 Hz
            lines = session.drain()

            for p in lines:
                counts[_bucket(p.level)] += 1

                # Inject a date-separator when the log stream crosses midnight
                if p.ts_epoch:
                    d = datetime.fromtimestamp(p.ts_epoch).date()
                    if session._last_sep_date != d:
                        session._last_sep_date = d
                        await websocket.send_json(date_sep_msg(d))

                await websocket.send_json(line_to_msg(p))

            if lines:
                await websocket.send_json({
                    "type":   "status",
                    "counts": counts.copy(),
                })

    except WebSocketDisconnect:
        pass
    finally:
        session.stop()


# Mount the static web UI last so /api/* routes are matched first.
# Wrapped in try/except so API and WebSocket work even before index.html exists.
try:
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
except RuntimeError:
    pass   # static/ not yet created


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=7654, reload=True)
