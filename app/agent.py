"""Tool-driven agent orchestration for Wallex Spot Alpha.

The agent is intentionally bounded: an AI planner may propose an action, but a
small set of deterministic tools (signal, risk, limits) is authoritative. This
keeps model output useful and auditable while making unsafe execution impossible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .engine import BacktestEngine, Candle, Decision, JsonlDecisionLog, Portfolio


@dataclass(frozen=True)
class PlannerProposal:
    action: str
    confidence: float
    thesis: str
    invalidation: str


class OfflinePlanner:
    """Deterministic planner used in demos and tests.

    It has the same contract as an LLM planner; replacing it does not change the
    risk or execution boundary.
    """

    def propose(self, signal: dict[str, Any], context: dict[str, Any]) -> PlannerProposal:
        if signal["signal"] == "buy" and signal["score"] >= context["min_signal_score"]:
            return PlannerProposal("buy", signal["score"], "trend, momentum and volatility checks agree", "EMA cross or RSI exhaustion")
        return PlannerProposal("hold", signal["score"], "not enough independent evidence for entry", "wait for a confirmed setup")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    def call(self, name: str, **kwargs: Any) -> Any:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name](**kwargs)

    def names(self) -> list[str]:
        return sorted(self._tools)


class TradingAgent:
    """Bounded observe -> reason -> risk -> shadow decision loop."""

    def __init__(self, engine: BacktestEngine, symbol: str | None = None, planner: OfflinePlanner | None = None, log: JsonlDecisionLog | None = None):
        self.engine = engine
        self.symbol = symbol or engine.symbol
        self.planner = planner or OfflinePlanner()
        self.log = log
        self.history: list[Candle] = []
        self.portfolio = Portfolio(engine.config.starting_cash, engine.config.starting_cash, engine.config.starting_cash, day_start_equity=engine.config.starting_cash)
        self.tools = ToolRegistry()
        self.tools.register("compute_signal", self._compute_signal)
        self.tools.register("risk_gate", self._risk_gate)
        self.tools.register("record_decision", self._record_decision)

    def _compute_signal(self) -> dict[str, Any]:
        return self.engine.strategy.evaluate(self.history)

    def _risk_gate(self, price: float, atr: float, timestamp: int) -> dict[str, Any]:
        quantity, checks = self.engine.risk.size_for_entry(self.portfolio, price, atr, timestamp)
        checks["quantity"] = quantity
        return checks

    def _record_decision(self, decision: Decision) -> Decision:
        if self.log:
            self.log.append(decision)
        return decision

    def on_candle(self, candle: Candle) -> Decision:
        self.history.append(candle)
        signal = self.tools.call("compute_signal")
        proposal = self.planner.propose(signal, {"min_signal_score": self.engine.config.min_signal_score})
        checks = self.tools.call("risk_gate", price=candle.close, atr=signal["atr"], timestamp=candle.timestamp)
        action = proposal.action if proposal.action == "buy" and checks["allowed"] else "hold"
        qty = checks.get("quantity", 0.0) if action == "buy" else 0.0
        reasons = list(signal["reasons"]) + [proposal.thesis, f"planner_confidence={proposal.confidence:.2f}", "SHADOW: execution tool is intentionally absent"]
        decision = Decision(candle.timestamp, self.symbol, action, proposal.confidence, candle.close, qty, reasons, {"proposal": proposal.__dict__, "risk": checks, "tools": self.tools.names()}, signal["evidence"], "shadow-agent")
        return self.tools.call("record_decision", decision=decision)

class OpenAICompatiblePlanner(OfflinePlanner):
    """Optional AI planner using an OpenAI-compatible JSON endpoint.

    The endpoint is opt-in and only receives aggregate indicator values, never
    credentials or raw account data. If unavailable or malformed, the agent
    falls back to the deterministic planner and records that fallback.
    """

    def __init__(self, endpoint: str, api_key: str, model: str = "gpt-4o-mini", timeout: float = 8.0):
        self.endpoint, self.api_key, self.model, self.timeout = endpoint, api_key, model, timeout

    def propose(self, signal: dict[str, Any], context: dict[str, Any]) -> PlannerProposal:
        import json
        import urllib.error
        import urllib.request
        body = {"model": self.model, "temperature": 0, "max_tokens": 120, "messages": [{"role": "system", "content": "Return JSON only: {action: buy|hold, confidence: 0..1, thesis: string, invalidation: string}. You are a conservative spot-only crypto research planner. Prices and account values are in تومان. Never suggest leverage, margin or a non-spot trade."}, {"role": "user", "content": json.dumps({"signal": signal, "min_signal_score": context["min_signal_score"]})}]}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
            action = parsed.get("action", "hold") if parsed.get("action") in {"buy", "hold"} else "hold"
            confidence = max(0.0, min(float(parsed.get("confidence", signal["score"])), 1.0))
            return PlannerProposal(action, confidence, str(parsed.get("thesis", "AI proposal")), str(parsed.get("invalidation", "signal invalidated")))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError):
            fallback = super().propose(signal, context)
            return PlannerProposal(fallback.action, fallback.confidence, fallback.thesis + " (offline fallback)", fallback.invalidation)
