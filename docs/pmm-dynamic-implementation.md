# PMM Dynamic: how Hummingbot does it, and how we'll build ours

Source reviewed: `strategies/controllers/market_making/pmm_dynamic.py` (the
Hummingbot v2 controller in this repo) plus the behavior it inherits from
`MarketMakingControllerBase`. Target: a `PMMDynamic` strategy for our stdlib
bot (`bot/strategies.py`), reusing the existing runner, dashboard, and
backtester.

## 1. What Hummingbot's pmm_dynamic actually does

It is PMM with two quantities recomputed from candles every refresh, plus
per-position risk management:

**Dynamic spreads (NATR).** Spreads are configured in *units of volatility*
(default `1,2,4`), not percent. Each tick the controller computes
`natr = NATR(high, low, close, length=14) / 100` (a fraction, e.g. 0.004 =
0.4%) from 3-minute candles. A configured spread of `2` means the order sits
`2 × natr` away from the reference price. Volatile market → wider quotes,
quiet market → tighter quotes, automatically.

**Dynamic reference price (MACD shift).** Instead of quoting around the mid,
it shifts the center price by a momentum signal:

```
macd, hist   = MACD(close, fast=21, slow=42, signal=9)
macd_z       = -(macd - mean(macd)) / std(macd)     # z-score, NEGATED → fade
hist_sign    = +1 if hist > 0 else -1               # histogram sign → follow
price_mult   = (0.5 · macd_z + 0.5 · hist_sign) · (natr / 2)
reference    = close · (1 + price_mult)
```

Two halves pulling opposite ways by design: the negated z-score *fades*
stretched momentum (MACD far above its mean → shade quotes down, expect
reversion), while the histogram sign *follows* the short-term trend. The
shift magnitude is scaled by `natr/2`, so the center moves at most roughly
half a volatility unit (the z-score can exceed ±1, so somewhat more in
extremes).

**Risk layer (triple barrier).** Each fill becomes a `PositionExecutor` with
stop-loss / take-profit / time-limit / trailing-stop from the base config —
risk is managed per position, not just per session.

Base-class behavior worth copying: executor refresh (cancel + requote after
`executor_refresh_time`), total budget distributed across levels, and a
cooldown after fills.

Quirks to be aware of (we can do slightly better):

- The z-score is computed over the whole fetched candle window (~142 rows),
  not a fixed rolling window — the signal drifts with the fetch size.
- The reference is anchored to the last candle **close**, which is up to one
  candle interval stale; we already track a live microprice, which is a
  strictly better anchor.

## 2. What we already have (mapping)

| Hummingbot | Ours (today) |
|---|---|
| candles feed (3m, ~142 rows) | runner fetches 3m klines every 60 s into `state.candles` (`bot.py:446`, limit=100 — needs bump to 150) |
| reference price (close + shift) | `state.fair` = live microprice (`strategies.py:38`) — better anchor |
| spreads in vol units × NATR | `PMMSimple` static % spreads — the piece to replace |
| inventory shading | `PMMSimple` skew + hard inventory cap — keep as-is |
| executor refresh / cooldown | runner `max_age` + `tolerance` repricing (`bot.py:463-479`) — keep |
| budget across levels | `total_amount_quote / n_levels` in `PMMSimple` — keep |
| triple barrier per fill | none yet — phase 2 (see §4) |
| PnL / kill switch | session `max_loss` kill switch — keep |

So the real work is: **pure-python NATR + MACD, and a `PMMDynamic` class that
turns them into `desired_orders()`**. Everything else is plumbing we own.

## 3. Implementation plan

### 3.1 Indicators (pure stdlib, `strategies.py`)

```python
def ema(values, period):                    # standard EMA, k = 2/(n+1)
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def natr(highs, lows, closes, length=14):   # Wilder ATR / close, fraction
    trs = [h - l for h, l in zip(highs[:1], lows[:1])]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
    atr = sum(trs[:length]) / length        # seed, then Wilder smoothing
    for tr in trs[length:]:
        atr = (atr * (length - 1) + tr) / length
    return atr / closes[-1]

def macd_features(closes, fast=21, slow=42, signal=9):
    macd = [f - s for f, s in zip(ema(closes, fast), ema(closes, slow))]
    hist = [m - s for m, s in zip(macd, ema(macd, signal))]
    window = macd[-100:]                    # FIXED window (hummingbot: whole df)
    mean = sum(window) / len(window)
    std = (sum((x - mean) ** 2 for x in window) / len(window)) ** 0.5 or 1e-12
    z = (macd[-1] - mean) / std
    return -z, (1 if hist[-1] > 0 else -1)
```

