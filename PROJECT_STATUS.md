# TradingAgents: current implementation and verification

Updated 2026-09-17 on Windows / Python 3.13.15.

The repository now has a runnable **paper trading desk**: local browser UI,
continuous scheduler, read-only Angel feed integration, existing research-engine
hook, deterministic risk checks, and persistent simulated orders/positions.
It does not provide live order execution through the new dashboard.

## Verified on this PC

- `.venv` with core, development and Angel One dependencies; `.env` untouched.
- Full suite: **816 passed, 2 skipped, 73 subtests passed** (18 warnings).
- Skips: optional Bedrock integration dependency; credential-dependent DeepSeek call.
- Ruff, `pip check`, CLI health and offline cycle passed.
- Headless Chromium: desktop/mobile render, start, pause, persistent emergency
  stop, reset and no console errors passed. Screenshots are in ignored `runtime_state/`.
- Built wheel includes all three dashboard assets.
- `requirements-windows-py313.lock.txt` captures this PC's tested versions; pip
  dry-run resolution passed. It is a Windows/Python 3.13 version snapshot, not a
  cross-platform or hash-verified lock. Browser testing tools are optional.

## Start the dashboard

From this repository root:

```powershell
.\.venv\Scripts\python.exe -m tradingagents.runtime
```

Open **http://127.0.0.1:8765**. The default source is explicitly labeled synthetic
DEMO data and the desk starts paused. Click **Start monitoring** to run cycles.
The engine is off by default, so it monitors without inventing a BUY signal.

