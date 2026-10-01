"""Production Python Worker. `python api.py` continues to run test mode locally."""

import json
import time
from copy import deepcopy

from workers import DurableObject, wsgi

from app import create_app
from app.application.errors import CredentialsUnavailable, InvalidScheduleData, UpstreamError
from app.infrastructure.worker_http import WorkerRakushifuConnection
from app.infrastructure.worker_state import month_to_json
from app.settings import MAX_REQUEST_BODY_BYTES


class SessionObject(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.cache = {}
        self.cache_seconds = 120
        self.max_cached_months = 3
        self.generation = 0

    async def create(self, state_json, month_json, key_json,
                     cache_seconds, max_cached_months):
        if await self.ctx.storage.get("session") is not None:
            raise RuntimeError("session already exists")
        self.generation += 1
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
        generation = self.generation
        try:
            result = await connection.fetch_async(
                key[0], key[1], viewer.store_id, viewer.genre_id)
            credentials = deepcopy(connection.export_state())
        except CredentialsUnavailable:
            await self.remove()
            return json.dumps({"error": "credentials"})
        except (UpstreamError, InvalidScheduleData) as error:
            return json.dumps({"error": "upstream", "message": str(error)})
        finally:
            connection.close()
        # External fetch yields to logout/alarm RPCs. Revalidate before writing;
        # consecutive storage calls are protected by Durable Object input gates.
        current_state = await self.read()
        if current_state is None or generation != self.generation:
            return json.dumps({"error": "expired"})
        state = json.loads(current_state)
        state.update(credentials)
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
        self.generation += 1
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
        await self.remove()


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


class Default(wsgi.entrypoint(app)):
    async def fetch(self, request):
        from workers import Request, Response

        if not isinstance(request, Request):
            request = Request(request)

        def too_large():
            return Response(
                json.dumps({"error": "リクエストが大きすぎます"}, ensure_ascii=False),
                status=413, headers={"Content-Type": "application/json; charset=utf-8",
                                     "Cache-Control": "no-store"},
            ).js_object

        length = request.headers.get("Content-Length")
        if length is not None and int(length) > MAX_REQUEST_BODY_BYTES:
            if request.body:
                await request.body.cancel()
            return too_large()
        if not request.body:
            return await super().fetch(request)

        # Bound actual streamed bytes, including bodies without Content-Length,
        # before the WSGI adapter can buffer or parse them.
        reader = request.body.getReader()
        body = bytearray()
        try:
            while True:
                chunk = await reader.read()
                if chunk.done:
                    break
                if len(body) + int(chunk.value.byteLength) > MAX_REQUEST_BODY_BYTES:
                    await reader.cancel()
                    return too_large()
                body.extend(chunk.value.to_bytes())
        finally:
            reader.releaseLock()
        headers = dict(request.headers.items())
        headers["Content-Length"] = str(len(body))
        bounded_request = Request(request.url, method=request.method,
                                  headers=headers, body=bytes(body))
        return await super().fetch(bounded_request)
