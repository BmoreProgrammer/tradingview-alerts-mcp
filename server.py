"""TradingView alerts MCP server.

Exposes Dee's TradingView alert lifecycle as MCP tools for Hermes Agent
(or Claude Desktop / Cursor / Claude Code):
  check_auth, list_alerts, list_saved_strategies, resolve_symbol,
  create_strategy_alert, pause_alerts, resume_alerts, delete_alerts,
  list_alert_fires

create_strategy_alert prefills the alert message with the JSON contract of
Dee's execution bot (bot/signal_adapters), so alerts created here speak the
bot's language. Note: when a Pine script sends its own alert() message
("alert() function calls only" in the dialog), the Pine message is what the
bot receives; the dialog message below is the fallback.

Auth: TV_USERNAME, TV_SESSIONID, TV_SESSIONID_SIGN, TV_DEVICE_T, TV_ECUID
from the environment (Hermes config.yaml `env:` block) or auth.json.
Every tool fails closed with an actionable message when auth is missing,
the session is rejected, or a filter matches nothing.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

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

mcp = FastMCP("tradingview-alerts")

# ---------------------------------------------------------------------------
# Bot webhook contract: alert message templates per strategy.
# Matches bot/signal_adapters/README.md. {{placeholders}} are TradingView
# alert placeholders, resolved at fire time.
# ---------------------------------------------------------------------------

STRATEGY_MESSAGE_TEMPLATES: dict[str, dict[str, Any]] = {
    "eq_zone": {
        "strategy_id": "eq_zone",
        "side": "{{strategy.order.action}}",
        "entry_price": "{{close}}",
        "ob_near": "{{close}}",
        "ob_far": "{{close}}",
        "buffer_pts": 5,
        "target_r": 2.0,
        "quantity": 2,
    },
    "breaker_15m": {
        "strategy_id": "breaker_15m",
        "side": "{{strategy.order.action}}",
        "entry_price": "{{close}}",
        "poi_edge": "{{close}}",
        "quantity": 6,
    },
    "fvg_cascade": {
        "strategy_id": "fvg_cascade",
        "side": "{{strategy.order.action}}",
        "entry_price": "{{close}}",
        "stop_price": "{{close}}",
        "tier": "T1",
        "quantity": 1,
    },
}


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _get_client() -> TradingViewAlertsClient:
    return TradingViewAlertsClient(TVAuth.from_env_or_file())


def _summarize(alert: dict) -> dict[str, Any]:
    return {
        "alert_id": alert.get("id"),
        "name": alert.get("name") or alert.get("alert_name"),
        "symbol": parse_tv_symbol(alert.get("symbol", "")),
        "timeframe": alert.get("resolution") or alert.get("interval"),
        "active": bool(alert.get("active")),
        "webhook_url": alert.get("webhook_url") or alert.get("webhookUrl"),
        "last_stop_reason": alert.get("last_stop_reason"),
    }


def _resolve_targets(
    client: TradingViewAlertsClient,
    alert_ids: Optional[list[int]] = None,
    symbol: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """Resolve alert_ids or filters to concrete alerts. Fail-closed."""
    if not alert_ids and not symbol and not name and not status:
        return None, (
            "Refusing to act: no target given. Pass alert_ids, or a "
            "symbol / name / status filter. This guard exists so a bare "
            "'pause alerts' can never touch your whole account."
        )
    alerts = client.list_alerts()
    if alert_ids:
        wanted = set(alert_ids)
        matched = [a for a in alerts if int(a.get("id", -1)) in wanted]
        missing = wanted - {int(a.get("id", -1)) for a in matched}
        if missing:
            return None, f"Alert id(s) not found on the account: {sorted(missing)}"
        return matched, None
    matched = alerts
    if symbol:
        s = symbol.lower()
        matched = [a for a in matched if s in parse_tv_symbol(a.get("symbol", "")).lower()]
    if name:
        n = name.lower()
        matched = [
            a for a in matched
            if n in str(a.get("name") or a.get("alert_name") or "").lower()
        ]
    if status:
        want_active = status.lower() in ("active", "on", "enabled", "firing")
        matched = [a for a in matched if bool(a.get("active")) == want_active]
    if not matched:
        return None, (
            "No alerts matched the filter "
            f"(symbol={symbol}, name={name}, status={status}). Nothing was changed. "
            "Run list_alerts to see what exists."
        )
    return matched, None


def _build_create_payload(
    *,
    symbol_doc: str,
    resolution: str,
    alert_name: str,
    message: str,
    pine_id: str,
    pine_version: str,
    inputs: dict[str, Any],
    webhook_url: Optional[str],
) -> dict[str, Any]:
    """Assemble the create_alert payload.

    Strategy inputs come from metaInfo.defaults.inputs, already keyed as
    in_0, in_1, ... Walking metaInfo.inputs by index is wrong (it starts
    with four hidden internal entries) and produces alerts that never fire.
    """
    condition = {
        "type": "strategy",
        "strategy_script": "StrategyScript@tv-scripting-101",
        "pine_id": pine_id,
        "pine_version": pine_version,
        "inputs": inputs,
    }
    payload: dict[str, Any] = {
        "symbol": symbol_doc,
        "resolution": resolution,
        "alert_name": alert_name,
        "message": message,
        "condition": condition,
    }
    if webhook_url:
        payload["webhook"] = True
        payload["webhook_url"] = webhook_url
    return payload


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def check_auth() -> dict[str, Any]:
    """Verify the TradingView session works. Reports alert counts. Run first after setup."""
    try:
        client = _get_client()
        alerts = client.list_alerts()
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    active = sum(1 for a in alerts if a.get("active"))
    return {
        "ok": True,
        "alerts_total": len(alerts),
        "alerts_active": active,
        "alerts_paused": len(alerts) - active,
    }


@mcp.tool()
def list_alerts(status: Optional[str] = None) -> dict[str, Any]:
    """List TradingView alerts with id, name, symbol, timeframe, active state,
    webhook URL and last_stop_reason. status: 'active' or 'paused' to filter."""
    try:
        client = _get_client()
        alerts = client.list_alerts()
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    if status:
        want_active = status.lower() in ("active", "on", "enabled", "firing")
        alerts = [a for a in alerts if bool(a.get("active")) == want_active]
    return {"ok": True, "count": len(alerts), "alerts": [_summarize(a) for a in alerts]}


@mcp.tool()
def list_saved_strategies() -> dict[str, Any]:
    """List Pine scripts saved on the TradingView account, flagging strategies.
    Use the script name from here when creating an alert."""
    try:
        client = _get_client()
        scripts = client.list_saved_scripts()
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    return {"ok": True, "count": len(scripts), "scripts": scripts}


@mcp.tool()
def resolve_symbol(query: str) -> dict[str, Any]:
    """Resolve free text like 'NQ', 'ES' or 'BTCUSDT' to canonical
    EXCHANGE:TICKER symbols using TradingView's own search."""
    try:
        client = _get_client()
        candidates = client.resolve_symbol(query)
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    if not candidates:
        return _err(f"No symbols found for {query!r}.")
    return {"ok": True, "candidates": candidates}