```powershell
# One offline monitoring cycle, then exit
.\.venv\Scripts\python.exe -m tradingagents.runtime --once

# Capability check (does not open the server)
.\.venv\Scripts\python.exe -m tradingagents.runtime --health

# Tests and lint
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

Paper state lives in ignored `runtime_state/demo.sqlite3` or `angel.sqlite3`.
`--db` selects another ledger. Existing ledger cash is preserved; `--cash` sets
only the initial balance of a new ledger. Do not reuse a ledger for a different
instrument or source; the ledger enforces that identity.

## Implemented components

| Component | Behavior |
| --- | --- |
| `runtime/market_snapshot.py` | Exact exchange/symbol/token matching from an Angel instrument master; rejects ambiguous/non-tradable/expired instruments. Validates OHLCV, lot sizes and quote timestamps. Computes 14-bar mean true range, recent/prior ATR ratio, and 14-change trend efficiency. |
| `runtime/angel_data.py` | Lazy authenticated read-only SmartAPI quotes, VIX, candles, cash, positions and order book. Reports connected only after login. Whitelists displayed account fields; does not place/cancel broker orders. |
| `runtime/paper_service.py` | Background loop with bounded consecutive errors/backoff; session checks before fetching/after analysis; fresh-data checks before engine/commit; existing TradingAgentsGraph integration; constrained cash/ATR/lot-based sizing. |
| `runtime/paper_ledger.py` | Transactional SQLite paper fills, cash, positions, realized/unrealized P&L, daily loss/drawdown, order/audit history and persistent kill state. Unique per-bar decision keys prevent duplicate fills after retries/restart. |
| `runtime/dashboard_server.py` and `runtime/static/` | Responsive local browser status and control UI. Loopback binding, host/origin checks and control token; no live-order HTTP endpoint. |
| Legacy router/bridge | Removed automatic BUY 10 and stale date literals; correct date/asset-type engine arguments; missing engine/Hold/REVIEW blocked. Overweight/Underweight require a separate sizing policy. |
| Legacy broker helpers | Unimplemented adapters no longer fake live fills; unknown adapter names fail; configuration is not reported as authentication; direct paper paths reject invalid quantities/prices. |

### Execution rules and limitations

- One instrument per paper service. Cash-funded, long-only positions; BUY opens
  one position, SELL reduces/closes existing holdings, no shorting or pyramiding.
- Defaults: initial paper cash 100000; maximum order value 10000; maximum quantity
  50; sizing capped at 1% of equity divided by twice ATR. These are implementation
  defaults, not a validated trading strategy. Lot sizes may reduce quantity to zero.
- Reference-price simulated immediate fills: no fees, slippage, queue/partial-fill
  model or actual exchange matching. This is not a profitability backtest.
- Paper account risk is separate from read-only real broker account information;
  real account margin is not used to authorize live orders.
- Requires 29 consecutive completed one-minute bars; insufficient early-session
  history, gaps, invalid OHLCV, stale quotes/VIX or stale completed bars block the cycle.
- Quotes/VIX max age 90 seconds; completed-bar close max age 180 seconds. Slow LLM
  responses cannot trade an expired snapshot. The engine call itself is synchronous
  on the worker; stop prevents its later commit, but does not cancel the provider call.
- Regular sessions stop opening paper positions in the final five minutes and
  attempt paper flattening using fresh data. If data is missing or the session
  already closed, positions remain visible as pending, not falsely closed.
- Emergency stop halts execution and persists across restart; it does **not**
  liquidate positions. Reset is explicit and does not restart the worker.
- Kill/duplicate/account guarantees apply to this paper service. Legacy direct
  live-adapter calls do not acquire its ledger/stop state and are not wired into it.
- UI shows P&L for the ledger's last observed trading session, not an inferred
  account-wide broker daily return.

## Angel data with paper execution

Required inputs beyond `.env`:

1. Current Angel instrument master JSON (`--master`).
2. Exact tradable symbol/exchange, and exact India VIX symbol from that master.
   An index such as NIFTY 50 is not itself an orderable contract. Select the
   intended equity or specific future/option contract and verify the mapping.
3. Verified exchange calendar JSON (`--calendar`), for the current year:
   `year` integer, `holidays` list of ISO dates, optional `special_sessions`
   mapping ISO dates to `{"open": "HH:MM", "close": "HH:MM"}`.
   Obtain actual holidays/special sessions from the exchange; the program does
   not invent dates or claim that an empty list is an authoritative calendar.
4. For research, a matching analysis ticker (`--analysis-symbol`) and configured
   LLM provider credentials/settings. `--engine` explicitly enables API usage.

Example command (replace local file paths/symbols with verified inputs):

```powershell
.\.venv\Scripts\python.exe -m tradingagents.runtime --source angel --master instruments.json --calendar exchange-calendar.json --exchange NSE --symbol SBIN-EQ --vix-symbol "India VIX" --analysis-symbol SBIN.NS --engine
```

The command still executes only paper orders. Omit `--engine` for read-only
monitoring. Broker source integration follows the [official Angel Python SDK](https://github.com/angel-one/smartapi-python).

## Still pending / external verification

- Real Angel authentication, current response formats and live-market paper soak
  testing with the user's credentials, chosen contract, master data and calendar.
- Validation of the research-symbol to tradable-contract relationship for the
  intended strategy. Relative Overweight/Underweight allocation policy, multi-
  instrument portfolios and more realistic fill/cost models are not implemented.
- A separate live execution rollout: approved real-order sizing/margin checks,
  durable live submission/reconciliation (including uncertain responses), partial
  fills, cancellation and a stop/flatten policy spanning the live adapter.
  Read-only broker order-book display is not that live execution state machine.
- Automatic authoritative instrument/calendar refresh; inputs are supplied files.
- `.env` and environment-template work remain deferred as requested. The inherited
  README's `.env.example` references still need a template in a later configuration pass.

## Re-create this PC environment

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows-py313.lock.txt
```

For other Python/platform combinations, resolve the declared dependencies:
`python -m pip install -e ".[dev,angelone]"`.

Optional browser regression checks (against a disposable demo server):

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts/verify_paper_dashboard.py
```

Earlier blueprints and `done on 12 sep.txt` are historical; this file supersedes
 their old PC paths and completion claims.
