import asyncio
import json
import sys
import time
import types
import unittest
from unittest.mock import patch

from app import create_app
from app.application.errors import StorageUnavailable
from app.infrastructure.worker_state import month_from_json, month_to_json
from app.infrastructure.worker_http import WorkerCookieJar
from app.infrastructure.rakushifu_mapper import map_schedule
from app.settings import AppSettings


class StorageModeTests(unittest.TestCase):
    def test_storage_failure_returns_503_instead_of_login_expired(self):
        class BrokenUseCases:
            def authenticated(self, token):
                raise StorageUnavailable("セッションの保存先に接続できません")

        response = create_app({"TESTING": True}, BrokenUseCases()).test_client().get(
            "/api/calendar?year=2026&month=6")
        self.assertEqual(response.status_code, 503)
        self.assertIn("保存先", response.get_json()["error"])

    def test_worker_cookie_jar_respects_domain_and_path(self):
        class Headers:
            def getSetCookie(self):
                return ["_Rakushifu_session=secret; Domain=.enterprise.rakushifu.com; Path=/; HttpOnly",
                        "subpath=visible; Path=/ajax"]

        jar = WorkerCookieJar()
        jar.read(Headers(), "https://skylark.enterprise.rakushifu.com/ajax/organizations")
        self.assertIn("_Rakushifu_session=secret", jar.header(
            "https://skylark.enterprise.rakushifu.com/staff/v2/schedules"))
        self.assertNotIn("subpath=visible", jar.header(
            "https://skylark.enterprise.rakushifu.com/staff/v2/schedules"))
        self.assertEqual(jar.header("https://api.accounts.rakushifu.com/"), "")

    def test_unknown_mode_and_local_production_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "APP_ENV"):
            AppSettings.load("testing")
        with self.assertRaisesRegex(RuntimeError, "Python Workers runtime"):
            create_app({"APP_ENV": "production"})

    def test_month_json_round_trip_keeps_breaks(self):
        payload = {
            "users": [{"id": 1, "name": "Viewer", "employee_code": "123",
                       "belonging_store_id": 10}],
            "shared": [{"date": "2026-06-08", "user_id": 1,
                        "attending_store_id": 10,
                        "start_time": {"hour": 23, "min": 0},
                        "end_time": {"hour": 25, "min": 0},
                        "rest_times": [{"start_hour": 23, "start_minute": 30,
                                        "end_hour": 24, "end_minute": 0}]}],
        }
        month = map_schedule(payload, 2026, 6)
        restored = month_from_json(month_to_json(month))
        self.assertEqual(restored.shifts[0].rest_minutes, 30)
        self.assertEqual(restored.staff[1].employee_code, "123")

    def test_durable_object_persists_session_and_uses_memory_cache(self):
        class FakeStorage:
            def __init__(self):
                self.data = {}
                self.alarm_at = None

            async def get(self, key):
                return self.data.get(key)

            async def put(self, key, value):
                self.data[key] = value

            async def delete(self, key):
                self.data.pop(key, None)

            async def setAlarm(self, when):
                self.alarm_at = when

            async def deleteAlarm(self):
                self.alarm_at = None

            async def deleteAll(self):
                self.data.clear()

        class FakeDurableObject:
            def __init__(self, ctx, env):
                self.ctx = ctx

        fake_workers = types.SimpleNamespace(
            DurableObject=FakeDurableObject,
            wsgi=types.SimpleNamespace(entrypoint=lambda app: app),
        )
        with patch.dict(sys.modules, {"workers": fake_workers}):
            from importlib import import_module
            worker = import_module("worker")

        storage = FakeStorage()
        ctx = types.SimpleNamespace(storage=storage)
        obj = worker.SessionObject(ctx, None)
        state = json.dumps({
            "viewer": {"account_id": "123", "staff_id": 1,
                       "store_id": 10, "genre_id": 2},
            "cookies": [], "csrf_token": "", "expires_at": time.time() + 3600,
        })

        async def run():
            await obj.create(state, "month-data", "[2026,6]", 120, 3)
            self.assertEqual(json.loads(await obj.read())["viewer"]["account_id"], "123")
            self.assertIsNotNone(storage.alarm_at)
            self.assertEqual(json.loads(await obj.month(2026, 6))["month"],
                             "month-data")
            recreated = worker.SessionObject(ctx, None)
            self.assertEqual(json.loads(await recreated.read())["cache_seconds"], 120)
            self.assertEqual(recreated.cache, {})
            await recreated.remove()
            self.assertIsNone(await obj.read())
            self.assertIsNone(storage.alarm_at)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
