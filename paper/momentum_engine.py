"""Short-horizon momentum taker window.

Every pair/mint arm in this repo is a passive two-sided inventory strategy, and
none of them ever captured the one edge that survives costs. The top-margin
wallet (0xce50c96b) does not predict outcomes - its terminal direction is a
53.8% coin flip - it round-trips inside the window, buying near 0.477 and
selling near 0.580, 62% of its fills as taker.

tools/momentum_probe.py measured the underlying effect on 600 BTC windows:
10s trade-VWAP returns are positively autocorrelated at every horizon
(lookback 10s -> horizon 10s: corr +0.2312, z +28.69), and after LIFTING THE
ASK in, HITTING THE BID out, and paying 0.07*p*(1-p) on both legs it nets
+0.0196/share above a 0.10 threshold over 1,030 trades.

This window implements exactly that and nothing else:

  * it never rests a quote (PairWindow._desired returns {} in momentum mode),
    so it cannot be confused with the maker arms or claim maker rebates
  * entries sweep displayed depth through paper.taker.sweep, paying real fees
  * a position is closed by a taker exit after momentum_hold_s, or at window
    end, whichever comes first
  * all cash, inventory, peak-capital, settlement and invalidation bookkeeping
    is inherited unchanged from PairWindow

It is deliberately a single-name directional strategy. It carries outcome risk
between entry and exit and is NOT an arbitrage.
"""
from __future__ import annotations

from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.taker import sweep, sweep_available


class MomentumWindow(PairWindow):
    """Take displayed liquidity after a short-horizon move, then exit."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if self.config.mode != "momentum":
            raise ValueError("MomentumWindow requires momentum mode")
        # mid history per side: list of (timestamp, mid)
        self._history: dict[bool, list[tuple[float, float]]] = {True: [], False: []}
        self._open_side: bool | None = None
        self._open_at: float | None = None
        self._open_shares = 0.0
        self.momentum_entries = 0
        self.momentum_exits = 0
        self.momentum_blocked = 0

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _mid(book: OrderBook) -> float | None:
        if book.best_bid is None or book.best_ask is None:
            return None
        return (book.best_bid + book.best_ask) / 2

    def _move(self, side: bool, now: float) -> float | None:
        """Mid change over the lookback, or None without a clean reference."""
        history = self._history[side]
        if not history:
            return None
        cutoff = now - self.config.momentum_lookback_s
        past = None
        for timestamp, mid in history:
            if timestamp <= cutoff:
                past = mid
            else:
                break
        if past is None:
            return None
        return history[-1][1] - past

    def _record_mid(self, side: bool, now: float, book: OrderBook) -> None:
        mid = self._mid(book)
        if mid is None:
            return
        history = self._history[side]
        history.append((now, mid))
        horizon = now - (self.config.momentum_lookback_s + 60)
        while len(history) > 2 and history[0][0] < horizon:
            history.pop(0)

    def _execute(self, now: float, side: bool, direction: str,
                 shares: float, book: OrderBook) -> dict[str, float | str] | None:
        legs = sweep(book, direction, shares) or sweep_available(book, direction, shares)
        if not legs:
            self.momentum_blocked += 1
            return None
        filled = sum(leg.shares for leg in legs)
        fees = sum(leg.fee for leg in legs)
        notional = sum(leg.price * leg.shares for leg in legs)
        if direction == "buy":
            self.inventory[side] += filled
            self.cash -= notional + fees
            self.buys += 1
        else:
            self.inventory[side] -= filled
            self.cash += notional - fees
            self.sells += 1
        self.taker_fees += fees
        self.filled_shares += filled
        self._sync_exposure(now)
        return {
            "kind": "momentum", "side": "up" if side else "down",
            "action": direction, "shares": filled,
            "price": notional / filled if filled else 0.0, "fee": fees,
        }

    # -- decision loop ---------------------------------------------------
    def on_books(self, now: float, up: OrderBook,
                 down: OrderBook) -> list[dict[str, float | str]]:
        # inherit first-books/invalidation/exposure handling; it posts no quotes
        records = super().on_books(now, up, down)
        if not self.full_window or now < self.start or now >= self.end:
            return records
        books = {True: up, False: down}
        for side in (True, False):
            self._record_mid(side, now, books[side])

        # exit first: a held position is closed by time or at window end
        if self._open_side is not None and self._open_at is not None:
            held = now - self._open_at
            near_end = now >= self.end - 15
            if held >= self.config.momentum_hold_s or near_end:
                side = self._open_side
                shares = min(self._open_shares, self.inventory[side])
                if shares > 0:
                    record = self._execute(now, side, "sell", shares, books[side])
                    if record is not None:
                        self.momentum_exits += 1
                        records.append(record)
                        self._open_side, self._open_at, self._open_shares = None, None, 0.0
                else:
                    self._open_side, self._open_at, self._open_shares = None, None, 0.0
            return records

        # entry: only with enough window left to complete the round trip
        if now + self.config.momentum_hold_s > self.end - 15:
            return records
        if now < self.start + self.config.new_pair_start_s:
            return records
        for side in (True, False):
            move = self._move(side, now)
            if move is None or move < self.config.momentum_threshold:
                continue
            room = self.config.max_inventory - self.inventory[side]
            shares = min(self.config.clip_shares, room)
            if shares <= 0:
                continue
            record = self._execute(now, side, "buy", shares, books[side])
            if record is not None:
                self.momentum_entries += 1
                self._open_side = side
                self._open_at = now
                self._open_shares = record["shares"]  # type: ignore[assignment]
                records.append(record)
            break
        return records
