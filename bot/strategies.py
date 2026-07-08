"""
Live (simplified) implementations of the three PoC strategies, parameter-
compatible with the hummingbot controllers in strategies/controllers/:

  GridStrike   <- controllers/generic/grid_strike.py
  PMMSimple    <- controllers/market_making/pmm_simple.py
  Supertrend   <- controllers/directional_trading/supertrend_v1.py

Each strategy exposes:
  tags()                    -> iterable of client-order-id tags it owns
  desired_orders(state)     -> list of Order it wants resting/executed now
  on_fill(tag, order_body)  -> notification that one of its orders filled
  summary()                 -> human-readable session summary

An Order is a plain dict: {tag, side, type, price, qty, quote_qty} — prices and
quantities are Decimals; the runner rounds them to exchange filters and handles
placement/cancellation, so strategies stay pure decision logic.
"""
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


def round_to(value, increment, mode=ROUND_HALF_UP):
    return (value / increment).quantize(Decimal("1"), rounding=mode) * increment


class MarketState:
    """Snapshot handed to strategies each tick."""

    def __init__(self, mid, bid, ask, filters, candles=None, ts=None):
        self.mid = mid
        self.bid = bid
        self.ask = ask
        self.filters = filters      # tick_size / step_size / min_notional
        self.candles = candles      # list of [open_time, o, h, l, c, ...] or None
        self.ts = ts or time.time()


# ---------------------------------------------------------------- grid_strike

class GridStrike:
    """BUY-side grid between start_price and end_price; a filled level places a
    take-profit SELL one grid step above (the controller's per-level barrier)."""

    PREFIX = "GS"

    def __init__(self, start_price=None, end_price=None, limit_price=None,
                 n_levels=8, total_amount_quote=Decimal("200"), max_open_orders=3):
        # price specs: absolute ("59000"), % offset from the startup mid
        # ("-3%"), or None for the default — resolved on the first tick
        self.start_spec = start_price
        self.end_spec = end_price
        self.limit_spec = limit_price
        self.start_price = self.end_price = self.limit_price = None
        self.n_levels = n_levels
        self.total_amount_quote = Decimal(total_amount_quote)
        self.max_open_orders = max_open_orders
        self.levels = []
        self.pending_tp = {}                    # tag -> tp Order
        self.fills = 0
        self.tp_fills = 0

    @staticmethod
    def _resolve_price(spec, mid, default_pct):
        if spec in (None, ""):
            return mid * (1 + Decimal(default_pct) / 100)
        s = str(spec).strip()
        if s.endswith("%"):
            return mid * (1 + Decimal(s[:-1]) / 100)
        return Decimal(s)

    def _init_grid(self, mid):
        self.start_price = self._resolve_price(self.start_spec, mid, -3)
        self.end_price = self._resolve_price(self.end_spec, mid, 3)
        if self.limit_spec in (None, ""):
            self.limit_price = self.start_price * Decimal("0.98")
        else:
            self.limit_price = self._resolve_price(self.limit_spec, mid, 0)
        if not (self.limit_price <= self.start_price < self.end_price):
            raise ValueError(
                f"grid prices out of order: limit {self.limit_price} <= "
                f"start {self.start_price} < end {self.end_price} must hold")
        step = (self.end_price - self.start_price) / self.n_levels
        self.levels = [self.start_price + step * i for i in range(self.n_levels)]
        self.step = step

    def tags(self):
        return ([f"{self.PREFIX}-L{i}" for i in range(self.n_levels)]
                + [f"{self.PREFIX}-T{i}" for i in range(self.n_levels)])

    def desired_orders(self, state):
        if not self.levels:
            self._init_grid(state.mid)
        # outside [limit_price, end_price] -> pull everything (controller bounds)
        if not (self.limit_price <= state.mid <= self.end_price):
            return list(self.pending_tp.values())
        amount_per_level = self.total_amount_quote / self.n_levels
        orders = []
        below = [(i, p) for i, p in enumerate(self.levels) if p < state.bid]
        for i, price in below[-self.max_open_orders:]:
            tag = f"{self.PREFIX}-L{i}"
            if f"{self.PREFIX}-T{i}" in self.pending_tp:
                continue        # level already filled, waiting on its take-profit
            orders.append({"tag": tag, "side": "BUY", "type": "LIMIT_MAKER",
                           "price": price, "quote_qty": amount_per_level})
        orders.extend(self.pending_tp.values())
        return orders

    def on_fill(self, tag, order_body):
        if tag.startswith(f"{self.PREFIX}-L"):
            self.fills += 1
            i = int(tag.split("-L")[1])
            qty = Decimal(order_body.get("executedQty", "0"))
            tp_tag = f"{self.PREFIX}-T{i}"
            self.pending_tp[tp_tag] = {
                "tag": tp_tag, "side": "SELL", "type": "LIMIT_MAKER",
                "price": self.levels[i] + self.step, "qty": qty,
            }
        elif tag.startswith(f"{self.PREFIX}-T"):
            self.tp_fills += 1
            self.pending_tp.pop(tag, None)

    def summary(self):
        return (f"grid_strike: {self.fills} grid buys filled, "
                f"{self.tp_fills} take-profits filled, "
                f"{len(self.pending_tp)} take-profits still resting")


