"""Local demo API. It runs on localhost and does not contain exchange credentials."""
from __future__ import annotations
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .agent import TradingAgent
from .engine import BacktestEngine, Candle, JsonlDecisionLog, RiskConfig, synthetic_candles
from .markets import CREDIT_INACTIVE_MARKETS, TMN_QUOTE, market_status


class AppState:
    def __init__(self) -> None:
        engine = BacktestEngine(config=RiskConfig(starting_cash=5_000_000.0))
        self.engine = engine
        self.agent = TradingAgent(engine, log=JsonlDecisionLog("data/decisions.jsonl"))
        self.adapter_enabled = False

    def backtest(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        candles = [Candle.from_mapping(row) for row in rows] if rows else synthetic_candles()
        result = self.engine.run(candles)
        # Keep response small enough for a browser demo while preserving evidence.
        return {k: result[k] for k in ("symbol", "mode", "quote_currency", "market_type", "leverage", "starting_cash", "ending_equity", "return_pct", "max_drawdown_pct", "metrics", "trades", "decisions")}


STATE = AppState()


class Handler(BaseHTTPRequestHandler):
    server_version = "WallexSpot/1.0"

    def _write(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
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
            return self._write({"ok": True, "service": "wallex-spot-alpha", "mode": "shadow", "orders_submitted": 0, "adapter_enabled": STATE.adapter_enabled})
        if self.path == "/api/config":
            return self._write({"symbol": STATE.engine.symbol, "risk": STATE.engine.config.__dict__, "tools": STATE.agent.tools.names(), "execution": "disabled", "market_type": "spot", "leverage": 1.0, "quote_currency": TMN_QUOTE})
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
        if self.path == "/api/execution/order":
            # A hard guard at the HTTP boundary in addition to the adapter guard.
            return self._write({"ok": False, "blocked": True, "orders_submitted": 0, "reason": "execution disabled in this deliverable"}, 403)
        self._write({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep output clean for a five-minute demo.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Wallex Spot Alpha local API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print(f"Wallex Spot Alpha listening on http://{args.host}:{args.port} (spot-only, تومان, execution disabled)")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
