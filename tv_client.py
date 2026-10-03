"""Client for TradingView's private alerts endpoints.

TradingView publishes no public alerts API. This module drives the same
private endpoints the TradingView web app uses, authenticated with the
user's own browser session cookies.

Empirically confirmed endpoint surface (all on pricealerts.tradingview.com):
    list_alerts, create_alert, stop_alerts (pause), restart_alerts (resume),
    delete_alerts, list_fires

There is no modify endpoint: editing an alert = delete + recreate.

Alert creation for a Pine strategy is a four-step chain:
    1. symbol-search/v3 resolves the symbol and its currency-id
    2. pine-facade/list?filter=saved finds the script's pine_id and version
    3. pine-facade/translate/<id>/<version> returns compiled metadata with
       the script's default input values
    4. create_alert posts a type:"strategy" condition wrapping
       StrategyScript@tv-scripting-101

Gotchas handled here (each cost real debugging time in the community):
  - Bodies are wrapped as {"payload": {...}}. The endpoint rejects (or
    misbehaves on) unwrapped bodies -- verified live against
    pricealerts.tradingview.com by the tradingview-mcp project.
  - Activation is asynchronous: create/stop/restart return before TradingView
    flips the state. State-changing calls must poll until the state settles.
  - Strategy inputs must come from metaInfo.defaults.inputs (already keyed
    as in_0, in_1, ...). metaInfo.inputs starts with four hidden internal
    entries, so walking it by index silently builds an alert that never fires.
  - The useful metadata is nested at result.metaInfo, not metaInfo.
  - `symbol` in alert records is a JSON document inside a string, prefixed
    with "=". Parse it before showing it to anyone.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

PRICE_ALERTS_BASE = "https://pricealerts.tradingview.com"
SYMBOL_SEARCH_BASE = "https://symbol-search.tradingview.com/symbol_search/v3"
PINE_FACADE_BASE = "https://pine-facade.tradingview.com/pine-facade"

AUTH_DIR = Path(__file__).resolve().parent

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Friendly condition names -> TradingView's internal price-alert condition
# types. Mapping verified live (crossing -> cross, greater_than -> greater,
# less_than -> less). "cross" covers crossing either direction on TV's side.
PRICE_CONDITION_MAP = {
    "crossing": "cross",
    "crossing_up": "cross",
    "crossing_down": "cross",
    "greater_than": "greater",
    "above": "greater",
    "less_than": "less",
    "below": "less",
}


# ---------------------------------------------------------------------------
# Errors: fail-closed, always with an actionable message
# ---------------------------------------------------------------------------

class TVConfigError(Exception):
    """Auth material is missing. Message tells the user exactly how to fix it."""


class TVAuthError(Exception):
    """The session was rejected by TradingView (cookies expired/invalidated)."""


class TVApiError(Exception):
    """TradingView answered with an error. Carries the raw message."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_COOKIE_KEYS = ("TV_SESSIONID", "TV_SESSIONID_SIGN", "TV_DEVICE_T", "TV_ECUID")

_AUTH_HELP = (
    "TradingView session cookies are missing or incomplete. To fix:\n"
    "1. Log in to tradingview.com in your browser\n"
    "2. Open DevTools (F12) -> Application -> Cookies -> https://www.tradingview.com\n"
    "3. Copy the values of: sessionid, sessionid_sign, device_t, tv_ecuid\n"
    "4. Set env vars TV_USERNAME, TV_SESSIONID, TV_SESSIONID_SIGN, TV_DEVICE_T, TV_ECUID\n"
    "   (in your Hermes config.yaml under this server's `env:` block),\n"
    "   or copy auth.example.json to auth.json next to server.py and fill it in.\n"
    "Never commit auth.json and never paste cookies into chat."
)


