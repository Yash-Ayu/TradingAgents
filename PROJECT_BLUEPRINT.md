# TradingAgents Project Blueprint

## 1. Project Goal

Build a safe-by-default intraday trading system for Indian markets:

- NIFTY
- BANKNIFTY
- SENSEX
- MIDCPNIFTY and other supported instruments

The system combines the existing multi-agent market analysis engine with:

- Angel One SmartAPI connectivity
- Read-only market-data access
- Risk-gated paper trading
- Guarded live trading
- Market-session monitoring
- Scheduled execution
- Browser dashboard

Live order placement must never happen automatically without all safety checks.

## 2. Current Environment

- OS: Windows
- Workspace: `C:\Users\welcome\TradingAgents`
- Python environment: `.venv`
- Python version: 3.14.6
- Activate environment:

```powershell
cd C:\Users\welcome\TradingAgents
.\.venv\Scripts\Activate.ps1
```

The VS Code interpreter must be:

```text
C:\Users\welcome\TradingAgents\.venv\Scripts\python.exe
```

## 3. Environment Variables

Secrets belong only in `.env`. Never put them in Python files, chat, screenshots, commits, or reports.

Required Angel One variables:

```env
ANGEL_API_KEY=your_rotated_api_key
ANGEL_CLIENT_ID=your_client_id
ANGEL_MPIN=your_mpin
ANGEL_TOTP_SECRET=your_totp_secret
```

Live controls:

```env
ANGEL_LIVE_TRADING_ENABLED=false
ANGEL_LIVE_CONFIRMATION=
```

To explicitly enable the guarded live adapter:

```env
ANGEL_LIVE_TRADING_ENABLED=true
ANGEL_LIVE_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING
```

Keep live mode disabled during development and paper testing.

## 4. Existing Architecture

### Analysis engine

The existing `TradingAgentsGraph` and analyst modules provide multi-agent market analysis, research, debate, risk discussion, portfolio decision-making, signal processing, and reports.

Do not bypass the existing analysis engine when integrating final trading decisions.

### Runtime safety layer

Important modules:

- `tradingagents/runtime/safe_runtime.py`
  - `RiskGate`
  - `SafeTradingRuntime`
  - Blocks trades for market close, high VIX, drawdown, weak trend, or high ATR.

- `tradingagents/runtime/controller.py`
  - Combines risk evaluation and order routing.

- `tradingagents/runtime/trade_router.py`
  - Routes only after risk evaluation.

- `tradingagents/runtime/order_gateway.py`
  - Paper orders are simulated.
  - Non-paper orders are blocked unless a real-order-capable adapter is supplied.

- `tradingagents/runtime/scheduler.py`
  - `SafeExecutionLoop`
  - `MarketScheduler`

- `tradingagents/runtime/market_session.py`
  - Indian NSE/BSE session hours: 09:15 to 15:30.
  - `TradingDashboard` aggregates runtime status.

- `tradingagents/runtime/app_config.py`
  - `AppRuntimeConfig`
  - `launch_trading_app`
  - Central app startup configuration.

## 5. Broker Integration

### Angel login

- `test.py` is the standalone Angel One SmartAPI login check.
- It loads `.env`, generates TOTP with `pyotp`, logs into SmartAPI, and hides tokens.

Run:

```powershell
python test.py
```

Expected successful output:

```text
Login successful
jwtToken received: yes
feedToken received: yes
Tokens are kept hidden for security.
```

### Angel market data

- `scripts/angel_market_data.py`
- Uses `ltpData` only.
- Does not place orders.

Run:

```powershell
python scripts\angel_market_data.py
```

Optional instrument variables:

```env
ANGEL_EXCHANGE=NSE
ANGEL_TRADING_SYMBOL=NIFTY 50
ANGEL_SYMBOL_TOKEN=99926000
```

### Guarded live adapter

- `tradingagents/runtime/broker_adapter.py`
- `AngelOneBrokerAdapter` calls SmartAPI `placeOrder` only if all checks pass.

Required live checks:

1. Angel credentials present.
2. `live_trading_enabled=True`.
3. Exact confirmation string:
   `I_UNDERSTAND_LIVE_TRADING`
4. `risk_approved=True` in the order.
5. Quantity is positive and at most 50 by default.
6. Order value is at most 100000 by default.
7. Exchange, trading symbol, and symbol token are present.
8. Order type is MARKET or LIMIT.
9. LIMIT order has a positive price.
10. Broker login succeeds.
11. Market/session gate must approve before routing.

The adapter returns broker order acceptance and order ID. It does not claim that an order is filled. Fill status must be checked separately.

## 6. Completed Work

