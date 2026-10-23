"""Local demo API; any optional exchange credential is read only from environment."""
from __future__ import annotations
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .agent import TradingAgent
from .engine import BacktestEngine, Candle, JsonlDecisionLog, RiskConfig, synthetic_candles
from .execution import ExecutionError, ManualWallexGateway
from .markets import CREDIT_INACTIVE_MARKETS, TMN_QUOTE, market_status
from .market_data import MarketDataError, fetch_candles


class AppState:
    def __init__(self) -> None:
        engine = BacktestEngine(config=RiskConfig(starting_cash=5_000_000.0))
        self.engine = engine
        log_path = os.environ.get("WALLEX_DECISION_LOG_PATH", "/tmp/wallex-decisions.jsonl")
        self.agent = TradingAgent(engine, log=JsonlDecisionLog(log_path))
        self.gateway = ManualWallexGateway()
        self.proposal_lock = threading.Lock()
        self.market_cursor = 0
        self.last_decision: dict[str, Any] | None = None
        self.pending_proposal: dict[str, Any] | None = None

    def refresh_proposal(self) -> dict[str, Any]:
        """Advance the bounded strategy from public candles and draft a buy suggestion."""
        with self.proposal_lock:
            candles = fetch_candles(self.engine.symbol)
            latest_decision = self.last_decision
            for candle in candles:
                if candle.timestamp <= self.market_cursor:
                    continue
                latest_decision = self.agent.on_candle(candle).as_dict()
                self.market_cursor = candle.timestamp
            self.last_decision = latest_decision
            if self.pending_proposal and self.pending_proposal["draft"]["draft_id"] not in self.gateway.drafts:
                self.pending_proposal = None
            if latest_decision and latest_decision.get("action") == "buy" and latest_decision.get("quantity", 0) > 0:
                if not self.pending_proposal or self.pending_proposal["decision"]["timestamp"] != latest_decision["timestamp"]:
                    self.pending_proposal = {
                        "decision": latest_decision,
                        "draft": self.gateway.create_draft({
                            "symbol": latest_decision["symbol"],
                            "side": "BUY",
                            "type": "MARKET",
                            "quantity": latest_decision["quantity"],
                        }),
                    }
            return {"ok": True, "decision": self.last_decision, "proposal": self.pending_proposal, "poll_after_seconds": 20}

    def backtest(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        candles = [Candle.from_mapping(row) for row in rows] if rows else synthetic_candles()
        result = self.engine.run(candles)
        # Keep response small enough for a browser demo while preserving evidence.
        return {k: result[k] for k in ("symbol", "mode", "quote_currency", "market_type", "leverage", "starting_cash", "ending_equity", "return_pct", "max_drawdown_pct", "metrics", "trades", "decisions")}


STATE = AppState()


class Handler(BaseHTTPRequestHandler):
    server_version = "WallexSpot/1.0"

    def _session_id(self) -> str:
        raw = self.headers.get("Cookie", "")
        for item in raw.split(";"):
            key, _, value = item.strip().partition("=")
            if key == "wallex_manual_session":
                return value
        return ""

    def _write(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _write_with_cookie(self, payload: Any, session_id: str, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"wallex_manual_session={session_id}; Max-Age=43200; HttpOnly; SameSite=Lax; Path=/")
        self.end_headers()
        self.wfile.write(raw)

    def _write_html(self, raw: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/":
            try:
                return self._write_html(open("app/static/index.html", "rb").read())
            except OSError:
                return self._write({"ok": True, "service": "wallex-spot-alpha"})
        if self.path == "/health":
            status = STATE.gateway.status()
            return self._write({"ok": True, "service": "wallex-spot-alpha", "mode": "shadow", "orders_submitted": status["orders_submitted"], "adapter_enabled": False, "execution": status})
        if self.path == "/api/config":
            return self._write({"symbol": STATE.engine.symbol, "risk": STATE.engine.config.__dict__, "tools": STATE.agent.tools.names(), "execution": STATE.gateway.status(), "market_type": "spot", "leverage": 1.0, "quote_currency": TMN_QUOTE})
        if self.path == "/api/execution/status":
            return self._write(STATE.gateway.status())
        if self.path == "/api/execution/proposal":
            if not STATE.gateway.session_active(self._session_id()):
                return self._write({"ok": False, "error": "approval session has not been started"}, 401)
            try:
                return self._write(STATE.refresh_proposal())
            except (MarketDataError, ExecutionError) as exc:
                return self._write({"ok": False, "error": str(exc)}, 503)
        if self.path == "/api/markets":
            return self._write({"market_type": "spot", "quote_currency": TMN_QUOTE, "credit_account_inactive_count": len(CREDIT_INACTIVE_MARKETS), "credit_account_inactive_markets": sorted(CREDIT_INACTIVE_MARKETS), "example": market_status(STATE.engine.symbol)})
        self._write({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._write({"error": "body must be JSON"}, 400)
        if self.path == "/api/backtest":
            try:
                return self._write(STATE.backtest(body.get("candles")))
            except (ValueError, KeyError) as exc:
                return self._write({"error": str(exc)}, 400)
        if self.path == "/api/shadow/candle":
            try:
                decision = STATE.agent.on_candle(Candle.from_mapping(body))
                return self._write({"decision": decision.as_dict(), "orders_submitted": 0, "execution": "disabled"})
            except (ValueError, KeyError) as exc:
                return self._write({"error": str(exc)}, 400)
        if self.path == "/api/execution/start":
            session_id = STATE.gateway.start_session()
            return self._write_with_cookie({"ok": True, "started": True, "requires_human_confirmation": True}, session_id)
        if self.path == "/api/execution/order/draft":
            try:
                return self._write(STATE.gateway.create_draft(body))
            except ExecutionError as exc:
                return self._write({"ok": False, "error": str(exc)}, 400)
        if self.path == "/api/execution/order/confirm":
            try:
                draft_id = str(body.get("draft_id", ""))
                if not draft_id:
                    raise ExecutionError("draft_id لازم است")
                return self._write(STATE.gateway.confirm(draft_id, self._session_id()))
            except ExecutionError as exc:
                return self._write({"ok": False, "error": str(exc)}, 400)
        if self.path == "/api/execution/order/reject":
            try:
                draft_id = str(body.get("draft_id", ""))
                if not draft_id:
                    raise ExecutionError("draft_id لازم است")
                return self._write(STATE.gateway.reject(draft_id, self._session_id()))
            except ExecutionError as exc:
                return self._write({"ok": False, "error": str(exc)}, 400)
        self._write({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep output clean for a five-minute demo.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Wallex Spot Alpha local API")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    print(f"Wallex Spot Alpha listening on http://{args.host}:{args.port} (spot-only, تومان, manual confirmation only)")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
