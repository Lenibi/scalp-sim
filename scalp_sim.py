"""
scalp_sim.py -- minute-bar scalping simulator.

TWO modes:
  Explorer (default, no args):
    python scalp_sim.py
    Embeds ALL available sessions across the configured tickers.
    Refresh the page = random new session.

  Single-session (explicit ticker + date):
    python scalp_sim.py SOXL 2026-02-13
    Generates a one-off HTML for that specific session.

Output goes in scalp_sim/ next to this script.

Features:
  - Candlestick chart (TradingView lightweight-charts)
  - Yesterday + overnight gap + today revealed bar-by-bar
  - Manual STEP default; PLAY supports 0.1x to 5x
  - BUY / SELL / CLOSE freeform paper trading
  - Position P&L (USD + CAD), TWS-style green/red gradient around entry
  - Solid green breakeven line at entry price
  - Auto-scroll on new bars when zoomed in
  - Big top-left HUD: time, price, % vs prev close, P&L
  - localStorage all-time stats
"""
import argparse
import json
import os
import webbrowser
from datetime import datetime, timezone

import pandas as pd

# scalp-sim-mobile is a sibling of `scheduled_trading` on the local
# filesystem. The big data CSVs live in scheduled_trading (private),
# never in this repo (which can be public). Override via SCALP_SIM_DATA
# env var if your layout differs.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DATA_ROOT = os.path.join(
    os.path.dirname(SCRIPT_DIR), 'scheduled_trading')
ROOT = os.environ.get('SCALP_SIM_DATA', _DEFAULT_DATA_ROOT)
DATA_DIR = os.path.join(ROOT, 'research', 'data', 'eod_1min_90d')

USD_TO_CAD = 1.36
TICKERS = ['SOXL', 'MSTR', 'COIN', 'NVDA', 'TQQQ', 'TSLA']
DATA_5S_DIR = os.path.join(ROOT, 'research', 'data', 'eod_5sec')


def load_bars_for_date(df, date):
    sub = df[df['d'] == date].copy()
    bars = []
    for _, r in sub.iterrows():
        ts = r['date']
        time_part = ts.split(' ')[1][:5]
        dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
        # Treat naive ET datetime as if it were UTC so the chart's
        # UTC-based axis displays ET clock times correctly.
        fake_utc = dt.replace(tzinfo=timezone.utc)
        bars.append({
            't': int(fake_utc.timestamp()),
            'o': round(float(r['open']), 4),
            'h': round(float(r['high']), 4),
            'l': round(float(r['low']), 4),
            'c': round(float(r['close']), 4),
            'm': time_part,
        })
    return bars


