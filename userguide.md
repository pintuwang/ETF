# SGX Xtrackers 10-year backfill: User Guide

## What this does
Builds a **daily** simulated SGD price history, from 31 Dec 2015, for the four Xtrackers UCITS ETFs that listed on SGX on 13 Oct 2026, and keeps it updated every weekday.

| SGX | Index | Proxy used for history |
|---|---|---|
| XUS | S&P 500 | SPY |
| EUS | S&P 500 Equal Weight | RSP |
| XND | Nasdaq-100 | QQQ |
| XWR | MSCI World | URTH |

## How the simulation works
For each trading day:

    growth = (close + dividend x (1 - 15% x US_weight)) / previous close
             x (1 + proxy_TER - Xtrackers_TER) ^ (calendar_days / 365)

- **Dividends:** the 15% US withholding tax that an Irish UCITS fund suffers is deducted from each actual dividend the proxy paid. No yield estimate is needed.
- **Fees:** the proxy's own TER is added back, since its price is already net of it, and the Xtrackers TER is subtracted.
- **SGD:** the USD index is multiplied by `SGD=X` (SGD per USD), forward-filled onto US trading days.

## Files
| File | Contents |
|---|---|
| `tickers.json` | Config: funds, proxies, TERs, US weight, WHT. Set `"active": false` to pause a fund |
| `data/raw/<SYMBOL>.csv` | Unadjusted close, dividends, splits, volume |
| `data/backfill_daily.csv` | The database: `usdsgd`, `<SGX>_proxy_tr_usd`, `<SGX>_sim_usd`, `<SGX>_sim_sgd` (all indexed to 100 at start), plus `<SGX>_actual_sgx_sgd` once Yahoo carries the SGX tickers |
| `data/summary.json` | SGD CAGR, USD CAGR, annualised volatility, max drawdown and trough date, and calendar-year returns |
| `data/status.json` | Last run, per-symbol OK/error, warnings |

## Setup
1. Create a repo and add these files, keeping the folder structure: `.github/workflows/update_prices.yml`.
2. Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.
3. Go to **Actions → Update SGX Xtrackers backfill → Run workflow**, tick **full_backfill**, and run it once. This builds the 10-year history (about 2,700 rows per symbol).
4. After that it runs automatically at 06:30 SGT, Tuesday to Saturday.

## Safety behaviour
- **Incremental runs** re-download the last 10 days and merge them.
- **A split in the window, or a close mismatch of more than 0.5% against stored data**, triggers a full refetch of that symbol, because Yahoo back-adjusts history.
- **If a required feed fails**, the run keeps the old data, records the error in `status.json`, and the run shows a red X. The SGX `.SI` tickers are optional and only show `n/a` if missing.
- **Single-day moves above 20%** are flagged in warnings but never deleted.

## Known limits
Not modelled: securities-lending income, sampling and tracking error, SGX bid-ask spread and premium/discount, and the timing mismatch between the FX close and the US close. URTH (MSCI World) has small daily tracking noise from non-US markets closing earlier. Treat the series as an approximation for analysis, not as the funds' real NAV.