# ----------------------------------------------------------------- pmm_simple

class PMMSimple:
    """Symmetric maker quotes at configured spreads around the mid price,
    refreshed on drift or age — the classic ORDERS-bucket consumer."""

    PREFIX = "PMM"

    def __init__(self, buy_spreads=(Decimal("0.001"), Decimal("0.003")),
                 sell_spreads=(Decimal("0.001"), Decimal("0.003")),
                 total_amount_quote=Decimal("200"), executor_refresh_time=60):
        self.buy_spreads = [Decimal(s) for s in buy_spreads]
        self.sell_spreads = [Decimal(s) for s in sell_spreads]
        self.total_amount_quote = Decimal(total_amount_quote)
        self.executor_refresh_time = executor_refresh_time
        self.fills = 0

    def tags(self):
        return ([f"{self.PREFIX}-B{i}" for i in range(len(self.buy_spreads))]
                + [f"{self.PREFIX}-S{i}" for i in range(len(self.sell_spreads))])

    def desired_orders(self, state):
        n_levels = len(self.buy_spreads) + len(self.sell_spreads)
        amount = self.total_amount_quote / n_levels
        orders = []
        for i, spread in enumerate(self.buy_spreads):
            orders.append({"tag": f"{self.PREFIX}-B{i}", "side": "BUY",
                           "type": "LIMIT_MAKER",
                           "price": state.mid * (1 - spread), "quote_qty": amount,
                           "tolerance": spread * Decimal("0.2"),
                           "max_age": self.executor_refresh_time})
        for i, spread in enumerate(self.sell_spreads):
            orders.append({"tag": f"{self.PREFIX}-S{i}", "side": "SELL",
                           "type": "LIMIT_MAKER",
                           "price": state.mid * (1 + spread), "quote_qty": amount,
                           "tolerance": spread * Decimal("0.2"),
                           "max_age": self.executor_refresh_time})
        return orders

    def on_fill(self, tag, order_body):
        self.fills += 1

    def summary(self):
        return f"pmm_simple: {self.fills} maker fills"


# ------------------------------------------------------------- entry filters

def bullish_fvgs(candles, lookback=100):
    """Unfilled bullish fair value gaps, newest last. 3-candle definition:
    candle A's HIGH < candle C's LOW leaves a gap (A.high, C.low) carved by the
    displacement candle B. The gap dies when a later candle's low trades back
    through the bottom of the zone."""
    zones = []
    cs = candles[-lookback:]
    for i in range(len(cs) - 2):
        gap_bottom = float(cs[i][2])        # high of candle A
        gap_top = float(cs[i + 2][3])       # low of candle C
        if gap_top > gap_bottom:
            invalidated = any(float(c[3]) <= gap_bottom for c in cs[i + 3:])
            if not invalidated:
                zones.append((gap_bottom, gap_top))
    return zones


