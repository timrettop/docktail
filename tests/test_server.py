"""
test_server.py — Tests for the FastAPI backend (server.py).

Uses FastAPI's synchronous TestClient so no async test runner is needed.
Docker subprocess calls are mocked throughout so the tests run in CI
without a Docker daemon.
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import server
from core import ParsedLine
from server import (
    _bucket,
    app,
    container_css_color,
    date_sep_msg,
    line_to_msg,
    list_containers,
)

client = TestClient(app, raise_server_exceptions=True)

# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_CONTAINERS = [
    {"name": "myapp",    "status": "Up 2 hours", "color": container_css_color("myapp")},
    {"name": "postgres", "status": "Up 2 hours", "color": container_css_color("postgres")},
]


def _mock_ps_output(*names: str) -> str:
    """Build fake 'docker ps' tab-separated output."""
    return "\n".join(f"{n}\tUp 1 hour" for n in names) + "\n"


# ── GET /api/health ───────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_200(self):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_status_ok(self):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_version_present(self):
        assert "version" in client.get("/api/health").json()


# ── GET /api/containers ───────────────────────────────────────────────────────

class TestContainersEndpoint:
    def test_returns_200(self):
        with patch("server.list_containers", return_value=MOCK_CONTAINERS):
            r = client.get("/api/containers")
        assert r.status_code == 200

    def test_response_has_containers_key(self):
        with patch("server.list_containers", return_value=MOCK_CONTAINERS):
            r = client.get("/api/containers")
        assert "containers" in r.json()

    def test_returns_list(self):
        with patch("server.list_containers", return_value=MOCK_CONTAINERS):
            r = client.get("/api/containers")
        assert isinstance(r.json()["containers"], list)

    def test_each_item_has_required_fields(self):
        with patch("server.list_containers", return_value=MOCK_CONTAINERS):
            items = client.get("/api/containers").json()["containers"]
        for item in items:
            assert "name"   in item
            assert "status" in item
            assert "color"  in item

    def test_color_is_css_hex(self):
        with patch("server.list_containers", return_value=MOCK_CONTAINERS):
            items = client.get("/api/containers").json()["containers"]
        for item in items:
            assert item["color"].startswith("#")
            assert len(item["color"]) == 7

    def test_empty_when_docker_unavailable(self):
        with patch("server.list_containers", return_value=[]):
            r = client.get("/api/containers")
        assert r.json()["containers"] == []

    def test_project_param_passed_through(self):
        with patch("server.list_containers", return_value=[]) as mock:
            client.get("/api/containers?project=mystack")
        mock.assert_called_once_with("mystack")


# ── list_containers() ─────────────────────────────────────────────────────────

class TestListContainers:
    def test_parses_docker_ps_output(self):
        mock_run = MagicMock()
        mock_run.return_value.stdout = _mock_ps_output("myapp", "postgres")
        with patch("server.subprocess.run", return_value=mock_run.return_value):
            with patch("server._docker_cmd", return_value=["docker"]):
                result = list_containers()
        assert len(result) == 2
        assert result[0]["name"] == "myapp"
        assert result[1]["name"] == "postgres"

    def test_each_item_has_color(self):
        mock_run = MagicMock()
        mock_run.return_value.stdout = _mock_ps_output("myapp")
        with patch("server.subprocess.run", return_value=mock_run.return_value):
            with patch("server._docker_cmd", return_value=["docker"]):
                result = list_containers()
        assert result[0]["color"].startswith("#")

    def test_returns_empty_on_timeout(self):
        with patch("server.subprocess.run",
                   side_effect=__import__('subprocess').TimeoutExpired([], 5)):
            with patch("server._docker_cmd", return_value=["docker"]):
                assert list_containers() == []

    def test_returns_empty_on_docker_not_found(self):
        with patch("server._docker_cmd", side_effect=RuntimeError("docker not found")):
            assert list_containers() == []

    def test_skips_blank_lines(self):
        mock_run = MagicMock()
        mock_run.return_value.stdout = "myapp\tUp 1 hour\n\n\n"
        with patch("server.subprocess.run", return_value=mock_run.return_value):
            with patch("server._docker_cmd", return_value=["docker"]):
                result = list_containers()
        assert len(result) == 1


# ── WebSocket /ws/logs ────────────────────────────────────────────────────────

class TestWebSocket:
    def _make_session(self, container_list):
        """Helper: connect and capture the first message."""
        with patch("server.list_containers",
                   return_value=[{"name": n, "color": "#fff", "status": "Up"}
                                 for n in container_list]):
            with patch.object(server.LogSession, "start"):   # don't spawn processes
                containers_param = ",".join(container_list)
                with client.websocket_connect(
                    f"/ws/logs?containers={containers_param}"
                ) as ws:
                    return ws.receive_json()

    def test_sends_connected_on_open(self):
        msg = self._make_session(["myapp"])
        assert msg["type"] == "connected"

    def test_connected_message_has_containers(self):
        msg = self._make_session(["myapp", "postgres"])
        assert "containers" in msg
        assert len(msg["containers"]) == 2

    def test_connected_containers_have_name_and_color(self):
        msg = self._make_session(["myapp"])
        c = msg["containers"][0]
        assert "name"  in c
        assert "color" in c

    def test_no_containers_sends_error(self):
        with patch("server.list_containers", return_value=[]):
            with client.websocket_connect("/ws/logs") as ws:
                msg = ws.receive_json()
        assert msg["type"] == "error"

    def test_error_message_has_message_field(self):
        with patch("server.list_containers", return_value=[]):
            with client.websocket_connect("/ws/logs") as ws:
                msg = ws.receive_json()
        assert "message" in msg


# ── Message serialisation ─────────────────────────────────────────────────────

class TestLineToMsg:
    def _line(self, **kwargs) -> ParsedLine:
        defaults = dict(
            raw="2026-05-30T12:00:00Z INFO myapp::sync: job done",
            service="myapp", level="INFO", timestamp="12:00:00.000",
            ts_epoch=1748606400.0, target="myapp::sync", message="job done",
        )
        defaults.update(kwargs)
        return ParsedLine(**defaults)

    def test_type_is_line(self):
        assert line_to_msg(self._line())["type"] == "line"

    def test_has_required_fields(self):
        msg = line_to_msg(self._line())
        for field in ("type", "service", "level", "timestamp", "ts_epoch",
                      "target", "message", "color"):
            assert field in msg

    def test_level_preserved(self):
        assert line_to_msg(self._line(level="WARN"))["level"] == "WARN"

    def test_service_preserved(self):
        assert line_to_msg(self._line(service="postgres"))["service"] == "postgres"

    def test_message_preserved(self):
        assert line_to_msg(self._line(message="hello world"))["message"] == "hello world"

    def test_color_is_css_hex(self):
        color = line_to_msg(self._line())["color"]
        assert color.startswith("#")

    def test_empty_service_gets_fallback_color(self):
        color = line_to_msg(self._line(service=""))["color"]
        assert color.startswith("#")

    def test_ts_epoch_used_for_smart_timestamp(self):
        # ts_epoch should produce a formatted timestamp, not raw ISO string
        msg = line_to_msg(self._line(ts_epoch=1748606400.0))
        assert "T" not in msg["timestamp"]   # should be HH:MM:SS not ISO

    def test_zero_ts_epoch_falls_back_to_timestamp_field(self):
        msg = line_to_msg(self._line(ts_epoch=0.0, timestamp="14:23:45.123"))
        assert msg["timestamp"] == "14:23:45.123"


class TestDateSepMsg:
    def test_type_is_date_sep(self):
        assert date_sep_msg(date(2026, 5, 30))["type"] == "date_sep"

    def test_label_contains_day_name(self):
        label = date_sep_msg(date(2026, 5, 30))["label"]
        assert "Saturday" in label

    def test_label_contains_month(self):
        label = date_sep_msg(date(2026, 5, 30))["label"]
        assert "May" in label

    def test_label_contains_year(self):
        label = date_sep_msg(date(2026, 5, 30))["label"]
        assert "2026" in label


class TestBucket:
    def test_error(self):       assert _bucket("ERROR")   == "ERROR"
    def test_warn(self):        assert _bucket("WARN")    == "WARN"
    def test_warning(self):     assert _bucket("WARNING") == "WARN"
    def test_info(self):        assert _bucket("INFO")    == "INFO"
    def test_debug(self):       assert _bucket("DEBUG")   == "DEBUG"
    def test_trace(self):       assert _bucket("TRACE")   == "DEBUG"
    def test_unknown(self):     assert _bucket("NOTICE")  == "INFO"


# ── Container CSS colours ─────────────────────────────────────────────────────

class TestContainerCssColor:
    def test_returns_css_hex(self):
        color = container_css_color("myapp")
        assert color.startswith("#")
        assert len(color) == 7

    def test_stable_for_same_name(self):
        assert container_css_color("myapp") == container_css_color("myapp")

    def test_different_names_may_differ(self):
        # Not guaranteed (palette cycles) but the first 12 unique names differ
        colors = [container_css_color(f"container-{i}") for i in range(12)]
        assert len(set(colors)) > 1

    def test_cycles_after_palette_exhausted(self):
        # Container 12 wraps back to slot 0 (palette has 12 entries) — just
        # verify it still returns a valid hex string, not that it matches c0.
        c12 = container_css_color("container-12")
        assert c12.startswith("#")
