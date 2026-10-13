"""账号安全原语:口令哈希、会话令牌、登录失败限流。只用标准库。

注意:认证/会话代码属于高风险区,上线前须经人工安全评审(见 docs/WEB_WORKBENCH.md)。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

PBKDF2_ITERATIONS = 600_000  # OWASP 对 PBKDF2-HMAC-SHA256 的建议下限量级;测试中可调小
MIN_PASSWORD_LENGTH = 12
SESSION_TTL_SECONDS = 12 * 3600
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# 不存在的用户也做一次等价计算,避免通过响应时间枚举用户名。
_DUMMY_HASH = None


def burn_password_check(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    verify_password(password, _DUMMY_HASH)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """库中只存令牌摘要,泄露数据库不等于泄露可用会话。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"口令至少 {MIN_PASSWORD_LENGTH} 个字符")


class LoginThrottle:
    """进程内的登录失败限流(单机部署足够;多实例部署需换共享存储)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def locked(self, username: str) -> bool:
        with self._lock:
            count, since = self._failures.get(username, (0, 0.0))
            if count >= MAX_FAILED_LOGINS and time.monotonic() - since < LOCKOUT_SECONDS:
                return True
            if count >= MAX_FAILED_LOGINS:
                self._failures.pop(username, None)
            return False

    def record_failure(self, username: str) -> None:
        with self._lock:
            count, _ = self._failures.get(username, (0, 0.0))
            self._failures[username] = (count + 1, time.monotonic())

    def reset(self, username: str) -> None:
        with self._lock:
            self._failures.pop(username, None)
