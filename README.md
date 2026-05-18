# scalp-sim

A browser-based intraday scalping simulator with mobile layout, hosted as a static site on GitHub Pages.

Play recorded 5-second-bar trading sessions from real IBKR data, with realistic friction (spread + slippage + commission) modeled per trade. Practice scalp setups without risking capital.

## Live

`https://lenibi.github.io/scalp-sim/explorer.html`

## What it does

- Random session on page load (or hit NEXT for another)
- Settings drawer: ticker filter, sequential vs random mode, 5-sec mode toggle
- 5-second tick replay with live candle formation (10-min default bucket, switchable 30s / 1m / 5m / 10m / 15m / 30m / Hourly)
- BUY / SHORT / CLOSE + stop-loss orders
- Per-trade Gross vs Net P&L showing the cost of bid/ask spread + commission
- Per-day P&L log persisted in localStorage
- TWS-style chart: previous-close dashed line, day-divider lines, position-gradient, breakeven line, stop-loss line, end-of-day summary overlay
- Mobile layout (≤ 700px): vertical stack, big touch targets, drawer-style Day Log / Trades / Settings panels

## Architecture

A single `scalp_sim.py` generator that bakes all session data into one self-contained `explorer.html` (~44 MB). The HTML loads `lightweight-charts.js` locally (no CDN). No backend, no API calls — fully static.

## Local dev

Data CSVs are NOT in this repo. The generator expects a sibling directory layout with IBKR 5-sec CSVs:

```
data_root/
└── research/data/eod_5sec/
    ├── AAPL_2026-05-07.csv
    ├── AAPL_2026-05-08.csv
    └── ...
```

Override the data path with `SCALP_SIM_DATA=/path/to/data_root` if your layout differs.

To regenerate the HTML:

```
python scalp_sim.py
```

To run locally:

```
python -m http.server 8000
# open http://localhost:8000/explorer.html
```

## Mobile (from your phone, no hosting)

On the same Wi-Fi as the PC running the server:

```
python -m http.server 8000
```

Then on phone: `http://<your-PC-LAN-IP>:8000/explorer.html`

## Disclaimer

This is a learning toy, not a strategy validator. Pattern recognition in the sim is heavily biased toward survivorship — what looks like edge here rarely survives rigorous backtests + real friction. Do not deploy real capital based on intuition built here.
