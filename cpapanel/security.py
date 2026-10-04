"""口令与会话安全。

* 口令：`pbkdf2_sha256`，20 万轮，随机 16 字节盐，格式 `pbkdf2_sha256$轮数$盐$摘要`。
* 比较：一律 `hmac.compare_digest`（常量时间），避免计时侧信道。
* 会话/API 令牌：`secrets.token_urlsafe(32)`，数据库里**只存 SHA-256 摘要**，明文仅返回一次。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional, Tuple

PBKDF2_ROUNDS = 200_000
PBKDF2_ALGO = "pbkdf2_sha256"


def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    if not password:
        raise ValueError("口令不能为空")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{PBKDF2_ALGO}${rounds}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        algo, rounds_s, salt_s, digest_s = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        rounds = int(rounds_s)
        salt = _unb64(salt_s)
        expected = _unb64(digest_s)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def constant_time_eq(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def generate_management_key() -> str:
    """生成一个符合上游习惯的管理密钥（sk- 前缀）。"""
    return "sk-" + secrets.token_hex(24)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def split_credentials(value: str) -> Tuple[str, str]:
    """`user:pass` → (user, pass)，用于 --auth 参数。"""
    if ":" not in (value or ""):
        return value, ""
    user, _, pwd = value.partition(":")
    return user, pwd
