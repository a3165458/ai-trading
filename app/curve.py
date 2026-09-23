from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class EquityCurve:
    """Equity points over time, thinned so a long run still fits in `limit` points.

    Points are at least `step_ms` apart; the newest point is a live tail that is
    replaced until the step elapses. When the list overflows, every other point is
    dropped and the step doubles, so resolution degrades evenly across the history.
    """

    def __init__(self, limit: int = 1500, step_ms: int = 2000):
        self.limit = max(10, int(limit))
        self.step_ms = max(1, int(step_ms))
        self.points: list[dict[str, Any]] = []
        self.rev = 0

    def add(self, point: dict[str, Any]) -> bool:
        """Returns True when appended, False when it replaced the live tail."""
        pts = self.points
        if len(pts) >= 2 and point["ts_ms"] - pts[-2]["ts_ms"] < self.step_ms:
            pts[-1] = point
            return False
        pts.append(point)
        if len(pts) > self.limit:
            kept = pts[::2]
            if kept[-1] is not pts[-1]:
                kept.append(pts[-1])
            self.points = kept
            self.step_ms *= 2
            self.rev += 1
        return True

    def to_json(self) -> dict[str, Any]:
        return {"step_ms": self.step_ms, "points": self.points}

    def load(self, data: dict[str, Any]) -> None:
        pts = [p for p in data.get("points") or [] if isinstance(p, dict) and "ts_ms" in p and "equity" in p]
        pts.sort(key=lambda p: p["ts_ms"])
        self.points = pts[-self.limit:]
        self.step_ms = max(self.step_ms, int(data.get("step_ms") or 0))
        self.rev += 1


def account_tag(account_index: int | None) -> str:
    return hashlib.sha256(str(account_index).encode()).hexdigest()[:16]


def read_store(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
