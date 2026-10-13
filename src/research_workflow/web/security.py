"""账号安全原语:口令哈希、会话令牌、登录失败限流。只用标准库。

注意:认证/会话代码属于高风险区,上线前须经人工安全评审(见 docs/WEB_WORKBENCH.md)。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import unicodedata

PBKDF2_ITERATIONS = 600_000  # OWASP 对 PBKDF2-HMAC-SHA256 的建议下限量级;测试中可调小
MIN_PASSWORD_LENGTH = 12
SESSION_TTL_SECONDS = 12 * 3600
MAX_FAILED_LOGINS = 5  # 同一 用户名+来源 的连续失败上限
MAX_FAILED_PER_IP = 30  # 同一来源跨用户名的失败上限(防撞库)
MAX_FAILED_PER_USER = 60  # 同一用户名跨来源的总闸(防分布式猜口令;远高于单源阈值,避免被轻易锁死)
LOCKOUT_SECONDS = 60
THROTTLE_CAPACITY = 10_000  # 失败记录表容量上限,超出后淘汰最旧项,防止被随机用户名撑爆内存
MAX_CONCURRENT_PASSWORD_CHECKS = 4  # PBKDF2 很耗 CPU:限制并发校验数,防止登录接口被拿来打满 CPU


def normalize_username(name: str) -> str:
    """统一用户名:NFKC + 去首尾空白 + casefold。限流键、查库、建号都必须经此函数。"""
    return unicodedata.normalize("NFKC", name).strip().casefold()


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
    """进程内的登录失败限流(单进程部署;多进程/多实例须换共享存储)。

    `limit` 次失败后锁定 `lockout` 秒;容量有上限,超出时淘汰最旧项。
    """

    def __init__(self, limit: int = MAX_FAILED_LOGINS, lockout: float = LOCKOUT_SECONDS,
                 capacity: int = THROTTLE_CAPACITY) -> None:
        self.limit, self.lockout, self.capacity = limit, lockout, capacity
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def locked(self, key: str) -> bool:
        with self._lock:
            count, since = self._failures.get(key, (0, 0.0))
            if count >= self.limit and time.monotonic() - since < self.lockout:
                return True
            if count >= self.limit:
                self._failures.pop(key, None)
            return False

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            self._failures.pop(key, None)
            self._failures[key] = (count + 1, time.monotonic())  # 重新插入 = 最近使用
            while len(self._failures) > self.capacity:
                self._failures.pop(next(iter(self._failures)))

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


password_check_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PASSWORD_CHECKS)