@dataclass
class TVAuth:
    username: str
    sessionid: str
    sessionid_sign: str
    device_t: str
    tv_ecuid: str

    @classmethod
    def from_env_or_file(cls) -> "TVAuth":
        values: dict[str, Optional[str]] = {k: os.environ.get(k) for k in _COOKIE_KEYS}
        values["TV_USERNAME"] = os.environ.get("TV_USERNAME")

        if any(v is None for v in values.values()):
            auth_file = AUTH_DIR / "auth.json"
            if auth_file.exists():
                try:
                    file_values = json.loads(auth_file.read_text())
                except (json.JSONDecodeError, OSError) as exc:
                    raise TVConfigError(f"auth.json exists but could not be read: {exc}")
                for key in ("TV_USERNAME", *_COOKIE_KEYS):
                    if not values.get(key):
                        values[key] = file_values.get(key)

        missing = [k for k in ("TV_USERNAME", *_COOKIE_KEYS) if not values.get(k)]
        if missing:
            raise TVConfigError(f"Missing {', '.join(missing)}.\n{_AUTH_HELP}")
        return cls(
            username=values["TV_USERNAME"],  # type: ignore[arg-type]
            sessionid=values["TV_SESSIONID"],  # type: ignore[arg-type]
            sessionid_sign=values["TV_SESSIONID_SIGN"],  # type: ignore[arg-type]
            device_t=values["TV_DEVICE_T"],  # type: ignore[arg-type]
            tv_ecuid=values["TV_ECUID"],  # type: ignore[arg-type]
        )

    def cookie_header(self) -> dict[str, str]:
        return {
            "sessionid": self.sessionid,
            "sessionid_sign": self.sessionid_sign,
            "device_t": self.device_t,
            "tv_ecuid": self.tv_ecuid,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_tv_symbol(raw: Any) -> str:
    """Turn TradingView's symbol field into a plain 'EXCHANGE:TICKER'.

    The API returns a JSON document inside a string, prefixed with '=':
        ='{"symbol":"BYBIT:BTCUSDT.P","currency-id":...}'
    Plain strings pass through unchanged.
    """
    if not isinstance(raw, str):
        return str(raw)
    text = raw[1:] if raw.startswith("=") else raw
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text).get("symbol", raw)
        except json.JSONDecodeError:
            return raw
    return text


_TIMEFRAME_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "1d": "1D", "1w": "1W",
    "1": "1", "3": "3", "5": "5", "15": "15", "30": "30",
    "60": "60", "120": "120", "240": "240", "1D": "1D", "1W": "1W",
}


def normalize_timeframe(tf: str) -> str:
    """Accept '15m', '15', '1h', '60', '1D' ... -> TradingView resolution."""
    key = tf.strip().lower()
    if key in _TIMEFRAME_MAP:
        return _TIMEFRAME_MAP[key]
    upper = tf.strip().upper()
    if upper in _TIMEFRAME_MAP:
        return _TIMEFRAME_MAP[upper]
    raise ValueError(
        f"Unknown timeframe {tf!r}. Use like '1m', '5m', '15m', '1h', '4h', '1D'."
    )


