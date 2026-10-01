import time
from collections import deque
from threading import Lock


class MemoryLoginLimiter:
    """Bound password forwarding to the upstream login endpoint."""

    def __init__(self, window_seconds: int = 300,
                 per_ip: int = 30, per_employee_and_ip: int = 5):
        self.window_seconds = window_seconds
        self.per_ip = per_ip
        self.per_employee_and_ip = per_employee_and_ip
        self._attempts = {}
        self._lock = Lock()

    def allow(self, ip: str, employee_code: str) -> bool:
        now = time.monotonic()
        keys = (("ip", ip, self.per_ip),
                ("employee", ip, employee_code, self.per_employee_and_ip))
        with self._lock:
            if len(self._attempts) > 1000:
                for old_key, attempts in list(self._attempts.items()):
                    while attempts and attempts[0] <= now - self.window_seconds:
                        attempts.popleft()
                    if not attempts:
                        del self._attempts[old_key]
            for item in keys:
                key, limit = item[:-1], item[-1]
                attempts = self._attempts.setdefault(key, deque())
                while attempts and attempts[0] <= now - self.window_seconds:
                    attempts.popleft()
                if len(attempts) >= limit:
                    return False
            for item in keys:
                self._attempts[item[:-1]].append(now)
            return True
