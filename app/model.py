from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from app.config import Settings
from app.types import Action, Decision, Snapshot

OPTIONS: tuple[Action, Action, Action] = ("buy", "sell", "hold")
QUESTION = "Should the execution system buy, sell, or hold this perpetual now?"


def completions_url(base: str) -> str:
    b = base.rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def parse_decision(body: dict[str, Any], latency_ms: float, source: str) -> Decision:
    extra = body.get("this_that")
    if isinstance(extra, dict) and extra.get("choice") in OPTIONS:
        probs = {o: 0.0 for o in OPTIONS}
        raw_p = extra.get("probabilities") or {}
        if isinstance(raw_p, dict):
            for k, v in raw_p.items():
                if k in OPTIONS:
                    probs[k] = float(v)
        s = sum(probs.values())
        if s > 0:
            probs = {k: v / s for k, v in probs.items()}
        choice = extra["choice"]
        conf = float(extra.get("confidence") or probs.get(choice, 0.0))
        return Decision(choice, probs, conf, extra.get("latency_ms") or latency_ms, source, raw=json.dumps(extra))

    choice0 = (body.get("choices") or [{}])[0]
    content = ((choice0.get("message") or {}).get("content")) or ""
    chosen: str | None = None
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            chosen = obj.get("answer") or obj.get("action") or obj.get("choice")
        elif isinstance(obj, str):
            chosen = obj
    except (json.JSONDecodeError, TypeError):
        token = content.strip().lower().strip('"')
        if token in OPTIONS:
            chosen = token

    probs = {o: 0.0 for o in OPTIONS}
    content_lp = ((choice0.get("logprobs") or {}).get("content")) or []
    tops = content_lp[0].get("top_logprobs") if content_lp else None
    if isinstance(tops, list):
        for item in tops:
            tok = str(item.get("token") or "").strip().strip('"')
            if tok in OPTIONS:
                probs[tok] = math.exp(float(item.get("logprob") or -99))
        s = sum(probs.values())
        if s > 0:
            probs = {k: v / s for k, v in probs.items()}
    if chosen not in OPTIONS:
        chosen = max(probs, key=probs.get) if sum(probs.values()) else "hold"
    if sum(probs.values()) == 0:
        probs[chosen] = 1.0
    return Decision(chosen, probs, probs.get(chosen, 0.0), latency_ms, source, raw=content[:2000])


class MockModel:
    name = "mock"

    async def decide(self, snapshot: Snapshot, state: str) -> Decision:
        t0 = time.perf_counter()
        ret = (snapshot.ret_1h_bps or 0) / 8 + (snapshot.ret_5m_bps or 0) / 20
        flow = snapshot.cvd
        signal = ret / 12 + snapshot.imbalance * 1.4 + math.tanh(flow * 50) * 0.8
        buy = 1 / (1 + math.exp(-signal))
        sell = 1 - buy
        edge = abs(buy - 0.5)
        if edge < 0.06:
            action: Action = "hold"
            hold = 0.55 + (0.06 - edge)
            rest = 1 - hold
            probs = {"buy": rest * buy, "sell": rest * sell, "hold": hold}
        else:
            action = "buy" if buy >= 0.5 else "sell"
            hold = max(0.04, 0.25 - edge)
            rest = 1 - hold
            probs = {"buy": rest * buy, "sell": rest * sell, "hold": hold}
        s = sum(probs.values())
        probs = {k: v / s for k, v in probs.items()}
        _ = state
        return Decision(action, probs, probs[action], (time.perf_counter() - t0) * 1000, "mock")


class OpenAIDecisionModel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.name = settings.openai_model
        self._http = httpx.AsyncClient(timeout=45.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def decide(self, snapshot: Snapshot, state: str) -> Decision:
        t0 = time.perf_counter()
        payload = {
            "model": self.settings.openai_model,
            "messages": [
                {"role": "system", "content": "Typed trading decision. Choose exactly one declared option."},
                {"role": "user", "content": state},
                {"role": "user", "content": QUESTION},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "decision",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"enum": list(OPTIONS)}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                },
            },
            "logprobs": True,
            "top_logprobs": 3,
            "temperature": 1,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.openai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.openai_api_key}"
        url = completions_url(self.settings.openai_base_url)
        try:
            r = await self._http.post(url, json=payload, headers=headers)
            latency = (time.perf_counter() - t0) * 1000
            if r.status_code >= 400:
                return Decision(
                    "hold",
                    {"buy": 0, "sell": 0, "hold": 1},
                    1.0,
                    latency,
                    "openai",
                    error=f"{r.status_code} {r.text[:500]}",
                )
            return parse_decision(r.json(), latency, "openai")
        except Exception as e:
            return Decision(
                "hold",
                {"buy": 0, "sell": 0, "hold": 1},
                1.0,
                (time.perf_counter() - t0) * 1000,
                "openai",
                error=str(e),
            )


def build_model(settings: Settings) -> MockModel | OpenAIDecisionModel:
    if settings.use_remote_model:
        return OpenAIDecisionModel(settings)
    return MockModel()
