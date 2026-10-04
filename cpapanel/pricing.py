"""模型计价与成本估算。

诚实声明：上游**不提供**价格，面板的成本是**估算值**。
内置价目表只是常见模型的近似公开价（USD / 1M tokens），
生产环境请用 `pricing.json` 覆盖成你自己的账单口径。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .log import get
from .util import jload, read_text, short

log = get("cpapanel.pricing")

# USD / 1M tokens
DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    # --- Anthropic ---
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-3-7-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "claude-haiku-4": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    # --- OpenAI / Codex ---
    "gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
    "gpt-5-mini": {"input": 0.25, "output": 2.0, "cache_read": 0.025, "cache_write": 0.0},
    "gpt-5-codex": {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
    "gpt-4.1": {"input": 2.0, "output": 8.0, "cache_read": 0.5, "cache_write": 0.0},
    "o3": {"input": 2.0, "output": 8.0, "cache_read": 0.5, "cache_write": 0.0},
    "o4-mini": {"input": 1.1, "output": 4.4, "cache_read": 0.275, "cache_write": 0.0},
    # --- Google ---
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cache_read": 0.31, "cache_write": 0.0},
    "gemini-2.5-flash": {"input": 0.3, "output": 2.5, "cache_read": 0.075, "cache_write": 0.0},
    "gemini-3-pro": {"input": 2.0, "output": 12.0, "cache_read": 0.5, "cache_write": 0.0},
    # --- xAI ---
    "grok-4": {"input": 3.0, "output": 15.0, "cache_read": 0.75, "cache_write": 0.0},
    "grok-code": {"input": 1.0, "output": 5.0, "cache_read": 0.25, "cache_write": 0.0},
    # --- 国内 ---
    "kimi-k2": {"input": 0.6, "output": 2.5, "cache_read": 0.15, "cache_write": 0.0},
    "glm-4.6": {"input": 0.6, "output": 2.2, "cache_read": 0.11, "cache_write": 0.0},
    "deepseek-v3": {"input": 0.27, "output": 1.1, "cache_read": 0.07, "cache_write": 0.0},
    "qwen3-coder": {"input": 0.3, "output": 1.2, "cache_read": 0.08, "cache_write": 0.0},
}


class Pricing:
    def __init__(self, table: Optional[Dict[str, Any]] = None):
        self.table: Dict[str, Dict[str, float]] = {
            k.lower(): {kk: float(vv) for kk, vv in v.items()} for k, v in DEFAULT_PRICING.items()
        }
        if table:
            self.merge(table)

    @classmethod
    def load(cls, path: Optional[str]) -> "Pricing":
        pricing = cls()
        if path and os.path.exists(path):
            data = jload(read_text(path), {})
            if isinstance(data, dict):
                pricing.merge(data.get("models") if "models" in data else data)
                log.info("已载入价格覆盖文件 %s（%d 个模型）", short(path, 80), len(data))
        return pricing

    def merge(self, table: Dict[str, Any]) -> None:
        for model, rates in (table or {}).items():
            if not isinstance(rates, dict):
                continue
            clean = {}
            for key in ("input", "output", "cache_read", "cache_write"):
                try:
                    clean[key] = float(rates.get(key) or 0.0)
                except (TypeError, ValueError):
                    clean[key] = 0.0
            self.table[str(model).lower()] = clean

    def rates_for(self, model: Optional[str]) -> Optional[Dict[str, float]]:
        """最长前缀匹配：`claude-sonnet-4-20250514` 能命中 `claude-sonnet-4`。"""
        if not model:
            return None
        name = str(model).lower()
        if name in self.table:
            return self.table[name]
        best: Optional[str] = None
        for key in self.table:
            if name.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        if best:
            return self.table[best]
        # 退化：用 '-' 分段做包含匹配
        for key in self.table:
            if key.split("-")[0] in name:
                return self.table[key]
        return None

    def estimate(self, model: Optional[str], input_tokens: int = 0, output_tokens: int = 0,
                 reasoning_tokens: int = 0, cached_tokens: int = 0) -> float:
        """返回 USD。推理 token 按输出价计。未知模型返回 0（不猜）。"""
        rates = self.rates_for(model)
        if not rates:
            return 0.0
        million = 1_000_000.0
        billable_input = max(0, int(input_tokens or 0) - int(cached_tokens or 0))
        cost = (
            billable_input * rates.get("input", 0.0)
            + int(cached_tokens or 0) * rates.get("cache_read", 0.0)
            + (int(output_tokens or 0) + int(reasoning_tokens or 0)) * rates.get("output", 0.0)
        ) / million
        return round(cost, 8)

    def known_models(self) -> List[str]:
        return sorted(self.table.keys())

    def as_dict(self) -> Dict[str, Any]:
        return {"models": self.table, "source": "builtin+override",
                "note": "估算价，USD / 1M tokens；请用 pricing.json 覆盖为你的账单口径"}


def write_template(path: str, pricing: Optional[Pricing] = None) -> str:
    """生成一份可编辑的价格覆盖模板。"""
    table = (pricing or Pricing()).table
    payload = {
        "_note": "仅需填写你要覆盖的模型；键为模型名前缀，USD / 1M tokens",
        "models": {k: table[k] for k in sorted(table)},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path
