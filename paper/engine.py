"""Pure MM logic for the paper trader. Fair value is computed OUTSIDE and passed
in (fair_tok) so ONE class runs every signal config (twap / binance / deribit /
confirmations / market-mid). Modes: roundtrip (buy+sell) or hold (buy only).
Sizing: fixed or opportunity (scale by signal strength). No I/O.

Fill model v2 (realism pass):
  * POSTED quotes: fills execute against quotes we posted earlier, and we may
    only refresh/cancel every `requote` seconds (latency). When the signal moves
    faster than we can re-quote, aggressors pick off our stale quote -> the
    adverse selection that v1 ignored.
  * MIN POST (Polymarket: limit orders need >= 5 shares): we only post a bid if
    we still have >= min_order shares of capacity, and only post an ask if we
    hold >= min_order shares. Partial fills of a posted order remain valid at
    any size.
  * FEES: crypto 5m markets run crypto_fees_v2 = taker-only (makers pay 0, and
    real makers even earn rebates, which we conservatively ignore). fee_bps
    covers any future maker fee; default 0.
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
                 min_signal, mode="roundtrip", size_mode="fixed", min_order=5.0,
                 requote=1.0, fee_bps=0.0, exit_first=False, xf_offset=0.02,
                 pair_balance=False):
        self.asset = asset
        self.slug = slug
        self.start = start
        self.end = start + 300
        self.spread = spread
        self.f = fill_frac
        self.max_inv = max_inventory
        self.min_signal = min_signal
        self.mode = mode            # "roundtrip" | "hold"
        self.exit_first = exit_first   # winner-style: entry-anchored asks + forced near-close exit
        self.pair_balance = pair_balance   # pair mode: bid only the side we hold LESS of (forces sets)
        self.xf_offset = xf_offset     # ask = avg entry + this (never follows fair away)
        self.cost_up = self.cost_dn = 0.0    # cost basis per side (for entry-anchored asks)
        self.size_mode = size_mode  # "fixed" | "opp"
        self.min_order = min_order  # min shares to POST an order (fills may be partial)
        self.requote = requote      # seconds between quote refreshes (latency)
        self.fee = fee_bps / 10000.0
        self.up_tok = ""
        self.down_tok = ""
        self.start_ref = "set"      # sentinel: windows are gated by fair_tok, not this
        self.settled = False
        self.inv_up = self.inv_dn = 0.0
        self.cash = self.deployed = self.peak = 0.0
        self.buys = self.sells = 0
        # posted (resting) quotes per token: refreshed at most every `requote` s
        self.q = {True: {"bid": None, "ask": None, "fair": None, "ts": -1e18},
                  False: {"bid": None, "ask": None, "fair": None, "ts": -1e18}}

    def _fill_size(self, size, fair_tok):
        strength = 1.0
        if self.size_mode == "opp":
            strength = max(0.2, min(2.0, abs(fair_tok - 0.5) / 0.25))
        return self.f * size * strength

    def _refresh(self, now, is_up, fair_tok):
        """Re-post/cancel quotes for this token — allowed at most every `requote` s.
        Between refreshes the posted quotes are STALE and can be picked off."""
        q = self.q[is_up]
        if now - q["ts"] < self.requote:
            return
        q["ts"] = now
        if fair_tok is None or fair_tok - 0.5 < self.min_signal:
            q["bid"] = q["ask"] = q["fair"] = None      # cancel both sides
            return
        inv = self.inv_up if is_up else self.inv_dn
        q["fair"] = fair_tok
        # bid only if we could still post a >= min_order-share order
        q["bid"] = (max(0.01, min(0.98, fair_tok - self.spread))
                    if self.max_inv - inv >= self.min_order else None)
        if self.pair_balance and q["bid"] is not None:
            other = self.inv_dn if is_up else self.inv_up
            if inv > other + 25:          # this side is ahead: stop bidding it, let the other catch up
                q["bid"] = None
        # ask only in roundtrip mode and only if we HOLD >= min_order shares.
        # exit_first: anchor the ask to OUR AVG ENTRY (+offset) so it does not chase
        # the model away from the flow -- the #1 reason vanilla asks never filled.
        if self.mode == "roundtrip" and inv >= self.min_order:
            if self.exit_first:
                cost = self.cost_up if is_up else self.cost_dn
                avg = cost / inv if inv > 0 else fair_tok
                q["ask"] = max(0.02, min(0.99, avg + self.xf_offset))
            else:
                q["ask"] = max(0.02, min(0.99, fair_tok + self.spread))
        else:
            q["ask"] = None

    def on_trade(self, now, is_up, price, size, is_sell, fair_tok):
        """A trade printed on this token. 1) try to fill against our POSTED
        (possibly stale) quotes; 2) then refresh quotes if latency allows.
        fair_tok = CURRENT fair value of this token (None => no signal)."""
        q = self.q[is_up]
        inv = self.inv_up if is_up else self.inv_dn
        rec = None

        # exit_first FORCED LIQUIDATION: in the last 40s, dump inventory as TAKER
        # into any print on this token (crossing the spread, taker fee applied).
        # Carrying nothing to the binary settlement is the whole point.
        if (self.exit_first and inv > 0 and now > self.end - 40 and price > 0.02):
            fill = min(inv, self._fill_size(size, q["fair"] if q["fair"] is not None else 0.5))
            if fill > 0:
                px = max(0.01, price - 0.01)
                fee = 0.07 * px * (1 - px)              # crypto_fees_v2 taker fee
                proceeds = fill * (px - fee)
                self.cash += proceeds
                self.deployed -= proceeds
                if is_up:
                    self.cost_up *= (1 - fill / self.inv_up) if self.inv_up else 0.0
                    self.inv_up -= fill
                else:
                    self.cost_dn *= (1 - fill / self.inv_dn) if self.inv_dn else 0.0
                    self.inv_dn -= fill
                self.sells += 1
                self._refresh(now, is_up, fair_tok)
                return {"action": "sell", "price": px, "size": fill, "signed_cash": proceeds}

        if is_sell and q["bid"] is not None and price <= q["bid"]:      # sell hits our bid -> BUY
            fill = min(self.max_inv - inv, self._fill_size(size, q["fair"]))
            if fill > 0:      # partial fills of a posted order are valid at any size
                cost = fill * price * (1 + self.fee)
                self.cash -= cost
                self.deployed += cost
                self.peak = max(self.peak, self.deployed)
                if is_up:
                    self.inv_up += fill
                    self.cost_up += fill * price
                else:
                    self.inv_dn += fill
                    self.cost_dn += fill * price
                self.buys += 1
                rec = {"action": "buy", "price": price, "size": fill, "signed_cash": -cost}
        elif (self.mode == "roundtrip" and (not is_sell) and q["ask"] is not None
              and price >= q["ask"] and inv > 0):                        # buy lifts our ask -> SELL
            fill = min(inv, self._fill_size(size, q["fair"]))
            if fill > 0:
                proceeds = fill * price * (1 - self.fee)
                self.cash += proceeds
                self.deployed -= proceeds
                if is_up:
                    self.cost_up *= (1 - fill / self.inv_up) if self.inv_up else 0.0
                    self.inv_up -= fill
                else:
                    self.cost_dn *= (1 - fill / self.inv_dn) if self.inv_dn else 0.0
                    self.inv_dn -= fill
                self.sells += 1
                rec = {"action": "sell", "price": price, "size": fill, "signed_cash": proceeds}

        self._refresh(now, is_up, fair_tok)
        return rec

    def settle(self, outcome_up: int) -> dict:
        residual = self.inv_up * outcome_up + self.inv_dn * (1 - outcome_up)
        return {"cash": self.cash, "residual": residual, "pnl": self.cash + residual,
                "capital": max(self.peak, 0.0), "buys": self.buys, "sells": self.sells,
                "resid_shares": self.inv_up + self.inv_dn,
                "n_fills": self.buys + self.sells, "outcome_up": outcome_up}
