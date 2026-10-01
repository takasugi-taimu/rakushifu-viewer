"""Synchronous Flask adapters for the Python Workers Durable Object bindings."""

import json
import re
import secrets
import time

from flask import request

from app.application.errors import (
    CredentialsUnavailable, SessionExpired, StorageUnavailable, UpstreamError,
)
from app.application.session import ActiveSession

from .worker_state import month_from_json, month_to_json, viewer_from_dict


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _binding(name):
    try:
        binding = getattr(request.environ["workers.env"], name)
    except (KeyError, AttributeError) as error:
        raise RuntimeError(f"production requires the {name} Durable Object binding") from error
    return binding


def _run(awaitable):
    from pyodide.ffi import run_sync
    try:
        return run_sync(awaitable)
    except Exception as error:
        raise StorageUnavailable("セッションの保存先に接続できません") from error


class DurableConnection:
    def __init__(self, stub, viewer):
        self.stub = stub
        self.viewer = viewer

    def fetch(self, year, month, store_id, genre_id):
        result = json.loads(_run(self.stub.month(year, month)))
        if result.get("error") == "expired":
            raise SessionExpired("ログインが必要です")
        if result.get("error") == "credentials":
            raise CredentialsUnavailable("らくしふのログインが期限切れです")
        if result.get("error") == "upstream":
            raise UpstreamError(result["message"])
        if result.get("error") != "ok":
            raise RuntimeError("unexpected Durable Object month result")
        return month_from_json(result["month"])

    def close(self):
        # The Durable Object owns the persistent state and cache.
        pass


class DurableSessionStore:
    def __init__(self, lifetime_seconds=3600, cache_seconds=120,
                 max_cached_months=3):
        self.lifetime_seconds = lifetime_seconds
        self.cache_seconds = cache_seconds
        self.max_cached_months = max_cached_months

    def _stub(self, token):
        return _binding("SESSIONS").getByName(token)

    def create(self, value: ActiveSession) -> str:
        token = secrets.token_urlsafe(32)
        connection = value.connection
        state = connection.export_state()
        state["expires_at"] = time.time() + self.lifetime_seconds
        initial = next(iter(value.cache.items()), None)
        initial_month = (month_to_json(initial[1][1]) if initial else "")
        initial_key = list(initial[0]) if initial else []
        _run(self._stub(token).create(
            json.dumps(state, ensure_ascii=False), initial_month,
            json.dumps(initial_key), self.cache_seconds, self.max_cached_months))
        connection.close()
        return token

    def get(self, token: str):
        if not TOKEN_PATTERN.fullmatch(token):
            return None
        serialized = _run(self._stub(token).read())
        if serialized is None:
            return None
        state = json.loads(serialized)
        viewer = viewer_from_dict(state["viewer"])
        return ActiveSession(viewer, DurableConnection(self._stub(token), viewer))

    def delete(self, token: str):
        if TOKEN_PATTERN.fullmatch(token):
            _run(self._stub(token).remove())


class DurableLoginLimiter:
    def __init__(self, window_seconds=300, per_ip=30,
                 per_employee_and_ip=5):
        self.window_seconds = window_seconds
        self.per_ip = per_ip
        self.per_employee_and_ip = per_employee_and_ip

    def allow(self, ip, employee_code):
        namespace = _binding("LOGIN_LIMITS")
        ip_allowed = _run(namespace.getByName("ip:" + ip).allow(
            self.window_seconds, self.per_ip))
        if not ip_allowed:
            return False
        return bool(_run(namespace.getByName(
            "employee:" + ip + ":" + employee_code).allow(
                self.window_seconds, self.per_employee_and_ip)))