class EntryFilter:
    """Gates long entries. Modes:
      always     no gate — enter whenever flat
      trend      supertrend direction must be up
      fvg        price must have retraced INTO an unfilled bullish FVG
      trend+fvg  both
    Shared by the spot strategy, the futures trader, and the backtester."""

    MODES = ("always", "trend", "fvg", "trend+fvg")

    def __init__(self, mode="always", length=20, multiplier=4.0):
        if mode not in self.MODES:
            raise ValueError(f"entry mode {mode!r} not in {self.MODES}")
        self.mode = mode
        self.length = length
        self.multiplier = multiplier

    def ok(self, candles, price):
        """-> (allowed, reason). price is a float; candles are raw klines."""
        if self.mode == "always":
            return True, "always-in"
        if not candles or len(candles) < self.length + 3:
            return False, "waiting for candle history"
        reasons = []
        if "trend" in self.mode:
            highs = [float(k[2]) for k in candles]
            lows = [float(k[3]) for k in candles]
            closes = [float(k[4]) for k in candles]
            _, direction = supertrend(highs, lows, closes,
                                      self.length, self.multiplier)
            if direction != 1:
                return False, "trend is DOWN"
            reasons.append("trend up")
        if "fvg" in self.mode:
            zones = bullish_fvgs(candles)
            hit = next((z for z in zones if z[0] <= price <= z[1]), None)
            if hit is None:
                return False, (f"price not inside any of the "
                               f"{len(zones)} open bullish FVGs")
            reasons.append(f"in FVG {hit[0]:.2f}-{hit[1]:.2f}")
        return True, " + ".join(reasons)


# --------------------------------------------------------------- cj_compound

class CjCompound:
    """CJ's compounding strategy: always ALL-IN with current capital, take
    profit at +target (default 3%), optional stop loss, then immediately
    re-enter with the grown (or shrunk) capital. Exponential by construction:
    capital after n winning trades = start x (1+target)^n."""

    PREFIX = "CJ"

    def __init__(self, target_pct=Decimal("0.03"), stop_pct=None,
                 total_amount_quote=Decimal("200"), reentry_cooldown=0,
                 entry_filter=None):
        self.target_pct = Decimal(target_pct)
        self.stop_pct = Decimal(stop_pct) if stop_pct not in (None, "") else None
        self.start_capital = Decimal(total_amount_quote)
        self.capital = Decimal(total_amount_quote)
        self.reentry_cooldown = reentry_cooldown    # seconds flat between trades
        self.entry_filter = entry_filter or EntryFilter("always")
        self.last_filter_reason = ""
        self.position_qty = Decimal("0")
        self.entry_price = None
        self.last_exit_ts = None    # stamped from state.ts (works in backtests too)
        self._exit_pending = False
        self.wins = self.losses = 0
        self._seq = 0

    def tags(self):
        return [f"{self.PREFIX}-IN", f"{self.PREFIX}-TP", f"{self.PREFIX}-OUT"]

    def desired_orders(self, state):
        if self.position_qty == 0:
            if self._exit_pending:
                self.last_exit_ts = state.ts
                self._exit_pending = False
            if (self.last_exit_ts is not None
                    and state.ts - self.last_exit_ts < self.reentry_cooldown):
                return []
            allowed, reason = self.entry_filter.ok(state.candles, float(state.mid))
            self.last_filter_reason = reason
            if not allowed:
                return []
            self._seq += 1
            return [{"tag": f"{self.PREFIX}-IN", "side": "BUY", "type": "MARKET",
                     "quote_qty": self.capital, "once": self._seq}]
        # holding: stop loss beats take-profit if both would apply
        if (self.stop_pct is not None
                and state.mid <= self.entry_price * (1 - self.stop_pct)):
            self._seq += 1
            return [{"tag": f"{self.PREFIX}-OUT", "side": "SELL", "type": "MARKET",
                     "qty": self.position_qty, "once": self._seq}]
        return [{"tag": f"{self.PREFIX}-TP", "side": "SELL", "type": "LIMIT_MAKER",
                 "price": self.entry_price * (1 + self.target_pct),
                 "qty": self.position_qty}]

    def on_fill(self, tag, order_body):
        qty = Decimal(order_body.get("executedQty", "0"))
        # market orders report price=0; the traded value is cummulativeQuoteQty
        quote_val = Decimal(order_body.get("cummulativeQuoteQty", "0") or "0")
        if not quote_val:
            quote_val = qty * Decimal(order_body.get("price", "0") or "0")
        if tag == f"{self.PREFIX}-IN":
            self.position_qty = qty
            self.entry_price = quote_val / qty if qty else None
        else:
            if quote_val:
                if quote_val > self.capital:
                    self.wins += 1
                else:
                    self.losses += 1
                self.capital = quote_val
            self.position_qty = Decimal("0")
            self.entry_price = None
            self._exit_pending = True

    def summary(self):
        growth = (self.capital / self.start_capital - 1) * 100
        state = (f"holding {self.position_qty.quantize(Decimal('0.00000001')).normalize():f} "
                 f"@ {self.entry_price:.2f}" if self.position_qty
                 else f"flat ({self.last_filter_reason or 'no signal yet'})")
        return (f"cj_compound: {self.wins} wins, {self.losses} losses; capital "
                f"{self.start_capital:.2f} -> {self.capital:.2f} ({growth:+.2f}%); "
                f"entry={self.entry_filter.mode}; {state}")


