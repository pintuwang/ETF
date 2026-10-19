#!/usr/bin/env python3
"""
SGX Xtrackers backfill + daily updater.

Builds a ~10-year DAILY simulated history in SGD for the four SGX-listed
Xtrackers UCITS ETFs (XUS, EUS, XND, XWR), using US-listed proxy ETFs
(SPY, RSP, QQQ, URTH) and USD/SGD FX from Yahoo Finance.

  First run (or --full): downloads everything from start_date.
  Later runs:            downloads only the last ~10 days and merges.

Raw data is stored UNADJUSTED (close + dividends) so the total-return index
is rebuilt from scratch every run. Yahoo's 'Adj Close' is NOT stored, because
it rewrites the whole history every time a dividend is paid.

Outputs (all in data/):
  raw/<SYMBOL>.csv      date, close, dividends, splits, volume
  backfill_daily.csv    wide table: FX, proxy TR, simulated Xtrackers in USD & SGD,
                        and actual SGX closes once Yahoo carries them
  summary.json          CAGR, volatility, max drawdown per fund (SGD)
  status.json           last run time, per-symbol status, warnings
"""
import argparse, json, math, os, sys, time
from datetime import datetime, timedelta, timezone
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
OVERLAP_DAYS = 10          # re-download this many days on incremental runs
MISMATCH_TOL = 0.005       # 0.5% close mismatch on overlap -> full refetch (split/revision)
JUMP_WARN = 0.20           # warn on >20% single-day move in a broad index ETF


# ---------------------------------------------------------------- helpers
def safe_json_load(path, default):
    try:
        with open(path) as f:
            txt = f.read().strip()
        return json.loads(txt) if txt else default
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_write(path, writer):
    tmp = path + ".tmp"
    writer(tmp)
    os.replace(tmp, path)


def write_csv(df, path):
    atomic_write(path, lambda p: df.to_csv(p, index=True, date_format="%Y-%m-%d"))


def write_json(obj, path):
    def w(p):
        with open(p, "w") as f:
            json.dump(obj, f, indent=2, default=str)
    atomic_write(path, w)


def load_raw(symbol):
    path = os.path.join(RAW, f"{symbol}.csv")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        df = pd.read_csv(path, index_col="date", parse_dates=["date"])
        return df if len(df) else None
    except Exception:
        return None


# ---------------------------------------------------------------- fetching
def yahoo_history(symbol, start, retries=4):
    """Return DataFrame[date -> close, dividends, splits, volume] or raise."""
    import yfinance as yf
    last_err = None
    for attempt in range(retries):
        try:
            h = yf.Ticker(symbol).history(start=start, interval="1d",
                                          auto_adjust=False, actions=True)
            if h is None or h.empty:
                raise ValueError("empty response")
            out = pd.DataFrame({
                "close": h["Close"].astype(float),
                "dividends": h.get("Dividends", 0.0),
                "splits": h.get("Stock Splits", 0.0),
                "volume": h.get("Volume", 0),
            })
            # Yahoo returns tz-aware exchange-local timestamps; keep the calendar date.
            out.index = pd.to_datetime([d.date() for d in out.index])
            out.index.name = "date"
            out = out[~out.index.duplicated(keep="last")].sort_index()
            out = out[out["close"] > 0].dropna(subset=["close"])
            if out.empty:
                raise ValueError("no valid closes")
            return out
        except Exception as e:  # rate limits, network, empty data
            last_err = e
            time.sleep(5 * (2 ** attempt))
    raise RuntimeError(f"{symbol}: {last_err}")


FETCH = yahoo_history  # swapped out in tests


def update_symbol(symbol, start_date, full, warnings):
    """Incrementally update data/raw/<symbol>.csv. Returns the full DataFrame."""
    old = None if full else load_raw(symbol)
    if old is None:
        new = FETCH(symbol, start_date)
        merged, mode = new, "full"
    else:
        since = (old.index.max() - timedelta(days=OVERLAP_DAYS)).strftime("%Y-%m-%d")
        new = FETCH(symbol, since)
        overlap = old.index.intersection(new.index)
        refetch = False
        if new["splits"].fillna(0).ne(0).any():
            warnings.append(f"{symbol}: split in update window -> full refetch")
            refetch = True
        elif len(overlap):
            diff = (new.loc[overlap, "close"] / old.loc[overlap, "close"] - 1).abs().max()
            if diff > MISMATCH_TOL:
                warnings.append(f"{symbol}: overlap close mismatch {diff:.2%} -> full refetch")
                refetch = True
        if refetch:
            merged, mode = FETCH(symbol, start_date), "full-refetch"
        else:
            merged = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
            mode = f"incremental (+{len(new.index.difference(old.index))} rows)"

    jumps = merged["close"].pct_change().abs()
    for d in jumps[jumps > JUMP_WARN].index:
        warnings.append(f"{symbol}: {jumps[d]:.1%} move on {d.date()} (check, not removed)")
    write_csv(merged, os.path.join(RAW, f"{symbol}.csv"))
    return merged, mode


