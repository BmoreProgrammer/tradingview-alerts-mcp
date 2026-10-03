# TradingView Alerts MCP Server

Manage your TradingView alerts from Hermes Agent (or Claude Desktop / Cursor /
Claude Code) in plain language: create Pine **strategy** alerts wired to your
trading bot's webhook, list them, pause/resume them, inspect firing history,
delete them.

```
"Pause every NQ alert."
"Create an alert from my eq_zone strategy on CME_MINI:NQ1! 5m, webhook to my bot."
"Which of my alerts are paused, and why did they stop?"
```

Built for Dee's execution bot: `create_strategy_alert` prefills the alert
message with the bot's JSON contract (`bot/signal_adapters/README.md`) for
`eq_zone`, `breaker_15m`, and `fvg_cascade`.

> **Not official.** TradingView publishes no alerts API. This drives the same
> private endpoints the web app uses (`pricealerts.tradingview.com`), as your
> own logged-in user, at a far lower rate than clicking through the UI. Those
> endpoints can change without notice. Your decision and risk.

## Setup

```bash
cd tradingview-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 1. Session cookies

TradingView has no alert API key; auth is your logged-in browser session.
The server needs four cookies plus your username. **Do this yourself in your
own browser; never paste cookies into chat.**

1. Log in to tradingview.com
2. DevTools (F12) → Application → Storage → Cookies → `https://www.tradingview.com`
3. Copy the values of `sessionid`, `sessionid_sign`, `device_t`, `tv_ecuid`
4. Note your TradingView username exactly as on your profile

Cookies expire, and logging in on another device can invalidate them. When
that happens every tool returns a clear "session rejected" error — just
refresh the cookies. Run `check_auth` anytime to verify.

### 2. Plug into Hermes Agent

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  tradingview-alerts:
    command: "/absolute/path/to/tradingview-mcp/.venv/bin/python"
    args: ["/absolute/path/to/tradingview-mcp/server.py"]
    env:
      TV_USERNAME: "YourTradingViewUsername"
      TV_SESSIONID: "paste-sessionid-cookie"
      TV_SESSIONID_SIGN: "paste-sessionid_sign-cookie"
      TV_DEVICE_T: "paste-device_t-cookie"
      TV_ECUID: "paste-tv_ecuid-cookie"
      # optional: default webhook for created alerts (your bot's receiver)
      TV_DEFAULT_WEBHOOK_URL: "https://your-bot-host:8100/tv-webhook"
```

Use absolute paths (Hermes does not inherit your shell `PATH`). Restart
Hermes; on startup it discovers the tools and registers them as
`mcp__tradingview-alerts__*`. The `hermes mcp` picker lists them.

Alternative to env vars: copy `auth.example.json` to `auth.json` next to
`server.py` and fill it in (it is gitignored; never commit it).

### 3. Verify

Ask Hermes: `check_auth` — it should report your alert counts. If it reports
a session error, your cookies need refreshing (step 1).

## Tools

| Tool | What it does |
|---|---|
| `check_auth` | Verify the session; report alert counts |
| `list_alerts` | Every alert: id, name, symbol, timeframe, active, webhook, `last_stop_reason` |
| `list_saved_strategies` | Your saved Pine scripts, flagging strategies |
| `resolve_symbol` | `NQ` → `CME_MINI:NQ1!` via TradingView's own search |
| `create_strategy_alert` | New alert from a saved Pine strategy, optional webhook |
| `create_price_alert` | Plain price alert (no Pine script): crossing / greater_than / less_than |
| `pause_alerts` | Stop alerts firing, keep them on the account |
| `resume_alerts` | Reactivate paused alerts |
| `delete_alerts` | Permanently delete alerts |
| `list_alert_fires` | Firing history |

`pause_alerts`, `resume_alerts`, `delete_alerts` target alerts by explicit
`alert_ids` or by `symbol` / `name` / `status` filter. They **refuse to run
with no target** (so a bare "pause alerts" can never nuke the account) and
**error when a filter matches nothing** instead of silently doing nothing.

`create_strategy_alert(script_name, symbol, timeframe, strategy_id, webhook_url?, alert_name?)`
runs the four-step chain: symbol search → saved-script lookup → compiled
metadata (default inputs, correctly keyed) → `create_alert` with a
`type: "strategy"` condition, then polls until TradingView flips the alert
active and reports `confirmed`. `strategy_id` picks the message template:

- `eq_zone` → `{"strategy_id":"eq_zone","side":"{{strategy.order.action}}",...,"target_r":2.0}`
- `breaker_15m` → `{"strategy_id":"breaker_15m",...,"quantity":6}`
- `fvg_cascade` → `{"strategy_id":"fvg_cascade",...,"tier":"T1"}`

`{{...}}` are TradingView alert placeholders resolved at fire time. Note:
when your Pine script sends its own `alert()` message ("alert() function
calls only" in the dialog), the Pine message is what the bot receives — the
dialog message above is the fallback. Keep the Pine JSON contracts in
`bot/signal_adapters/README.md` as the source of truth.

`create_price_alert(symbol, price, condition?, message?, webhook_url?, alert_name?)`
creates a simple level alert without any Pine script: `condition` is one of
`crossing` (either direction), `crossing_up`, `crossing_down`, `greater_than`
/ `above`, `less_than` / `below`. It resolves the symbol, posts a
`cross` / `greater` / `less` condition with the trigger price, then polls
until the alert is active.

Price alerts send a **plain-text message, not the bot's strategy JSON** — the
bot's `/tv-webhook` would reject it fail-closed, so point the webhook at a
notification receiver (Discord, ntfy, a generic endpoint), not at the bot.
For execution, use `create_strategy_alert`. The price-alert wire format
(condition types, `{"payload": ...}` body wrapping) comes from a
community project that verified it live against TradingView's private API;
it has not been live-tested from this server yet, so if `create_price_alert`
returns an API error, paste the error back for a fix.

There is **no modify endpoint** on TradingView's side: changing an alert
means delete + recreate (it gets a new `alert_id`).

## Fail-closed by design

- No cookies / incomplete auth → every tool returns how to fix it, nothing runs.
- Session rejected (expired cookies, login elsewhere) → clear refresh instructions.
- Pause/resume/delete with no target → refused.
- Filter matches nothing → error, nothing changed.
- Alert creation with no compiled default inputs → refused (an alert built
  without them is accepted by TradingView and then **never fires**).
- API errors surface TradingView's raw message instead of a silent `ok`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

26 tests, all offline (HTTP layer faked). Live verification needs your
session: run `check_auth` after setup.
