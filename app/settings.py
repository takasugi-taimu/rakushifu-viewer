import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppSettings:
    environment: str
    session_seconds: int = 3600
    cache_seconds: int = 120
    max_cached_months: int = 3

    @classmethod
    def load(cls, environment=None):
        name = environment if environment is not None else os.environ.get("APP_ENV", "test")
        if name not in ("test", "production"):
            raise ValueError("APP_ENV must be 'test' or 'production'")
        return cls(name)
