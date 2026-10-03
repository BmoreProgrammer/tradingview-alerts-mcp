"""Tests for the TradingView alerts MCP server.

Live TradingView calls are never made here: the HTTP layer is faked and the
server's client factory is monkeypatched. What IS verified: fail-closed
behavior (no auth, no target, no match, bad inputs), payload construction
(inputs keyed from defaults, '=' symbol doc), polling logic, and that every
tool is registered with the MCP app.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server
from tv_client import (
    PRICE_CONDITION_MAP,
    TVApiError,
    TVAuth,
    TVAuthError,
    TVConfigError,
    TradingViewAlertsClient,
    build_symbol_doc,
    normalize_timeframe,
    parse_tv_symbol,
    wait_for_alert_state,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeHTTP:
    """Stands in for httpx.Client. Records posts, serves scripted responses."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.script: dict[str, FakeResponse] = {}

    def post(self, url, json=None):
        self.posts.append((url, json or {}))
        endpoint = url.rstrip("/").rsplit("/", 1)[-1]
        return self.script.get(endpoint, FakeResponse(200, {"result": []}))

    def get(self, url, params=None):
        self.gets.append((url, params or {}))
        return FakeResponse(200, {"symbols": []})


ALERTS = [
    {"id": 11, "name": "eq NQ 5m", "symbol": '={"symbol":"CME_MINI:NQ1!"}',
     "resolution": "5", "active": True, "webhook_url": "https://bot/x"},
    {"id": 22, "name": "breaker ES 15m", "symbol": "CME_MINI:ES1!",
     "resolution": "15", "active": False, "last_stop_reason": "auto"},
]


class FakeClient:
    """Stands in for TradingViewAlertsClient at the server layer."""

    def __init__(self, alerts=None):
        self.alerts = [dict(a) for a in (alerts if alerts is not None else ALERTS)]
        self.stopped: list[int] = []
        self.restarted: list[int] = []
        self.deleted: list[int] = []
        self.created: list[dict] = []
        self.scripts = [
            {"name": "eq_zone_v3", "pine_id": "PUB;abc1", "version": "3",
             "is_strategy": True, "author": "Dee"},
            {"name": "My RSI Study", "pine_id": "PUB;xyz9", "version": "1",
             "is_strategy": False, "author": "Dee"},
        ]

    def list_alerts(self):
        return [dict(a) for a in self.alerts]

    def list_saved_scripts(self):
        return self.scripts

    def resolve_symbol(self, query):
        return [{"symbol": "CME_MINI:NQ1!", "description": "Nasdaq 100",
                 "type": "futures", "exchange": "CME_MINI"}]

    def translate_script(self, pine_id, version):
        return {"defaults": {"inputs": {"in_0": True, "in_1": 14}}}

    def create_alert(self, payload):
        self.created.append(payload)
        return {"id": 99, "active": False}

    def stop_alerts(self, ids):
        self.stopped.extend(ids)

    def restart_alerts(self, ids):
        self.restarted.extend(ids)

    def delete_alerts(self, ids):
        self.deleted.extend(ids)

    def list_fires(self, alert_id=None, limit=50):
        return [{"alert_id": alert_id or 11, "fired_at": "2026-10-01T06:00:00Z"}]


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(server, "_get_client", lambda: client)
    # polling is timing-sensitive; tests control it explicitly elsewhere
    monkeypatch.setattr(server, "wait_for_alert_state", lambda *a, **k: True)
    return client


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_parse_tv_symbol_json_in_string():
    raw = '={"symbol":"BYBIT:BTCUSDT.P","currency-id":123}'
    assert parse_tv_symbol(raw) == "BYBIT:BTCUSDT.P"


def test_parse_tv_symbol_plain_passthrough():
    assert parse_tv_symbol("CME_MINI:ES1!") == "CME_MINI:ES1!"


def test_parse_tv_symbol_malformed_json_returns_raw():
    raw = '={"symbol":oops'
    assert parse_tv_symbol(raw) == raw


def test_normalize_timeframe():
    assert normalize_timeframe("15m") == "15"
    assert normalize_timeframe("1h") == "60"
    assert normalize_timeframe("4h") == "240"
    assert normalize_timeframe("1D") == "1D"
    assert normalize_timeframe("5") == "5"


def test_normalize_timeframe_invalid():
    with pytest.raises(ValueError):
        normalize_timeframe("fortnight")


def test_build_symbol_doc_roundtrip():
    doc = build_symbol_doc("CME_MINI:NQ1!")
    assert doc.startswith("=")
    assert json.loads(doc[1:])["symbol"] == "CME_MINI:NQ1!"


# ---------------------------------------------------------------------------
# Auth: fail-closed with actionable message
# ---------------------------------------------------------------------------

