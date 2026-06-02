"""
test_parser.py — Tests for parse_line() and _norm().

Each class targets a specific log format.  Container names and log content
are generic — the tests verify format detection, not any particular app.
"""
import pytest
from core import ParsedLine, _norm, parse_line


# ── _norm: level normalisation ────────────────────────────────────────────────

class TestNorm:
    def test_canonical_levels_pass_through(self):
        for lvl in ("ERROR", "WARN", "INFO", "DEBUG"):
            assert _norm(lvl) == lvl

    def test_warning_to_warn(self):
        assert _norm("WARNING") == "WARN"

    def test_short_codes(self):
        assert _norm("WRN") == "WARN"
        assert _norm("ERR") == "ERROR"
        assert _norm("INF") == "INFO"
        assert _norm("DBG") == "DEBUG"

    def test_critical_to_error(self):
        assert _norm("CRITICAL") == "ERROR"

    def test_trace_to_debug(self):
        assert _norm("TRACE") == "DEBUG"

    def test_case_insensitive(self):
        assert _norm("warning") == "WARN"
        assert _norm("Warning") == "WARN"
        assert _norm("error")   == "ERROR"
        assert _norm("info")    == "INFO"
        assert _norm("debug")   == "DEBUG"


# ── Format A: Rust tracing-subscriber ────────────────────────────────────────

class TestParseRustTracing:
    """
    Format A: [service |] ts1 ts2  LEVEL  target::path: message kv=val

    Output from services using Rust's tracing crate with tracing-subscriber.
    The 2m= suffix on field names is a tracing-subscriber ANSI artefact where
    the ESC[ prefix gets stripped by Docker, leaving '2m' before '='.
    """

    COMPOSE = (
        "myapp  | 2026-05-30T14:58:52.035712555Z "
        "2026-05-30T14:58:52.035632Z  INFO "
        "myapp::sync::processor: Skipping asset: file exists with same name and size "
        'asset_id2m="AbCdEf123ExampleId" path2m=/data/media/example.heic'
    )
    DOCKER = COMPOSE.split(" | ", 1)[1]

    def test_compose_service(self):
        assert parse_line(self.COMPOSE).service == "myapp"

    def test_compose_level(self):
        assert parse_line(self.COMPOSE).level == "INFO"

    def test_compose_target(self):
        assert parse_line(self.COMPOSE).target == "myapp::sync::processor"

    def test_compose_message_contains_description(self):
        assert "Skipping asset" in parse_line(self.COMPOSE).message

    def test_compose_message_excludes_target(self):
        # target should be separated out, not left in the message
        assert "myapp::sync::processor" not in parse_line(self.COMPOSE).message

    def test_docker_service_from_default(self):
        assert parse_line(self.DOCKER, default_service="myapp").service == "myapp"

    def test_docker_level(self):
        assert parse_line(self.DOCKER, default_service="myapp").level == "INFO"

    def test_ts_epoch_is_positive(self):
        assert parse_line(self.COMPOSE).ts_epoch > 0

    def test_timestamp_formatted(self):
        # Should be HH:MM:SS.mmm, not the raw ISO string
        ts = parse_line(self.COMPOSE).timestamp
        assert "T" not in ts
        assert len(ts) <= 12


# ── Format E: level-first (short level code, no target) ──────────────────────

class TestParseLevelFirst:
    """
    Format E: ts1  LEVEL  message

    Some Go services emit a bare short level code as the first token after
    the docker timestamp, e.g. INF or WRN with no structured target field.
    """

    LINE = "2026-05-30T16:55:49.870768781Z INF Starting background worker process"

    def test_level(self):
        assert parse_line(self.LINE, "go-service").level == "INFO"

    def test_service_from_default(self):
        assert parse_line(self.LINE, "go-service").service == "go-service"

    def test_message_contains_content(self):
        assert "Starting" in parse_line(self.LINE, "go-service").message

    def test_message_does_not_start_with_level(self):
        msg = parse_line(self.LINE, "go-service").message
        assert not msg.startswith("INF")

    def test_timestamp_set(self):
        assert parse_line(self.LINE, "go-service").timestamp != ""


# ── Format F: logfmt (Go structured loggers) ─────────────────────────────────

class TestParseLogfmt:
    """
    Format F: ts1  time="..." level=warning msg="..."

    Common in Go services using logrus, zerolog, or similar structured loggers.
    The level and message are encoded as key=value pairs.
    """

    LINE = (
        '2026-05-30T16:55:51.304812479Z '
        'time="2026-05-30T16:55:51Z" level=warning '
        'msg="connection pool limit reached" '
        'component=worker count=3'
    )

    def test_level(self):
        assert parse_line(self.LINE, "go-service").level == "WARN"

    def test_message_extracted(self):
        assert "connection pool limit reached" in parse_line(self.LINE, "go-service").message

    def test_message_excludes_level_tag(self):
        # Should not include the raw level= tag
        assert "level=warning" not in parse_line(self.LINE, "go-service").message