- Repository and Python environment verified.
- `.venv` selected and dependencies installed for Angel SmartAPI.
- `smartapi-python`, `pyotp`, `logzero`, `websocket-client`, and `python-dotenv` installed.
- Angel One TOTP login implemented and verified.
- Sensitive token output hidden.
- `.env` loading implemented.
- `.env` added to `.gitignore`.
- Angel One read-only NIFTY 50 market-data request verified.
- Broker adapter scaffold supports paper, Angel, Zerodha, and Upstox names.
- Real guarded Angel One order adapter implemented.
- Fake live fills blocked.
- Paper trading smoke flow implemented.
- One-cycle scheduled market monitor implemented.
- Focused safety tests passing.

## 7. Runnable Safety Checks

### Paper smoke test

```powershell
python scripts\paper_trading_smoke.py
```

Expected behavior:

- Safe snapshot: paper order accepted.
- Risky snapshot: trade blocked.
- No live order sent.

### One-cycle market monitor

```powershell
python scripts\market_monitor_once.py
```

When the Indian market is closed, expected behavior is:

```text
risk_blocked
action: flatten
reason: Market is closed
```

### Focused tests

```powershell
python -m pytest -q tests\test_safe_runtime.py tests\test_market_session.py tests\test_market_order_gateway.py tests\test_trade_router.py tests\test_angel_one_live_adapter.py
```

Last verified result: 12 tests passed for the safety/runtime slice, and 11 tests passed for the live adapter integration slice.

## 8. Pending Work

### Phase 1: Real market snapshot

- Fetch live OHLCV/quote data from Angel One.
- Normalize instrument identity and symbol tokens.
- Add timestamp and stale-data detection.
- Reject data older than the configured freshness limit.

### Phase 2: Real risk snapshot

Build a deterministic snapshot containing:

- `is_market_open`
- `vix`
- `drawdown_pct`
- `trend_strength`
- `atr_ratio`
- current price
- daily loss
- open positions
- available margin
- data timestamp

Do not infer risk from LLM text alone.

### Phase 3: Paper execution loop

- Run only in paper mode first.
- Fetch market data on a schedule.
- Evaluate risk gate.
- Generate agent signal.
- Convert signal to a constrained paper order.
- Record decision, risk reasons, and simulated result.

### Phase 4: Browser dashboard

Build a local dashboard showing:

- Connection status
- Market open/closed status
- Current LTP and timestamp
- Risk state
- Risk reasons
- Paper/live mode indicator
- Daily P&L and drawdown
- Open positions
- Last decision
- Last order result
- Emergency stop button

The dashboard must show a prominent live-mode warning and keep live mode disabled by default.

### Phase 5: Scheduled runner

- Add a long-running market-session loop.
- Start only during Indian market hours.
- Poll market data at a controlled interval.
- Stop at market close.
- Flatten/stop according to risk policy.
- Handle network/API errors with retry limits and cooldown.
- Never duplicate an order after a timeout without checking broker order status.

### Phase 6: Live rollout

Use this order:

1. Paper mode with historical/replay snapshots.
2. Paper mode during live market hours.
3. Read-only Angel account monitoring.
4. Live adapter with quantity 1 and strict value limit.
5. Manual confirmation for every first live order.
6. Only then consider carefully increasing limits.

Never start directly with unrestricted live automation.

## 9. Safety Requirements

- Default broker mode: paper.
- Default live flag: false.
- No order outside market hours.
- No order without fresh market data.
- No order if risk state is elevated or high.
- No order if drawdown limit is reached.
- No order if margin/balance is insufficient.
- No order if symbol token is missing or ambiguous.
- No unlimited retries for order requests.
- No duplicate order after uncertain network response.
- Keep an audit log for every decision and order attempt.
- Add emergency kill switch before scheduled live execution.
- Never expose tokens or credentials in logs.

## 10. Important Existing Behavior

`python scripts\angel_live_start.py` checks configuration and returns readiness. It does not itself place an order.

A `ready` result means configuration can be constructed, not that a trade was executed.

The current `test.py` is an authentication test, not the complete trading app.

## 11. Tomorrow's Recommended Sequence

1. Verify `.env` contains rotated credentials and live mode is false.
2. Run `python test.py`.
3. Run `python scripts\angel_market_data.py`.
4. Run `python scripts\paper_trading_smoke.py`.
5. Build deterministic Angel risk snapshot.
6. Add paper scheduler using real market data.
7. Add local browser dashboard.
8. Run full project test suite after installing core dependencies.
9. Keep live mode disabled until paper flow is stable.

## 12. Do Not Do

- Do not paste Python code into `.env`.
- Do not commit `.env`.
- Do not share API keys, MPIN, TOTP secrets, JWT tokens, or feed tokens.
- Do not test live orders with an empty/low-balance account and assume a failed request proves safety.
- Do not treat a broker `accepted` response as a fill.
- Do not enable live mode before paper and read-only tests pass.
