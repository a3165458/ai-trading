from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from app.config import Settings
from app.types import Action, Decision, Snapshot

OPTIONS: tuple[Action, Action] = ("buy", "sell")
QUESTION = "Should the execution system buy or sell this perpetual now?"


def _normalize_probs(raw: dict[str, Any] | None) -> dict[str, float]:
    probs = {o: 0.0 for o in OPTIONS}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in OPTIONS:
                probs[k] = float(v)
    s = sum(probs.values())
    if s > 0:
        return {k: v / s for k, v in probs.items()}
    return {"buy": 0.5, "sell": 0.5}


def _pick(chosen: Any, probs: dict[str, float]) -> Action:
    if chosen in OPTIONS:
        return chosen  # type: ignore[return-value]
    return "buy" if probs.get("buy", 0) >= probs.get("sell", 0) else "sell"


def error_decision(source: str, latency_ms: float, error: str, prompt: str = "") -> Decision:
    return Decision(
        "buy",
        {"buy": 0.5, "sell": 0.5},
        0.0,
        latency_ms,
        source,
        error=error,
        prompt=prompt,
        question=QUESTION,
    )


def _logprobs_public(content_lp: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(content_lp, list):
        return out
    for item in content_lp:
        if not isinstance(item, dict):
            continue
        tops = []
        for t in item.get("top_logprobs") or []:
            if not isinstance(t, dict):
                continue
            lp = float(t.get("logprob") or -99)
            tops.append({"token": str(t.get("token") or ""), "logprob": lp, "p": math.exp(lp)})
        lp0 = float(item.get("logprob") or -99)
        out.append({
            "token": str(item.get("token") or ""),
            "logprob": lp0,
            "p": math.exp(lp0),
            "top": tops,
        })
    return out


def completions_url(base: str) -> str:
    b = base.rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def parse_decision(body: dict[str, Any], latency_ms: float, source: str, prompt: str = "") -> Decision:
    extra = body.get("this_that") if isinstance(body.get("this_that"), dict) else None
    choice0 = (body.get("choices") or [{}])[0]
    content = ((choice0.get("message") or {}).get("content")) or ""
    content_lp = ((choice0.get("logprobs") or {}).get("content")) or []
    logprobs = _logprobs_public(content_lp)

    chosen: str | None = None
    probs: dict[str, float] | None = None
    conf: float | None = None
    lat = latency_ms

    if extra:
        probs = _normalize_probs(extra.get("probabilities") if isinstance(extra.get("probabilities"), dict) else None)
        chosen = extra.get("choice")
        if extra.get("confidence") is not None:
            conf = float(extra["confidence"])
        if extra.get("latency_ms") is not None:
            lat = float(extra["latency_ms"])

    if chosen is None:
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

    raw_lp: dict[str, float] = {}
    if logprobs:
        for t in (logprobs[0].get("top") or []):
            tok = str(t.get("token") or "").strip().strip('"')
            if tok in OPTIONS:
                raw_lp[tok] = float(t.get("p") or 0)
    if probs is None:
        if raw_lp:
            probs = _normalize_probs(raw_lp)
        elif chosen in OPTIONS:
            probs = {o: (1.0 if o == chosen else 0.0) for o in OPTIONS}
        else:
            probs = {"buy": 0.5, "sell": 0.5}
    chosen = _pick(chosen, probs)
    if conf is None:
        conf = probs.get(chosen, 0.0)
    raw = json.dumps({"content": content, "this_that": extra, "logprobs": logprobs}, default=str)[:4000]
    return Decision(
        chosen, probs, conf, lat, source,
        raw=raw, prompt=prompt, question=QUESTION, content=content,
        this_that=extra, logprobs=logprobs,
    )


class MockModel:
    name = "mock"

    async def decide(self, snapshot: Snapshot, state: str) -> Decision:
        t0 = time.perf_counter()
        ret = (snapshot.ret_1h_bps or 0) / 8 + (snapshot.ret_5m_bps or 0) / 20
        flow = snapshot.cvd
        signal = ret / 12 + snapshot.imbalance * 1.4 + math.tanh(flow * 50) * 0.8
        buy = 1 / (1 + math.exp(-signal))
        sell = 1 - buy
        action: Action = "buy" if buy >= sell else "sell"
        probs = _normalize_probs({"buy": buy, "sell": sell})
        _ = state
        return Decision(
            action, probs, probs[action], (time.perf_counter() - t0) * 1000, "mock",
            prompt=state, question=QUESTION,
            content=json.dumps({"answer": action}),
            this_that={"choice": action, "probabilities": probs, "confidence": probs[action], "signal": round(signal, 4)},
            logprobs=[{
                "token": action,
                "logprob": math.log(max(probs[action], 1e-9)),
                "p": probs[action],
                "top": [
                    {"token": "buy", "logprob": math.log(max(probs["buy"], 1e-9)), "p": probs["buy"]},
                    {"token": "sell", "logprob": math.log(max(probs["sell"], 1e-9)), "p": probs["sell"]},
                ],
            }],
        )


def parse_jev_decision(body: dict[str, Any], latency_ms: float, source: str = "jev") -> Decision:
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("jev response has no answers")
    payload = answers.get("action") or next(iter(answers.values()), None)
    if not isinstance(payload, dict):
        raise ValueError("jev response has no action answer")
    probs = _normalize_probs(payload.get("probabilities") if isinstance(payload.get("probabilities"), dict) else None)
    chosen = _pick(payload.get("choice"), probs)
    conf = float(payload.get("confidence") or probs.get(chosen, 0.0))
    return Decision(
        chosen, probs, conf, latency_ms, source,
        raw=json.dumps(payload)[:4000],
        question=QUESTION,
        content=json.dumps({"answer": chosen}),
        this_that=payload,
    )


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
            "top_logprobs": 2,
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
                return error_decision("thisthat", latency, f"{r.status_code} {r.text[:500]}", prompt=state)
            return parse_decision(r.json(), latency, "thisthat", prompt=state)
        except Exception as e:
            return error_decision("thisthat", (time.perf_counter() - t0) * 1000, str(e), prompt=state)


class JevDecisionModel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.name = settings.jev_model
        self._http = httpx.AsyncClient(timeout=45.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def decide(self, snapshot: Snapshot, state: str) -> Decision:
        t0 = time.perf_counter()
        payload = {
            "model": self.settings.jev_model,
            "state": state,
            "questions": {
                "action": {
                    "type": "choice",
                    "instructions": QUESTION,
                    "criteria": {
                        "buy": "Open or add a long, or buy this perpetual now.",
                        "sell": "Open or add a short, or sell this perpetual now.",
                    },
                }
            },
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.jev_api_key:
            headers["Authorization"] = f"Bearer {self.settings.jev_api_key}"
        try:
            r = await self._http.post(self.settings.jev_api_url, json=payload, headers=headers)
            latency = (time.perf_counter() - t0) * 1000
            if r.status_code >= 400:
                return error_decision("jev", latency, f"{r.status_code} {r.text[:500]}", prompt=state)
            d = parse_jev_decision(r.json(), latency, "jev")
            d.prompt = state
            return d
        except Exception as e:
            return error_decision("jev", (time.perf_counter() - t0) * 1000, str(e), prompt=state)


DecisionModel = MockModel | OpenAIDecisionModel | JevDecisionModel


def build_model(settings: Settings) -> DecisionModel:
    backend = settings.resolved_backend
    if backend == "jev":
        return JevDecisionModel(settings)
    if backend == "thisthat":
        return OpenAIDecisionModel(settings)
    return MockModel()
