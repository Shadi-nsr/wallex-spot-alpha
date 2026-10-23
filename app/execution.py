"""Human-confirmed Wallex order gateway; never called from the agent loop."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import time
import urllib.request
from typing import Any

from .markets import CREDIT_INACTIVE_MARKETS, normalize_symbol


class ExecutionError(ValueError):
    pass


class ManualWallexGateway:
    def __init__(self, *, clock: Any = time.time) -> None:
        self.base = os.getenv("WALLEX_API_BASE_URL", "https://api.wallex.ir").rstrip("/")
        self.key = os.getenv("WALLEX_API_KEY", "")
        self.client_id = os.getenv("WALLEX_SUB_ACCOUNT_CLIENT_ID", "")
        self.token = os.getenv("WALLEX_CONFIRM_TOKEN", "")
        self.mode = os.getenv("WALLEX_EXECUTION_MODE", "disabled").lower()
        self.clock = clock
        self.drafts: dict[str, tuple[dict[str, Any], int]] = {}
        self.orders_submitted = 0

    @property
    def configured(self) -> bool:
        return self.mode == "manual" and bool(self.key and self.client_id and self.token)

    def status(self) -> dict[str, Any]:
        return {"mode": "manual-confirmation" if self.mode == "manual" else "disabled", "configured": self.configured, "orders_submitted": self.orders_submitted, "requires_human_confirmation": True}

    def _order(self, p: dict[str, Any]) -> dict[str, Any]:
        s = normalize_symbol(str(p.get("symbol", ""))).replace("/", "")
        shown = f"{s[:-3]}/TMN" if s.endswith("TMN") else ""
        if not s.endswith("TMN") or shown in CREDIT_INACTIVE_MARKETS:
            raise ExecutionError("only active TMN spot markets are allowed")
        side, typ = str(p.get("side", "")).upper(), str(p.get("type", "MARKET")).upper()
        if side not in {"BUY", "SELL"} or typ not in {"MARKET", "LIMIT"}:
            raise ExecutionError("side/type is invalid")
        try:
            qty = float(p["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("quantity is invalid") from exc
        if qty <= 0:
            raise ExecutionError("quantity must be positive")
        o: dict[str, Any] = {"symbol": s, "side": side, "type": typ, "quantity": qty}
        if typ == "LIMIT":
            try:
                price = float(p["price"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ExecutionError("LIMIT orders require price") from exc
            if price <= 0:
                raise ExecutionError("price must be positive")
            o["price"] = price
        return o

    def create_draft(self, payload: dict[str, Any], *, ttl: int = 300) -> dict[str, Any]:
        order, exp = self._order(payload), int(self.clock()) + ttl
        draft_id = secrets.token_urlsafe(18)
        self.drafts[draft_id] = (order, exp)
        return {"draft_id": draft_id, "order": order, "expires_at": exp, "requires_confirmation": True}

    def confirm(self, draft_id: str, token: str) -> dict[str, Any]:
        draft = self.drafts.get(draft_id)
        if not draft:
            raise ExecutionError("draft not found or already used")
        order, exp = draft
        if int(self.clock()) >= exp:
            self.drafts.pop(draft_id, None)
            raise ExecutionError("draft expired")
        if not self.configured:
            raise ExecutionError("manual gateway is not configured")
        if not hmac.compare_digest(token, self.token):
            raise ExecutionError("confirmation token is invalid")
        req = urllib.request.Request(f"{self.base}/v1/account/orders", data=json.dumps(order).encode(), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json", "X-API-Key": self.key, "sub-account-client-id": self.client_id})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                answer = json.loads(res.read().decode() or "{}")
        except Exception as exc:
            raise ExecutionError("Wallex rejected the order") from exc
        self.drafts.pop(draft_id, None)
        self.orders_submitted += 1
        return {"ok": True, "order": order, "wallex": answer}
