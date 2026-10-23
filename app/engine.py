"""Deterministic research and paper-trading engine for a Wallex agent.

The module deliberately has no network or exchange dependency.  It turns OHLCV
candles into auditable decisions, applies risk controls, and can be reused by a
live adapter in shadow mode without ever submitting an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional

from .markets import TMN_QUOTE, ensure_spot_tmn_symbol


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "Candle":
        def number(*keys: str, default: float = 0.0) -> float:
            for key in keys:
                if key in row and row[key] is not None:
                    return float(row[key])
            return default

        raw_ts = row.get("timestamp", row.get("time", row.get("t", 0)))
        if isinstance(raw_ts, str):
            try:
                ts = int(float(raw_ts))
            except ValueError:
                ts = int(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp())
        else:
            ts = int(raw_ts)
        # Wallex and many market APIs return milliseconds; normalize to seconds.
        if ts > 10_000_000_000:
            ts //= 1000
        candle = cls(ts, number("open", "o"), number("high", "h"), number("low", "l"), number("close", "c"), number("volume", "v"))
        if candle.close <= 0 or candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            raise ValueError(f"invalid OHLC candle: {row!r}")
        return candle

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskConfig:
    starting_cash: float = 5_000_000.0
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.25
    # Kept as a compatibility field for old callers, but leverage is fixed at
    # one and any leveraged configuration is rejected.
    max_leverage: float = 1.0
    fee_bps: float = 12.0
    slippage_bps: float = 5.0
    atr_period: int = 14
    stop_atr: float = 2.0
    take_atr: float = 3.0
    max_drawdown: float = 0.12
    daily_loss_limit: float = 0.03
    cooldown_bars: int = 3
    min_signal_score: float = 0.60

    def validate(self) -> None:
        if not (0 < self.risk_per_trade <= 0.1):
            raise ValueError("risk_per_trade must be in (0, .1]")
        if not (0 < self.max_position_pct <= 1):
            raise ValueError("max_position_pct must be in (0, 1]")
        if self.max_leverage != 1.0:
            raise ValueError("leverage is disabled; spot trading requires max_leverage=1.0")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fees and slippage must be non-negative")
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    entry_timestamp: int
    stop_price: float
    take_price: float
    entry_fee: float = 0.0


@dataclass
class Trade:
    symbol: str
    side: str
    quantity: float
    price: float
    timestamp: int
    reason: str
    fee: float
    pnl: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    timestamp: int
    symbol: str
    action: str
    signal_score: float
    price: float
    quantity: float
    reasons: list[str]
    risk_checks: dict[str, Any]
    evidence: dict[str, Any]
    mode: str = "shadow"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Portfolio:
    cash: float
    equity: float
    peak_equity: float
    position: Optional[Position] = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    bars_since_exit: int = 10**9
    day_start_equity: float = 0.0
    day_key: str = ""
    halted: bool = False

    def snapshot(self, price: float | None = None) -> dict[str, Any]:
        mark = self.position.quantity * (price if price is not None else self.position.entry_price) if self.position else 0.0
        return {
            "cash": round(self.cash, 8),
            "equity": round(self.equity, 8),
            "peak_equity": round(self.peak_equity, 8),
            "quote_currency": TMN_QUOTE,
            "realized_pnl": round(self.realized_pnl, 8),
            "fees_paid": round(self.fees_paid, 8),
            "position": asdict(self.position) if self.position else None,
            "marked_position_value": round(mark, 8),
            "halted": self.halted,
        }


class EmaRsiAtrStrategy:
    """A small deterministic strategy whose reasoning is easy to audit.

    A long setup requires a fast EMA above a slow EMA, RSI in a non-overheated
    zone and a positive close-vs-EMA impulse.  The score is a confidence-like
    agreement ratio; it is not a claim of probabilistic accuracy.
    """

    def __init__(self, fast: int = 9, slow: int = 21, rsi_period: int = 14, atr_period: int = 14):
        if not (2 <= fast < slow):
            raise ValueError("fast EMA must be >=2 and below slow EMA")
        self.fast, self.slow, self.rsi_period, self.atr_period = fast, slow, rsi_period, atr_period

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0.0
        alpha = 2 / (period + 1)
        ema = values[0]
        for value in values[1:]:
            ema = alpha * value + (1 - alpha) * ema
        return ema

    @staticmethod
    def _rsi(closes: list[float], period: int) -> float:
        if len(closes) <= period:
            return 50.0
        gains: list[float] = []
        losses: list[float] = []
        for before, after in zip(closes[-period - 1:-1], closes[-period:]):
            delta = after - before
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0 if avg_gain else 50.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    @staticmethod
    def _atr(candles: list[Candle], period: int) -> float:
        if len(candles) < 2:
            return max(candles[-1].close * 0.01, 1e-8) if candles else 1.0
        trs: list[float] = []
        for prev, candle in zip(candles[-period - 1:], candles[-period:]):
            trs.append(max(candle.high - candle.low, abs(candle.high - prev.close), abs(candle.low - prev.close)))
        return max(sum(trs) / len(trs), candles[-1].close * 0.0005)

    def evaluate(self, candles: list[Candle]) -> dict[str, Any]:
        if not candles:
            return {"signal": "hold", "score": 0.0, "atr": 0.0, "reasons": ["no market data"], "evidence": {}}
        closes = [c.close for c in candles]
        last = candles[-1]
        fast = self._ema(closes[-self.slow * 2:], self.fast)
        slow = self._ema(closes[-self.slow * 2:], self.slow)
        rsi = self._rsi(closes, self.rsi_period)
        atr = self._atr(candles, self.atr_period)
        impulse = (last.close / slow - 1) if slow else 0.0
        bullish = fast > slow and 45 <= rsi <= 72 and impulse > 0
        bearish = fast < slow and 28 <= rsi <= 55 and impulse < 0
        checks = [fast > slow, 45 <= rsi <= 72, impulse > 0] if bullish else [fast < slow, 28 <= rsi <= 55, impulse < 0] if bearish else []
        score = sum(checks) / 3 if checks else 0.0
        signal = "buy" if bullish and score >= 2 / 3 else "sell" if bearish and score >= 2 / 3 else "hold"
        reasons = [f"EMA{self.fast}={fast:.4f} vs EMA{self.slow}={slow:.4f}", f"RSI{self.rsi_period}={rsi:.1f}", f"impulse={impulse * 100:.2f}%", f"ATR={atr:.4f}"]
        return {"signal": signal, "score": score, "atr": atr, "reasons": reasons, "evidence": {"ema_fast": fast, "ema_slow": slow, "rsi": rsi, "impulse": impulse, "atr": atr, "lookback": len(candles)}}


class RiskManager:
    def __init__(self, config: RiskConfig):
        config.validate()
        self.config = config

    def risk_state(self, portfolio: Portfolio, timestamp: int) -> dict[str, Any]:
        day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        if portfolio.day_key != day:
            portfolio.day_key = day
            portfolio.day_start_equity = portfolio.equity
        drawdown = 1 - portfolio.equity / portfolio.peak_equity if portfolio.peak_equity else 0.0
        day_loss = 1 - portfolio.equity / portfolio.day_start_equity if portfolio.day_start_equity else 0.0
        return {"drawdown": drawdown, "day_loss": day_loss, "max_drawdown": self.config.max_drawdown, "daily_loss_limit": self.config.daily_loss_limit, "cooldown_bars": portfolio.bars_since_exit, "halted": portfolio.halted}

    def size_for_entry(self, portfolio: Portfolio, price: float, atr: float, timestamp: int) -> tuple[float, dict[str, Any]]:
        state = self.risk_state(portfolio, timestamp)
        reasons: list[str] = []
        allowed = True
        if portfolio.halted or state["drawdown"] >= self.config.max_drawdown:
            allowed, portfolio.halted = False, True
            reasons.append("max drawdown circuit breaker")
        if state["day_loss"] >= self.config.daily_loss_limit:
            allowed, portfolio.halted = False, True
            reasons.append("daily loss limit circuit breaker")
        if portfolio.position is not None:
            allowed = False
            reasons.append("one position per symbol")
        if portfolio.bars_since_exit < self.config.cooldown_bars:
            allowed = False
            reasons.append("cooldown after exit")
        stop_distance = max(atr * self.config.stop_atr, price * 0.001)
        risk_budget = portfolio.equity * self.config.risk_per_trade
        by_risk = risk_budget / stop_distance
        # Spot-only sizing: the notional cap is funded by available equity and
        # never multiplied by leverage.
        by_notional = portfolio.equity * self.config.max_position_pct / price
        quantity = max(0.0, min(by_risk, by_notional)) if allowed else 0.0
        return quantity, {"allowed": allowed, "reasons": reasons, "risk_budget": risk_budget, "stop_distance": stop_distance, "size_by_risk": by_risk, "size_by_notional": by_notional, "quantity": quantity, "market_type": "spot", "leverage": 1.0, "quote_currency": TMN_QUOTE}


class BacktestEngine:
    def __init__(self, symbol: str = "BTC/TMN", config: RiskConfig | None = None, strategy: EmaRsiAtrStrategy | None = None):
        self.symbol = ensure_spot_tmn_symbol(symbol)
        self.config = config or RiskConfig()
        self.strategy = strategy or EmaRsiAtrStrategy(atr_period=self.config.atr_period)
        self.risk = RiskManager(self.config)

    def _fill_price(self, price: float, side: str) -> float:
        slip = self.config.slippage_bps / 10_000
        return price * (1 + slip if side == "buy" else 1 - slip)

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.config.fee_bps / 10_000

    def run(self, candles: Iterable[Candle], mode: str = "backtest") -> dict[str, Any]:
        data = list(candles)
        if not data:
            raise ValueError("at least one candle is required")
        portfolio = Portfolio(self.config.starting_cash, self.config.starting_cash, self.config.starting_cash, day_start_equity=self.config.starting_cash)
        trades: list[Trade] = []
        decisions: list[Decision] = []
        equity_curve: list[dict[str, float]] = []
        history: list[Candle] = []

        for candle in data:
            history.append(candle)
            portfolio.bars_since_exit += 1
            # Mark to market and update day/circuit-breaker state before deciding.
            if portfolio.position:
                portfolio.equity = portfolio.cash + portfolio.position.quantity * candle.close
            else:
                portfolio.equity = portfolio.cash
            portfolio.peak_equity = max(portfolio.peak_equity, portfolio.equity)
            state = self.risk.risk_state(portfolio, candle.timestamp)
            pos = portfolio.position
            if pos:
                exit_reason = None
                exit_price = candle.close
                # Conservative ordering: stop wins if a candle touches both levels.
                if candle.low <= pos.stop_price:
                    exit_price, exit_reason = pos.stop_price, "stop_loss"
                elif candle.high >= pos.take_price:
                    exit_price, exit_reason = pos.take_price, "take_profit"
                if exit_reason or state["drawdown"] >= self.config.max_drawdown or state["day_loss"] >= self.config.daily_loss_limit:
                    if not exit_reason:
                        exit_reason, exit_price = "risk_circuit_breaker", candle.close
                    fill = self._fill_price(exit_price, "sell")
                    notional = pos.quantity * fill
                    fee = self._fee(notional)
                    pnl = (fill - pos.entry_price) * pos.quantity - fee - pos.entry_fee
                    portfolio.cash += notional - fee
                    portfolio.realized_pnl += pnl
                    portfolio.fees_paid += fee
                    portfolio.position = None
                    portfolio.bars_since_exit = 0
                    if exit_reason == "risk_circuit_breaker":
                        portfolio.halted = True
                    trades.append(Trade(self.symbol, "sell", pos.quantity, fill, candle.timestamp, exit_reason, fee, pnl))
                    portfolio.equity = portfolio.cash
                    state = self.risk.risk_state(portfolio, candle.timestamp)

            signal = self.strategy.evaluate(history)
            quantity, risk_checks = self.risk.size_for_entry(portfolio, candle.close, signal["atr"], candle.timestamp)
            action = "buy" if signal["signal"] == "buy" and quantity > 0 and signal["score"] >= self.config.min_signal_score else "hold"
            reasons = list(signal["reasons"])
            if action == "buy":
                fill = self._fill_price(candle.close, "buy")
                stop = fill - risk_checks["stop_distance"]
                take = fill + signal["atr"] * self.config.take_atr
                fee = self._fee(quantity * fill)
                if portfolio.cash >= quantity * fill + fee:
                    portfolio.cash -= quantity * fill + fee
                    portfolio.fees_paid += fee
                    portfolio.position = Position(self.symbol, quantity, fill, candle.timestamp, stop, take, fee)
                    trades.append(Trade(self.symbol, "buy", quantity, fill, candle.timestamp, "signal_entry", fee, 0.0))
                    reasons.append("risk budget and notional cap passed")
                else:
                    action = "hold"
                    quantity = 0.0
                    risk_checks["allowed"] = False
                    risk_checks["reasons"].append("insufficient cash after fees")
            decisions.append(Decision(candle.timestamp, self.symbol, action, signal["score"], candle.close, quantity if action == "buy" else 0.0, reasons, risk_checks | {"portfolio": self.risk.risk_state(portfolio, candle.timestamp)}, signal["evidence"], mode))
            portfolio.equity = portfolio.cash + (portfolio.position.quantity * candle.close if portfolio.position else 0.0)
            portfolio.peak_equity = max(portfolio.peak_equity, portfolio.equity)
            equity_curve.append({"timestamp": candle.timestamp, "equity": portfolio.equity})

        if portfolio.position:
            last = data[-1]
            pos = portfolio.position
            fill = self._fill_price(last.close, "sell")
            fee = self._fee(pos.quantity * fill)
            pnl = (fill - pos.entry_price) * pos.quantity - fee - pos.entry_fee
            portfolio.cash += pos.quantity * fill - fee
            portfolio.realized_pnl += pnl
            portfolio.fees_paid += fee
            trades.append(Trade(self.symbol, "sell", pos.quantity, fill, last.timestamp, "end_of_data", fee, pnl))
            portfolio.position = None
            portfolio.equity = portfolio.cash
            equity_curve[-1]["equity"] = portfolio.equity

        wins = [trade for trade in trades if trade.side == "sell" and trade.pnl > 0]
        sells = [trade for trade in trades if trade.side == "sell"]
        ret = portfolio.equity / self.config.starting_cash - 1
        max_dd = 0.0
        peak = self.config.starting_cash
        for point in equity_curve:
            peak = max(peak, point["equity"])
            max_dd = max(max_dd, 1 - point["equity"] / peak)
        return {
            "symbol": self.symbol, "mode": mode, "starting_cash": self.config.starting_cash,
            "quote_currency": TMN_QUOTE, "market_type": "spot", "leverage": 1.0,
            "ending_equity": portfolio.equity, "return_pct": ret * 100, "max_drawdown_pct": max_dd * 100,
            "trades": [trade.as_dict() for trade in trades], "decisions": [decision.as_dict() for decision in decisions],
            "equity_curve": equity_curve, "portfolio": portfolio.snapshot(),
            "metrics": {"closed_trades": len(sells), "winning_trades": len(wins), "win_rate_pct": (len(wins) / len(sells) * 100 if sells else 0.0), "fees_paid": portfolio.fees_paid, "realized_pnl": portfolio.realized_pnl, "quote_currency": TMN_QUOTE},
        }


class JsonlDecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, decision: Decision) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")


class ShadowTrader:
    """Paper-only wrapper: emits decisions and never submits an exchange order."""

    def __init__(self, engine: BacktestEngine, log: JsonlDecisionLog | None = None):
        self.engine, self.log, self.history = engine, log, []

    def on_candle(self, candle: Candle) -> Decision:
        self.history.append(candle)
        signal = self.engine.strategy.evaluate(self.history)
        # Shadow uses the same risk gates but does not mutate a live portfolio.
        portfolio = Portfolio(self.engine.config.starting_cash, self.engine.config.starting_cash, self.engine.config.starting_cash, day_start_equity=self.engine.config.starting_cash)
        quantity, checks = self.engine.risk.size_for_entry(portfolio, candle.close, signal["atr"], candle.timestamp)
        action = "buy" if signal["signal"] == "buy" and quantity > 0 and signal["score"] >= self.engine.config.min_signal_score else "hold"
        decision = Decision(candle.timestamp, self.engine.symbol, action, signal["score"], candle.close, quantity if action == "buy" else 0.0, signal["reasons"] + ["SHADOW: no order submitted"], checks, signal["evidence"], "shadow")
        if self.log:
            self.log.append(decision)
        return decision


class RawWallexAdapter:
    """Explicitly disabled exchange boundary.

    It is intentionally a no-op adapter. Production wiring must opt in through
    a separately reviewed implementation; this class makes accidental trading
    impossible in a demo or evaluation environment.
    """

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def _blocked(self, operation: str) -> dict[str, Any]:
        return {"ok": False, "operation": operation, "blocked": True, "reason": "live Wallex operations are disabled; use shadow mode"}

    def market_snapshot(self, symbol: str) -> dict[str, Any]:
        return self._blocked("market_snapshot")

    def place_order(self, **_: Any) -> dict[str, Any]:
        return self._blocked("place_order")

    def cancel_order(self, **_: Any) -> dict[str, Any]:
        return self._blocked("cancel_order")


def load_candles(path: str | Path) -> list[Candle]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        rows = payload.get("candles", payload) if isinstance(payload, dict) else payload
    return [Candle.from_mapping(row) for row in rows]


def synthetic_candles(count: int = 240, start: float = 3_500_000.0) -> list[Candle]:
    """Generate deterministic demo candles with prices expressed in تومان."""
    candles: list[Candle] = []
    price = start
    for i in range(count):
        wave = math.sin(i / 9) * 0.018 + math.sin(i / 31) * 0.035
        drift = 0.00055 if (i // 48) % 2 == 0 else -0.00035
        close = max(1.0, price * (1 + wave * 0.08 + drift))
        high = max(price, close) * (1 + 0.007 + abs(math.sin(i)) * 0.003)
        low = min(price, close) * (1 - 0.007 - abs(math.cos(i)) * 0.0025)
        candles.append(Candle(1_700_000_000 + i * 900, price, high, low, close, 1000 + i * 2))
        price = close
    return candles