# ---------------------------------------------------------------- build
def tr_index(px, div_keep=1.0, annual_carry=0.0):
    """Total-return index from close + dividends.
    div_keep: fraction of each dividend retained (1 - withholding leakage).
    annual_carry: extra annual return (proxy TER add-back minus target TER),
                  accrued by calendar days between observations."""
    close, div = px["close"], px["dividends"].fillna(0.0)
    growth = (close + div * div_keep) / close.shift(1)
    days = pd.Series(px.index, index=px.index).diff().dt.days
    growth = growth * (1 + annual_carry) ** (days / 365.0)
    growth.iloc[0] = 1.0
    return 100.0 * growth.cumprod()


def max_drawdown(s):
    dd = s / s.cummax() - 1
    return float(dd.min()), str(dd.idxmin().date())


def build(cfg, raw, fx):
    funds = [f for f in cfg["funds"] if f.get("active", True)]
    wht = cfg["us_dividend_wht"]
    # Common calendar = US trading days (union of proxy dates); FX forward-filled.
    dates = sorted(set().union(*[raw[f["proxy"]].index for f in funds]))
    idx = pd.DatetimeIndex(dates, name="date")
    usdsgd = fx["close"].reindex(idx.union(fx.index)).sort_index().ffill(limit=5).reindex(idx)
    out = pd.DataFrame(index=idx)
    out["usdsgd"] = usdsgd

    summary = {"as_of": None, "start": None, "funds": {}}
    for f in funds:
        px = raw[f["proxy"]].reindex(idx).dropna(subset=["close"])
        tag = f["sgx"]
        proxy_tr = tr_index(px)
        sim_usd = tr_index(px, div_keep=1 - wht * f["us_weight"],
                           annual_carry=f["proxy_ter"] - f["xt_ter"])
        fx_f = usdsgd.reindex(px.index)
        sim_sgd = 100.0 * (sim_usd / sim_usd.iloc[0]) * (fx_f / fx_f.iloc[0])
        out[f"{tag}_proxy_tr_usd"] = proxy_tr
        out[f"{tag}_sim_usd"] = sim_usd
        out[f"{tag}_sim_sgd"] = sim_sgd
        if f.get("sgx_yahoo") and f["sgx_yahoo"] in raw:
            out[f"{tag}_actual_sgx_sgd"] = raw[f["sgx_yahoo"]]["close"].reindex(idx)

        s = sim_sgd.dropna()
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        rets = s.pct_change().dropna()
        mdd, mdd_date = max_drawdown(s)
        # Calendar-year returns: base for each year = previous year-end (or the start point).
        ye = s.groupby(s.index.year).last()
        base = ye.shift(1)
        base.iloc[0] = s.iloc[0]
        cal = {int(y): round(ye[y] / base[y] - 1, 5) for y in ye.index
               if not (y == s.index[0].year and ye[y] == s.iloc[0])}  # drop stub start year
        summary["funds"][tag] = {
            "name": f["name"], "proxy": f["proxy"],
            "cagr_sgd": round((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1, 5),
            "cagr_usd": round((sim_usd.iloc[-1] / sim_usd.iloc[0]) ** (1 / yrs) - 1, 5),
            "vol_ann_sgd": round(float(rets.std() * math.sqrt(252)), 5),
            "max_drawdown_sgd": round(mdd, 5), "max_drawdown_trough": mdd_date,
            "calendar_returns_sgd": cal,
        }
        summary["start"] = str(s.index[0].date())
        summary["as_of"] = str(s.index[-1].date())
    return out, summary


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="ignore stored data, redownload all")
    args = ap.parse_args(argv)

    cfg = safe_json_load(os.path.join(ROOT, "tickers.json"), None)
    if not cfg:
        sys.exit("tickers.json missing or empty")
    os.makedirs(RAW, exist_ok=True)
    start = cfg["start_date"]
    funds = [f for f in cfg["funds"] if f.get("active", True)]

    warnings, status, raw, hard_fail = [], {}, {}, False
    required = [cfg["fx"]["symbol"]] + [f["proxy"] for f in funds]
    optional = [f["sgx_yahoo"] for f in funds if f.get("sgx_yahoo")]

    for sym in required + optional:
        try:
            raw[sym], mode = update_symbol(sym, start, args.full, warnings)
            status[sym] = {"ok": True, "mode": mode, "last_date": str(raw[sym].index.max().date()),
                           "rows": len(raw[sym])}
        except Exception as e:
            cached = load_raw(sym)
            if cached is not None:
                raw[sym] = cached           # keep going on stale data, but flag it
            status[sym] = {"ok": False, "error": str(e)[:300],
                           "stale_last_date": str(cached.index.max().date()) if cached is not None else None}
            if sym in required:
                hard_fail = True

    missing = [s for s in required if s not in raw]
    if not missing:
        table, summary = build(cfg, raw, raw[cfg["fx"]["symbol"]])
        write_csv(table.round(6), os.path.join(DATA, "backfill_daily.csv"))
        write_json(summary, os.path.join(DATA, "summary.json"))
    write_json({"run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbols": status, "warnings": warnings, "missing_required": missing},
               os.path.join(DATA, "status.json"))

    for w in warnings:
        print("WARN", w)
    for s, st in status.items():
        tag = "OK " if st["ok"] else ("ERR" if s in required else "n/a")
        print(f"{s:8s} {tag} {st.get('mode') or st.get('error')}")
    if hard_fail:
        sys.exit(1)   # red X in Actions so a broken feed doesn't go unnoticed


if __name__ == "__main__":
    main()