def build_symbol_doc(full_symbol: str, extra: Optional[dict] = None) -> str:
    """Build the '='-prefixed JSON-in-string symbol document the API expects."""
    doc = {"symbol": full_symbol}
    if extra:
        doc.update(extra)
    return "=" + json.dumps(doc, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TradingViewAlertsClient:
    def __init__(self, auth: TVAuth, timeout: float = 20.0):
        self.auth = auth
        self._http = httpx.Client(
            cookies=auth.cookie_header(),
            headers={"User-Agent": _BROWSER_UA, "Referer": "https://www.tradingview.com/"},
            timeout=timeout,
            # Deterministic: never pick up ambient HTTP(S)_PROXY env vars.
            trust_env=False,
        )

    # -- low level ------------------------------------------------------
    def _post(self, endpoint: str, payload: dict) -> Any:
        url = f"{PRICE_ALERTS_BASE}/{endpoint}"
        # Wire format: pricealerts.* expects the body wrapped as
        # {"payload": {...}} (verified live against the real endpoint by the
        # tradingview-mcp project; unwrapped bodies are rejected/misbehave).
        # Content-Type stays application/json -- the text/plain requirement
        # only applies to in-page browser fetches avoiding CORS preflight.
        try:
            resp = self._http.post(url, json={"payload": payload})
        except httpx.HTTPError as exc:
            raise TVApiError(f"Network error calling {endpoint}: {exc}")
        if resp.status_code in (401, 403):
            raise TVAuthError(
                "TradingView rejected the session (HTTP "
                f"{resp.status_code}). Your cookies expired or were invalidated "
                "by a login on another device. Refresh them:\n" + _AUTH_HELP
            )
        try:
            data = resp.json()
        except ValueError:
            raise TVApiError(f"{endpoint} returned non-JSON (HTTP {resp.status_code})")
        if isinstance(data, dict):
            err = data.get("error") or data.get("errors")
            if err:
                text = json.dumps(err)
                if "unauthorized" in text.lower() or "auth" in text.lower():
                    raise TVAuthError(
                        "TradingView rejected the session. Refresh your cookies:\n" + _AUTH_HELP
                    )
                if "no_such_endpoint" in text.lower():
                    raise TVApiError(
                        f"Endpoint '{endpoint}' does not exist on TradingView's side. "
                        "Their private API changed; the server needs an update."
                    )
                raise TVApiError(f"{endpoint} failed: {text}")
            if "result" in data:
                return data["result"]
        return data

    # -- alerts ---------------------------------------------------------
    def list_alerts(self) -> list[dict]:
        result = self._post("list_alerts", {})
        return result if isinstance(result, list) else []

    def create_alert(self, payload: dict) -> dict:
        result = self._post("create_alert", payload)
        return result if isinstance(result, dict) else {"raw": result}

    def stop_alerts(self, alert_ids: list[int]) -> Any:
        return self._post("stop_alerts", {"alert_ids": alert_ids})

    def restart_alerts(self, alert_ids: list[int]) -> Any:
        return self._post("restart_alerts", {"alert_ids": alert_ids})

    def delete_alerts(self, alert_ids: list[int]) -> Any:
        return self._post("delete_alerts", {"alert_ids": alert_ids})

    def list_fires(self, alert_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        payload: dict[str, Any] = {"limit": limit}
        if alert_id is not None:
            payload["alert_id"] = alert_id
        result = self._post("list_fires", payload)
        return result if isinstance(result, list) else []

    # -- discovery ------------------------------------------------------
    def resolve_symbol(self, query: str, limit: int = 8) -> list[dict]:
        """Resolve free text to canonical symbols via TradingView's own search."""
        try:
            resp = self._http.get(
                SYMBOL_SEARCH_BASE,
                params={"text": query, "hl": "1", "lang": "en", "domain": "production"},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TVApiError(f"Symbol search failed for {query!r}: {exc}")
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        out = []
        for entry in symbols[:limit]:
            full = entry.get("symbol") or ""
            if entry.get("exchange"):
                full = f"{entry['exchange']}:{full}" if ":" not in full else full
            out.append({
                "symbol": full,
                "description": entry.get("description", ""),
                "type": entry.get("type", ""),
                "exchange": entry.get("exchange", ""),
            })
        return out

    def list_saved_scripts(self) -> list[dict]:
        """Pine scripts saved on the account (strategies flagged)."""
        try:
            resp = self._http.get(f"{PINE_FACADE_BASE}/list", params={"filter": "saved"})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TVApiError(f"Could not list saved Pine scripts: {exc}")
        scripts = data.get("result") or data.get("scripts") or []
        out = []
        for s in scripts if isinstance(scripts, list) else []:
            out.append({
                "name": s.get("scriptName") or s.get("name"),
                "pine_id": s.get("scriptIdPart") or s.get("pineId"),
                "version": s.get("version"),
                "is_strategy": (s.get("scriptType") or s.get("type") or "").lower() == "strategy",
                "author": s.get("author"),
            })
        return out

    def translate_script(self, pine_id: str, version: str) -> dict:
        """Compiled metadata for a saved script; defaults live at result.metaInfo."""
        url = f"{PINE_FACADE_BASE}/translate/{pine_id}/{version}"
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TVApiError(f"Could not translate Pine script {pine_id}: {exc}")
        meta = (data.get("result") or {}).get("metaInfo")
        if not meta:
            raise TVApiError(f"No compiled metadata for script {pine_id} v{version}")
        return meta


def wait_for_alert_state(
    client: TradingViewAlertsClient,
    alert_id: int,
    want_active: bool,
    timeout: float = 12.0,
    interval: float = 1.5,
) -> bool:
    """Poll list_alerts until the alert reaches the wanted active state.

    Activation is asynchronous on TradingView's side: create/stop/restart
    return before the state flips. Returns True if the state settled in
    time, False on timeout (the change may still land later).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for alert in client.list_alerts():
            if int(alert.get("id", -1)) == alert_id:
                if bool(alert.get("active")) == want_active:
                    return True
        time.sleep(interval)
    return False