### 3.2 The strategy class (`strategies.py`)

`PMMDynamic(PMMSimple)` — same fill tracking, inventory ratio, skew and cap;
only the quote geometry changes:

```python
def desired_orders(self, state):
    if not state.candles or len(state.candles) < self.macd_slow + self.macd_signal:
        return []                                    # warm-up: quote nothing
    highs/lows/closes = parse(state.candles)         # same as Supertrend does
    vol = natr(highs, lows, closes, self.natr_length)
    macd_z, hist_sign = macd_features(closes, ...)
    shift = (0.5 * macd_z + 0.5 * hist_sign) * (vol / 2)
    center = state.fair * (1 + Decimal(shift))       # live microprice, not close
    center *= (1 - self.skew * inventory_ratio * Decimal(vol))  # keep our shading
    for i, units in enumerate(self.spreads_in_vol):  # e.g. [1, 2]
        offset = Decimal(units) * Decimal(vol)
        yield BUY  at center * (1 - offset)          # tag PMMD-B{i}
        yield SELL at center * (1 + offset)          # tag PMMD-S{i}
    # tolerance: offset * 0.2, max_age: executor_refresh_time (same as PMMSimple)
```

Notes:
- spreads are **units of volatility** (floats like `1, 2`), a new mental
  model for the user — the dashboard label must say so.
- inventory skew now scales with `vol` too (PMMSimple scales with the inner
  spread; with dynamic spreads those are the same thing).
- warm-up guard: no quotes until enough candles (≈ 51 rows at defaults).

### 3.3 Wiring (each is a small diff)

1. **Runner** (`bot.py`): candle fetch `limit=100 → 150`; add
   `"pmm_dynamic"` to strategy construction with its args; add argparse flags
   `--pmmd-spreads`, `--natr-length`, `--macd-fast/slow/signal`.
   The 60 s candle refresh is fine — indicators live on 3 m bars.
2. **API** (`api_server.py`): add `pmm_dynamic` to `VALID_STRATEGIES`; add
   the new keys to `NUMERIC_FLAGS`.
3. **Dashboard** (`webapp/app/dashboard/page.js`): 4th segment
   "MM dynamic" with fields: spreads in vol units (`1, 2`), NATR length (14),
   MACD fast/slow/signal (21/42/9). Refresh/budget/leverage reuse the shared
   fields.
4. **Backtester** (`backtest.py`): it already feeds candles through the same
   `MarketState`; register the strategy so `pmm_dynamic` is comparable
   against grid/pmm/supertrend in the comparison table.

### 3.4 Defaults

`spreads_in_vol=1,2 · natr_length=14 · macd=21/42/9 · candles=3m ·
refresh=15s` — matches Hummingbot's defaults except we start with two levels
instead of three (smaller budget slices on testnet).

## 4. Phase 2 — the triple barrier

Skipped in v1 (inventory cap + session `max_loss` still protect us), then:

- per-fill TP/SL as **reduce-only price-triggered orders** on the exchange —
  we already ship Binance Algo Order support (commit `cffa8d2`); Gate futures
  has the equivalent `price_orders` endpoint (to add in `gate_client.py`);
- time-limit exit: runner-side — market-close any position older than
  `time_limit` seconds.

Exchange-side triggers are deliberately preferred over bot-side monitoring:
they survive a bot crash, which is the failure mode that matters.

## 5. Order of work

1. Indicators + `PMMDynamic` + unit test against known values (one sitting).
2. Runner/API flags + dashboard segment.
3. Backtest registration → compare vs `pmm` over the same week of candles;
   we expect fewer, better-placed quotes in trends and tighter quotes in chop.
4. Live dry-run on Gate futures testnet, then small live budget.
5. Phase 2 triple barrier.
