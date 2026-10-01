"""Production Python Worker. `python api.py` continues to run test mode locally."""

import json
import time

from workers import DurableObject, wsgi

from app import create_app
from app.application.errors import CredentialsUnavailable, InvalidScheduleData, UpstreamError
from app.infrastructure.worker_http import WorkerRakushifuConnection
from app.infrastructure.worker_state import month_to_json


class SessionObject(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.cache = {}
        self.cache_seconds = 120
        self.max_cached_months = 3

    async def create(self, state_json, month_json, key_json,
                     cache_seconds, max_cached_months):
        if await self.ctx.storage.get("session") is not None:
            raise RuntimeError("session already exists")
        self.cache_seconds = int(cache_seconds)
        self.max_cached_months = int(max_cached_months)
        state = json.loads(state_json)
        state["cache_seconds"] = self.cache_seconds
        state["max_cached_months"] = self.max_cached_months
        state_json = json.dumps(state, ensure_ascii=False)
        await self.ctx.storage.put("session", state_json)
        await self.ctx.storage.setAlarm(
            int(state["expires_at"] * 1000))
        key = json.loads(key_json)
        if month_json and len(key) == 2:
            self.cache[tuple(key)] = (time.time() + self.cache_seconds, month_json)

    async def read(self):
        state_json = await self.ctx.storage.get("session")
        if state_json is None:
            return None
        if json.loads(state_json)["expires_at"] <= time.time():
            await self.remove()
            return None
        return state_json

    async def month(self, year, month):
        state_json = await self.read()
        if state_json is None:
            return json.dumps({"error": "expired"})
        state = json.loads(state_json)
        self.cache_seconds = state["cache_seconds"]
        self.max_cached_months = state["max_cached_months"]
        key = (int(year), int(month))
        cached = self.cache.get(key)
        if cached and cached[0] > time.time():
            return json.dumps({"error": "ok", "month": cached[1]})
        connection = WorkerRakushifuConnection.from_state(state)
        viewer = connection.viewer
        try:
            result = await connection.fetch_async(
                key[0], key[1], viewer.store_id, viewer.genre_id)
        except CredentialsUnavailable:
            await self.remove()
            return json.dumps({"error": "credentials"})
        except (UpstreamError, InvalidScheduleData) as error:
            return json.dumps({"error": "upstream", "message": str(error)})
        state.update(connection.export_state())
        await self.ctx.storage.put("session", json.dumps(state, ensure_ascii=False))
        now = time.time()
        self.cache = {old: value for old, value in self.cache.items()
                      if value[0] > now}
        if key not in self.cache and len(self.cache) >= self.max_cached_months:
            oldest = min(self.cache, key=lambda item: self.cache[item][0])
            del self.cache[oldest]
        serialized = month_to_json(result)
        self.cache[key] = (now + self.cache_seconds, serialized)
        return json.dumps({"error": "ok", "month": serialized})

    async def remove(self):
        self.cache.clear()
        await self.ctx.storage.deleteAlarm()
        await self.ctx.storage.deleteAll()

    async def alarm(self):
        state_json = await self.ctx.storage.get("session")
        if state_json is None:
            return
        expires_at = json.loads(state_json)["expires_at"]
        if expires_at > time.time():
            await self.ctx.storage.setAlarm(int(expires_at * 1000))
            return
        self.cache.clear()
        await self.ctx.storage.deleteAll()


class LoginLimitObject(DurableObject):
    async def allow(self, window_seconds, limit):
        now = time.time()
        value = await self.ctx.storage.get("attempts")
        attempts = [instant for instant in json.loads(value) if instant > now - window_seconds] if value else []
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        await self.ctx.storage.put("attempts", json.dumps(attempts))
        await self.ctx.storage.setAlarm(int((now + window_seconds) * 1000))
        return True

    async def alarm(self):
        await self.ctx.storage.deleteAll()


app = create_app({"APP_ENV": "production", "APP_COOKIE_SECURE": True},
                 worker_runtime=True)
Default = wsgi.entrypoint(app)
