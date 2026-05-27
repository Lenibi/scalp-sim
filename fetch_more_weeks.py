"""
Fetch additional SOXL + MSTR 24/7 1-min data and split into per-day files
for the scalp-sim-mobile app.

Currently the app has data 2025-12-30 -> 2026-05-15. Fetch:
- Recent: 2026-05-18 to 2026-05-27 (the missing 8 trading days)
- Older: 2025-09 to 2025-12 (extend backward 4 months)

Run on TWS port 7496 (live, read-only).
"""
import os
import sys
import time
import json
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'scheduled_trading'))
from ib_insync import IB, Stock

OUT_DIR = Path(__file__).parent / 'data' / 'days_1min'
OUT_DIR.mkdir(parents=True, exist_ok=True)
TICKERS = ['SOXL', 'MSTR']

print('Connecting to IBKR live (7496)...', flush=True)
ib = IB()
try:
    ib.connect('127.0.0.1', 7496, clientId=9601, timeout=15)
    print('  Connected', flush=True)
except Exception as e:
    print(f'  FAILED: {e}', flush=True)
    print('  Trying paper port 7497...', flush=True)
    try:
        ib.connect('127.0.0.1', 7497, clientId=9601, timeout=15)
    except Exception as e2:
        print(f'  Both failed: {e2}', flush=True)
        sys.exit(1)


def fetch_session(contract, end_dt_str, duration='1 D'):
    """Fetch one chunk of 1-min bars."""
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_dt_str,
            durationStr=duration,
            barSizeSetting='1 min',
            whatToShow='TRADES',
            useRTH=False,
            formatDate=2,
        )
        return bars or []
    except Exception as e:
        print(f'    Error: {e}', flush=True)
        return []


def fetch_24h_for_date(ticker, date_str):
    """Fetch 24 hours for a given session date.
    The "session" is 4 AM ET of date through next-day 4 AM ET.
    We combine SMART (regular + extended) + OVERNIGHT chunks.
    """
    out_path = OUT_DIR / f'{ticker}_{date_str}.json'
    if out_path.exists():
        return False  # already have

    smart = Stock(ticker, 'SMART', 'USD')
    ib.qualifyContracts(smart)
    overnight = Stock(ticker, 'OVERNIGHT', 'USD')
    try:
        ib.qualifyContracts(overnight)
    except Exception:
        overnight = None

    # Try one 24h chunk ending date+1 04:00 ET
    end_dt = f'{date_str.replace("-", "")}-12:00:00'  # end of session day noon
    all_bars = []
    smart_bars = fetch_session(smart, end_dt, '2 D')
    all_bars.extend(smart_bars)
    time.sleep(0.5)
    if overnight is not None:
        ov_bars = fetch_session(overnight, end_dt, '2 D')
        all_bars.extend(ov_bars)
        time.sleep(0.5)

    if not all_bars:
        return False

    # Dedupe by timestamp
    seen = {}
    for b in all_bars:
        ts = b.date
        if hasattr(ts, 'isoformat'):
            key = ts.isoformat()
        else:
            key = str(ts)
        # Prefer SMART over OVERNIGHT if duplicate
        if key not in seen:
            seen[key] = b

    # Filter to session day: keep only bars in ET date == date_str OR (prev day & hh>=20)
    # Simpler: just keep all bars from start of date_str (00:00 ET) to end (23:59 ET)
    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    out_bars = []
    for b in seen.values():
        bd = b.date.date() if hasattr(b.date, 'date') else None
        if bd == target_date:
            # Compute ET hhmm
            try:
                if hasattr(b.date, 'astimezone'):
                    et = b.date.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York'))
                else:
                    et = b.date
                hhmm = et.strftime('%H:%M')
                out_bars.append({
                    't': int(et.timestamp()),
                    'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close,
                    'v': b.volume,
                    'm': hhmm,
                })
            except Exception:
                continue

    if not out_bars:
        return False

    out_bars.sort(key=lambda x: x['t'])
    with open(out_path, 'w') as f:
        json.dump(out_bars, f, separators=(',', ':'))
    print(f'  wrote {out_path.name} ({len(out_bars)} bars)', flush=True)
    return True


# Build list of dates to fetch
# Recent: 2026-05-18 through 2026-05-27 (skip weekends)
# Older: 2025-09-01 through 2025-12-29 (extend backward)
def weekday_dates(start_str, end_str):
    start = datetime.strptime(start_str, '%Y-%m-%d').date()
    end = datetime.strptime(end_str, '%Y-%m-%d').date()
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    return out


# Two ranges
recent = weekday_dates('2026-05-18', '2026-05-27')
older = weekday_dates('2025-09-01', '2025-12-29')

# Existing files
existing_dates = set()
for f in OUT_DIR.glob('*.json'):
    parts = f.stem.rsplit('_', 1)
    if len(parts) == 2:
        existing_dates.add(parts[1])

todo = []
for ticker in TICKERS:
    for d in recent + older:
        if d not in existing_dates:
            todo.append((ticker, d))

print(f'\nWill fetch {len(todo)} (ticker, date) pairs (recent + older)\n', flush=True)

written = 0
for i, (ticker, d) in enumerate(todo):
    if i % 10 == 0:
        print(f'[{i}/{len(todo)}] fetching {ticker} {d}...', flush=True)
    try:
        if fetch_24h_for_date(ticker, d):
            written += 1
        time.sleep(0.3)  # IBKR rate limit
    except Exception as e:
        print(f'  ERROR for {ticker} {d}: {e}', flush=True)
        time.sleep(2)

ib.disconnect()
print(f'\nDONE. Wrote {written} new files.', flush=True)
