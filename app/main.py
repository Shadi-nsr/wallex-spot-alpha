from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from .agent import OpenAICompatiblePlanner, TradingAgent
from .engine import BacktestEngine, Candle, JsonlDecisionLog, RiskConfig, ShadowTrader, load_candles, synthetic_candles

AGENTROUTER_ENDPOINT = "https://agentrouter.org/v1/chat/completions"


def _data(path: str | None, count: int) -> list[Candle]:
    return load_candles(path) if path else synthetic_candles(count=count)


def _engine(args: argparse.Namespace) -> BacktestEngine:
    config = RiskConfig(starting_cash=args.cash, risk_per_trade=args.risk, max_position_pct=args.max_position)
    return BacktestEngine(symbol=args.symbol, config=config)


def cmd_demo(args: argparse.Namespace) -> None:
    engine = _engine(args)
    result = engine.run(_data(args.input, args.bars), mode="backtest")
    print(json.dumps({k: result[k] for k in ("symbol", "quote_currency", "market_type", "leverage", "starting_cash", "ending_equity", "return_pct", "max_drawdown_pct", "metrics")}, ensure_ascii=False, indent=2))
    if args.log:
        Path(args.log).write_text("\n".join(json.dumps(d, ensure_ascii=False, sort_keys=True) for d in result["decisions"]) + "\n", encoding="utf-8")


def cmd_shadow(args: argparse.Namespace) -> None:
    engine = _engine(args)
    log = JsonlDecisionLog(args.log)
    planner = None
    if args.ai_endpoint:
        api_key = os.environ.get(args.ai_key_env, "")
        if api_key:
            planner = OpenAICompatiblePlanner(args.ai_endpoint, api_key, model=args.ai_model)
    agent = TradingAgent(engine, planner=planner, log=log)
    count = 0
    for candle in _data(args.input, args.bars):
        decision = agent.on_candle(candle)
        count += 1
        if args.verbose:
            print(json.dumps(decision.as_dict(), ensure_ascii=False))
    print(json.dumps({"mode": "shadow-agent", "symbol": args.symbol, "candles": count, "log": str(args.log), "orders_submitted": 0}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Wallex Spot Alpha — bounded spot research/paper agent")
    p.add_argument("--symbol", default="BTC/TMN", help="spot symbol quoted in تومان, for example BTC/TMN")
    p.add_argument("--cash", type=float, default=5_000_000.0)
    p.add_argument("--risk", type=float, default=0.01)
    p.add_argument("--max-position", type=float, default=0.25)
    p.add_argument("--input", help="JSON/JSONL OHLCV candles")
    p.add_argument("--bars", type=int, default=240)
    p.add_argument("--ai-endpoint", default=os.environ.get("WALLEX_AI_ENDPOINT", AGENTROUTER_ENDPOINT), help="optional OpenAI-compatible /chat/completions endpoint")
    p.add_argument("--ai-model", default=os.environ.get("WALLEX_AI_MODEL", "gpt-6.1-sol"))
    p.add_argument("--ai-key-env", default="WALLEX_AI_API_KEY", help="environment variable name; key is never written to logs")
    sub = p.add_subparsers(dest="command", required=True)
    # Also accept common options after the subcommand, which is friendlier in a demo.
    def sub_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--symbol", default=argparse.SUPPRESS)
        parser.add_argument("--cash", type=float, default=argparse.SUPPRESS)
        parser.add_argument("--risk", type=float, default=argparse.SUPPRESS)
        parser.add_argument("--max-position", type=float, default=argparse.SUPPRESS)
        parser.add_argument("--input", default=argparse.SUPPRESS)
        parser.add_argument("--bars", type=int, default=argparse.SUPPRESS)
        parser.add_argument("--ai-endpoint", default=argparse.SUPPRESS)
        parser.add_argument("--ai-model", default=argparse.SUPPRESS)
        parser.add_argument("--ai-key-env", default=argparse.SUPPRESS)
    demo = sub.add_parser("demo", help="run deterministic backtest")
    sub_common(demo)
    demo.add_argument("--log", help="write decision JSONL")
    demo.set_defaults(fn=cmd_demo)
    shadow = sub.add_parser("shadow", help="observe candles and emit paper-only decisions")
    sub_common(shadow)
    shadow.add_argument("--log", default="data/decisions.jsonl")
    shadow.add_argument("--verbose", action="store_true")
    shadow.set_defaults(fn=cmd_shadow)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.fn(args)
