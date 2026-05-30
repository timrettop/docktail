# docktail

A Docker log viewer with real-time level filtering, per-container color coding,
live counters, and cross-container timestamp ordering.

## Features (Phase 1 — terminal)

- **Watch all running containers** automatically (`docker ps`), specific containers,
  or all containers in a compose project (`--project`)
- **Level filtering** — toggle ERROR / WARN / INFO / DEBUG display live with `e/w/i/d`
- **All levels always counted** — counters in the header update even for hidden levels
- **Hidden line counter** — shows how many lines are filtered out and why
- **Color-coded service names** — each container gets a stable distinct color
- **Cross-container timestamp ordering** — merge queue sorts lines from concurrent
  containers by their log timestamps, not arrival order
- **Multi-format log parsing** — Rust tracing, NestJS, Python logging, logfmt,
  bracket `[LEVEL]`, s6/hotio init scripts, and more
- **Date-aware timestamps** — `HH:MM:SS.mmm` for today, `yesterday HH:MM:SS`,
  `Mon HH:MM:SS`, `May 28 HH:MM:SS`, `YYYY-MM-DD HH:MM:SS` for older history
- **Date separator rules** — visual divider injected when log stream crosses midnight
- **Idle / alive indicator** — animated spinner shows the process is running even
  when containers are quiet
- **Works with `docker compose` and `docker-compose`** — detects which is available
- **No compose file needed** — uses `docker ps` and `docker logs` directly

## Requirements

- Python 3.10+
- Docker (CLI in `$PATH`)
- Unix/macOS terminal (uses `termios` / ANSI escape sequences)

## Installation

```bash
git clone https://github.com/timrettop/docktail.git
cd docktail
# No dependencies to install — uses stdlib only
```

## Usage

```bash
# Watch all running containers
python3 docktail.py

# Watch specific containers
python3 docktail.py api postgres

# Watch all containers in a compose project
python3 docktail.py --project mystack

# Skip noisy infrastructure containers
python3 docktail.py --skip nginx redis watchtower

# Start at WARN level, fetch 100 lines of history
python3 docktail.py --show WARN,ERROR --tail 100

# Pipe from docker compose (stdin mode)
docker compose logs -f | python3 docktail.py --stdin
docker-compose logs -f | python3 docktail.py --stdin
```

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `e` | Toggle ERROR display |
| `w` | Toggle WARN display |
| `i` | Toggle INFO display |
| `d` | Toggle DEBUG display |
| `p` | Pause / resume output |
| `c` | Clear counters |
| `q` / Ctrl+C | Quit |

## CLI options

| Option | Default | Description |
| --- | --- | --- |
| `--show LEVELS` | `ERROR,WARN,INFO` | Comma-separated levels to display |
| `--tail N` | `50` | Lines of history per container on start |
| `--project NAME` | — | Filter to a compose project by label |
| `--skip NAME...` | — | Exclude containers when watching all |
| `--stdin` | — | Read from stdin instead of `docker logs` |

## Roadmap

### Phase 2 — Web UI

- FastAPI + WebSocket backend wrapping `docker ps` / `docker logs`
- Single-file `index.html` frontend (no build step)
- Container picker, live level toggles, counter badges
- Auto-reconnect on disconnect

### Phase 3 — Unraid plugin

- `.plg` XML plugin definition
- Installs to `/usr/local/emhttp/plugins/docktail/`
- Menu item under the Docker tab
- `go` file starts the backend at boot on a fixed port
- Accessible at `http://tower/plugins/docktail/`
