# BTC Polymarket Paper Monitor v2

This project is a conservative, **monitor-only** starting point intended for Render.
It does not place live orders or simulated wagers. It shows spot prices, checks
cross-source discrepancies, monitors a baseline trend, and attempts market discovery.

## Important caveats
- Binance and Coinbase prices are spot-price cross-checks, not the Polymarket
  resolution oracle. The exact resolution source must be verified in each market's
  rules before any trading software uses it.
- Gamma market discovery can miss markets due to endpoint changes, schema, or
  search/filter limitations. This app fails closed: no market means blocked.
- The UP/DOWN signal is a simple MA/momentum illustration, not a validated model,
  not investment advice, and not suitable for live trading.
- This version intentionally has no order placement and no live trading secrets.

## Deploy to Render from iPhone
1. Download/extract this ZIP.
2. Upload the folder to a GitHub repository (Render deploys from GitHub).
3. In Render, create **New Web Service**, connect the repo.
4. Runtime: Python. Build command: `pip install -r requirements.txt`.
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
6. Health path: `/api/health`.
7. Deploy. Visit the service URL and `/api/status`.

## Dashboard/API
- `/` Dashboard
- `/api/health` health check
- `/api/status` full current state
- `/api/price` prices + discrepancy validation
- `/api/market` market discovery state
- `/api/prediction` baseline monitor signal
- `/api/paper/stats` monitor stats (no simulated bets)

## Environment variables
- `PAPER_BALANCE` default 1000
- `PAPER_STAKE` default 10 (display only)
- `MAX_PRICE_DIVERGENCE_PCT` default 0.15
- `MIN_SECONDS_TO_END` default 35

## Safety
This is monitor-only. Do not turn it into a live trading system without a proper
market-resolution source, orderbook validation, robust backtests, calibration,
risk controls, and test coverage.