# ── Format C: bracket [LEVEL] ─────────────────────────────────────────────────

class TestParseBracket:
    """
    Format C: ts1  [LEVEL]  message  or  [LEVEL]: message

    Used by HAProxy, load balancers, and various proxy containers.
    """

    LINE    = "2026-05-03T14:24:11.002773960Z [WARNING]  (12) : Proxy backend stopped"
    COMPOSE = "haproxy  | " + LINE

    def test_direct_level(self):
        assert parse_line(self.LINE, "haproxy").level == "WARN"

    def test_direct_service_from_default(self):
        assert parse_line(self.LINE, "haproxy").service == "haproxy"

    def test_direct_message(self):
        assert "Proxy backend stopped" in parse_line(self.LINE, "haproxy").message

    def test_compose_service(self):
        assert parse_line(self.COMPOSE).service == "haproxy"

    def test_compose_level(self):
        assert parse_line(self.COMPOSE).level == "WARN"


# ── Format B: NestJS ──────────────────────────────────────────────────────────

class TestParseNestJS:
    """
    Format B: ts1  [Nest] pid - MM/DD/YYYY, HH:MM:SS AM  LEVEL  [Context]  message

    Used by NestJS applications.  The context in square brackets becomes the
    parsed target field.
    """

    LINE = (
        "2026-05-28T05:00:00.022787652Z "
        "[Nest] 7 - 05/28/2026, 12:00:00 AM     WARN "
        "[Microservices:LibraryService] No valid import paths found for library "
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    )

    def test_level(self):
        assert parse_line(self.LINE, "nestapp").level == "WARN"

    def test_target(self):
        assert parse_line(self.LINE, "nestapp").target == "Microservices:LibraryService"

    def test_message(self):
        assert "No valid import paths" in parse_line(self.LINE, "nestapp").message

    def test_message_excludes_nest_header(self):
        msg = parse_line(self.LINE, "nestapp").message
        assert "[Nest]" not in msg


# ── Format D: Python logging module ──────────────────────────────────────────

class TestParsePythonLogging:
    """
    Format D: ts1  YYYY-MM-DD HH:MM:SS[,ms]  [context]  LEVEL  message

    Python's logging module produces this format.  Field ordering and
    separators vary between configurations (dash, double-colon, comma-ms).
    """

    # Standard Python logging with dash separator and double-colon prefix
    APP1 = (
        "2026-05-30T14:19:33.078615557Z "
        "2026-05-30 09:19:33 - WARNING :: Worker Thread-15 : No input data received."
    )
    # Minimal Python logging: date time LEVEL message
    APP2 = (
        "2026-05-30T03:43:11.576100008Z "
        "2026-05-30 20:43:11 WARNING Session token expires in 3 days"
    )
    # Python logging with logger name and request ID
    APP3 = (
        "2026-05-29T05:30:30.434826005Z "
        "2026-05-29 00:30:30,434 - root                (abc123def456) :  "
        "ERROR (signalr_client:226) - MyApp SignalR client error"
    )

    def test_app1_level(self):
        assert parse_line(self.APP1, "app1").level == "WARN"

    def test_app1_message(self):
        assert "Worker Thread-15" in parse_line(self.APP1, "app1").message

    def test_app2_level(self):
        assert parse_line(self.APP2, "app2").level == "WARN"

    def test_app2_message(self):
        assert "expires in 3 days" in parse_line(self.APP2, "app2").message

    def test_app3_level(self):
        assert parse_line(self.APP3, "app3").level == "ERROR"

    def test_app3_message_contains_app_name(self):
        assert "MyApp SignalR" in parse_line(self.APP3, "app3").message


# ── Format C2: bracket-with-date (container init scripts) ────────────────────

class TestParseBracketWithDate:
    """
    Format C2: ts1  [YYYY-MM-DD HH:MM:SS]  [LEVEL]  message

    Used by hotio and linuxserver.io container init scripts (s6, cont-init).
    The bracketed date prefix is stripped; only the message is kept.
    """

    LINE    = "2026-05-30T19:15:29.392235451Z [2026-05-30 14:15:29] [INF] Executing usermod..."
    COMPOSE = "downloader  | " + LINE

    def test_direct_level(self):
        assert parse_line(self.LINE, "downloader").level == "INFO"

    def test_direct_message(self):
        assert "Executing usermod" in parse_line(self.LINE, "downloader").message

    def test_message_excludes_date_bracket(self):
        # The [2026-05-30 14:15:29] prefix should be stripped
        msg = parse_line(self.LINE, "downloader").message
        assert "[2026-05-30" not in msg

    def test_compose_service(self):
        assert parse_line(self.COMPOSE).service == "downloader"

    def test_compose_level(self):
        assert parse_line(self.COMPOSE).level == "INFO"

    def test_compose_message(self):
        assert "Executing usermod" in parse_line(self.COMPOSE).message