def load_all_data():
    """Load every available (ticker, date) session.

    Returns:
      catalog: list of {ticker, date, prev_date, context_dates, has_5sec, prev_close}
      bars_by_key: dict { f'{ticker}_{date}': [bars,...] }
    """
    bars_by_key = {}
    days_by_ticker = {}
    for t in TICKERS:
        csv_path = os.path.join(DATA_DIR, f'{t}.csv')
        if not os.path.exists(csv_path):
            print(f'  [skip] No data for {t}')
            continue
        df = pd.read_csv(csv_path)
        df['d'] = df['date'].str[:10]
        all_dates = sorted(set(df['d']))
        days_by_ticker[t] = all_dates
        for d in all_dates:
            bars = load_bars_for_date(df, d)
            if bars:
                bars_by_key[f'{t}_{d}'] = bars
        print(f'  {t}: {len(all_dates)} days')

    # Load 5-second bars where available. We cap how many sessions per
    # ticker get embedded into the public explorer.html so the artifact
    # stays under GitHub Pages' 100MB single-file limit. Override with
    # SCALP_SIM_LIMIT_PER_TICKER (default 8 = ~25MB for 9 tickers).
    LIMIT_PER_TICKER = int(os.environ.get('SCALP_SIM_LIMIT_PER_TICKER', '8'))
    bars_5s_by_key = {}
    if os.path.isdir(DATA_5S_DIR):
        # Group files by ticker, take the most recent LIMIT_PER_TICKER dates
        files_by_ticker = {}
        for fname in os.listdir(DATA_5S_DIR):
            if not fname.endswith('.csv'):
                continue
            stem = fname[:-4]
            try:
                ticker_5s, date_5s = stem.rsplit('_', 1)
            except ValueError:
                continue
            files_by_ticker.setdefault(ticker_5s, []).append((date_5s, fname))
        kept_files = []
        for ticker_5s, lst in files_by_ticker.items():
            lst.sort(key=lambda p: p[0], reverse=True)   # newest first
            kept = lst[:LIMIT_PER_TICKER]
            kept_files.extend((ticker_5s, d, fn) for d, fn in kept)
            print(f'  5-sec quota: {ticker_5s} keeping {len(kept)}/{len(lst)} sessions')
        for ticker_5s, date_5s, fname in kept_files:
            path = os.path.join(DATA_5S_DIR, fname)
            df_5s = pd.read_csv(path)
            df_5s['d'] = df_5s['date'].str[:10]
            bars = load_bars_for_date(df_5s, date_5s)
            if bars:
                bars_5s_by_key[f'{ticker_5s}_{date_5s}'] = bars

    # Mobile build: catalog is driven by the 5-sec data we have. Each kept
    # 5-sec session = one catalog entry. Prev/context dates come from the
    # 1-min data when available, otherwise from other 5-sec dates we have.
    # This guarantees the 5-SEC MODE filter ALWAYS finds matching sessions
    # (the previous bug was catalog built from outdated 1-min data with no
    # overlap with the newer 5-sec sessions).
    catalog = []
    CONTEXT_DAYS = 10
    # Group all dates per ticker from ANY data source so prev_date works.
    all_dates_by_ticker = {t: list(d) for t, d in days_by_ticker.items()}
    for key in bars_5s_by_key:
        t, d = key.rsplit('_', 1)
        all_dates_by_ticker.setdefault(t, [])
        if d not in all_dates_by_ticker[t]:
            all_dates_by_ticker[t].append(d)
    for t in all_dates_by_ticker:
        all_dates_by_ticker[t].sort()
    # Build catalog from sessions that have 5-sec data.
    # Compute prev_close server-side so it works for tickers without 1-min
    # context (SPY/QQQ/AAPL). Try 1-min first, then peek at the previous
    # 5-sec session's last bar, then fall back to today's first open.
    def find_prev_close(ticker, prev_date, today_first_open):
        # Try 1-min data first
        key = f'{ticker}_{prev_date}'
        if key in bars_by_key and bars_by_key[key]:
            return bars_by_key[key][-1]['c']
        # Try 5-sec data for the prev date if cached
        if key in bars_5s_by_key and bars_5s_by_key[key]:
            return bars_5s_by_key[key][-1]['c']
        # Try to read the prev-date 5-sec CSV directly (it may not be in
        # the kept quota set but still present on disk)
        csv_path = os.path.join(DATA_5S_DIR, f'{ticker}_{prev_date}.csv')
        if os.path.exists(csv_path):
            try:
                df_p = pd.read_csv(csv_path, usecols=['close'])
                if not df_p.empty:
                    return float(df_p['close'].iloc[-1])
            except Exception:
                pass
        # Last resort: today's open (gap_pct will read as 0)
        return today_first_open

    for key in sorted(bars_5s_by_key):
        ticker, date = key.rsplit('_', 1)
        dates = all_dates_by_ticker.get(ticker, [date])
        i = dates.index(date) if date in dates else 0
        prev_date = dates[i - 1] if i > 0 else date
        context_dates = dates[max(0, i - CONTEXT_DAYS):i] if i > 0 else []
        today_bars = bars_5s_by_key.get(key, [])
        today_first_open = today_bars[0]['o'] if today_bars else None
        prev_close = find_prev_close(ticker, prev_date, today_first_open)
        catalog.append({
            'ticker': ticker,
            'date': date,
            'prev_date': prev_date,
            'context_dates': context_dates,
            'has_5sec': True,
            'prev_close': prev_close,
        })
    print(f'  catalog size: {len(catalog)} sessions (all have 5-sec data)')
    return catalog, bars_by_key, bars_5s_by_key


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Scalp Sim - __BUILD_STAMP__</title>
<script src="lightweight-charts.js"></script>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Arial, sans-serif;
    background: #0f1419;
    color: #d5d8dc;
  }
  /* COMPACT MODE: TWS-style minimal layout, just the chart in a centered box.
     Remove `compact` class from <body> to restore the full UI. */
  body.compact {
    display: flex; align-items: center; justify-content: center;
    height: 100vh; padding-top: 56px; /* clear the fixed topbar */
    box-sizing: border-box;
  }
  /* Pin the topbar to the very top of the page; everything else stays centered. */
  body.compact #topbar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: #0a0a0a; padding: 8px 16px;
    border-bottom: 1px solid #1f1f1f;
    height: 48px; box-sizing: border-box;
  }
  body.compact #app {
    display: grid;
    grid-template-rows: 1fr auto;
    grid-template-columns: 260px 1fr 320px;
    grid-template-areas:
      "daylog     chart      trades"
      "daylog     controls   trades";
    gap: 8px;
    padding: 8px;
    width: 95vw;
    height: calc(100vh - 72px);  /* viewport - fixed topbar(48) - padding(16) - margin(8) */
  }
  /* Hide the in-flow topbar grid slot since topbar is position:fixed */
  body.compact #topbar { grid-area: unset; }
  body.compact #setup,
  body.compact #right { display: none; }
  body.compact #topbar .hud-section.hud-divider:not(.compact-keep) { display: none; }
  body.compact #chart-wrap { min-height: 0; }
  body:not(.compact) #compact-trades,
  body:not(.compact) #compact-daylog,
  body:not(.compact) #compact-settings { display: none; }
  /* Settings drawer (mobile only -- positioned by mobile media query) */
  #compact-settings {
    background: #1a1f26;
    border: 1px solid #2a3038;
    display: flex; flex-direction: column; overflow: hidden;
    color: #d5d8dc; font-size: 13px;
  }
  #compact-settings .header {
    background: #232830; color: #f6c143;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
    padding: 8px 12px; border-bottom: 1px solid #2a3038;
  }
  #compact-settings .body-wrap { padding: 12px; overflow-y: auto; flex: 1; }
  #compact-settings .settings-section + .settings-section {
    margin-top: 18px; padding-top: 14px; border-top: 1px solid #2a3038;
  }
  #compact-settings .settings-label {
    color: #889; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; margin-bottom: 8px;
  }
  #compact-settings .settings-row {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; padding: 8px 0; font-size: 13px; color: #d5d8dc;
  }
  #compact-settings .settings-row select,
  #compact-settings .settings-row input[type=checkbox] {
    accent-color: #f6c143;
  }
  #compact-settings .settings-row select {
    background: #232830; color: #d5d8dc; border: 1px solid #2a3038;
    padding: 6px 8px; border-radius: 4px; font-size: 13px; min-width: 120px;
  }
  #compact-settings .settings-hint {
    color: #889; font-size: 11px; margin-top: 6px; line-height: 1.4;
  }
  /* Wrap-toast: appears briefly when sequential mode wraps to the oldest day. */
  #wrap-toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    background: #1a1f26; border: 1px solid #f6c143; color: #f6c143;
    padding: 10px 16px; border-radius: 6px; font-size: 13px; font-weight: 600;
    box-shadow: 0 4px 16px rgba(0,0,0,0.6);
    opacity: 0; pointer-events: none; z-index: 200;
    transition: opacity 200ms ease-out;
  }
  #wrap-toast.show { opacity: 1; }
  #compact-daylog {
    grid-area: daylog;
    background: #1a1f26;
    border: 1px solid #2a3038;
    border-radius: 6px;
    display: flex; flex-direction: column; overflow: hidden;
  }
  #compact-daylog .header {
    background: #232830; color: #f6c143;
    font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
    padding: 8px 12px; border-bottom: 1px solid #2a3038;
    display: flex; align-items: center; gap: 8px;
  }
  #compact-daylog .reset-btn {
    background: none; border: 1px solid #555; color: #889;
    font-size: 9px; padding: 2px 6px; border-radius: 3px;
    cursor: pointer; font-family: inherit;
  }
  #compact-daylog .reset-btn:hover { color: #fff; border-color: #888; }
  #compact-daylog table {
    width: 100%; border-collapse: collapse;
    font-family: "Consolas", monospace; font-size: 12px;
  }
  #compact-daylog thead th {
    background: #232830; color: #889; font-weight: 500;
    text-transform: uppercase; font-size: 10px;
    padding: 5px 6px; text-align: right;
    border-bottom: 1px solid #2a3038;
    position: sticky; top: 0;
  }
  #compact-daylog thead th.l, #compact-daylog tbody td.l { text-align: left; }
  #compact-daylog tbody td {
    padding: 5px 6px; text-align: right;
    border-bottom: 1px solid #232830; color: #d5d8dc;
  }
  #compact-daylog tbody tr:hover { background: #232830; }
  #compact-daylog tbody td.win { color: #4ade80; font-weight: 600; }
  #compact-daylog tbody td.loss { color: #f87171; font-weight: 600; }
  #compact-daylog .body-wrap { flex: 1; overflow-y: auto; }
  #compact-daylog .empty {
    color: #555; text-align: center; padding: 20px;
    font-size: 11px; font-style: italic;
  }
  #compact-daylog .totals {
    background: #232830; border-top: 1px solid #2a3038;
    padding: 8px 12px; display: flex; justify-content: space-between;
    font-family: "Consolas", monospace; font-size: 13px; font-weight: 700;
  }
  #compact-daylog .totals .lab { color: #889; text-transform: uppercase;
    font-size: 10px; letter-spacing: 0.06em; }
  #compact-trades {
    grid-area: trades;
    background: #1a1f26;
    border: 1px solid #2a3038;
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #compact-trades .header {
    background: #232830;
    color: #f6c143;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 8px 12px;
    border-bottom: 1px solid #2a3038;
  }
  #compact-trades table {
    width: 100%;
    border-collapse: collapse;
    font-family: "Consolas", monospace;
    font-size: 12px;
  }
  #compact-trades thead th {
    background: #232830;
    color: #889;
    font-weight: 500;
    text-transform: uppercase;
    font-size: 10px;
    padding: 5px 6px;
    text-align: right;
    border-bottom: 1px solid #2a3038;
    position: sticky;
    top: 0;
  }
  #compact-trades thead th.l, #compact-trades tbody td.l { text-align: left; }
  #compact-trades tbody td {
    padding: 5px 6px;
    text-align: right;
    border-bottom: 1px solid #232830;
    color: #d5d8dc;
  }
  #compact-trades tbody td.win { color: #4ade80; font-weight: 600; }
  #compact-trades tbody td.loss { color: #f87171; font-weight: 600; }
  #compact-trades .body-wrap { overflow-y: auto; flex: 1; }
  #compact-trades .empty {
    padding: 20px;
    text-align: center;
    color: #4a5060;
    font-style: italic;
    font-size: 12px;
  }

  /* ====================== MOBILE LAYOUT (< 700px) ======================
     One screen, no scroll. Chart dominates. Day-log + Trades are drawers
     (hidden by default, slide in from left/right). Bigger touch targets. */
  @media (max-width: 700px) {
    body.compact {
      align-items: stretch;
      padding-top: 38px;
      height: 100vh;
      min-height: 100vh;
      overflow: hidden;
    }
    /* Single-row compact topbar */
    body.compact #topbar {
      flex-wrap: nowrap; gap: 4px; padding: 4px 6px;
      height: 38px;
      overflow: hidden;
    }
    body.compact #topbar > span[title*="build"],
    body.compact #topbar > #friction-label,
    body.compact #topbar > #ticker-filter,
    body.compact #topbar .desktop-only-toggle,
    body.compact #topbar .desktop-only-ticker-filter,
    body.compact #topbar #fivesec-badge { display: none !important; }
    body.compact #topbar #btn-next {
      font-size: 11px; padding: 4px 10px; height: 30px;
    }
    body.compact #topbar > label[title*="5-sec"] {
      font-size: 10px; padding: 3px 6px;
    }
    body.compact #topbar #session-id { font-size: 11px; max-width: 100%; overflow: hidden; }
    /* App = flex column, chart at top eats most of the screen */
    body.compact #app {
      display: flex;
      flex-direction: column;
      width: 100vw;
      height: calc(100vh - 38px);
      padding: 4px;
      gap: 4px;
      box-sizing: border-box;
    }
    body.compact #chart-wrap {
      width: 100%;
      flex: 1 1 auto;
      min-height: 280px;
      order: 1;
    }
    body.compact #controls {
      order: 2; padding: 4px 4px 14px 4px;
      flex: 0 0 auto;
    }
    body.compact #controls {
      gap: 5px;
    }
    body.compact #controls .ctrl-row {
      flex-wrap: wrap; gap: 5px; margin: 0;
    }
    body.compact #controls > div:last-child { display: none; }  /* hide shortcut hints */

    /* TIER 1 -- most-used: +5s, BUY, SHORT, CLOSE. Tall and bold. */
    body.compact #controls .ctrl-row.tier-1 > button {
      flex: 1 1 0; min-width: 0;
      min-height: 56px; padding: 0 4px;
      font-size: 15px; font-weight: 800;
    }
    body.compact #controls .ctrl-row.tier-1 > #btn-step {
      flex: 1.25 1 0;  /* +5s slightly wider to feel "hero" */
    }

    /* TIER 2 -- secondary: +1m, +5m, BUY+SL, SHORT+SL, Stop%. Medium. */
    body.compact #controls .ctrl-row.tier-2 > button {
      flex: 1 1 0; min-width: 0;
      min-height: 40px; padding: 0 4px;
      font-size: 12px; font-weight: 700;
    }
    body.compact #controls .ctrl-row.tier-2 > label {
      flex: 0 0 auto; gap: 4px !important; font-size: 11px !important;
      align-items: center; display: inline-flex;
    }
    body.compact #controls .ctrl-row.tier-2 #stop-pct {
      width: 46px !important; min-height: 32px; font-size: 12px;
    }

    /* TIER 3 -- rarely-used admin: PLAY, Speed, RESET, NEXT DAY. Small. */
    body.compact #controls .ctrl-row.tier-3 > button {
      flex: 1 1 0; min-width: 0;
      min-height: 26px; padding: 0 6px;
      font-size: 10px; font-weight: 600;
      opacity: 0.7;
    }
    body.compact #controls .ctrl-row.tier-3 > select {
      flex: 0 0 auto; width: auto; max-width: 78px;
      min-height: 26px; font-size: 10px; padding: 2px 4px;
    }
    body.compact #controls .ctrl-row.tier-3 > label.ctrl { display: none; }

    /* JS rewrites button text on mobile so labels fit (see applyMobileLabels). */
    /* Day log + Trades become DRAWERS that slide in from left/right */
    body.compact #compact-daylog,
    body.compact #compact-trades,
    body.compact #compact-settings {
      position: fixed; top: 38px; bottom: 0;
      width: 86vw; max-width: 380px;
      z-index: 95;
      transform: translateX(0);
      transition: transform 220ms ease-out;
      box-shadow: 0 0 30px rgba(0, 0, 0, 0.8);
    }
    body.compact #compact-daylog { left: 0; transform: translateX(-100%); }
    body.compact #compact-trades { right: 0; transform: translateX(100%); }
    body.compact #compact-settings { right: 0; transform: translateX(100%); }
    body.compact #compact-daylog.open { transform: translateX(0); }
    body.compact #compact-trades.open { transform: translateX(0); }
    body.compact #compact-settings.open { transform: translateX(0); }
    body.compact #compact-daylog .header,
    body.compact #compact-trades .header,
    body.compact #compact-settings .header { font-size: 13px; padding: 10px 12px; }
    /* Drawer-toggle buttons in the topbar (only visible on mobile) */
    body.compact .topbar-drawer-btn {
      display: inline-flex; align-items: center; justify-content: center;
      width: 32px; height: 32px;
      background: #1a1f26; border: 1px solid #2a3038; color: #f6c143;
      border-radius: 4px; font-size: 16px;
      cursor: pointer; padding: 0; margin: 0 2px;
    }
    /* Backdrop when a drawer is open */
    body.compact #drawer-backdrop {
      position: fixed; inset: 38px 0 0 0;
      background: rgba(0,0,0,0.5);
      z-index: 92;
      display: none;
    }
    body.compact #drawer-backdrop.open { display: block; }
    /* Compress chart inner bars on mobile */
    #chart-title-bar { height: 34px; gap: 6px; padding: 0 6px; }
    #ticker-header { top: 34px; height: 18px; font-size: 11px; gap: 6px; padding: 0 6px; }
    #session-progress { top: 52px; height: 5px; }
    #ctrl-time { font-size: 16px !important; padding: 0 6px !important; letter-spacing: 0 !important; }
    .day-divider { top: 34px; }
    /* Keep +/- zoom in the chart title bar -- shrink them a touch on mobile. */
    #chart-zoom-in, #chart-zoom-out { width: 26px !important; height: 26px !important; font-size: 14px !important; }
    /* End-of-day summary fits the smaller screen */
    #eod-summary .amount { font-size: 48px; }
    #eod-summary { padding: 20px 24px; }
  }
  /* Drawer-toggle topbar buttons -- hidden on desktop, shown on mobile */
  .topbar-drawer-btn { display: none; }
  body:not(.compact) #app {
    display: grid;
    grid-template-rows: auto auto 1fr auto;
    grid-template-columns: 1fr 340px;
    grid-template-areas:
      "topbar topbar"
      "setup  right"
      "chart  right"
      "controls right";
    gap: 10px;
    padding: 12px;
    height: 100vh;
  }
  .hud-section { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .hud-divider {
    border-left: 1px solid #2a3038;
    padding-left: 16px;
  }
  .hud-section-row { display: flex; gap: 12px; align-items: baseline; }
  .hud-section-label {
    font-size: 9px;
    color: #889;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
  }
  #topbar {
    grid-area: topbar;
    background: #1a1f26;
    border: 1px solid #2a3038;
    border-radius: 8px;
    padding: 10px 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: center;
  }
  #topbar select, #topbar input[type=date] {
    background: #232830;
    color: #d5d8dc;
    border: 1px solid #2a3038;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
  }
  #topbar label.toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    color: #889;
    font-size: 12px;
    cursor: pointer;
  }
  #topbar label.toggle input { accent-color: #f6c143; cursor: pointer; }
  #topbar .session-id {
    color: #f6c143;
    font-weight: 700;
    font-size: 15px;
  }
  #topbar .live-badge {
    background: #f6c143;
    color: #1a1500;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  #setup {
    grid-area: setup;
    background: #1a1f26;
    border: 1px solid #2a3038;
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    overflow-x: auto;
  }
  .metric {
    background: #232830;
    padding: 8px 12px;
    border-radius: 6px;
    min-width: 100px;
    white-space: nowrap;
  }
  .metric .label {
    color: #889;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .metric .val {
    font-size: 17px;
    font-weight: 600;
    color: #fff;
    margin-top: 2px;
  }
  .metric.good .val { color: #4ade80; }
  .metric.bad .val { color: #f87171; }
  .metric.neu .val { color: #f6c143; }
  #chart-wrap {
    grid-area: chart;
    position: relative;
    background: #000000;
    border: 1px solid #2a3038;
    border-radius: 4px;
    overflow: hidden;
    min-height: 350px;
  }
  #chart-title-bar {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 9;
    height: 44px;
    background: #2a2a2a;
    border-bottom: 1px solid #000;
    color: #cccccc;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 0 12px;
    font-size: 13px;
    font-weight: 600;
  }
  #chart-title-bar .ctb-ticker { color: #66b3ff; font-weight: 700; }
  #chart-title-bar .ctb-candle-label { color: #cccccc; }
  #ticker-header {
    position: absolute;
    top: 44px;     /* directly below chart-title-bar */
    left: 0;
    right: 0;
    z-index: 8;
    height: 22px;
    background: #444;
    color: #fff;
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 12px;
    font-size: 13px;
    font-weight: 700;
    font-family: "Consolas", monospace;
    border-bottom: 1px solid #000;
    pointer-events: none;
  }
  #ticker-header.up { background: #1a7a2a; }
  #ticker-header.down { background: #9a2929; }
  #ticker-header .th-ticker {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  #ticker-header .th-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #fff;
  }
  #chart { width: 100%; height: 100%; }
  #overlay { position: absolute; inset: 0; pointer-events: none; z-index: 5; }
  #pos-gradient {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 2;
    display: none;
  }
  #yday-bg {
    position: absolute;
    top: 0;
    bottom: 30px;
    left: 0;
    background: rgba(246, 193, 67, 0.06);
    border-right: 2px dashed #f6c143;
    display: none;
  }
  .day-label {
    position: absolute;
    bottom: 36px;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 10px;
    background: rgba(15, 20, 25, 0.85);
    border-radius: 4px;
    letter-spacing: 0.05em;
    display: none;
  }
  #yday-label { color: #f6c143; }
  #today-label { color: #4ade80; }
  #overnight-tag {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    font-size: 10px;
    font-style: italic;
    color: #889;
    text-align: center;
    line-height: 1.4;
    display: none;
  }
  /* Session progress bar: 3px sliver pinned to the top of the chart area,
     filling left-to-right as the trading day progresses. Yellow at 9:30 ->
     orange at midday -> red near 4pm. Glanceable, doesn't touch candles. */
  #session-progress {
    position: absolute;
    top: 66px; left: 0; right: 0;        /* below chart-title-bar (44) + ticker-header (22) */
    height: 7px;
    background: rgba(255, 255, 255, 0.08);
    z-index: 10;
    pointer-events: none;
  }
  #session-progress-fill {
    height: 100%; width: 0%;
    background: linear-gradient(to right, #ffffff 0%, #93c5fd 50%, #2563eb 100%);
    transition: width 0.25s ease-out;
  }
  /* End-of-day session summary: huge centered $ amount over the chart. */
  #eod-summary {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    z-index: 50;
    background: rgba(0, 0, 0, 0.85);
    border: 2px solid #444;
    border-radius: 12px;
    padding: 32px 56px;
    text-align: center;
    pointer-events: auto;
    display: none;
  }
  #eod-summary .label {
    font-size: 14px; color: #999;
    text-transform: uppercase; letter-spacing: 0.15em;
    margin-bottom: 8px;
  }
  #eod-summary .amount {
    font-family: "Consolas", monospace;
    font-size: 80px; font-weight: 800;
    line-height: 1;
  }
  #eod-summary .sub {
    font-size: 13px; color: #888; margin-top: 12px;
  }
  #eod-summary.pos .amount { color: #4ade80; }
  #eod-summary.neg .amount { color: #f87171; }
  #eod-summary.flat .amount { color: #cccccc; }
  #eod-summary .close-x {
    position: absolute; top: 8px; right: 14px;
    color: #888; font-size: 18px; cursor: pointer;
    background: none; border: none; padding: 0;
  }
  #eod-summary .close-x:hover { color: #fff; }
  /* Per-day vertical dashed separators, TWS-style */
  .day-divider {
    position: absolute;
    top: 44px;          /* below chart title bar */
    bottom: 30px;       /* above x-axis */
    width: 0;
    border-left: 1px dashed #555;
    pointer-events: none;
    z-index: 4;
  }
  #hud-time {
    font-family: "Consolas", "Courier New", monospace;
    font-size: 28px;
    font-weight: 700;
    color: #f6c143;
    line-height: 1;
  }
  #hud-price {
    font-family: "Consolas", monospace;
    font-size: 17px;
    font-weight: 600;
    color: #fff;
  }
  #hud-change { font-size: 13px; font-weight: 600; }
  #hud-starting {
    font-size: 10px;
    color: #6b7280;
  }
  #hud-account-now {
    font-family: "Consolas", monospace;
    font-size: 14px;
    color: #d5d8dc;
    font-weight: 600;
  }
  #hud-pnl-usd {
    font-family: "Consolas", monospace;
    font-size: 24px;
    font-weight: 700;
    line-height: 1.1;
  }
  #hud-pnl-cad {
    font-family: "Consolas", monospace;
    font-size: 12px;
    color: #889;
  }
  .hud-flat { color: #4a5060 !important; }
  .hud-positive { color: #4ade80; }
  .hud-negative { color: #f87171; }
  #controls {
    grid-area: controls;
    background: #1a1f26;
    border: 1px solid #2a3038;
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .ctrl-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  button {
    background: #232830;
    color: #d5d8dc;
    border: 1px solid #2a3038;
    padding: 10px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    /* Mobile: prevent long-press from selecting the button text and from
       triggering the iOS callout / double-tap-to-zoom. */
    -webkit-user-select: none;
    -moz-user-select: none;
    -ms-user-select: none;
    user-select: none;
    -webkit-touch-callout: none;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  button:hover { background: #2a3038; }
  button.primary { background: #2a6dd2; border-color: #2a6dd2; color: white; }
  button.primary:hover { background: #1e5cbd; }
  button.success { background: #4ade80; border-color: #4ade80; color: #062713; }
  button.success:hover { background: #38c66c; }
  button.danger { background: #f87171; border-color: #f87171; color: #1a0000; }
  button.danger:hover { background: #e85f5f; }
  button.ghost { background: transparent; color: #889; border: 1px solid #2a3038; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  input[type=number], select {
    background: #232830;
    color: #d5d8dc;
    border: 1px solid #2a3038;
    padding: 7px 10px;
    border-radius: 6px;
    font-size: 13px;
    width: 90px;
  }
  label.ctrl {
    color: #889;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  #right {
    grid-area: right;
    background: #1a1f26;
    border: 1px solid #2a3038;
    border-radius: 8px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    overflow-y: auto;
  }
  h3 {
    margin: 0 0 8px 0;
    font-size: 11px;
    text-transform: uppercase;
    color: #f6c143;
    font-weight: 700;
    letter-spacing: 0.08em;
  }
  .stat-row {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    padding: 4px 0;
  }
  .stat-row .label { color: #889; }
  .stat-row .val { font-weight: 500; font-family: "Consolas", monospace; }
  .pnl-positive { color: #4ade80; font-weight: 600; }
  .pnl-negative { color: #f87171; font-weight: 600; }
  .pnl-zero { color: #d5d8dc; }
  #trade-log {
    background: #131722;
    border: 1px solid #2a3038;
    border-radius: 6px;
    padding: 8px;
    font-family: "Consolas", monospace;
    font-size: 11px;
    max-height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.4;
  }
</style>
</head>
<body class="compact">
<!-- Remove 'compact' class above to restore the full dashboard layout. -->
<div id="app">
  <div id="topbar">
    <button id="btn-next" class="primary" title="Next session (random or sequential, see Settings)">NEXT &raquo;</button>
    <!-- 5-SEC MODE moved into Settings drawer on mobile (still rendered here for desktop). -->
    <label class="toggle desktop-only-toggle" style="background:#3b3a1f;border:1px solid #f6c143;padding:4px 8px;border-radius:4px;cursor:pointer;color:#f6c143;font-weight:600;align-self:center;display:inline-flex;align-items:center;gap:6px;white-space:nowrap" title="ON: only sessions with 5-sec tick data. STEP = +5s. OFF: includes 1-min sessions. STEP = +1min.">
      <input type="checkbox" id="fivesec-toggle" style="accent-color:#f6c143;margin:0">
      5-SEC MODE
    </label>
    <span style="color:#888;font-family:Consolas,monospace;font-size:10px;align-self:center" title="HTML build timestamp">build __BUILD_STAMP__</span>
    <span id="friction-label" style="color:#f6c143;font-family:Consolas,monospace;font-size:11px;padding:0 8px;border:1px solid #f6c143;border-radius:4px;align-self:center" title="Round-trip friction modeled on every trade">friction --</span>
    <div class="hud-section">
      <span class="session-id" id="session-id">--</span>
      <div style="display:flex;gap:4px;margin-top:3px;align-self:flex-start">
        <span class="live-badge" id="fivesec-badge" style="display:none">5-SEC LIVE</span>
      </div>
    </div>
    <div class="hud-section hud-divider">
      <div id="hud-time">--:--</div>
      <div class="hud-section-row">
        <span id="hud-price">$--.--</span>
        <span id="hud-change">--</span>
      </div>
    </div>
    <div class="hud-section hud-divider">
      <div class="hud-section-label">ACCOUNT</div>
      <div id="hud-starting">Start: $10,000 USD</div>
      <div id="hud-account-now">$10,000.00 USD</div>
    </div>
    <div class="hud-section hud-divider">
      <div class="hud-section-label">TOTAL P&amp;L</div>
      <div id="hud-pnl-usd" class="hud-flat">$0.00 USD</div>
      <div id="hud-pnl-cad">$0.00 CAD</div>
    </div>
    <span style="flex:1"></span>
    <!-- Mobile-only drawer toggles (hidden on desktop, shown via media query) -->
    <button class="topbar-drawer-btn" id="fab-daylog" title="Day Log">&#9776;</button>
    <button class="topbar-drawer-btn" id="fab-trades" title="Trades">&#128202;</button>
    <button class="topbar-drawer-btn" id="fab-settings" title="Settings">&#9881;</button>
    <select id="ticker-filter" class="desktop-only-ticker-filter">
      <option value="">All tickers</option>
    </select>
  </div>
  <div id="setup"></div>
  <div id="chart-wrap">
    <!-- TWS-style chart title bar (SOXL ▾  10 min candles ▾) -->
    <div id="chart-title-bar">
      <select id="bucket-select" class="ctb-candle-label" style="background:transparent;color:#cccccc;border:1px solid #444;border-radius:3px;padding:2px 6px;font-size:13px;cursor:pointer;outline:none">
        <option value="30">30 sec</option>
        <option value="60">1 min</option>
        <option value="300">5 min</option>
        <option value="600" selected>10 min</option>
        <option value="900">15 min</option>
        <option value="1800">30 min</option>
        <option value="3600">Hourly</option>
      </select>
      <button id="chart-zoom-in" title="Zoom in (fewer bars visible, same as scroll wheel up)" style="background:#1a1a1a;color:#cccccc;border:1px solid #444;border-radius:3px;width:24px;height:24px;font-size:14px;font-weight:700;cursor:pointer;line-height:1;padding:0;margin-left:2px">&plus;</button>
      <button id="chart-zoom-out" title="Zoom out (more bars visible, same as scroll wheel down)" style="background:#1a1a1a;color:#cccccc;border:1px solid #444;border-radius:3px;width:24px;height:24px;font-size:14px;font-weight:700;cursor:pointer;line-height:1;padding:0">&minus;</button>
      <span style="flex:1"></span>
      <span id="ctrl-time" style="font-family:Consolas,monospace;font-size:28px;font-weight:800;color:#f6c143;padding:0 12px;letter-spacing:0.05em;line-height:1" title="Current simulated time">--:--:-- --</span>
    </div>
    <div id="ticker-header">
      <span class="th-ticker"><span class="th-dot"></span><span id="th-symbol">--</span></span>
      <span id="th-price">--.--</span>
      <span id="th-change-dollar">--</span>
      <span id="th-change-pct">--%</span>
      <span style="flex:1"></span>
      <span id="th-pos-pnl" style="display:none;padding:2px 10px;border-radius:3px;font-size:14px;font-weight:700"></span>
    </div>
    <div id="chart"></div>
    <div id="session-progress"><div id="session-progress-fill"></div></div>
    <div id="pos-gradient"></div>
    <div id="eod-summary">
      <button class="close-x" id="eod-close-x" title="Dismiss">&times;</button>
      <div class="label">End of Session</div>
      <div class="amount" id="eod-amount">$0.00</div>
      <div class="sub" id="eod-sub">0 trades</div>
    </div>
    <div id="overlay">
      <div id="yday-bg"></div>
      <div id="yday-label" class="day-label"></div>
      <div id="today-label" class="day-label"></div>
      <div id="overnight-tag">OVERNIGHT<br>(no trading)</div>
    </div>
  </div>
  <div id="controls">
    <!-- TIER 1: most-used actions (+5s, BUY, SHORT, CLOSE). Big, prominent. -->
    <div class="ctrl-row tier-1">
      <button id="btn-step" class="primary" title="One tick (5s in 5-sec mode, 1min otherwise)">+5s</button>
      <button id="btn-buy" class="success">BUY (LONG, all-in)</button>
      <button id="btn-sell" class="danger">SHORT (all-in)</button>
      <button id="btn-close" style="background:#f6c143;color:#1a1500;font-weight:700;border:1px solid #d9a325">CLOSE</button>
    </div>
    <!-- TIER 2: secondary (+1m, +5m only in 5-sec mode; BUY+SL, SHORT+SL, Stop%). Medium. -->
    <div class="ctrl-row tier-2">
      <button id="btn-step-1m" title="Jump forward 1 minute" style="display:none">+1m</button>
      <button id="btn-step-5m" title="Jump forward 5 minutes" style="display:none">+5m</button>
      <button id="btn-buy-sl" class="success" style="opacity:0.85" title="Buy long and auto-close if price drops by stop %">BUY + STOP</button>
      <button id="btn-sell-sl" class="danger" style="opacity:0.85" title="Short and auto-close if price rises by stop %">SHORT + STOP</button>
      <label style="display:flex;align-items:center;gap:6px;color:#ccc;font-size:12px">
        Stop %
        <input id="stop-pct" type="number" min="0.1" max="20" step="0.1" value="2.0"
               style="width:60px;background:#1a1a1a;border:1px solid #444;color:#fff;padding:4px 6px;border-radius:4px;font-family:Consolas,monospace;font-size:13px;text-align:right">
      </label>
    </div>
    <!-- TIER 3: rarely-used admin (PLAY, Speed, RESET, NEXT DAY). Small, muted. -->
    <div class="ctrl-row tier-3">
      <button id="btn-play">PLAY</button>
      <label class="ctrl">Speed</label>
      <select id="speed">
        <option value="10000">0.1x (very slow)</option>
        <option value="4000">0.25x</option>
        <option value="2000">0.5x</option>
        <option value="1333">0.75x</option>
        <option value="1000" selected>1x</option>
        <option value="500">2x</option>
        <option value="200">5x</option>
        <option value="100">10x</option>
        <option value="50">20x</option>
        <option value="25">40x (5-sec firehose)</option>
        <option value="10">100x</option>
      </select>
      <button id="btn-reset" class="ghost">RESET</button>
    </div>
    <div style="display:flex;justify-content:center;color:#666;font-size:10px;font-family:Consolas,monospace;padding-top:6px;letter-spacing:0.5px">
      shortcuts:
      <span style="color:#888;margin-left:8px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">Space</kbd> next candle</span>
      <span style="color:#888;margin-left:12px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">B</kbd> buy</span>
      <span style="color:#888;margin-left:8px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">S</kbd> short</span>
      <span style="color:#888;margin-left:8px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">C</kbd> close</span>
      <span style="color:#888;margin-left:8px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">P</kbd> play/pause</span>
      <span style="color:#888;margin-left:8px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">F</kbd> hold +5m</span>
      <span style="color:#888;margin-left:8px"><kbd style="background:#222;border:1px solid #444;padding:1px 5px;border-radius:3px;color:#bbb">R</kbd> reset</span>
    </div>
  </div>
  <!-- Mobile drawer backdrop (the FAB buttons themselves now live in the topbar) -->
  <div id="drawer-backdrop"></div>
  <div id="compact-daylog">
    <div class="header">
      <span>Day Log</span>
      <span style="flex:1"></span>
      <button class="reset-btn" id="daylog-reset" title="Clear all-time day log">RESET</button>
    </div>
    <div class="body-wrap">
      <table>
        <thead>
          <tr>
            <th class="l">Date</th>
            <th class="l">Tkr</th>
            <th>%</th>
            <th>Net $</th>
          </tr>
        </thead>
        <tbody id="daylog-tbody"></tbody>
      </table>
      <div class="empty" id="daylog-empty">No days played yet</div>
    </div>
    <div class="totals">
      <span><span class="lab">Total</span> <span id="daylog-total-pnl" style="color:#cccccc">$0.00</span></span>
      <span><span class="lab">Days</span> <span id="daylog-total-count" style="color:#fff">0</span></span>
    </div>
  </div>
  <div id="compact-trades">
    <div class="header" style="display:flex;align-items:center;gap:8px">
      <span>Trades</span>
      <span style="flex:1"></span>
      <span style="color:#888;font-size:10px;text-transform:uppercase;letter-spacing:0.05em">Account</span>
      <span id="ctrl-account-now" style="font-family:Consolas,monospace;font-size:14px;font-weight:700;color:#4ade80">$10,000.00</span>
    </div>
    <div class="body-wrap">
      <table>
        <thead>
          <tr>
            <th class="l">Buy</th>
            <th class="l">Sell</th>
            <th>%</th>
            <th title="Ideal P&amp;L: no fees, no slippage, no spread">Gross</th>
            <th title="After spread, slippage, $2 commission" style="background:rgba(255,255,255,0.06)">Net</th>
          </tr>
        </thead>
        <tbody id="trades-tbody"></tbody>
      </table>
      <div class="empty" id="trades-empty">No trades yet</div>
    </div>
  </div>
  <!-- Settings drawer (mobile only). Slides from the right like #compact-trades. -->
  <aside id="compact-settings">
    <div class="header">Settings</div>
    <div class="body-wrap">
      <div class="settings-section">
        <div class="settings-label">Session</div>
        <label class="settings-row">
          <span>Ticker</span>
          <select id="ticker-filter-mobile"></select>
        </label>
        <label class="settings-row" id="settings-sequential-row" style="display:none">
          <span>Sequential (chronological)</span>
          <input type="checkbox" id="sequential-toggle">
        </label>
        <label class="settings-row">
          <span>5-sec mode</span>
          <input type="checkbox" id="fivesec-toggle-mobile">
        </label>
      </div>
      <div class="settings-section">
        <button id="btn-newrandom-settings" class="primary" style="width:100%;padding:10px">NEW RANDOM &#x21bb;</button>
        <div class="settings-hint">Same as pressing NEXT with "All tickers" selected.</div>
      </div>
    </div>
  </aside>
  <!-- Wrap-toast (transient banner on settings-wrap event) -->
  <div id="wrap-toast"></div>
  <div id="right">
    <div>
      <h3>Position</h3>
      <div class="stat-row"><span class="label">Side</span><span class="val" id="pos-side">FLAT</span></div>
      <div class="stat-row"><span class="label">Shares</span><span class="val" id="pos-shares">0</span></div>
      <div class="stat-row"><span class="label">Avg entry</span><span class="val" id="pos-entry">$--.--</span></div>
      <div class="stat-row"><span class="label">Unrealized (USD)</span><span class="val pnl-zero" id="pos-unreal-usd">$0.00</span></div>
      <div class="stat-row"><span class="label">Unrealized (CAD)</span><span class="val pnl-zero" id="pos-unreal-cad">$0.00</span></div>
    </div>
    <div>
      <h3>Session P&amp;L</h3>
      <div class="stat-row"><span class="label">Realized (USD)</span><span class="val pnl-zero" id="pnl-real-usd">$0.00</span></div>
      <div class="stat-row"><span class="label">Realized (CAD)</span><span class="val pnl-zero" id="pnl-real-cad">$0.00</span></div>
      <div class="stat-row"><span class="label">Total (USD)</span><span class="val pnl-zero" id="pnl-total-usd">$0.00</span></div>
      <div class="stat-row"><span class="label">Total (CAD)</span><span class="val pnl-zero" id="pnl-total-cad">$0.00</span></div>
      <div class="stat-row"><span class="label">Trades</span><span class="val" id="pnl-trades">0</span></div>
    </div>
    <div>
      <h3>Buy &amp; Hold Baseline</h3>
      <div class="stat-row"><span class="label">B&amp;H now (USD)</span><span class="val" id="bh-pnl-usd">$0.00</span></div>
      <div class="stat-row"><span class="label">You vs B&amp;H</span><span class="val" id="vs-bh">--</span></div>
    </div>
    <div>
      <h3>Trade Log</h3>
      <div id="trade-log"></div>
    </div>
    <div>
      <h3>All-Time Stats <span style="float:right;cursor:pointer;color:#889;font-size:9px;font-weight:400" id="reset-stats">[reset]</span></h3>
      <div class="stat-row"><span class="label">Sessions played</span><span class="val" id="all-sessions">0</span></div>
      <div class="stat-row"><span class="label">Total P&amp;L (USD)</span><span class="val pnl-zero" id="all-pnl-usd">$0.00</span></div>
      <div class="stat-row"><span class="label">Total P&amp;L (CAD)</span><span class="val pnl-zero" id="all-pnl-cad">$0.00</span></div>
      <div class="stat-row"><span class="label">Win rate</span><span class="val" id="all-wr">--</span></div>
    </div>
  </div>
</div>

<script>
// Sanity check: lightweight-charts must be loaded (vendored locally)
if (typeof LightweightCharts === 'undefined') {
  alert('FATAL: lightweight-charts library did not load. Check that lightweight-charts.js sits next to explorer.html.');
}
// All data is embedded
const CATALOG = __CATALOG__;
const BARS_BY_KEY = __BARS__;
const BARS_5S_BY_KEY = __BARS_5S__;
const USD_TO_CAD = 1.36;
// Candle bucket size (seconds). Persisted in localStorage. Mutable so the
// dropdown can rebuild aggregations in-place WITHOUT a page reload --
// preserving the user's tickIdx / position / trades through the rebuild.
let CANDLE_BUCKET_SECONDS = parseInt(
  localStorage.getItem('scalp_sim_bucket_sec') || '600', 10
);

// -------- Realistic friction model --------
// Round-trip cost in bps (basis points = 0.01%). Includes bid-ask spread
// estimate + typical slippage on market orders for a $10k position.
// Values calibrated from observed IBKR fills + typical OPRA spreads.
// Liquid majors: ~10-20 bps. Mid-liquid leveraged ETFs: ~15-25 bps.
// Thin inverse ETFs: 30-50 bps.
const FRICTION_BPS_BY_TICKER = {
  // Volatile but liquid majors
  MSTR: 20, COIN: 18, TSLA: 10, NVDA: 8,
  // 3x leveraged long ETFs (liquid)
  TQQQ: 12, SOXL: 15,
  // 2x single-name long ETFs (thinner)
  NVDL: 25, CONL: 35, MSTU: 30, MSTX: 35, TSLL: 20,
  // Inverse 3x ETFs (liquid)
  SOXS: 22, SQQQ: 22,
  // Single-name inverse ETFs (thin)
  MSTZ: 40, TSLZ: 40, NVDZ: 40,
};
const FRICTION_BPS_DEFAULT = 25;
const COMMISSION_PER_SIDE_USD = 1.00;   // IBKR Tiered minimum
function frictionBps(ticker) {
  return FRICTION_BPS_BY_TICKER[ticker] !== undefined
    ? FRICTION_BPS_BY_TICKER[ticker]
    : FRICTION_BPS_DEFAULT;
}
const LS_KEYS = {
  fivesecOnly: 'scalp_sim_fivesec_only',
  tickerFilter: 'scalp_sim_ticker',
  sequential: 'scalp_sim_sequential',
  stats: 'scalp_sim_stats',
  dayLog: 'scalp_sim_day_log',
};
const SS_KEYS = {
  forceSession: 'scalp_sim_force_session',
  justWrapped: 'scalp_sim_just_wrapped',
};

function clearChildren(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function pickSession() {
  // Forced session via sessionStorage (NEXT button in sequential mode) takes precedence.
  const forcedRaw = sessionStorage.getItem(SS_KEYS.forceSession);
  if (forcedRaw) {
    sessionStorage.removeItem(SS_KEYS.forceSession);   // one-shot
    try {
      const f = JSON.parse(forcedRaw);
      const found = CATALOG.find(s => s.ticker === f.ticker && s.date === f.date);
      if (found) return found;
    } catch (e) {}
  }
  // 5-sec-only is the DEFAULT (null -> '1'). User can uncheck to see 1-min sessions.
  const fivesecRaw = localStorage.getItem(LS_KEYS.fivesecOnly);
  const fivesecOnly = fivesecRaw === null ? true : (fivesecRaw === '1');
  const tickerFilter = localStorage.getItem(LS_KEYS.tickerFilter) || '';
  let pool = CATALOG;
  if (fivesecOnly) pool = pool.filter(s => s.has_5sec);
  if (tickerFilter) pool = pool.filter(s => s.ticker === tickerFilter);
  if (pool.length === 0) {
    alert('No sessions match the filters. Resetting.');
    localStorage.removeItem(LS_KEYS.fivesecOnly);
    localStorage.removeItem(LS_KEYS.tickerFilter);
    pool = CATALOG;
  }
  return pool[Math.floor(Math.random() * pool.length)];
}

// Pool of sessions matching the current filters EXCEPT ticker. Used by NEXT
// for the "All tickers" random case so the user gets fresh tickers each press.
function filteredPool(ignoreTickerFilter) {
  const fivesecRaw = localStorage.getItem(LS_KEYS.fivesecOnly);
  const fivesecOnly = fivesecRaw === null ? true : (fivesecRaw === '1');
  const tickerFilter = localStorage.getItem(LS_KEYS.tickerFilter) || '';
  let pool = CATALOG;
  if (fivesecOnly) pool = pool.filter(s => s.has_5sec);
  if (!ignoreTickerFilter && tickerFilter) pool = pool.filter(s => s.ticker === tickerFilter);
  return pool;
}

// Pick a session for this page load
const SESSION = pickSession();
const TICKER = SESSION.ticker;
const DATE = SESSION.date;
const PREV_DATE = SESSION.prev_date;
// Context = all prior trading days (TWS-style continuous history)
const CONTEXT_DATES = SESSION.context_dates || [PREV_DATE];
const CONTEXT_BARS_RAW = CONTEXT_DATES.flatMap(d => BARS_BY_KEY[TICKER + '_' + d] || []);
const PREV_BARS_RAW = CONTEXT_BARS_RAW;
const TODAY_BARS_RAW = BARS_BY_KEY[TICKER + '_' + DATE] || [];

function expandBars(raw) {
  return raw.map(b => ({
    time: b.t, open: b.o, high: b.h, low: b.l, close: b.c, hhmm: b.m,
  }));
}

// Aggregate raw bars (1-min or 5-sec) into N-second candles
function aggregateBars(bars, bucketSec) {
  const out = [];
  let cur = null;
  for (const b of bars) {
    const bucket = Math.floor(b.time / bucketSec) * bucketSec;
    if (!cur || cur.time !== bucket) {
      if (cur) out.push(cur);
      cur = { time: bucket, open: b.open, high: b.high, low: b.low, close: b.close };
    } else {
      cur.high = Math.max(cur.high, b.high);
      cur.low = Math.min(cur.low, b.low);
      cur.close = b.close;
    }
  }
  if (cur) out.push(cur);
  return out;
}

// Live-tick mode: ALWAYS play tick by tick. The 10-min candle updates with
// each tick. Tick granularity depends on what data is available:
//   - 5-sec ticks if we fetched them for this session (badge shows)
//   - 1-min ticks otherwise
// Either way, the rightmost in-progress 10-min candle's H/L/C move with each
// STEP, exactly like TWS shows a forming bar update on every tick.
// 5-SEC MODE = toggle ON in topbar AND session has 5-sec data. When the user
// unchecks the toggle, we force 1-min ticks even on sessions that DO have
// 5-sec data, so unchecking actually leaves 5-sec behavior behind.
const _fivesecToggleRaw = localStorage.getItem(LS_KEYS.fivesecOnly);
const _fivesecToggleOn = _fivesecToggleRaw === null ? true : (_fivesecToggleRaw === '1');
const FIVE_SEC_MODE = _fivesecToggleOn && !!SESSION.has_5sec;
const TODAY_5S_BARS = expandBars(BARS_5S_BY_KEY[TICKER + '_' + DATE] || []);
const TODAY_1M_BARS = expandBars(TODAY_BARS_RAW);
const TODAY_TICKS = FIVE_SEC_MODE ? TODAY_5S_BARS : TODAY_1M_BARS;

// Context (prior days): aggregate 1-min to 10-min for display.
// Cap to last ~30 bars of yesterday so the chart has context to the left
// of today's live candle without dragging in a week of history.
let PREV_BARS_FULL = aggregateBars(expandBars(PREV_BARS_RAW), CANDLE_BUCKET_SECONDS);
let PREV_BARS = PREV_BARS_FULL.slice(-30);
// Today (full session, for B&H baseline computation): aggregate ticks to 10-min
let TODAY_BARS = aggregateBars(TODAY_TICKS, CANDLE_BUCKET_SECONDS);

// Compute setup metrics
const SETUP = (() => {
  // prev_close is computed server-side (Python) on the catalog entry so it
  // works for tickers without 1-min context (SPY/QQQ/AAPL). Fall back to
  // the last yday 1-min bar if SESSION.prev_close is absent.
  const ydayBars = (BARS_BY_KEY[TICKER + '_' + PREV_DATE] || []).map(b =>
    ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c, hhmm: b.m }));
  const prevClose = (SESSION.prev_close !== undefined && SESSION.prev_close !== null)
    ? SESSION.prev_close
    : (ydayBars.length ? ydayBars[ydayBars.length - 1].close : null);
  const todayOpen = TODAY_BARS.length ? TODAY_BARS[0].open : null;
  const todayClose = TODAY_BARS.length ? TODAY_BARS[TODAY_BARS.length - 1].close : null;
  const todayHigh = TODAY_BARS.length ? Math.max(...TODAY_BARS.map(b => b.high)) : null;
  const todayLow = TODAY_BARS.length ? Math.min(...TODAY_BARS.map(b => b.low)) : null;
  return {
    prev_close: prevClose,
    today_open: todayOpen,
    today_close: todayClose,
    today_high: todayHigh,
    today_low: todayLow,
    overnight_gap_pct: prevClose && todayOpen ? (todayOpen - prevClose) / prevClose * 100 : null,
    intraday_pct: todayOpen && todayClose ? (todayClose - todayOpen) / todayOpen * 100 : null,
    total_pct: prevClose && todayClose ? (todayClose - prevClose) / prevClose * 100 : null,
    dow: new Date(DATE + 'T00:00:00Z').toLocaleDateString('en-US', { weekday: 'long', timeZone: 'UTC' }),
  };
})();

// ---- Topbar setup ----
document.getElementById('session-id').textContent = TICKER + ' ' + DATE + ' (' + SETUP.dow + ')';
const ctbSymbol = document.getElementById('ctb-symbol');
if (ctbSymbol) ctbSymbol.textContent = TICKER + ' ▾';
if (FIVE_SEC_MODE) document.getElementById('fivesec-badge').style.display = 'inline-block';

// Populate ticker filter (desktop topbar) and its mobile mirror inside the settings drawer.
const tickers = Array.from(new Set(CATALOG.map(s => s.ticker))).sort();
const tFilter = document.getElementById('ticker-filter');
const tFilterMobile = document.getElementById('ticker-filter-mobile');
function buildTickerOptions(sel, includeAll) {
  if (includeAll) {
    const optAll = document.createElement('option');
    optAll.value = '';
    optAll.textContent = 'All tickers';
    sel.appendChild(optAll);
  }
  tickers.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t;
    opt.textContent = t;
    sel.appendChild(opt);
  });
}
buildTickerOptions(tFilter, false);   // topbar select already has "All tickers" hardcoded
buildTickerOptions(tFilterMobile, true);
const _initialTicker = localStorage.getItem(LS_KEYS.tickerFilter) || '';
tFilter.value = _initialTicker;
tFilterMobile.value = _initialTicker;

{
  const _r = localStorage.getItem(LS_KEYS.fivesecOnly);
  const _checked = (_r === null) ? true : (_r === '1');
  document.getElementById('fivesec-toggle').checked = _checked;
  document.getElementById('fivesec-toggle-mobile').checked = _checked;
}
document.getElementById('sequential-toggle').checked = localStorage.getItem(LS_KEYS.sequential) === '1';

// Show/hide the Sequential row based on whether a specific ticker is chosen.
function refreshSequentialVisibility() {
  const row = document.getElementById('settings-sequential-row');
  const hasTicker = !!(localStorage.getItem(LS_KEYS.tickerFilter) || '');
  row.style.display = hasTicker ? 'flex' : 'none';
}
refreshSequentialVisibility();

// Keep desktop + mobile 5-sec toggles in sync, persist, and reload to repick.
function _setFivesec(checked) {
  localStorage.setItem(LS_KEYS.fivesecOnly, checked ? '1' : '0');
  window.location.reload();
}
document.getElementById('fivesec-toggle').addEventListener('change', e => _setFivesec(e.target.checked));
document.getElementById('fivesec-toggle-mobile').addEventListener('change', e => _setFivesec(e.target.checked));

// Sequential checkbox -- persist only; takes effect on next NEXT press.
document.getElementById('sequential-toggle').addEventListener('change', e => {
  localStorage.setItem(LS_KEYS.sequential, e.target.checked ? '1' : '0');
});

// Ticker filter -- desktop + mobile mirrors. Persist + reload (since the
// catalog filter affects which session pickSession picks). Also refreshes
// the Sequential row visibility.
function _setTickerFilter(val) {
  if (val) localStorage.setItem(LS_KEYS.tickerFilter, val);
  else localStorage.removeItem(LS_KEYS.tickerFilter);
  window.location.reload();
}
tFilter.addEventListener('change', e => _setTickerFilter(e.target.value));
tFilterMobile.addEventListener('change', e => _setTickerFilter(e.target.value));

// NEW RANDOM button inside settings drawer = ignore current ticker filter,
// pick fully random session anywhere.
document.getElementById('btn-newrandom-settings').addEventListener('click', () => {
  const pool = filteredPool(true);
  if (!pool.length) { window.location.reload(); return; }
  const s = pool[Math.floor(Math.random() * pool.length)];
  sessionStorage.setItem(SS_KEYS.forceSession, JSON.stringify({ ticker: s.ticker, date: s.date }));
  window.location.reload();
});
// Candle bucket dropdown -- rebuild aggregations IN PLACE, no page reload.
// 30-sec is only meaningful in 5-sec mode; hide it in 1-min mode and snap
// to 1-min if user was already on 30-sec.
const _bSel = document.getElementById('bucket-select');

// Rebuild PREV_BARS / TODAY_BARS / live candle from scratch at the new
// bucket size, while preserving tickIdx, position, trades.
function rebuildBuckets(newBucketSec) {
  if (newBucketSec === CANDLE_BUCKET_SECONDS) return;
  CANDLE_BUCKET_SECONDS = newBucketSec;
  localStorage.setItem('scalp_sim_bucket_sec', String(newBucketSec));
  // Re-aggregate everything at the new bucket
  PREV_BARS_FULL = aggregateBars(expandBars(PREV_BARS_RAW), CANDLE_BUCKET_SECONDS);
  PREV_BARS = PREV_BARS_FULL.slice(-30);
  TODAY_BARS = aggregateBars(TODAY_TICKS, CANDLE_BUCKET_SECONDS);
  // Wipe the chart series and replay ticks 0..(tickIdx-1) to rebuild the
  // live candle at the new bucket size. Position state stays intact.
  const savedTickIdx = tickIdx;
  candleSeries.setData(PREV_BARS);
  liveCandle = null;
  liveTickPrice = null;
  tickIdx = 0;
  for (let i = 0; i < savedTickIdx; i++) {
    if (tickIdx >= TODAY_TICKS.length) break;
    const tick = TODAY_TICKS[tickIdx];
    const bucket = Math.floor(tick.time / CANDLE_BUCKET_SECONDS) * CANDLE_BUCKET_SECONDS;
    if (!liveCandle || liveCandle.time !== bucket) {
      const dt = new Date(bucket * 1000);
      const hhmm = String(dt.getUTCHours()).padStart(2, '0') + ':' +
                   String(dt.getUTCMinutes()).padStart(2, '0');
      liveCandle = {
        time: bucket,
        open: tick.open, high: tick.high, low: tick.low, close: tick.close,
        hhmm: hhmm,
      };
    } else {
      liveCandle.high = Math.max(liveCandle.high, tick.high);
      liveCandle.low = Math.min(liveCandle.low, tick.low);
      liveCandle.close = tick.close;
    }
    liveTickPrice = tick.close;
    candleSeries.update(liveCandle);
    tickIdx++;
  }
  // Re-set markers (they're keyed by timestamps that still exist) and
  // restore any active price lines (B/E, stop) for an open position.
  candleSeries.setMarkers(markers);
  if (position.side !== 'flat') {
    if (activePriceLine) { candleSeries.removePriceLine(activePriceLine); activePriceLine = null; }
    if (stopLine) { candleSeries.removePriceLine(stopLine); stopLine = null; }
    const bePrice = breakevenPrice(position.entry, position.shares, position.side);
    activePriceLine = candleSeries.createPriceLine({
      price: bePrice, color: '#4ade80', lineWidth: 2, lineStyle: 0,
      axisLabelVisible: true,
      title: position.side === 'long' ? 'B/E LONG' : 'B/E SHORT',
    });
    if (position.stopPrice !== null) {
      stopLine = candleSeries.createPriceLine({
        price: position.stopPrice, color: '#f87171', lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true,
        title: 'STOP ' + position.stopPct.toFixed(1) + '%',
      });
    }
  }
  updateUI(); updatePositionGradient(); updateOverlay();
  updatePnlTag(); updateSessionProgress();
}

if (_bSel) {
  if (!FIVE_SEC_MODE) {
    const _30opt = _bSel.querySelector('option[value="30"]');
    if (_30opt) _30opt.remove();
    if (CANDLE_BUCKET_SECONDS === 30) {
      CANDLE_BUCKET_SECONDS = 60;
      localStorage.setItem('scalp_sim_bucket_sec', '60');
    }
  }
  _bSel.value = String(CANDLE_BUCKET_SECONDS);
  _bSel.addEventListener('change', e => {
    rebuildBuckets(parseInt(e.target.value, 10));
  });

}

// +/- chart zoom buttons (same effect as mouse wheel on the chart).
// "+" = bars become wider (zoom in on recent candles).
// "-" = bars become narrower (more history visible).
// barSpacing-based zoom works reliably on mobile, unlike setVisibleLogicalRange
// which the touch handler can override during pinch/swipe.
function chartZoom(direction) {
  const ts = chart.timeScale();
  const cur = ts.options().barSpacing || 6;
  const factor = direction === 'in' ? 1.4 : 1 / 1.4;
  const next = Math.max(2, Math.min(80, cur * factor));
  ts.applyOptions({ barSpacing: next });
  ts.scrollToRealTime();   // keep the live candle anchored on the right
  rescaleY(true);          // explicit user zoom -- refit Y even if user had locked it
}
document.getElementById('chart-zoom-in').addEventListener('click', () => chartZoom('in'));
document.getElementById('chart-zoom-out').addEventListener('click', () => chartZoom('out'));

// ---- Mobile drawer toggles ----
function isMobile() { return window.matchMedia('(max-width: 700px)').matches; }

// Shorten button labels on mobile so 3-button rows actually fit.
{
  function applyMobileLabels() {
    const buy = document.getElementById('btn-buy');
    const sell = document.getElementById('btn-sell');
    const buyStop = document.getElementById('btn-buy-sl');
    const sellStop = document.getElementById('btn-sell-sl');
    // Find the "Stop %" text node so we can shorten it on mobile.
    const stopInput = document.getElementById('stop-pct');
    const stopLabel = stopInput ? stopInput.parentNode : null;
    if (isMobile()) {
      if (buy)  buy.textContent  = 'BUY';
      if (sell) sell.textContent = 'SHORT';
      if (buyStop)  buyStop.textContent  = 'BUY+SL';
      if (sellStop) sellStop.textContent = 'SHORT+SL';
      if (stopLabel && stopLabel.firstChild && stopLabel.firstChild.nodeType === 3) {
        stopLabel.firstChild.nodeValue = 'SL%';
      }
    } else {
      if (buy)  buy.textContent  = 'BUY (LONG, all-in)';
      if (sell) sell.textContent = 'SHORT (all-in)';
      if (buyStop)  buyStop.textContent  = 'BUY + STOP';
      if (sellStop) sellStop.textContent = 'SHORT + STOP';
      if (stopLabel && stopLabel.firstChild && stopLabel.firstChild.nodeType === 3) {
        stopLabel.firstChild.nodeValue = '\n        Stop %\n        ';
      }
    }
  }
  applyMobileLabels();
  window.addEventListener('resize', applyMobileLabels);
}
function openDrawer(which) {
  const dl = document.getElementById('compact-daylog');
  const tr = document.getElementById('compact-trades');
  const st = document.getElementById('compact-settings');
  const bd = document.getElementById('drawer-backdrop');
  dl.classList.toggle('open', which === 'daylog');
  tr.classList.toggle('open', which === 'trades');
  st.classList.toggle('open', which === 'settings');
  bd.classList.toggle('open', which !== null);
}
function closeDrawers() {
  document.getElementById('compact-daylog').classList.remove('open');
  document.getElementById('compact-trades').classList.remove('open');
  document.getElementById('compact-settings').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
}
{
  const fabD = document.getElementById('fab-daylog');
  const fabT = document.getElementById('fab-trades');
  const fabS = document.getElementById('fab-settings');
  const bd   = document.getElementById('drawer-backdrop');
  if (fabD && fabT && fabS && bd) {
    // Visibility now handled purely by CSS (mobile media query). Just close
    // drawers if user resizes back to desktop.
    window.addEventListener('resize', () => { if (!isMobile()) closeDrawers(); });
    fabD.addEventListener('click', () => {
      const dl = document.getElementById('compact-daylog');
      openDrawer(dl.classList.contains('open') ? null : 'daylog');
    });
    fabT.addEventListener('click', () => {
      const tr = document.getElementById('compact-trades');
      openDrawer(tr.classList.contains('open') ? null : 'trades');
    });
    fabS.addEventListener('click', () => {
      const st = document.getElementById('compact-settings');
      openDrawer(st.classList.contains('open') ? null : 'settings');
    });
    bd.addEventListener('click', closeDrawers);
  }
}

// If we just landed here from a sequential-mode wrap, show a brief toast.
{
  const wrappedTicker = sessionStorage.getItem(SS_KEYS.justWrapped);
  if (wrappedTicker) {
    sessionStorage.removeItem(SS_KEYS.justWrapped);
    const t = document.getElementById('wrap-toast');
    if (t) {
      t.textContent = 'Reached end of ' + wrappedTicker + ' -- wrapped to oldest day';
      // Wait one frame so the transition can run.
      requestAnimationFrame(() => { t.classList.add('show'); });
      setTimeout(() => { t.classList.remove('show'); }, 3000);
    }
  }
}

// ---- Setup metric cards ----
function fmtPct(v) {
  if (v === null || v === undefined) return "--";
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
}
function fmtMoney(v, sign) {
  if (v === null || v === undefined) return "$--.--";
  if (sign) return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
  return (v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(2);
}
function metricEl(label, val, cls) {
  const d = document.createElement('div');
  d.className = 'metric' + (cls ? ' ' + cls : '');
  const l = document.createElement('div'); l.className = 'label'; l.textContent = label;
  const v = document.createElement('div'); v.className = 'val'; v.textContent = val;
  d.appendChild(l); d.appendChild(v);
  return d;
}

function renderSetup() {
  const host = document.getElementById('setup');
  clearChildren(host);
  host.appendChild(metricEl('Ticker', TICKER, 'neu'));
  host.appendChild(metricEl('Date', DATE));
  host.appendChild(metricEl('Day', SETUP.dow));
  host.appendChild(metricEl('Prev close', '$' + (SETUP.prev_close || 0).toFixed(2)));
  host.appendChild(metricEl("Today's open", '$' + (SETUP.today_open || 0).toFixed(2)));
  host.appendChild(metricEl('Overnight gap', fmtPct(SETUP.overnight_gap_pct),
    SETUP.overnight_gap_pct > 0 ? 'good' : (SETUP.overnight_gap_pct < 0 ? 'bad' : '')));
  host.appendChild(metricEl("Today close (spoiler)", '$' + (SETUP.today_close || 0).toFixed(2)));
  host.appendChild(metricEl("Today intraday", fmtPct(SETUP.intraday_pct),
    SETUP.intraday_pct > 0 ? 'good' : 'bad'));
  host.appendChild(metricEl("Today total", fmtPct(SETUP.total_pct),
    SETUP.total_pct > 0 ? 'good' : 'bad'));
  host.appendChild(metricEl("Today high", '$' + (SETUP.today_high || 0).toFixed(2), 'good'));
  host.appendChild(metricEl("Today low", '$' + (SETUP.today_low || 0).toFixed(2), 'bad'));
}
renderSetup();

// ---- Chart ----
const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {
  layout: { background: { color: '#000000' }, textColor: '#cccccc' },
  grid: {
    vertLines: { color: '#1f1f1f', style: 2, visible: true },
    horzLines: { color: '#1f1f1f', style: 2, visible: true },
  },
  rightPriceScale: {
    borderColor: '#444444',
    scaleMargins: { top: 0.1, bottom: 0.1 },
  },
  timeScale: {
    borderColor: '#444444',
    timeVisible: true,
    secondsVisible: false,
    tickMarkFormatter: (time, tickMarkType) => {
      const dt = new Date(time * 1000);
      // TWS-style: show date labels at day-of-month boundaries,
      // time labels at hour boundaries within a day.
      if (tickMarkType === LightweightCharts.TickMarkType.Year ||
          tickMarkType === LightweightCharts.TickMarkType.Month ||
          tickMarkType === LightweightCharts.TickMarkType.DayOfMonth) {
        const month = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][dt.getUTCMonth()];
        return month + ' ' + dt.getUTCDate();
      }
      const h = String(dt.getUTCHours()).padStart(2, '0');
      const m = String(dt.getUTCMinutes()).padStart(2, '0');
      if (m !== '00') return '';
      return h + ':' + m;
    },
    barSpacing: 10,       // clean TWS-ish density; lightweight-charts hardcodes body/gap ratio
  },
  crosshair: {
    mode: 1,
    vertLine: { color: '#777777', style: 2, width: 1 },
    horzLine: { color: '#777777', style: 2, width: 1 },
  },
  height: chartEl.clientHeight,
  width: chartEl.clientWidth,
});

// TWS-style candles: solid green for up, solid red for down, white wicks
const candleSeries = chart.addCandlestickSeries({
  upColor: '#22cc55',             // solid green body for up
  downColor: '#cc2222',           // solid red body for down
  borderUpColor: '#22cc55',
  borderDownColor: '#cc2222',
  wickUpColor: '#cccccc',         // white-ish wick
  wickDownColor: '#cccccc',
  borderVisible: false,           // no per-bar borders -> bodies look glued together
  priceLineColor: '#ffeb3b',      // yellow current-price line (TWS style)
  priceLineWidth: 1,
  priceLineStyle: 2,              // dashed
  priceLineVisible: true,
  lastValueVisible: true,
});

// rightOffset 0 = live candle flush against the right axis (no gap).
// shiftVisibleRangeOnNewBar auto-scrolls left as new bars arrive.
const RIGHT_OFFSET = 0;
chart.timeScale().applyOptions({ rightOffset: RIGHT_OFFSET, shiftVisibleRangeOnNewBar: true });
candleSeries.setData(PREV_BARS);
function anchorRight() {
  chart.timeScale().scrollToRealTime();
}
anchorRight();

// Previous-close dashed line: rendered via HTML overlay (NOT createPriceLine)
// so it doesn't pull the Y-axis auto-scale toward itself when the price has
// moved far away. Position updates on every visible-range change.
function updatePrevCloseLine() {
  let line = document.getElementById('prev-close-line');
  let label = document.getElementById('prev-close-label');
  if (!SETUP.prev_close) {
    if (line) line.style.display = 'none';
    if (label) label.style.display = 'none';
    return;
  }
  if (!line) {
    line = document.createElement('div');
    line.id = 'prev-close-line';
    line.style.cssText =
      'position:absolute;left:0;right:0;height:0;' +
      'border-top:1px dashed #888;pointer-events:none;z-index:3;';
    _dayDividerHost.appendChild(line);
  }
  if (!label) {
    label = document.createElement('div');
    label.id = 'prev-close-label';
    // Sit on top of the right price axis, matching its dark grey background
    label.style.cssText =
      'position:absolute;right:0;background:#444;color:#cccccc;' +
      'font-family:Consolas,monospace;font-size:11px;font-weight:600;' +
      'padding:1px 6px;border-radius:2px;pointer-events:none;z-index:6;' +
      'transform:translateY(-50%);';
    label.textContent = SETUP.prev_close.toFixed(2);
    _dayDividerHost.appendChild(label);
  }
  // Match the right-axis width so the yellow current-price label (drawn on
  // canvas at the axis) sits in the same horizontal slot, then we hide the
  // grey when they're vertically close.
  const y = candleSeries.priceToCoordinate(SETUP.prev_close);
  if (y === null) {
    line.style.display = 'none';
    label.style.display = 'none';
    return;
  }
  line.style.display = 'block';
  line.style.top = y + 'px';
  // Hide the grey label if it would overlap the yellow current-price axis tag.
  // Yellow is painted on the canvas (so it inherently stays "in front" of
  // anything we draw to its left), but at the right-axis position they share
  // pixels. Threshold 22px = the yellow label's full height + padding.
  const cur = curPrice();
  if (cur !== null && cur !== undefined) {
    const curY = candleSeries.priceToCoordinate(cur);
    if (curY !== null && Math.abs(curY - y) < 22) {
      label.style.display = 'none';
      return;
    }
  }
  label.style.display = 'block';
  label.style.top = y + 'px';
}

let todayIdx = 0;
let tickIdx = 0;             // 5-sec tick index (5-sec mode only)
let liveCandle = null;       // current in-progress 10-min candle (5-sec mode)
let liveTickPrice = null;    // latest 5-sec close (drives yellow price line)
const markers = [];

// 5-sec mode: a "tick" advances 5 seconds and updates the live candle.
// 1-min mode: a "tick" advances one 10-min candle (TODAY_BARS[todayIdx]).
function curPrice() {
  if (FIVE_SEC_MODE) return liveTickPrice;
  const bar = curBar();
  return bar ? bar.close : null;
}

// ---- Day-boundary overlay ----
const ydayLabel = document.getElementById('yday-label');
const todayLabel = document.getElementById('today-label');
const ydayBg = document.getElementById('yday-bg');
const overnightTag = document.getElementById('overnight-tag');
ydayLabel.textContent = 'YESTERDAY ' + PREV_DATE;
todayLabel.textContent = 'TODAY ' + DATE;

function updateOverlay() {
  // Multi-day TWS-style view: no overnight gap visualization.
  // Continuous candles across days, with date labels handled by tickMarkFormatter.
  ydayBg.style.display = 'none';
  ydayLabel.style.display = 'none';
  todayLabel.style.display = 'none';
  overnightTag.style.display = 'none';
  updateDayDividers();
  updatePrevCloseLine();
  updatePnlTag();
}

// Live P&L badge in the green/red ticker-header strip (right-aligned).
// Shows "%move +/-$NET" while a position is open. Hidden when flat. Uses
// the same friction model as the realized P&L calc.
function updatePnlTag() {
  const tag = document.getElementById('th-pos-pnl');
  if (!tag) return;
  if (position.side === 'flat') { tag.style.display = 'none'; return; }
  const cur = curPrice();
  if (cur === null || cur === undefined) { tag.style.display = 'none'; return; }
  const exitFill = fillPrice(cur, position.side, false);
  const gross = position.side === 'long'
    ? (exitFill - position.entry) * position.shares
    : (position.entry - exitFill) * position.shares;
  const net = gross - 2 * COMMISSION_PER_SIDE_USD;
  const pctMove = position.side === 'long'
    ? (exitFill - position.entry) / position.entry * 100
    : (position.entry - exitFill) / position.entry * 100;
  const sideLabel = position.side === 'long' ? 'LONG' : 'SHORT';
  tag.textContent = sideLabel + '  ' +
                    (pctMove >= 0 ? '+' : '') + pctMove.toFixed(2) + '%  ' +
                    (net >= 0 ? '+' : '-') + '$' + Math.abs(net).toFixed(2);
  tag.style.background = net >= 0 ? '#1a7a2a' : '#9a2929';
  tag.style.color = '#fff';
  tag.style.display = 'inline-block';
}

// Big centered end-of-day P&L overlay shown when the last tick is processed.
function showEodSummary() {
  const box = document.getElementById('eod-summary');
  const amt = document.getElementById('eod-amount');
  const sub = document.getElementById('eod-sub');
  if (!box || !amt || !sub) return;
  const sign = realizedPnl >= 0 ? '+' : '-';
  amt.textContent = sign + '$' + Math.abs(realizedPnl).toFixed(2);
  const wr = tradeCount > 0 ? ((wins / tradeCount) * 100).toFixed(0) : '--';
  sub.textContent = tradeCount + ' trade' + (tradeCount === 1 ? '' : 's') +
                    '  ·  win rate ' + wr + '%' +
                    '  ·  ~CAD ' + sign + '$' +
                    Math.abs(realizedPnl * USD_TO_CAD).toFixed(2);
  box.classList.remove('pos', 'neg', 'flat');
  box.classList.add(realizedPnl > 0.001 ? 'pos' :
                    (realizedPnl < -0.001 ? 'neg' : 'flat'));
  box.style.display = 'block';
}

// Fill the session-progress bar based on % of trading day elapsed.
// Driven by the live tick's time, so it advances smoothly as advance() runs.
function updateSessionProgress() {
  const fill = document.getElementById('session-progress-fill');
  if (!fill || TODAY_BARS.length === 0) return;
  const sessionStart = TODAY_BARS[0].time;
  const sessionEnd = TODAY_BARS[TODAY_BARS.length - 1].time + CANDLE_BUCKET_SECONDS;
  const totalSec = sessionEnd - sessionStart;
  let elapsedSec = 0;
  if (tickIdx > 0 && tickIdx <= TODAY_TICKS.length) {
    elapsedSec = TODAY_TICKS[tickIdx - 1].time - sessionStart;
  }
  const pct = Math.max(0, Math.min(100, (elapsedSec / totalSec) * 100));
  fill.style.width = pct.toFixed(2) + '%';
}

// Render vertical dashed lines at each day boundary (where one bar's UTC date
// differs from the previous bar's). TWS-style multi-day separator.
const _dayDividerHost = document.getElementById('overlay');
function dayBoundariesFromData() {
  // We treat the (fake-UTC) bar timestamps as ET. A "day" is identified by the
  // YYYY-MM-DD prefix of the bar time.
  const data = candleSeries.data() || [];
  const out = [];
  let lastDay = null;
  for (const b of data) {
    const day = new Date(b.time * 1000).toISOString().slice(0, 10);
    if (lastDay !== null && day !== lastDay) out.push(b.time);
    lastDay = day;
  }
  return out;
}
function updateDayDividers() {
  // Wipe and redraw on every visible-range change. Cheap enough for ~5-15 lines.
  const existing = _dayDividerHost.querySelectorAll('.day-divider');
  existing.forEach(el => el.remove());
  const boundaries = dayBoundariesFromData();
  // timeToCoordinate returns the CENTER of the bar at that time. Subtract
  // half a barSpacing so the divider sits on the LEFT EDGE of the first
  // bar of the new day (i.e. between yesterday's close and today's open),
  // not bisecting the today-open candle.
  const barSpacing = chart.timeScale().options().barSpacing;
  for (const t of boundaries) {
    const x = chart.timeScale().timeToCoordinate(t);
    if (x === null) continue;       // off-screen
    const div = document.createElement('div');
    div.className = 'day-divider';
    div.style.left = (x - barSpacing / 2) + 'px';
    _dayDividerHost.appendChild(div);
  }
}

let playing = false;
let timer = null;
let position = { side: 'flat', shares: 0, entry: 0, stopPct: null, stopPrice: null };
let stopLine = null;   // dashed red price-line for visible stop loss
let realizedPnl = 0;
let tradeCount = 0;
let wins = 0;
let activePriceLine = null;
// Per-session trade history for the compact table:
//   { side, buyTime, sellTime, entry, exit, pctMove, pnl }
const sessionTrades = [];
let pendingEntry = null;

function applyPnlClass(el, v) {
  el.classList.remove('pnl-positive', 'pnl-negative', 'pnl-zero');
  if (v > 0.001) el.classList.add('pnl-positive');
  else if (v < -0.001) el.classList.add('pnl-negative');
  else el.classList.add('pnl-zero');
}

function logTrade(s) {
  const log = document.getElementById('trade-log');
  log.textContent = s + '\n' + log.textContent;
}

function curBar() {
  // advance() builds liveCandle in BOTH 5-sec and 1-min modes, so just return it.
  return liveCandle;
}

function updateTickerHeader() {
  const header = document.getElementById('ticker-header');
  document.getElementById('th-symbol').textContent = TICKER;
  const bar = curBar();
  if (!bar || !SETUP.prev_close) {
    document.getElementById('th-price').textContent = '$' + (SETUP.prev_close || 0).toFixed(2);
    document.getElementById('th-change-dollar').textContent = '0.00';
    document.getElementById('th-change-pct').textContent = '0.00%';
    header.classList.remove('up', 'down');
    return;
  }
  const change = bar.close - SETUP.prev_close;
  const pct = change / SETUP.prev_close * 100;
  document.getElementById('th-price').textContent = bar.close.toFixed(2);
  document.getElementById('th-change-dollar').textContent =
    (change >= 0 ? '+' : '') + change.toFixed(2);
  document.getElementById('th-change-pct').textContent =
    (change >= 0 ? '+' : '') + pct.toFixed(2) + '%';
  header.classList.remove('up', 'down');
  header.classList.add(change >= 0 ? 'up' : 'down');
}

// TWS-style gradient: profit region (saturated) -> entry line (faint) ->
// loss region (saturated). For longs, above entry = green (profit), below
// = red (loss). Reversed for shorts.
function updatePositionGradient() {
  const overlay = document.getElementById('pos-gradient');
  if (position.side === 'flat') { overlay.style.display = 'none'; return; }
  // Split the gradient at the *true breakeven* line (not raw fill), so the
  // green/red regions match the visible B/E horizontal line.
  const bePrice = breakevenPrice(position.entry, position.shares, position.side);
  let entryY = candleSeries.priceToCoordinate(bePrice);
  const height = chartEl.clientHeight;
  // If B/E is off-chart, decide which side it's on by comparing to current
  // price. Clamp the split so the gradient still renders meaningfully
  // (otherwise the overlay either disappears or shows a solid mis-color).
  if (entryY === null) {
    const cur = curPrice();
    if (cur === null || cur === undefined) {
      overlay.style.display = 'none';
      return;
    }
    // If B/E is BELOW visible range (cur > bePrice), entryY conceptually = height + epsilon
    // If B/E is ABOVE visible range (cur < bePrice), entryY conceptually = -epsilon
    entryY = cur > bePrice ? height + 5 : -5;
  }
  const pct = Math.max(0, Math.min(100, (entryY / height) * 100));
  const isLong = position.side === 'long';
  const profitTop = isLong ? 'rgba(74,222,128,0.35)' : 'rgba(248,113,113,0.35)';
  const profitMid = isLong ? 'rgba(74,222,128,0.04)' : 'rgba(248,113,113,0.04)';
  const lossMid   = isLong ? 'rgba(248,113,113,0.04)' : 'rgba(74,222,128,0.04)';
  const lossBot   = isLong ? 'rgba(248,113,113,0.35)' : 'rgba(74,222,128,0.35)';
  overlay.style.display = 'block';
  overlay.style.background =
    'linear-gradient(to bottom, ' +
    profitTop + ' 0%, ' +
    profitMid + ' ' + pct + '%, ' +
    lossMid   + ' ' + pct + '%, ' +
    lossBot   + ' 100%)';
}

function updateUI() {
  const bar = curBar();
  if (bar) {
    document.getElementById('pos-shares').textContent = position.shares.toFixed(2);
  }
  document.getElementById('pos-side').textContent = position.side.toUpperCase();
  document.getElementById('pos-shares').textContent = position.shares.toFixed(2);
  document.getElementById('pos-entry').textContent =
    position.entry > 0 ? '$' + position.entry.toFixed(2) : '$--.--';

  // Unrealized = what you'd actually pocket if you hit CLOSE right now
  // (exit-side spread + 2x commission already paid in mind).
  let unreal = 0;
  if (bar && position.side !== 'flat') {
    const exitFill = fillPrice(bar.close, position.side, false);
    const gross = position.side === 'long'
      ? (exitFill - position.entry) * position.shares
      : (position.entry - exitFill) * position.shares;
    unreal = gross - 2 * COMMISSION_PER_SIDE_USD;
  }
  const usdEl = document.getElementById('pos-unreal-usd');
  usdEl.textContent = fmtMoney(unreal, true);
  applyPnlClass(usdEl, unreal);
  const cadEl = document.getElementById('pos-unreal-cad');
  cadEl.textContent = fmtMoney(unreal * USD_TO_CAD, true);
  applyPnlClass(cadEl, unreal);

  const realUsdEl = document.getElementById('pnl-real-usd');
  realUsdEl.textContent = fmtMoney(realizedPnl, true);
  applyPnlClass(realUsdEl, realizedPnl);
  const realCadEl = document.getElementById('pnl-real-cad');
  realCadEl.textContent = fmtMoney(realizedPnl * USD_TO_CAD, true);
  applyPnlClass(realCadEl, realizedPnl);

  const total = realizedPnl + unreal;
  const totalUsdEl = document.getElementById('pnl-total-usd');
  totalUsdEl.textContent = fmtMoney(total, true);
  applyPnlClass(totalUsdEl, total);
  const totalCadEl = document.getElementById('pnl-total-cad');
  totalCadEl.textContent = fmtMoney(total * USD_TO_CAD, true);
  applyPnlClass(totalCadEl, total);

  document.getElementById('pnl-trades').textContent = tradeCount;

  if (bar && TODAY_BARS.length > 0) {
    // All-in baseline: hold the full starting cash in long from open
    const sizeUsd = 10000;
    const bhShares = sizeUsd / TODAY_BARS[0].open;
    const bhPnl = (bar.close - TODAY_BARS[0].open) * bhShares;
    const bhEl = document.getElementById('bh-pnl-usd');
    if (bhEl) {
      bhEl.textContent = fmtMoney(bhPnl, true);
      const diff = total - bhPnl;
      const vsBhEl = document.getElementById('vs-bh');
      if (vsBhEl) {
        vsBhEl.textContent = (diff >= 0 ? '+' : '') + '$' + diff.toFixed(2);
        applyPnlClass(vsBhEl, diff);
      }
    }
  }
  // Update the compact-mode account display in the controls bar
  const ctrlAcc = document.getElementById('ctrl-account-now');
  if (ctrlAcc) {
    const accVal = 10000 + realizedPnl + unreal;
    ctrlAcc.textContent = '$' + accVal.toFixed(2);
    ctrlAcc.style.color = accVal >= 10000 ? '#4ade80' : '#f87171';
  }

  const hudTime = document.getElementById('hud-time');
  const hudPrice = document.getElementById('hud-price');
  const hudChange = document.getElementById('hud-change');
  const hudPnlUsd = document.getElementById('hud-pnl-usd');
  const hudPnlCad = document.getElementById('hud-pnl-cad');
  if (bar) {
    hudTime.textContent = bar.hhmm;
    hudPrice.textContent = '$' + bar.close.toFixed(2);
    if (SETUP.prev_close) {
      const pct = (bar.close - SETUP.prev_close) / SETUP.prev_close * 100;
      hudChange.textContent = 'vs prev close: ' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
      hudChange.classList.remove('hud-positive', 'hud-negative');
      hudChange.classList.add(pct >= 0 ? 'hud-positive' : 'hud-negative');
    }
  } else {
    hudTime.textContent = '--:--';
    hudPrice.textContent = '$--.--';
    hudChange.textContent = 'vs prev close: --';
  }
  // HUD: total session P&L (realized + unrealized) + account value
  const STARTING_CASH_USD = 10000;
  const totalPnL = realizedPnl + unreal;
  const accountValueUsd = STARTING_CASH_USD + totalPnL;
  const accountValueCad = accountValueUsd * USD_TO_CAD;
  document.getElementById('hud-account-now').textContent =
    'Account now: $' + accountValueUsd.toFixed(2) + ' USD  /  $' + accountValueCad.toFixed(2) + ' CAD';
  hudPnlUsd.classList.remove('hud-flat', 'hud-positive', 'hud-negative');
  hudPnlCad.classList.remove('hud-flat', 'hud-positive', 'hud-negative');
  if (totalPnL === 0 && position.side === 'flat') {
    hudPnlUsd.textContent = '$0.00 USD';
    hudPnlUsd.classList.add('hud-flat');
    hudPnlCad.textContent = '$0.00 CAD';
    hudPnlCad.classList.add('hud-flat');
  } else {
    const sign = totalPnL >= 0 ? '+' : '-';
    hudPnlUsd.textContent = sign + '$' + Math.abs(totalPnL).toFixed(2) + ' USD';
    hudPnlUsd.classList.add(totalPnL >= 0 ? 'hud-positive' : 'hud-negative');
    hudPnlCad.textContent = sign + '$' + Math.abs(totalPnL * USD_TO_CAD).toFixed(2) + ' CAD';
    hudPnlCad.classList.add(totalPnL >= 0 ? 'hud-positive' : 'hud-negative');
  }
  updatePositionGradient();
  updateTickerHeader();
}

// Every STEP advances exactly one tick. The 10-min candle updates
// dynamically -- TWS-style live bar formation. Lightweight-charts handles
// auto-scroll-on-new-bar natively via shiftVisibleRangeOnNewBar; we don't
// need to manually scroll the visible range here.
function advance() {
  if (tickIdx >= TODAY_TICKS.length) { pause(); return; }
  const tick = TODAY_TICKS[tickIdx];
  const isLastTick = (tickIdx === TODAY_TICKS.length - 1);
  const bucket = Math.floor(tick.time / CANDLE_BUCKET_SECONDS) * CANDLE_BUCKET_SECONDS;

  if (!liveCandle || liveCandle.time !== bucket) {
    const dt = new Date(bucket * 1000);
    const hhmm = String(dt.getUTCHours()).padStart(2, '0') + ':' +
                 String(dt.getUTCMinutes()).padStart(2, '0');
    liveCandle = {
      time: bucket,
      open: tick.open, high: tick.high, low: tick.low, close: tick.close,
      hhmm: hhmm,
    };
  } else {
    liveCandle.high = Math.max(liveCandle.high, tick.high);
    liveCandle.low = Math.min(liveCandle.low, tick.low);
    liveCandle.close = tick.close;
  }
  liveTickPrice = tick.close;
  candleSeries.update(liveCandle);
  tickIdx++;
  // Stop loss check: compare this CURRENT tick's H/L (not liveCandle.H/L,
  // which is the cumulative high/low of the entire 10-min bucket and may
  // include moves from BEFORE the user entered the position).
  if (position.side !== 'flat' && position.stopPrice !== null) {
    const hit = position.side === 'long'
      ? (tick.low <= position.stopPrice)
      : (tick.high >= position.stopPrice);
    if (hit) closePosition();
  }
  // Update the visible clock in the chart title bar (12-hour ET, AM/PM)
  const _clk = document.getElementById('ctrl-time');
  if (_clk) {
    const td = new Date(tick.time * 1000);
    let h = td.getUTCHours();
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12;
    if (h === 0) h = 12;
    _clk.textContent =
      h + ':' +
      String(td.getUTCMinutes()).padStart(2, '0') + ':' +
      String(td.getUTCSeconds()).padStart(2, '0') + ' ' + ampm;
  }
  chart.timeScale().scrollToRealTime();
  updateUI();
  updatePositionGradient();   // gradient follows price-axis on each tick
  updateSessionProgress();    // fills the top-edge progress bar
  updatePnlTag();             // floating live P&L badge next to yellow tag
  // End-of-session: auto-close any open position at the last tick's price,
  // then show the big day-total overlay.
  if (isLastTick) {
    if (position.side !== 'flat') closePosition();
    recordDay();           // persist this day's P&L to localStorage
    showEodSummary();
    pause();
  }
}

function play() {
  if (tickIdx >= TODAY_TICKS.length) return;
  playing = true;
  document.getElementById('btn-play').textContent = 'PAUSE';
  document.getElementById('btn-play').classList.add('primary');
  const speed = parseInt(document.getElementById('speed').value);
  timer = setInterval(advance, speed);
}

function pause() {
  playing = false;
  document.getElementById('btn-play').textContent = 'PLAY';
  document.getElementById('btn-play').classList.remove('primary');
  if (timer) { clearInterval(timer); timer = null; }
}

function reset() {
  pause();
  candleSeries.setData(PREV_BARS);
  candleSeries.setMarkers([]);
  markers.length = 0;
  todayIdx = 0;
  tickIdx = 0;
  liveCandle = null;
  liveTickPrice = null;
  const _clk0 = document.getElementById('ctrl-time');
  if (_clk0) _clk0.textContent = '--:--:-- --';
  const _spf = document.getElementById('session-progress-fill');
  if (_spf) _spf.style.width = '0%';
  const _eod = document.getElementById('eod-summary');
  if (_eod) _eod.style.display = 'none';
  position = { side: 'flat', shares: 0, entry: 0, stopPct: null, stopPrice: null };
  if (stopLine) { try { candleSeries.removePriceLine(stopLine); } catch(e){} stopLine = null; }
  realizedPnl = 0;
  tradeCount = 0;
  wins = 0;
  document.getElementById('trade-log').textContent = '';
  sessionTrades.length = 0;
  pendingEntry = null;
  renderTradesTable();
  if (activePriceLine) { candleSeries.removePriceLine(activePriceLine); activePriceLine = null; }
  chart.timeScale().fitContent();
  // Always reveal the first bar so the user sees today's open immediately
  advance();
  setTimeout(() => { updateOverlay(); updatePositionGradient(); }, 50);
  updateUI();
}

// Realistic fill: longs pay the ask (~mid + half-spread/slippage), shorts hit
// the bid. Costs are split equally between entry and exit.
function fillPrice(midPrice, side, isEntry) {
  const halfBps = frictionBps(TICKER) / 2;
  const mult = halfBps / 10000;
  // Entry-long pays UP (ask); entry-short receives LESS (bid).
  // Exit-long sells at bid (LESS); exit-short covers at ask (UP).
  const longUp  = (side === 'long' && isEntry)  || (side === 'short' && !isEntry);
  return midPrice * (longUp ? (1 + mult) : (1 - mult));
}

// True breakeven price: the mid-price at which closing now would net zero
// after exit-side spread + 2x commission. Above-mid for long, below-mid for short.
function breakevenPrice(entryFill, shares, side) {
  const halfBps = frictionBps(TICKER) / 2;
  const mult = halfBps / 10000;
  const commPerShare = (2 * COMMISSION_PER_SIDE_USD) / shares;
  if (side === 'long') {
    // exit_fill = entryFill + commPerShare; exit_mid = exit_fill / (1 - mult)
    return (entryFill + commPerShare) / (1 - mult);
  }
  // short: exit_fill = entryFill - commPerShare; exit_mid = exit_fill / (1 + mult)
  return (entryFill - commPerShare) / (1 + mult);
}

function openPosition(side, stopPct) {
  const bar = curBar();
  if (!bar) return;                     // press STEP first
  if (position.side !== 'flat') return; // already open
  const accountValue = 10000 + realizedPnl;
  const sizeDollars = accountValue;
  if (!(sizeDollars > 0)) return;
  const entryFill = fillPrice(bar.close, side, true);
  const shares = sizeDollars / entryFill;
  position.side = side;
  position.shares = shares;
  position.entry = entryFill;           // store the actual fill, not the mid
  // Stop loss (optional): trigger when mid price moves against entryFill by stopPct%
  if (stopPct && stopPct > 0) {
    position.stopPct = stopPct;
    position.stopPrice = side === 'long'
      ? entryFill * (1 - stopPct / 100)
      : entryFill * (1 + stopPct / 100);
  } else {
    position.stopPct = null;
    position.stopPrice = null;
  }
  const bePrice = breakevenPrice(entryFill, shares, side);
  const arrow = side === 'long' ? '+' : '-';
  logTrade(bar.hhmm + ' ' + side.toUpperCase() + ' ' + shares.toFixed(2) +
           ' @ $' + entryFill.toFixed(2) +
           '  mid=$' + bar.close.toFixed(2) +
           '  B/E=$' + bePrice.toFixed(2) +
           '  (' + arrow + '$' + sizeDollars.toFixed(0) + ')');
  markers.push({
    time: bar.time,
    position: side === 'long' ? 'belowBar' : 'aboveBar',
    color: side === 'long' ? '#4ade80' : '#f87171',
    shape: side === 'long' ? 'arrowUp' : 'arrowDown',
    text: side === 'long' ? 'BUY' : 'SELL',
  });
  candleSeries.setMarkers(markers);
  if (activePriceLine) candleSeries.removePriceLine(activePriceLine);
  activePriceLine = candleSeries.createPriceLine({
    price: bePrice,                     // true breakeven after round-trip costs
    color: '#4ade80',
    lineWidth: 2,
    lineStyle: 0,                       // solid
    axisLabelVisible: true,
    title: side === 'long' ? 'B/E LONG' : 'B/E SHORT',
  });
  // Optional stop-loss line (red dashed, with price label)
  if (stopLine) { candleSeries.removePriceLine(stopLine); stopLine = null; }
  if (position.stopPrice !== null) {
    stopLine = candleSeries.createPriceLine({
      price: position.stopPrice,
      color: '#f87171',
      lineWidth: 1,
      lineStyle: 2,                     // dashed
      axisLabelVisible: true,
      title: 'STOP ' + position.stopPct.toFixed(1) + '%',
    });
  }
  pendingEntry = {
    side: side, buyTime: bar.hhmm || '--:--',
    entryFill: entryFill, entryMid: bar.close,
  };
  updateUI();
  updatePositionGradient();
}

function closePosition() {
  if (position.side === 'flat') return;
  const bar = curBar();
  if (!bar) return;
  const exitFill = fillPrice(bar.close, position.side, false);
  // "Ideal" P&L: pretend you got mid-mid fills with no commission
  const entryMid = pendingEntry ? pendingEntry.entryMid : position.entry;
  const idealShares = (10000 + realizedPnl) / entryMid;
  const idealPnl = position.side === 'long'
    ? (bar.close - entryMid) * idealShares
    : (entryMid - bar.close) * idealShares;
  // Realistic net P&L: bid/ask fills minus round-trip commission
  const grossPnl = position.side === 'long'
    ? (exitFill - position.entry) * position.shares
    : (position.entry - exitFill) * position.shares;
  const commission = 2 * COMMISSION_PER_SIDE_USD;   // entry + exit
  const pnl = grossPnl - commission;
  realizedPnl += pnl;
  tradeCount++;
  if (pnl > 0) wins++;
  const pnlStr = (pnl >= 0 ? '+' : '-') + '$' + Math.abs(pnl).toFixed(2);
  const pnlCadStr = (pnl >= 0 ? '+' : '-') + '$' + Math.abs(pnl * USD_TO_CAD).toFixed(2) + ' CAD';
  logTrade(bar.hhmm + ' CLOSE ' + position.side.toUpperCase() +
           ' @ $' + exitFill.toFixed(2) +
           '  mid=$' + bar.close.toFixed(2) +
           '  -> ' + pnlStr + ' (' + pnlCadStr + ')' +
           '  [comm $' + commission.toFixed(2) + ']');
  markers.push({
    time: bar.time, position: 'inBar', color: '#f6c143', shape: 'circle', text: 'X',
  });
  candleSeries.setMarkers(markers);
  if (activePriceLine) { candleSeries.removePriceLine(activePriceLine); activePriceLine = null; }
  if (stopLine) { candleSeries.removePriceLine(stopLine); stopLine = null; }
  // Record the completed trade for the compact table
  if (pendingEntry) {
    const pctMove = pendingEntry.side === 'long'
      ? (exitFill - pendingEntry.entryFill) / pendingEntry.entryFill * 100
      : (pendingEntry.entryFill - exitFill) / pendingEntry.entryFill * 100;
    sessionTrades.push({
      side: pendingEntry.side,
      buyTime: pendingEntry.buyTime,
      sellTime: bar.hhmm,
      entry: pendingEntry.entryFill,
      exit: exitFill,
      pctMove: pctMove,
      pnl: pnl,                         // net of all friction
      pnlGross: idealPnl,               // ideal mid-to-mid, no costs
    });
    pendingEntry = null;
    renderTradesTable();
  }
  position = { side: 'flat', shares: 0, entry: 0, stopPct: null, stopPrice: null };
  updateUI();
  updatePositionGradient();
  renderAllTimeStats();
}

function renderTradesTable() {
  const tbody = document.getElementById('trades-tbody');
  const empty = document.getElementById('trades-empty');
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  if (sessionTrades.length === 0) {
    if (empty) empty.style.display = 'block';
    return;
  }
  if (empty) empty.style.display = 'none';
  // Most recent first
  for (let i = sessionTrades.length - 1; i >= 0; i--) {
    const t = sessionTrades[i];
    const tr = document.createElement('tr');
    const cls = t.pnl >= 0 ? 'win' : 'loss';
    function td(text, extraCls) {
      const cell = document.createElement('td');
      if (extraCls) cell.className = extraCls;
      cell.textContent = text;
      return cell;
    }
    const gross = t.pnlGross !== undefined ? t.pnlGross : t.pnl;
    const grossCls = gross >= 0 ? 'win' : 'loss';
    tr.appendChild(td(t.buyTime, 'l'));
    tr.appendChild(td(t.sellTime, 'l'));
    tr.appendChild(td((t.pctMove >= 0 ? '+' : '') + t.pctMove.toFixed(2) + '%', cls));
    tr.appendChild(td((gross >= 0 ? '+' : '-') + '$' + Math.abs(gross).toFixed(2), grossCls));
    const netCell = td((t.pnl >= 0 ? '+' : '-') + '$' + Math.abs(t.pnl).toFixed(2), cls);
    netCell.style.background = 'rgba(255,255,255,0.06)';   // emphasize NET column
    tr.appendChild(netCell);
    tbody.appendChild(tr);
  }
}

function loadAllTimeStats() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEYS.stats) || '{}');
    return Object.assign({
      sessions: 0, total_pnl_usd: 0, total_trades: 0, total_wins: 0,
      last_session: null,
    }, s);
  } catch (e) {
    return { sessions: 0, total_pnl_usd: 0, total_trades: 0, total_wins: 0, last_session: null };
  }
}

function renderAllTimeStats() {
  const s = loadAllTimeStats();
  const sessionKey = TICKER + '_' + DATE;
  let sessions = s.sessions;
  if (tradeCount > 0 && s.last_session !== sessionKey) sessions++;
  const totalPnl = (s.total_pnl_usd || 0) + realizedPnl;
  const totalTrades = (s.total_trades || 0) + tradeCount;
  const totalWins = (s.total_wins || 0) + wins;
  document.getElementById('all-sessions').textContent = sessions;
  const usdEl = document.getElementById('all-pnl-usd');
  usdEl.textContent = fmtMoney(totalPnl, true);
  applyPnlClass(usdEl, totalPnl);
  const cadEl = document.getElementById('all-pnl-cad');
  cadEl.textContent = fmtMoney(totalPnl * USD_TO_CAD, true);
  applyPnlClass(cadEl, totalPnl);
  const wr = totalTrades > 0 ? (totalWins / totalTrades * 100).toFixed(0) + '%' : '--';
  document.getElementById('all-wr').textContent = wr + ' (' + totalWins + '/' + totalTrades + ')';
}

window.addEventListener('beforeunload', () => {
  const s = loadAllTimeStats();
  const sessionKey = TICKER + '_' + DATE;
  if (tradeCount > 0) {
    s.total_pnl_usd = (s.total_pnl_usd || 0) + realizedPnl;
    s.total_trades = (s.total_trades || 0) + tradeCount;
    s.total_wins = (s.total_wins || 0) + wins;
    if (s.last_session !== sessionKey) {
      s.sessions = (s.sessions || 0) + 1;
      s.last_session = sessionKey;
    }
    try { localStorage.setItem(LS_KEYS.stats, JSON.stringify(s)); } catch(e) {}
  }
});

function resetAllTimeStats() {
  if (confirm('Wipe all-time scalp sim stats?')) {
    localStorage.removeItem(LS_KEYS.stats);
    renderAllTimeStats();
  }
}

// ---- Day Log (per-day session P&L, persisted in localStorage) ----
function loadDayLog() {
  try { return JSON.parse(localStorage.getItem(LS_KEYS.dayLog) || '[]'); }
  catch (e) { return []; }
}
function saveDayLog(arr) {
  try { localStorage.setItem(LS_KEYS.dayLog, JSON.stringify(arr)); } catch (e) {}
}
// Append (or replace) the current session's day-log entry. Called from
// NEXT DAY, end-of-session, and NEW RANDOM when leaving a session with
// at least one trade.
function recordDay() {
  if (tradeCount === 0) return;          // don't log empty days
  const arr = loadDayLog();
  // If we already have a row for this exact (ticker,date), update it
  // instead of duplicating (e.g. user closes & opens same session twice).
  const key = TICKER + '_' + DATE;
  const startCash = 10000;
  const pctMove = (realizedPnl / startCash) * 100;
  const entry = {
    ticker: TICKER, date: DATE,
    pnl: realizedPnl, pct: pctMove,
    trades: tradeCount, wins: wins,
    when: Date.now(),
  };
  const existing = arr.findIndex(e => e.ticker === entry.ticker && e.date === entry.date);
  if (existing >= 0) arr[existing] = entry; else arr.push(entry);
  saveDayLog(arr);
  renderDayLog();
}
function renderDayLog() {
  const tbody = document.getElementById('daylog-tbody');
  const empty = document.getElementById('daylog-empty');
  const totalEl = document.getElementById('daylog-total-pnl');
  const countEl = document.getElementById('daylog-total-count');
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  const arr = loadDayLog();
  if (arr.length === 0) {
    if (empty) empty.style.display = 'block';
    if (totalEl) { totalEl.textContent = '$0.00'; totalEl.style.color = '#ccc'; }
    if (countEl) countEl.textContent = '0';
    return;
  }
  if (empty) empty.style.display = 'none';
  // newest first
  const sorted = [...arr].sort((a, b) => b.when - a.when);
  for (const e of sorted) {
    const tr = document.createElement('tr');
    const cls = e.pnl >= 0 ? 'win' : 'loss';
    function td(text, extraCls) {
      const cell = document.createElement('td');
      if (extraCls) cell.className = extraCls;
      cell.textContent = text;
      return cell;
    }
    tr.appendChild(td(e.date.slice(5), 'l'));   // MM-DD
    tr.appendChild(td(e.ticker, 'l'));
    tr.appendChild(td((e.pct >= 0 ? '+' : '') + e.pct.toFixed(2) + '%', cls));
    tr.appendChild(td((e.pnl >= 0 ? '+' : '-') + '$' + Math.abs(e.pnl).toFixed(2), cls));
    tbody.appendChild(tr);
  }
  const total = arr.reduce((s, e) => s + e.pnl, 0);
  if (totalEl) {
    totalEl.textContent = (total >= 0 ? '+' : '-') + '$' + Math.abs(total).toFixed(2);
    totalEl.style.color = total > 0.01 ? '#4ade80' : (total < -0.01 ? '#f87171' : '#ccc');
  }
  if (countEl) countEl.textContent = arr.length;
}
function resetDayLog() {
  if (confirm('Wipe all day-log history? This cannot be undone.')) {
    localStorage.removeItem(LS_KEYS.dayLog);
    renderDayLog();
  }
}

// Find the next chronologically-ordered session for a given ticker.
// Wraps to the oldest session if we're already on the newest. Returns
// {session, wrapped} so the caller can show a wrap toast.
function nextSequentialForTicker(ticker, currentDate) {
  const sameTicker = filteredPool(true)
    .filter(s => s.ticker === ticker)
    .sort((a, b) => a.date.localeCompare(b.date));
  if (!sameTicker.length) return { session: null, wrapped: false };
  const idx = sameTicker.findIndex(s => s.date === currentDate);
  if (idx < 0) return { session: sameTicker[0], wrapped: false };
  if (idx + 1 >= sameTicker.length) return { session: sameTicker[0], wrapped: true };
  return { session: sameTicker[idx + 1], wrapped: false };
}

// Primary "give me another session" action. Behavior depends on settings:
//   - No ticker filter           -> random session anywhere
//   - Ticker filter + sequential -> next chronological day for that ticker (wraps)
//   - Ticker filter + random     -> random session for that ticker
function goToNextSession() {
  const hasPos = position.side !== 'flat';
  if (hasPos) {
    const ok = confirm('Move to next session? Your open ' + position.side.toUpperCase() +
                       ' position will be CLOSED at the current price first.');
    if (!ok) return;
    closePosition();
  }
  recordDay();

  const tickerFilter = localStorage.getItem(LS_KEYS.tickerFilter) || '';
  const sequential = localStorage.getItem(LS_KEYS.sequential) === '1';

  if (tickerFilter && sequential) {
    const { session, wrapped } = nextSequentialForTicker(tickerFilter, DATE);
    if (!session) {
      alert('No sessions available for ' + tickerFilter + '.');
      return;
    }
    if (wrapped) sessionStorage.setItem(SS_KEYS.justWrapped, tickerFilter);
    sessionStorage.setItem(SS_KEYS.forceSession,
      JSON.stringify({ ticker: session.ticker, date: session.date }));
  } else {
    // Random path -- pick now (so consecutive presses give different sessions
    // even with the same ticker filter) and force it for the reload.
    const pool = filteredPool(false);
    if (!pool.length) { window.location.reload(); return; }
    const s = pool[Math.floor(Math.random() * pool.length)];
    sessionStorage.setItem(SS_KEYS.forceSession,
      JSON.stringify({ ticker: s.ticker, date: s.date }));
  }
  window.location.reload();
}

// TWS-style Y-axis behavior:
//   - Time-scale changes (pan, zoom) refit Y to the visible bars
//   - BUT if the user has manually dragged the price axis to set their own
//     range, respect that. Lightweight-charts sets autoScale=false internally
//     on user price-axis drag; we read that flag and skip the refit.
//   - Explicit user actions (RECENTER button, NEXT session) can pass force=true
//     to re-enable autoScale.
const _BASE_SCALE_MARGINS = { top: 0.1, bottom: 0.1 };
let _autoRescaling = false;
let _epsilonFlip = false;
function rescaleY(force) {
  if (_autoRescaling) return;          // re-entrancy guard
  const ps = chart.priceScale('right');
  // If user manually adjusted the Y axis (autoScale = false), don't override
  // unless this is an explicit force (e.g. recenter button).
  if (!force && ps.options().autoScale === false) return;
  _autoRescaling = true;
  ps.applyOptions({ autoScale: true });
  _epsilonFlip = !_epsilonFlip;
  const eps = _epsilonFlip ? 0.0001 : 0;
  ps.applyOptions({
    scaleMargins: { top: _BASE_SCALE_MARGINS.top + eps, bottom: _BASE_SCALE_MARGINS.bottom },
  });
  _autoRescaling = false;
}
chart.timeScale().subscribeVisibleTimeRangeChange(() => {
  rescaleY();
  updateOverlay(); updatePositionGradient();
});
chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
  rescaleY();
  updateOverlay(); updatePositionGradient();
});
// TWS-style: when zooming with the wheel, keep the right edge anchored to the
// latest bar (extra bars appear on the LEFT, not on both sides). The user can
// still pan left freely by dragging.
chartEl.addEventListener('wheel', () => {
  setTimeout(() => {
    const range = chart.timeScale().getVisibleLogicalRange();
    if (range) {
      const lastEdge = (candleSeries.data() || []).length - 0.5 + RIGHT_OFFSET;
      const width = range.to - range.from;
      // Re-anchor right edge to (last bar + RIGHT_OFFSET) so the live candle
      // always has breathing room from the price axis after a wheel zoom.
      chart.timeScale().setVisibleLogicalRange({
        from: lastEdge - width, to: lastEdge,
      });
    }
    rescaleY(true);    // TWS behavior: wheel zoom is explicit, refit Y
    updateOverlay(); updatePositionGradient();
  }, 30);
}, { passive: true });
chartEl.addEventListener('mouseup', () => {
  setTimeout(() => { updateOverlay(); updatePositionGradient(); }, 10);
});
setTimeout(updateOverlay, 100);

window.addEventListener('resize', () => {
  chart.applyOptions({ height: chartEl.clientHeight, width: chartEl.clientWidth });
  setTimeout(() => { updateOverlay(); updatePositionGradient(); }, 50);
});

function setZoomSplit() {
  // Half yesterday + half today (centered on the overnight boundary)
  if (!PREV_BARS.length || !TODAY_BARS.length) return;
  const prevMid = PREV_BARS[Math.floor(PREV_BARS.length / 2)].time;
  const todayMid = TODAY_BARS[Math.floor(TODAY_BARS.length / 2)].time;
  chart.timeScale().setVisibleRange({ from: prevMid, to: todayMid });
}

function setZoomToday() {
  // Today's session with the last bit of yesterday for context
  if (!TODAY_BARS.length) return;
  const startTime = PREV_BARS.length
    ? PREV_BARS[Math.max(0, PREV_BARS.length - 15)].time
    : TODAY_BARS[0].time;
  const endTime = TODAY_BARS[TODAY_BARS.length - 1].time;
  chart.timeScale().setVisibleRange({ from: startTime, to: endTime });
}

function setZoomCloseup() {
  // 1-hour window centered on the current revealed bar (or today open if none yet)
  const bar = curBar() || TODAY_BARS[0];
  if (!bar) return;
  const half = 30 * 60;
  chart.timeScale().setVisibleRange({ from: bar.time - half, to: bar.time + half });
}

// Tick = 5s in 5-sec mode, 1min in 1-min mode. stepN advances N ticks fast.
const TICK_SECONDS = FIVE_SEC_MODE ? 5 : 60;
function stepN(seconds) {
  pause();
  const ticksToAdvance = Math.max(1, Math.round(seconds / TICK_SECONDS));
  for (let i = 0; i < ticksToAdvance; i++) {
    if (tickIdx >= TODAY_TICKS.length) break;
    advance();
  }
}
// STEP = one tick (smallest unit). 1m/5m = multi-tick jumps.
// Press-and-hold any of these for >= 1 second to enter repeat mode (acts
// like holding the Space bar). Tap = single fire.
//
// setPointerCapture is the key: it locks pointer events to the button so
// the repeat keeps firing even if the finger drifts slightly off the button
// bounds. Without it, pointerleave would stop the repeat prematurely.
function attachHoldRepeat(btn, fireFn, initialDelay = 1000, interval = 110) {
  let holdTimer = null;
  let repeatTimer = null;
  let suppressClick = false;
  let activePointerId = null;
  function start(e) {
    if (activePointerId !== null) return;   // already holding from another finger
    activePointerId = e.pointerId;
    try { btn.setPointerCapture(e.pointerId); } catch (err) {}
    suppressClick = true;
    fireFn();    // immediate single fire on press
    holdTimer = setTimeout(() => {
      repeatTimer = setInterval(fireFn, interval);
    }, initialDelay);
  }
  function stop(e) {
    if (e && activePointerId !== null && e.pointerId !== activePointerId) return;
    activePointerId = null;
    if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
    if (repeatTimer) { clearInterval(repeatTimer); repeatTimer = null; }
    // Let the synthetic click that may follow pointerup be swallowed first.
    setTimeout(() => { suppressClick = false; }, 400);
  }
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', stop);
  btn.addEventListener('pointercancel', stop);
  // No pointerleave -- with setPointerCapture, the button keeps receiving
  // events until real pointerup/cancel, regardless of finger drift.
  // Block the synthetic click after touch so we don't double-fire.
  btn.addEventListener('click', e => {
    if (suppressClick) { e.preventDefault(); e.stopPropagation(); }
  }, true);
}

const _step1m = document.getElementById('btn-step-1m');
const _step5m = document.getElementById('btn-step-5m');
// In 5-sec mode show +1m and +5m. In 1-min mode show only +5m
// (since STEP already advances 1 minute).
if (FIVE_SEC_MODE) {
  _step1m.style.display = '';
  _step5m.style.display = '';
} else {
  _step1m.style.display = 'none';
  _step5m.style.display = '';
  document.getElementById('btn-step').textContent = 'STEP +1m';
  _step5m.textContent = '+5m';
}
attachHoldRepeat(document.getElementById('btn-step'), () => stepN(TICK_SECONDS));
attachHoldRepeat(_step1m, () => stepN(60));
attachHoldRepeat(_step5m, () => stepN(300));
document.getElementById('btn-play').addEventListener('click', () => { if (playing) pause(); else play(); });
document.getElementById('btn-reset').addEventListener('click', () => {
  if (confirm('Reset the session? You will lose your open position, trades, and progress for this day.')) reset();
});
document.getElementById('btn-buy').addEventListener('click', () => openPosition('long'));
document.getElementById('btn-sell').addEventListener('click', () => openPosition('short'));
document.getElementById('btn-close').addEventListener('click', closePosition);
function _getStopPct() {
  const raw = parseFloat(document.getElementById('stop-pct').value);
  return (isFinite(raw) && raw > 0) ? raw : null;
}
document.getElementById('btn-buy-sl').addEventListener('click',
  () => openPosition('long', _getStopPct()));
document.getElementById('btn-sell-sl').addEventListener('click',
  () => openPosition('short', _getStopPct()));
document.getElementById('speed').addEventListener('change', () => { if (playing) { pause(); play(); } });
document.getElementById('reset-stats').addEventListener('click', resetAllTimeStats);
document.getElementById('eod-close-x').addEventListener('click',
  () => { document.getElementById('eod-summary').style.display = 'none'; });
document.getElementById('daylog-reset').addEventListener('click', resetDayLog);
document.getElementById('btn-next').addEventListener('click', goToNextSession);

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === ' ') { e.preventDefault(); pause(); advance(); }
  else if (e.key.toLowerCase() === 'p') { if (playing) pause(); else play(); }
  else if (e.key.toLowerCase() === 'b') openPosition('long');
  else if (e.key.toLowerCase() === 's') openPosition('short');
  else if (e.key.toLowerCase() === 'c') closePosition();
  else if (e.key.toLowerCase() === 'r') reset();
  else if (e.key.toLowerCase() === 'f') { e.preventDefault(); stepN(300); } // hold F to fast-forward 5min
  else if (e.key === '1') setZoomSplit();
  else if (e.key === '2') setZoomToday();
  else if (e.key === '3') setZoomCloseup();
});

// Show current ticker's friction model in the topbar
const _fricLabel = document.getElementById('friction-label');
if (_fricLabel) {
  _fricLabel.textContent =
    'friction ' + frictionBps(TICKER) + ' bps RT + $' +
    (2 * COMMISSION_PER_SIDE_USD).toFixed(0) + ' comm';
}
updateUI();
renderAllTimeStats();
renderDayLog();
// No pre-roll: chart loads in "pre-market" state (prev bars visible,
// clock at --:--:--). First STEP click reveals the first tick.
// Initial view: ~25 bars of prev session for context. If Y range feels
// wrong on gap days, user can drag the price axis to lock their own range.
setTimeout(() => {
  const data = candleSeries.data() || [];
  if (data.length > 0) {
    const visible = Math.min(data.length, 25);
    const lastEdge = data.length - 0.5 + RIGHT_OFFSET;
    chart.timeScale().setVisibleLogicalRange({
      from: lastEdge - visible,
      to: lastEdge,
    });
    rescaleY(true);
  }
}, 100);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description='Generate scalp sim HTML')
    parser.add_argument('ticker', nargs='?', default=None)
    parser.add_argument('date', nargs='?', default=None)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()

    print('Loading all sessions...')
    catalog, bars_by_key, bars_5s_by_key = load_all_data()
    print(f'Total: {len(catalog)} sessions across {len(set(c["ticker"] for c in catalog))} tickers')

    # If ticker/date given, narrow the catalog to just that session
    out_name = 'explorer.html'
    if args.ticker:
        ticker = args.ticker.upper()
        if args.date:
            catalog = [c for c in catalog if c['ticker'] == ticker and c['date'] == args.date]
            out_name = f'sim_{ticker}_{args.date}.html'
        else:
            catalog = [c for c in catalog if c['ticker'] == ticker]
            out_name = f'sim_{ticker}.html'
        if not catalog:
            raise SystemExit('No matching sessions')
        print(f'Filtered catalog: {len(catalog)} sessions')

    import datetime as _dt
    build_stamp = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html = (HTML_TEMPLATE
            .replace('__CATALOG__', json.dumps(catalog))
            .replace('__BARS__', json.dumps(bars_by_key))
            .replace('__BARS_5S__', json.dumps(bars_5s_by_key))
            .replace('__BUILD_STAMP__', build_stamp))
    print(f'Build stamp: {build_stamp}')
    out_path = os.path.join(SCRIPT_DIR, out_name)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f'\nWrote {out_path} ({size_mb:.1f} MB)')

    if not args.no_open:
        webbrowser.open('file://' + out_path.replace('\\', '/'))
        print('Opened in browser.')

    print()
    print('CONTROLS:')
    print('  Refresh page          = new random session')
    print('  NEXT button           = pick another session')
    print('  Ticker filter         = restrict to one ticker')
    print('  STEP / Space          = next bar')
    print('  PLAY / P              = auto-play (0.1x to 5x)')
    print('  BUY / B               = open long')
    print('  SELL / S              = open short')
    print('  CLOSE / C             = exit position')


if __name__ == '__main__':
    main()
