import json
import tempfile
import unittest
from pathlib import Path

from app.agent import TradingAgent
from app.engine import BacktestEngine, Candle, JsonlDecisionLog, RawWallexAdapter, RiskConfig, synthetic_candles
from app.markets import CREDIT_INACTIVE_MARKETS, market_status, normalize_symbol


class EngineTests(unittest.TestCase):
    def test_candle_normalizes_milliseconds(self):
        c = Candle.from_mapping({"timestamp": 1700000000000, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 3})
        self.assertEqual(c.timestamp, 1700000000)

    def test_backtest_is_deterministic_and_respects_budget(self):
        cfg = RiskConfig(starting_cash=5_000_000, risk_per_trade=0.01)
        candles = synthetic_candles(180)
        first = BacktestEngine(config=cfg).run(candles)
        second = BacktestEngine(config=cfg).run(candles)
        self.assertEqual(first["ending_equity"], second["ending_equity"])
        self.assertLessEqual(first["max_drawdown_pct"], 12.1)
        self.assertGreaterEqual(first["ending_equity"], 0)
        self.assertEqual(first["starting_cash"], 5_000_000)

    def test_decisions_have_evidence_and_shadow_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            agent = TradingAgent(BacktestEngine(config=RiskConfig(starting_cash=5_000_000)), log=JsonlDecisionLog(path))
            for candle in synthetic_candles(50):
                decision = agent.on_candle(candle)
                self.assertIn("SHADOW", " ".join(decision.reasons))
                self.assertIn("atr", decision.evidence)
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 50)
            self.assertIn("risk", json.loads(lines[-1])["risk_checks"])

    def test_live_boundary_is_hard_blocked(self):
        adapter = RawWallexAdapter(enabled=False)
        self.assertTrue(adapter.place_order(symbol="BTCUSDT", side="buy", quantity=1)["blocked"])
        self.assertTrue(adapter.cancel_order(order_id="x")["blocked"])

    def test_spot_toman_market_policy(self):
        self.assertEqual(normalize_symbol("BTCUSDT"), "BTC/USDT")
        self.assertEqual(BacktestEngine(symbol="BTCTMN").symbol, "BTC/TMN")
        self.assertTrue(market_status("A/TMN")["credit_account_inactive"])
        self.assertTrue(market_status("BTC/TMN")["spot_tradeable"])
        self.assertGreater(len(CREDIT_INACTIVE_MARKETS), 300)

    def test_leverage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "leverage is disabled"):
            RiskConfig(max_leverage=2.0).validate()


if __name__ == "__main__":
    unittest.main()