# ── Short level code with secondary timestamp ─────────────────────────────────

class TestParseShortLevelCode:
    """
    Some services emit a second (app-internal) timestamp followed by a short
    level code before the message.  e.g. ts1 ts2 WRN message
    """

    LINE = (
        "2026-05-30T16:57:53.687021369Z 2026-05-30T16:57:53Z "
        "WRN Connection limit reached for upstream backend pool"
    )

    def test_level(self):
        assert parse_line(self.LINE, "proxy").level == "WARN"

    def test_message(self):
        assert "Connection limit reached" in parse_line(self.LINE, "proxy").message


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestParseEdgeCases:
    def test_blank_line_after_docker_timestamp(self):
        """A line that is only a docker timestamp should not crash or lose service."""
        p = parse_line("2026-05-30T19:15:29.391247572Z", "app")
        assert p.service == "app"
        assert p.level == "INFO"   # default

    def test_compose_blank_line(self):
        """Compose format with nothing after the timestamp."""
        p = parse_line("app  | 2026-05-30T19:15:29.391247572Z")
        assert p.service == "app"

    def test_ascii_art_pipe_chars_direct(self):
        """
        ASCII art containing ' | ' must not be mistaken for a compose prefix.
        The bug: _COMPOSE_PRE with \\S+? matched the ISO timestamp as the
        service name because ` | ` follows it in the ASCII art content.
        Fixed by excluding ':' from the service character class (ISO timestamps
        always contain colons; docker container names never do).
        """
        line = "2026-05-30T19:15:29.391265955Z | | | | (_) | |_| | (_) |"
        p = parse_line(line, "app")
        assert p.service == "app"   # NOT '2026-05-30T19:15:29.391265955Z'
        assert "| | | |" in p.message

    def test_ascii_art_pipe_chars_compose(self):
        """Same ASCII art coming through docker compose logs."""
        line = "app  | 2026-05-30T19:15:29.391265955Z | | | | (_) | |_| | (_) |"
        p = parse_line(line)
        assert p.service == "app"
        assert "| | | |" in p.message

    def test_s6_init_line_parsed_as_info(self):
        """s6-rc init lines contain 'info' as a keyword; should be INFO level."""
        line = "2026-05-30T19:15:29.365915747Z s6-rc: info: service s6rc-oneshot-runner: starting"
        p = parse_line(line, "app")
        assert p.level == "INFO"
        assert "s6-rc" in p.message

    def test_always_returns_parsed_line(self):
        assert isinstance(parse_line("garbage"), ParsedLine)
        assert isinstance(parse_line(""), ParsedLine)

    def test_never_raises(self):
        """parse_line must not raise on any string input."""
        inputs = [
            "",
            "   ",
            "\x00\x01\x02\x03",
            "a" * 5000,
            "\033[31mcolored\033[m",
            "| | | pipe chars | | |",
            "http://example.com/path?query=value&other=123",
        ]
        for raw in inputs:
            try:
                parse_line(raw)
            except Exception as exc:
                pytest.fail(f"parse_line raised {type(exc).__name__} on {raw[:60]!r}")

    def test_ansi_codes_not_in_message(self):
        """ANSI escape sequences in the raw line must not leak into the message."""
        line = "2026-05-30T12:00:00Z \033[32mINFO\033[m some message here"
        p = parse_line(line)
        assert "\033" not in p.message

    def test_ts_epoch_positive_for_valid_line(self):
        line = (
            "myapp  | 2026-05-30T14:58:52.035712555Z "
            "2026-05-30T14:58:52.035632Z  INFO myapp::auth: login successful"
        )
        assert parse_line(line).ts_epoch > 0

    def test_compose_hyphenated_service_name(self):
        """Container names with hyphens should parse correctly."""
        line = "ml-worker  | 2026-05-30T12:00:00Z INFO processing batch job"
        p = parse_line(line)
        assert p.service == "ml-worker"

    def test_level_not_duplicated_in_message(self):
        """The level keyword should not appear at the start of the message."""
        line = "2026-05-30T12:00:00Z INFO service started successfully"
        p = parse_line(line)
        assert not p.message.startswith("INFO")