# -------------------------------------------------------------- supertrend_v1

def supertrend(highs, lows, closes, length=20, multiplier=4.0):
    """Standard supertrend (Wilder ATR); returns (trend_value, direction) for
    the last bar. Validated against pandas_ta in the PoC backtests."""
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:length]) / length
    upper = lower = None
    direction = 1
    trend = None
    for i in range(length, len(closes)):
        atr = (atr * (length - 1) + trs[i - 1]) / length
        hl2 = (highs[i] + lows[i]) / 2
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        upper = basic_upper if upper is None or basic_upper < upper or closes[i - 1] > upper else upper
        lower = basic_lower if lower is None or basic_lower > lower or closes[i - 1] < lower else lower
        if closes[i] > upper:
            direction = 1
        elif closes[i] < lower:
            direction = -1
        trend = lower if direction == 1 else upper
    return trend, direction


class Supertrend:
    """Directional: market-BUY on an active long signal, exit to quote when the
    trend flips (spot has no shorting — 'short' means flat)."""

    PREFIX = "ST"

    def __init__(self, length=20, multiplier=4.0, percentage_threshold=0.01,
                 order_amount_quote=Decimal("50"), interval="3m"):
        self.length = length
        self.multiplier = multiplier
        self.percentage_threshold = percentage_threshold
        self.order_amount_quote = Decimal(order_amount_quote)
        self.interval = interval
        self.position_qty = Decimal("0")
        self.entries = 0
        self.exits = 0
        self.last_signal = "none"
        self._seq = 0

    def tags(self):
        return [f"{self.PREFIX}-IN", f"{self.PREFIX}-OUT"]

    def desired_orders(self, state):
        if not state.candles or len(state.candles) < self.length + 2:
            return []
        highs = [float(k[2]) for k in state.candles]
        lows = [float(k[3]) for k in state.candles]
        closes = [float(k[4]) for k in state.candles]
        trend, direction = supertrend(highs, lows, closes,
                                      self.length, self.multiplier)
        distance = abs(closes[-1] - trend) / closes[-1]
        active = distance < self.percentage_threshold
        self.last_signal = (f"direction={'LONG' if direction == 1 else 'SHORT'} "
                            f"distance={distance:.4%} "
                            f"({'ACTIVE' if active else 'idle'})")
        if direction == 1 and active and self.position_qty == 0:
            self._seq += 1
            return [{"tag": f"{self.PREFIX}-IN", "side": "BUY", "type": "MARKET",
                     "quote_qty": self.order_amount_quote, "once": self._seq}]
        if direction == -1 and self.position_qty > 0:
            self._seq += 1
            return [{"tag": f"{self.PREFIX}-OUT", "side": "SELL", "type": "MARKET",
                     "qty": self.position_qty, "once": self._seq}]
        return []

    def on_fill(self, tag, order_body):
        qty = Decimal(order_body.get("executedQty", "0"))
        if tag == f"{self.PREFIX}-IN":
            self.position_qty += qty
            self.entries += 1
        else:
            self.position_qty = max(Decimal("0"), self.position_qty - qty)
            self.exits += 1

    def summary(self):
        shown = self.position_qty.quantize(Decimal("0.00000001")).normalize()
        return (f"supertrend_v1: {self.entries} entries, {self.exits} exits, "
                f"open position {shown:f} base; "
                f"last signal: {self.last_signal}")
