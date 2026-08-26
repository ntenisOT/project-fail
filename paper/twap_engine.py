"""Take the favoured side late in the window using the official TWAP signal.

Both independent review seats returned NO-GO on trading the backtested version
of this. They were right about the backtest, and every one of their objections
is answered by running it here instead:

  "no fill model, unlimited size, consumed liquidity"
      The paper engine fills only against REAL public prints after consuming
      queue-ahead depth, and sweeps only displayed size. This is the fill
      model they asked for.
  "received_at ignored - textbook lookahead"
      The view is read with the wall clock, and the feed publishes on receipt,
      so a sample is invisible until it has actually arrived. On the live feed
      that is a median 1.678s behind the moment it describes.
  "labels never joined to official settlement"
      Paper settles on the official outcome like every other arm.
  "wrong-side token, and the sim is blind to it"
      The window is handed its up/down tokens by market_metadata, the same
      fail-closed path every other arm uses. There is no positional guess.

What this does NOT fix, and what the test therefore has to answer:
  * At T+240 the current TWAP (covering T+180-240) shares NO overlap with the
    settling TWAP (T+240-300). This is autocorrelation, a forecast, not the
    "already observable" story. It is regime-dependent by construction.
  * The asks liftable at 0.90 may be precisely the stale quotes a maker is
    about to cancel, so 0.90 may be a fair price for fill uncertainty rather
    than a mispricing. Only real queue-aware fills can settle that, which is
    exactly what this arm measures.
"""
from __future__ import annotations

from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.reference_view import ReferenceView
from paper.taker import sweep, sweep_available


class TwapWindow(PairWindow):
    """Buys the side the partial TWAP favours, once, late in the window."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.view: ReferenceView | None = kwargs.pop("reference_view", None)  # type: ignore[assignment]
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.twap_taken = False
        self.twap_signal_bps: float | None = None
        self.twap_blocked = 0
        self.twap_no_signal = 0
        self.twap_shares = 0.0

    def _desired(self, now: float, up: OrderBook,
                 down: OrderBook) -> dict[tuple[bool, str], float]:
        return {}          # takes liquidity; never rests a quote

    def on_books(self, now: float, up: OrderBook,
                 down: OrderBook) -> list[dict[str, float | str]]:
        records = super().on_books(now, up, down)
        if not self.full_window or now < self.start or now >= self.end:
            return records
        at = self.config.twap_entry_s
        if at is None or self.twap_taken or now < self.start + at:
            return records
        if self.view is None:
            return records

        # `now` is the wall clock, and the feed publishes on receipt, so this
        # cannot see a sample before it arrived.
        signal = self.view.signal_bps(
            self.asset, float(self.start), now=now,
            max_age_s=self.config.twap_max_age_s)
        if signal is None:
            self.twap_no_signal += 1
            return records
        if abs(signal) < self.config.twap_min_bps:
            return records

        self.twap_taken = True
        self.twap_signal_bps = signal
        side = signal > 0
        book = up if side else down
        shares = self.config.clip_shares
        legs = sweep(book, "buy", shares) or sweep_available(book, "buy", shares)
        if not legs:
            self.twap_blocked += 1
            return records
        for leg in legs:
            cost = leg.price * leg.shares + leg.fee
            self.inventory[side] += leg.shares
            self.cash -= cost
            self.filled_shares += leg.shares
            self.taker_fees += leg.fee
            self.buys += 1
            self.twap_shares += leg.shares
            records.append({
                "action": "twap_buy", "price": leg.price, "size": leg.shares,
                "signed_cash": -cost, "outcome_up": int(side),
            })
        self._update_peak()
        self._sync_exposure(now)
        return records

    def settle(self, now: float, outcome_up: int,
               ) -> tuple[dict[str, float | int], dict[str, object]]:
        settlement, metrics = super().settle(now, outcome_up)
        metrics.update({
            "twap_taken": int(self.twap_taken),
            "twap_shares": self.twap_shares,
            "twap_blocked": self.twap_blocked,
            "twap_no_signal": self.twap_no_signal,
            "twap_signal_bps": self.twap_signal_bps,
        })
        return settlement, metrics
