"""Pure MM logic for the paper trader. Fair value is computed OUTSIDE and passed
in (fair_tok) so ONE class runs every signal config (twap / binance / deribit /
confirmations / market-mid). Modes: roundtrip (buy+sell) or hold (buy only).
Sizing: fixed or opportunity (scale by signal strength). No I/O.
"""
from __future__ import annotations

import bisect
import statistics

K_SKEW = 400.0


class TWAP:
    def __init__(self, window: float = 60.0):
        self.window = window
        self.ts: list[float] = []
        self.v: list[float] = []

    def add(self, t: float, val: float) -> None:
        self.ts.append(t)
        self.v.append(val)
        cut = t - 300
        while self.ts and self.ts[0] < cut:
            self.ts.pop(0)
            self.v.pop(0)

    def at(self, t: float):
        if not self.ts:
            return None
        hi = bisect.bisect_right(self.ts, t)
        lo = bisect.bisect_left(self.ts, t - self.window, 0, hi)
        if hi <= lo:
            return None
        return statistics.fmean(self.v[lo:hi])

    def now(self):
        return self.at(self.ts[-1]) if self.ts else None


def fair_up(now, ref, k: float = K_SKEW):
    """Map a price move (now vs window-start ref) to a probability the up side wins."""
    if now is None or not ref:
        return None
    return min(0.98, max(0.02, 0.5 + k * (now - ref) / ref))


class PaperWindow:
    def __init__(self, asset, slug, start, spread, fill_frac, max_inventory,
                 min_signal, mode="roundtrip", size_mode="fixed"):
        self.asset = asset
        self.slug = slug
        self.start = start
        self.end = start + 300
        self.spread = spread
        self.f = fill_frac
        self.max_inv = max_inventory
        self.min_signal = min_signal
        self.mode = mode            # "roundtrip" | "hold"
        self.size_mode = size_mode  # "fixed" | "opp"
        self.up_tok = ""
        self.down_tok = ""
        self.start_ref = "set"      # sentinel: windows are gated by fair_tok, not this
        self.settled = False
        self.inv_up = self.inv_dn = 0.0
        self.cash = self.deployed = self.peak = 0.0
        self.buys = self.sells = 0

    def _fill_size(self, size, fair_tok):
        strength = 1.0
        if self.size_mode == "opp":
            strength = max(0.2, min(2.0, abs(fair_tok - 0.5) / 0.25))
        return self.f * size * strength

    def on_trade(self, is_up, price, size, is_sell, fair_tok):
        """fair_tok = fair value of THIS token (None => no signal => no quote)."""
        if fair_tok is None or fair_tok - 0.5 < self.min_signal:
            return None
        bid = max(0.01, min(0.98, fair_tok - self.spread))
        ask = max(0.02, min(0.99, fair_tok + self.spread))
        inv = self.inv_up if is_up else self.inv_dn

        if is_sell and price <= bid:                       # aggressive sell hits our bid -> BUY
            fill = min(self.max_inv - inv, self._fill_size(size, fair_tok))
            if fill <= 0:
                return None
            self.cash -= fill * price
            self.deployed += fill * price
            self.peak = max(self.peak, self.deployed)
            if is_up:
                self.inv_up += fill
            else:
                self.inv_dn += fill
            self.buys += 1
            return {"action": "buy", "price": price, "size": fill, "signed_cash": -fill * price}

        if self.mode == "roundtrip" and (not is_sell) and price >= ask and inv > 0:  # buy lifts ask -> SELL
            fill = min(inv, self._fill_size(size, fair_tok))
            if fill <= 0:
                return None
            self.cash += fill * price
            self.deployed -= fill * price
            if is_up:
                self.inv_up -= fill
            else:
                self.inv_dn -= fill
            self.sells += 1
            return {"action": "sell", "price": price, "size": fill, "signed_cash": fill * price}

        return None

    def settle(self, outcome_up: int) -> dict:
        residual = self.inv_up * outcome_up + self.inv_dn * (1 - outcome_up)
        return {"cash": self.cash, "residual": residual, "pnl": self.cash + residual,
                "capital": max(self.peak, 0.0), "buys": self.buys, "sells": self.sells,
                "resid_shares": self.inv_up + self.inv_dn,
                "n_fills": self.buys + self.sells, "outcome_up": outcome_up}
