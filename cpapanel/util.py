"""通用小工具：时间、JSON、掩码、路径、并发辅助。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from typing import Any, Dict, Iterable, Optional

UTC = _dt.timezone.utc

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


# --------------------------------------------------------------------------- 时间

def now_ts() -> int:
    """当前 UNIX 秒（整数，全库统一用它）。"""
    return int(time.time())


def iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[int]:
    """把上游五花八门的时间表示解析成 UNIX 秒。

    支持：unix 秒/毫秒、RFC3339、`2006-01-02 15:04:05`、`2006-01-02`。
    解析失败返回 None（调用方按「未知」处理，绝不抛错）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        # 启发式：> 1e12 视为毫秒
        return int(v / 1000) if v > 1e12 else int(v)
    s = str(value).strip()
    if not s:
        return None
    if _NUM_RE.match(s):
        try:
            return parse_ts(float(s))
        except (TypeError, ValueError):
            return None
    s2 = s.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = _dt.datetime.strptime(s, fmt).replace(tzinfo=UTC)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def day_of(ts: int) -> str:
    return _dt.datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- JSON

def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def jdump_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def jload(text: Any, default: Any = None) -> Any:
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return default
    text = text.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------- 取值

def first(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按顺序取第一个存在且非空的键（大小写与下划线/连字符不敏感）。"""
    if not isinstance(data, dict):
        return default
    normalized = {_norm_key(k): v for k, v in data.items()}
    for key in keys:
        v = normalized.get(_norm_key(key))
        if v is not None and v != "":
            return v
    return default


def _norm_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "enabled"):
        return True
    if s in ("0", "false", "no", "n", "off", "disabled", ""):
        return False
    return default


def deep_get(obj: Any, path: str, default: Any = None) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def pick_int(data: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        v = first(data, key)
        if v is not None:
            n = as_int(v)
            if n is not None:
                return n
    return None


# --------------------------------------------------------------------------- 文本

def mask_secret(value: Optional[str], keep_head: int = 6, keep_tail: int = 4) -> str:
    """脱敏：`sk-abc1234567xyz` -> `sk-abc…7xyz`。空值返回空串。"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return f"{s[:keep_head]}…{s[-keep_tail:]}"


def short(text: Any, limit: int = 200) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def slugify(text: Any, fallback: str = "item") -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text or "")).strip("-")
    return s or fallback


def redact_obj(obj: Any, keys: Iterable[str] = ("management_key", "password", "token",
                                                "api_key", "secret", "authorization")) -> Any:
    """递归脱敏（用于日志与审计，避免密钥落盘）。"""
    lowered = {k.lower() for k in keys}
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in lowered:
                out[k] = mask_secret(v if isinstance(v, str) else str(v))
            else:
                out[k] = redact_obj(v, keys)
        return out
    if isinstance(obj, list):
        return [redact_obj(v, keys) for v in obj]
    return obj


# --------------------------------------------------------------------------- 文件

def ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def write_text_atomic(path: str, text: str, mode: int = 0o600) -> None:
    """先写临时文件再 rename，避免半截文件；默认 0600 权限（配置里有密钥）。"""
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def human_bytes(n: Any) -> str:
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并（用于配置默认值 + 用户覆盖）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
