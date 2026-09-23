from __future__ import annotations

from typing import Any

_DROP = {
    "prompt",
    "raw",
    "this_that",
    "logprobs",
    "tx_hash",
    "content",
    "question",
    "api_key",
    "private_key",
    "l1_address",
    "account_index",
    "auth",
    "token",
}


def public_payload(obj: Any) -> Any:
    """Strip keys, wallet ids, prompts, and tx hashes from anything sent to the browser."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if k in _DROP or lk in _DROP or "private" in lk or lk.endswith("_key"):
                continue
            if v and (lk == "error" or (lk == "message" and obj.get("event") == "error")):
                out[k] = "failed"
                continue
            out[k] = public_payload(v)
        return out
    if isinstance(obj, list):
        return [public_payload(x) for x in obj]
    return obj
