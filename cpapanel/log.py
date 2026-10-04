"""日志：stdout + 可选文件 + 内存环形缓冲（Web UI 的「面板日志」页用）。"""

from __future__ import annotations

import collections
import logging
import os
import sys
import threading
from typing import Any, Deque, Dict, List, Optional

_BUFFER: Deque[Dict[str, Any]] = collections.deque(maxlen=2000)
_LOCK = threading.Lock()
_SEQ = 0
_CONFIGURED = False


def _record(level: str, logger_name: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
    global _SEQ
    with _LOCK:
        _SEQ += 1
        _BUFFER.append({
            "seq": _SEQ,
            "ts": __import__("time").time(),
            "level": level,
            "logger": logger_name,
            "message": message,
            "extra": extra or {},
        })


def buffer(limit: int = 200, after_seq: int = 0, level: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取内存日志（最新的在最后）。"""
    with _LOCK:
        items = list(_BUFFER)
    if after_seq:
        items = [i for i in items if i["seq"] > after_seq]
    if level:
        wanted = level.upper()
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
        floor = order.get(wanted, 0)
        items = [i for i in items if order.get(i["level"], 0) >= floor]
    return items[-limit:]


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 由 logging 调用
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            _record(record.levelname, record.name, message)
        except Exception:
            pass


def setup(level: str = "INFO", log_file: Optional[str] = None, quiet: bool = False) -> logging.Logger:
    """初始化根日志。幂等，可重复调用。"""
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if _CONFIGURED:
        return logging.getLogger("cpapanel")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    if not quiet:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass
    buf = _BufferHandler()
    buf.setLevel(logging.DEBUG)
    root.addHandler(buf)
    # 第三方库降噪
    for noisy in ("urllib3", "http.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
    return logging.getLogger("cpapanel")


def get(name: str = "cpapanel") -> logging.Logger:
    return logging.getLogger(name)


def note(level: str, message: str, **extra: Any) -> None:
    """不经过 logging 的旁路记录（用于采集/巡检的结构化事件）。"""
    _record(level.upper(), "cpapanel.event", message, extra)
    logging.getLogger("cpapanel.event").log(
        getattr(logging, level.upper(), logging.INFO), "%s %s", message, extra or "")