def test_auth_missing_raises_with_help(monkeypatch, tmp_path):
    for key in ("TV_USERNAME", "TV_SESSIONID", "TV_SESSIONID_SIGN",
                "TV_DEVICE_T", "TV_ECUID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("tv_client.AUTH_DIR", tmp_path)  # no auth.json there
    with pytest.raises(TVConfigError) as excinfo:
        TVAuth.from_env_or_file()
    assert "sessionid" in str(excinfo.value).lower()


def test_post_unauthorized_becomes_auth_error():
    auth = TVAuth("u", "s", "ss", "d", "e")
    client = TradingViewAlertsClient(auth)
    client._http = FakeHTTP()
    client._http.script["list_alerts"] = FakeResponse(401, {})
    with pytest.raises(TVAuthError) as excinfo:
        client.list_alerts()
    assert "cookies" in str(excinfo.value).lower()


def test_post_api_error_surfaces_message():
    auth = TVAuth("u", "s", "ss", "d", "e")
    client = TradingViewAlertsClient(auth)
    client._http = FakeHTTP()
    client._http.script["delete_alerts"] = FakeResponse(200, {"error": "boom_detail"})
    with pytest.raises(TVApiError) as excinfo:
        client.delete_alerts([1])
    assert "boom_detail" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Target resolution: the guards that prevent acting on the whole account
# ---------------------------------------------------------------------------

def test_pause_refuses_with_no_target(fake):
    result = server.pause_alerts()
    assert result["ok"] is False
    assert "no target" in result["error"].lower()
    assert fake.stopped == []


def test_pause_errors_when_filter_matches_nothing(fake):
    result = server.pause_alerts(symbol="ZZZZ:NOTREAL")
    assert result["ok"] is False
    assert "no alerts matched" in result["error"].lower()
    assert fake.stopped == []


def test_pause_by_symbol_filter(fake):
    result = server.pause_alerts(symbol="NQ1!")
    assert result["ok"] is True
    assert result["paused"] == [11]
    assert fake.stopped == [11]


def test_delete_by_name_filter(fake):
    result = server.delete_alerts(name="breaker")
    assert result["ok"] is True
    assert result["deleted"] == [22]
    assert fake.deleted == [22]


def test_resume_unknown_id_errors(fake):
    result = server.resume_alerts(alert_ids=[12345])
    assert result["ok"] is False
    assert "not found" in result["error"].lower()
    assert fake.restarted == []


# ---------------------------------------------------------------------------
# create_strategy_alert
# ---------------------------------------------------------------------------

def test_create_happy_path_prefills_bot_contract(fake, monkeypatch):
    monkeypatch.setenv("TV_DEFAULT_WEBHOOK_URL", "https://bot.example.com/tv-webhook")
    result = server.create_strategy_alert(
        script_name="eq_zone", symbol="NQ", timeframe="5m", strategy_id="eq_zone")
    assert result["ok"] is True
    assert result["alert_id"] == 99
    assert result["activation"] == "confirmed active"
    assert result["inputs_applied"] == 2
    assert result["webhook_url"] == "https://bot.example.com/tv-webhook"

    payload = fake.created[0]
    assert payload["symbol"].startswith("=")
    assert json.loads(payload["symbol"][1:])["symbol"] == "CME_MINI:NQ1!"
    assert payload["resolution"] == "5"
    # inputs pass through keyed from defaults (never re-indexed)
    assert payload["condition"]["inputs"] == {"in_0": True, "in_1": 14}
    assert payload["condition"]["type"] == "strategy"
    # bot contract in the message
    message = json.loads(payload["message"])
    assert message["strategy_id"] == "eq_zone"
    assert message["target_r"] == 2.0


def test_create_refuses_non_strategy_script(fake):
    result = server.create_strategy_alert(
        script_name="RSI Study", symbol="NQ", timeframe="5m", strategy_id="eq_zone")
    assert result["ok"] is False
    assert "not a strategy" in result["error"].lower()
    assert fake.created == []


def test_create_refuses_unknown_script(fake):
    result = server.create_strategy_alert(
        script_name="nope", symbol="NQ", timeframe="5m", strategy_id="eq_zone")
    assert result["ok"] is False
    assert "no saved pine script matches" in result["error"].lower()


def test_create_refuses_when_no_default_inputs(fake, monkeypatch):
    monkeypatch.setattr(
        FakeClient, "translate_script",
        lambda self, pine_id, version: {"defaults": {"inputs": {}}})
    result = server.create_strategy_alert(
        script_name="eq_zone", symbol="NQ", timeframe="5m", strategy_id="eq_zone")
    assert result["ok"] is False
    assert "never fire" in result["error"].lower()
    assert fake.created == []


def test_create_without_webhook_warns(fake, monkeypatch):
    monkeypatch.delenv("TV_DEFAULT_WEBHOOK_URL", raising=False)
    result = server.create_strategy_alert(
        script_name="eq_zone", symbol="NQ", timeframe="5m", strategy_id="eq_zone")
    assert result["ok"] is True
    assert "webhook_warning" in result
    assert "webhook" not in fake.created[0]


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def test_wait_for_alert_state_settles():
    calls = {"n": 0}

    class Flipper:
        def list_alerts(self):
            calls["n"] += 1
            return [{"id": 7, "active": calls["n"] >= 3}]

    assert wait_for_alert_state(Flipper(), 7, True, timeout=5, interval=0.01) is True
    assert calls["n"] >= 3


def test_wait_for_alert_state_times_out():
    class Never:
        def list_alerts(self):
            return [{"id": 7, "active": False}]

    assert wait_for_alert_state(Never(), 7, True, timeout=0.05, interval=0.01) is False


# ---------------------------------------------------------------------------
# Read tools + registration
# ---------------------------------------------------------------------------

def test_list_alerts_parses_symbol(fake):
    result = server.list_alerts()
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["alerts"][0]["symbol"] == "CME_MINI:NQ1!"


def test_list_alerts_status_filter(fake):
    result = server.list_alerts(status="paused")
    assert result["count"] == 1
    assert result["alerts"][0]["alert_id"] == 22


def test_list_alert_fires(fake):
    result = server.list_alert_fires(alert_id=11)
    assert result["ok"] is True
    assert result["fires"][0]["alert_id"] == 11


def test_check_auth_reports_counts(fake):
    result = server.check_auth()
    assert result == {"ok": True, "alerts_total": 2,
                      "alerts_active": 1, "alerts_paused": 1}


def test_all_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {"check_auth", "list_alerts", "list_saved_strategies",
                "resolve_symbol", "create_strategy_alert", "create_price_alert",
                "pause_alerts", "resume_alerts", "delete_alerts", "list_alert_fires"}
    assert expected <= names, f"missing: {expected - names}"


# ---------------------------------------------------------------------------
# Price alerts
# ---------------------------------------------------------------------------

def test_post_wraps_body_in_payload():
    """pricealerts.* expects {"payload": {...}}, not the bare body."""
    auth = TVAuth("u", "s", "ss", "d", "e")
    client = TradingViewAlertsClient(auth)
    client._http = FakeHTTP()
    client.delete_alerts([1])
    url, body = client._http.posts[0]
    assert url.endswith("/delete_alerts")
    assert body == {"payload": {"alert_ids": [1]}}


def test_price_condition_map():
    assert PRICE_CONDITION_MAP["crossing"] == "cross"
    assert PRICE_CONDITION_MAP["crossing_down"] == "cross"
    assert PRICE_CONDITION_MAP["greater_than"] == "greater"
    assert PRICE_CONDITION_MAP["above"] == "greater"
    assert PRICE_CONDITION_MAP["less_than"] == "less"
    assert PRICE_CONDITION_MAP["below"] == "less"


def test_create_price_alert_happy_path(fake):
    result = server.create_price_alert(symbol="NQ", price=24000, condition="crossing")
    assert result["ok"] is True
    assert result["alert_id"] == 99
    assert result["symbol"] == "CME_MINI:NQ1!"
    assert result["condition"] == "crossing"
    assert result["price"] == 24000.0
    assert result["activation"] == "confirmed active"
    assert "bot" in result["note"].lower() or "execution" in result["note"].lower()

    payload = fake.created[0]
    assert payload["symbol"].startswith("=")
    assert json.loads(payload["symbol"][1:])["symbol"] == "CME_MINI:NQ1!"
    assert payload["condition"] == {"type": "cross"}
    assert payload["price"] == 24000.0
    assert "{{ticker}}" in payload["message"]


def test_create_price_alert_condition_aliases(fake):
    result = server.create_price_alert(symbol="NQ", price=24000, condition="greater_than")
    assert result["ok"] is True
    assert fake.created[0]["condition"] == {"type": "greater"}

    result = server.create_price_alert(symbol="NQ", price=24000, condition="below")
    assert result["ok"] is True
    assert fake.created[1]["condition"] == {"type": "less"}


def test_create_price_alert_rejects_bad_condition(fake):
    result = server.create_price_alert(symbol="NQ", price=24000, condition="moonshot")
    assert result["ok"] is False
    assert "unknown condition" in result["error"].lower()
    assert fake.created == []


def test_create_price_alert_rejects_bad_price(fake):
    for bad in (0, -5):
        result = server.create_price_alert(symbol="NQ", price=bad)
        assert result["ok"] is False
        assert "positive" in result["error"].lower()
    assert fake.created == []