@mcp.tool()
def create_strategy_alert(
    script_name: str,
    symbol: str,
    timeframe: str,
    strategy_id: str,
    webhook_url: Optional[str] = None,
    alert_name: Optional[str] = None,
) -> dict[str, Any]:
    """Create an alert from a saved Pine strategy.

    script_name: fuzzy name of your saved Pine script (see list_saved_strategies).
    symbol: e.g. 'CME_MINI:NQ1!' or 'NQ' (resolved via TradingView search).
    timeframe: '1m', '3m', '5m', '15m', '1h', ...
    strategy_id: eq_zone | breaker_15m | fvg_cascade -- selects the bot's
        alert-message JSON template. Unknown ids get a generic template.
    webhook_url: where TradingView POSTs on fire. Defaults to the
        TV_DEFAULT_WEBHOOK_URL env var (your bot's /tv-webhook receiver).
    """
    try:
        client = _get_client()
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))

    try:
        resolution = normalize_timeframe(timeframe)
    except ValueError as exc:
        return _err(str(exc))

    # 1. resolve symbol
    try:
        candidates = client.resolve_symbol(symbol)
    except (TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    if not candidates:
        return _err(f"Could not resolve symbol {symbol!r}. Try resolve_symbol first.")
    full_symbol = candidates[0]["symbol"]
    symbol_doc = build_symbol_doc(full_symbol)

    # 2. find the saved script
    try:
        scripts = client.list_saved_scripts()
    except (TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    needle = script_name.lower()
    matches = [s for s in scripts if s.get("name") and needle in s["name"].lower()]
    if not matches:
        names = [s.get("name") for s in scripts if s.get("name")]
        return _err(
            f"No saved Pine script matches {script_name!r}. "
            f"Saved scripts: {names or 'none'}"
        )
    script = matches[0]
    if not script.get("is_strategy"):
        return _err(
            f"Script {script.get('name')!r} is not a strategy. "
            "This tool creates strategy alerts only."
        )

    # 3. compiled metadata -> default inputs (already keyed in_0, in_1, ...)
    try:
        meta = client.translate_script(script["pine_id"], str(script["version"]))
    except (TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    defaults = (meta.get("defaults") or {}).get("inputs") or {}
    if not isinstance(defaults, dict) or not defaults:
        return _err(
            f"Script {script.get('name')!r} returned no default inputs. "
            "Creating the alert without them would never fire, so refusing."
        )

    # 4. message template for the bot's webhook contract
    template = STRATEGY_MESSAGE_TEMPLATES.get(strategy_id, {"strategy_id": strategy_id})
    message = json.dumps(template)
    hook = webhook_url or os.environ.get("TV_DEFAULT_WEBHOOK_URL")
    name = alert_name or f"{script.get('name')} {full_symbol} {resolution}"

    payload = _build_create_payload(
        symbol_doc=symbol_doc,
        resolution=resolution,
        alert_name=name,
        message=message,
        pine_id=script["pine_id"],
        pine_version=str(script["version"]),
        inputs=defaults,
        webhook_url=hook,
    )
    try:
        created = client.create_alert(payload)
    except (TVAuthError, TVApiError) as exc:
        return _err(str(exc))

    new_id = created.get("id") or created.get("alert_id")
    confirmed = False
    if new_id:
        confirmed = wait_for_alert_state(client, int(new_id), want_active=True)

    result: dict[str, Any] = {
        "ok": True,
        "alert_id": new_id,
        "name": name,
        "symbol": full_symbol,
        "timeframe": resolution,
        "strategy_id": strategy_id,
        "inputs_applied": len(defaults),
        "webhook_url": hook,
        "activation": "confirmed active" if confirmed else (
            "not yet active after polling; it may still flip active, "
            "or run list_alerts / resume_alerts to force it"
        ),
    }
    if not hook:
        result["webhook_warning"] = (
            "No webhook URL set: pass webhook_url or set TV_DEFAULT_WEBHOOK_URL "
            "to your bot's /tv-webhook receiver, or this alert cannot reach the bot."
        )
    return result


@mcp.tool()
def create_price_alert(
    symbol: str,
    price: float,
    condition: str = "crossing",
    message: Optional[str] = None,
    webhook_url: Optional[str] = None,
    alert_name: Optional[str] = None,
) -> dict[str, Any]:
    """Create a plain price alert on a symbol -- no Pine script needed.

    symbol: e.g. 'CME_MINI:NQ1!' or 'NQ' (resolved via TradingView search).
    price: the trigger price, e.g. 24000.
    condition: crossing | crossing_up | crossing_down | greater_than | above |
        less_than | below. ('crossing' fires when price crosses the level
        either way.)
    message: alert message; supports TradingView placeholders like
        {{ticker}} and {{close}}. Defaults to "{{ticker}} <condition> <price>".
    webhook_url: where TradingView POSTs on fire. Defaults to the
        TV_DEFAULT_WEBHOOK_URL env var.

    NOTE: price alerts send a plain-text message, not the bot's strategy JSON
    contract -- the bot's /tv-webhook would reject it fail-closed. Use this
    for watch/notification alerts (Discord, a generic webhook, TV app push),
    not for execution. For bot execution use create_strategy_alert.
    """
    try:
        client = _get_client()
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))

    tv_type = PRICE_CONDITION_MAP.get((condition or "").strip().lower())
    if not tv_type:
        return _err(
            f"Unknown condition {condition!r}. "
            f"Valid: {', '.join(sorted(PRICE_CONDITION_MAP))}."
        )
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return _err(f"price must be a number, got {price!r}.")
    if price_f <= 0:
        return _err(f"price must be positive, got {price_f}.")

    try:
        candidates = client.resolve_symbol(symbol)
    except (TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    if not candidates:
        return _err(f"Could not resolve symbol {symbol!r}. Try resolve_symbol first.")
    full_symbol = candidates[0]["symbol"]
    symbol_doc = build_symbol_doc(full_symbol)

    cond_label = (condition or "crossing").strip().lower()
    name = alert_name or f"{full_symbol} {cond_label} {price_f:g}"
    msg = message or f"{{{{ticker}}}} {cond_label} {price_f:g} ({{{{close}}}})"
    hook = webhook_url or os.environ.get("TV_DEFAULT_WEBHOOK_URL")

    payload: dict[str, Any] = {
        "symbol": symbol_doc,
        "resolution": "1",
        "alert_name": name,
        "message": msg,
        "condition": {"type": tv_type},
        "price": price_f,
    }
    if hook:
        payload["webhook"] = True
        payload["webhook_url"] = hook

    try:
        created = client.create_alert(payload)
    except (TVAuthError, TVApiError) as exc:
        return _err(str(exc))

    new_id = created.get("id") or created.get("alert_id")
    confirmed = False
    if new_id:
        confirmed = wait_for_alert_state(client, int(new_id), want_active=True)

    result: dict[str, Any] = {
        "ok": True,
        "alert_id": new_id,
        "name": name,
        "symbol": full_symbol,
        "condition": cond_label,
        "price": price_f,
        "message": msg,
        "webhook_url": hook,
        "activation": "confirmed active" if confirmed else (
            "not yet active after polling; it may still flip active, "
            "or run list_alerts / resume_alerts to force it"
        ),
        "note": (
            "Plain-text price alert, not bot execution: the bot's /tv-webhook "
            "expects the strategy JSON contract and would reject this message "
            "fail-closed. Point the webhook at a notification receiver instead."
        ),
    }
    if not hook:
        result["webhook_warning"] = (
            "No webhook URL set: this alert will only notify via the TradingView "
            "app/email. Pass webhook_url to also POST somewhere on fire."
        )
    return result


@mcp.tool()
def pause_alerts(
    alert_ids: Optional[list[int]] = None,
    symbol: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Pause alerts (they stay on the account). Target by alert_ids, or filter
    by symbol / name / status. Refuses to run with no target."""
    try:
        client = _get_client()
        targets, error = _resolve_targets(client, alert_ids, symbol, name, status)
        if error:
            return _err(error)
        assert targets is not None
        ids = [int(a.get("id")) for a in targets]
        client.stop_alerts(ids)
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    settled = [i for i in ids if wait_for_alert_state(client, i, want_active=False)]
    return {
        "ok": True,
        "paused": ids,
        "confirmed_paused": settled,
        "pending": [i for i in ids if i not in settled],
    }


@mcp.tool()
def resume_alerts(
    alert_ids: Optional[list[int]] = None,
    symbol: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Resume paused alerts. Target by alert_ids, or filter by symbol / name /
    status. Refuses to run with no target."""
    try:
        client = _get_client()
        targets, error = _resolve_targets(client, alert_ids, symbol, name, status)
        if error:
            return _err(error)
        assert targets is not None
        ids = [int(a.get("id")) for a in targets]
        client.restart_alerts(ids)
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    settled = [i for i in ids if wait_for_alert_state(client, i, want_active=True)]
    return {
        "ok": True,
        "resumed": ids,
        "confirmed_active": settled,
        "pending": [i for i in ids if i not in settled],
    }


@mcp.tool()
def delete_alerts(
    alert_ids: Optional[list[int]] = None,
    symbol: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Permanently delete alerts. Target by alert_ids, or filter by symbol /
    name / status. Refuses to run with no target. Cannot be undone."""
    try:
        client = _get_client()
        targets, error = _resolve_targets(client, alert_ids, symbol, name, status)
        if error:
            return _err(error)
        assert targets is not None
        ids = [int(a.get("id")) for a in targets]
        client.delete_alerts(ids)
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    return {"ok": True, "deleted": ids, "warning": "Deletion is permanent."}


@mcp.tool()
def list_alert_fires(alert_id: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
    """Firing history for one alert (or recent fires overall). Useful when an
    alert goes quiet: check last_stop_reason via list_alerts alongside this."""
    try:
        client = _get_client()
        fires = client.list_fires(alert_id=alert_id, limit=limit)
    except (TVConfigError, TVAuthError, TVApiError) as exc:
        return _err(str(exc))
    return {"ok": True, "count": len(fires), "fires": fires}


if __name__ == "__main__":
    mcp.run()
