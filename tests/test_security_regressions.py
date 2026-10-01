import asyncio
import importlib.util
import io
import json
from http.client import HTTPMessage
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from werkzeug.test import EnvironBuilder

from app import create_app
from app.domain.models import ShiftMonth
from app.settings import MAX_REQUEST_BODY_BYTES


class FakeStorage:
    def __init__(self):
        self.data = {}
        self.alarm_at = None

    async def get(self, key):
        return self.data.get(key)

    async def put(self, key, value):
        self.data[key] = value

    async def setAlarm(self, when):
        self.alarm_at = when

    async def deleteAlarm(self):
        self.alarm_at = None

    async def deleteAll(self):
        self.data.clear()


class FakeDurableObject:
    def __init__(self, ctx, env):
        self.ctx = ctx


class FakeEntrypoint:
    async def fetch(self, request):
        return request


class FakeRequest:
    def __init__(self, url, method=None, headers=None, body=None):
        if not isinstance(url, str):
            url, method, headers, body = url.url, url.method, url.headers, url.body
        normalized = {}
        for name, value in headers.items():
            key = name.lower()
            normalized[key] = (normalized[key] + ", " + value
                               if key in normalized else value)
        self.headers = HTTPMessage()
        for name, value in normalized.items():
            self.headers[name] = value
        self.url, self.method, self.body = url, method, body


class FakeResponse:
    def __init__(self, body, status, headers):
        self.body, self.status, self.headers = body, status, headers
        self.js_object = self


FAKE_WORKERS = types.SimpleNamespace(
    DurableObject=FakeDurableObject, Request=FakeRequest, Response=FakeResponse,
    wsgi=types.SimpleNamespace(entrypoint=lambda app: FakeEntrypoint),
)


def load_worker():
    spec = importlib.util.spec_from_file_location(
        "security_test_worker", Path(__file__).resolve().parents[1] / "worker.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": FAKE_WORKERS}):
        spec.loader.exec_module(module)
    return module


class SecurityRegressionTests(unittest.TestCase):
    def test_inflight_fetch_cannot_restore_a_revoked_or_expired_session(self):
        worker = load_worker()
        for action in ("logout", "alarm", "expiry", "replacement", "active"):
            with self.subTest(action=action):
                async def run():
                    storage = FakeStorage()
                    obj = worker.SessionObject(types.SimpleNamespace(storage=storage), None)
                    state = {
                        "viewer": {"account_id": "DEMO", "staff_id": 1,
                                   "store_id": 10, "genre_id": 2},
                        "cookies": [], "csrf_token": "", "expires_at": 4600,
                    }
                    await obj.create(json.dumps(state), "", "[]", 120, 3)
                    started, release = asyncio.Event(), asyncio.Event()
                    connection = Mock()
                    connection.viewer = types.SimpleNamespace(store_id=10, genre_id=2)
                    cookies = [{"name": "demo", "value": "dummy",
                                "domain": "rakushifu.com", "path": "/"}]
                    connection.export_state.return_value = {
                        "csrf_token": "refreshed", "cookies": cookies}
                    connection.close.side_effect = cookies.clear

                    async def fetch(*args):
                        started.set()
                        await release.wait()
                        return ShiftMonth(2026, 10, {}, ())

                    connection.fetch_async = fetch
                    with patch.object(worker.WorkerRakushifuConnection, "from_state",
                                      return_value=connection):
                        task = asyncio.create_task(obj.month(2026, 10))
                        await started.wait()
                        if action in ("logout", "replacement"):
                            await obj.remove()
                            self.assertIsNone(await obj.read())
                            if action == "replacement":
                                await obj.create(json.dumps(state), "", "[]", 120, 3)
                        elif action in ("alarm", "expiry"):
                            clock.return_value = 4601
                            if action == "alarm":
                                await obj.alarm()
                        release.set()
                        result = json.loads(await task)

                    connection.close.assert_called_once()
                    if action == "active":
                        self.assertEqual(result["error"], "ok")
                        self.assertEqual(json.loads(await obj.read())["csrf_token"], "refreshed")
                        self.assertEqual(json.loads(await obj.read())["cookies"][0]["value"], "dummy")
                        self.assertEqual(storage.alarm_at, 4600000)
                    else:
                        self.assertEqual(result, {"error": "expired"})
                        self.assertEqual(obj.cache, {})
                        if action == "replacement":
                            self.assertEqual(json.loads(await obj.read())["csrf_token"], "")
                            self.assertEqual(storage.alarm_at, 4600000)
                        else:
                            self.assertEqual(storage.data, {})
                            self.assertIsNone(storage.alarm_at)

                with patch.object(worker.time, "time", return_value=1000) as clock:
                    asyncio.run(run())

    def test_flask_rejects_oversized_login_without_reading_or_authenticating(self):
        for environment in ("test", "production"):
            with self.subTest(environment=environment):
                use_cases = Mock()
                app = create_app({"APP_ENV": environment, "TESTING": True},
                                 use_cases, worker_runtime=True)
                body = io.BytesIO(b"x" * (MAX_REQUEST_BODY_BYTES + 1))
                environ = EnvironBuilder(path="/login", method="POST",
                                         content_type="application/json").get_environ()
                environ["CONTENT_LENGTH"] = str(MAX_REQUEST_BODY_BYTES + 1)
                environ["wsgi.input"] = body
                with app.request_context(environ):
                    response = app.full_dispatch_request()
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(body.tell(), 0)
                use_cases.login.assert_not_called()

    def test_flask_accepts_normal_login_and_bounds_unknown_length_streams(self):
        use_cases = Mock()
        use_cases.login.return_value = "a" * 43
        app = create_app({"TESTING": True}, use_cases)
        response = app.test_client().post("/login", json={
            "employee_code": "DEMO", "password": "test-password"})
        self.assertEqual(response.status_code, 200)
        use_cases.login.assert_called_once_with("DEMO", "test-password")
        use_cases.reset_mock()
        environ = EnvironBuilder(path="/login", method="POST",
                                 content_type="application/json").get_environ()
        environ.pop("CONTENT_LENGTH", None)
        environ["wsgi.input_terminated"] = True
        environ["wsgi.input"] = io.BytesIO(b"x" * (MAX_REQUEST_BODY_BYTES + 1))
        response = app.test_client().open(environ)
        self.assertEqual(response.status_code, 413)
        use_cases.login.assert_not_called()

    def test_worker_bounds_declared_and_actual_body_before_wsgi(self):
        worker = load_worker()

        class Stream:
            def __init__(self, chunks):
                self.chunks = iter(chunks)
                self.cancelled = self.released = False
                self.read_count = 0

            def getReader(self):
                return self

            async def read(self):
                self.read_count += 1
                data = next(self.chunks, None)
                return types.SimpleNamespace(done=data is None, value=(
                    types.SimpleNamespace(byteLength=len(data), to_bytes=lambda: data)
                    if data is not None else None))

            async def cancel(self):
                self.cancelled = True

            def releaseLock(self):
                self.released = True

        cases = (
            ({"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}, [], 413),
            ({}, [b"x" * MAX_REQUEST_BODY_BYTES, b"x"], 413),
            ({"Content-Length": "1"}, [b"x" * (MAX_REQUEST_BODY_BYTES + 1)], 413),
            ({}, [b"x" * MAX_REQUEST_BODY_BYTES], 200),
            ({}, [b'{"employee_code":"DEMO","password":"test-password"}'], 200),
            ({"content-length": "2", "content-type": "application/json"}, [b'{}'], 200),
            ({"transfer-encoding": "chunked", "content-type": "application/json"}, [b'{}'], 200),
        )
        for headers, chunks, status in cases:
            with self.subTest(headers=headers, size=sum(map(len, chunks))):
                stream = Stream(chunks)
                request = types.SimpleNamespace(url="https://example.test/login",
                                                method="POST", headers=headers, body=stream)
                with patch.dict(sys.modules, {"workers": FAKE_WORKERS}):
                    result = asyncio.run(worker.Default().fetch(request))
                if status == 413:
                    self.assertEqual(result.status, 413)
                    self.assertTrue(stream.cancelled)
                    if int(headers.get("Content-Length", "0")) > MAX_REQUEST_BODY_BYTES:
                        self.assertEqual(stream.read_count, 0)
                else:
                    self.assertEqual(result.body, b"".join(chunks))
                    self.assertEqual(int(result.headers["Content-Length"]), len(result.body))
                    self.assertNotIn("transfer-encoding", result.headers)
                if stream.read_count:
                    self.assertTrue(stream.released)

    def test_worker_preserves_json_body_for_authenticated_pay_estimate(self):
        worker = load_worker()
        payload = {"year": 2026, "month": 10, "hourly_wage": 1200,
                   "night_bonus_percent": 25}
        body = json.dumps(payload).encode()
        chunk = types.SimpleNamespace(byteLength=len(body), to_bytes=lambda: body)

        class Stream:
            def __init__(self):
                self.chunks = iter([chunk, None])

            def getReader(self):
                return self

            async def read(self):
                value = next(self.chunks)
                return types.SimpleNamespace(done=value is None, value=value)

            def releaseLock(self):
                pass

        request = types.SimpleNamespace(
            url="https://example.test/api/pay/estimate", method="POST", body=Stream(),
            headers={"content-type": "application/json", "content-length": str(len(body))},
        )
        with patch.dict(sys.modules, {"workers": FAKE_WORKERS}):
            bounded = asyncio.run(worker.Default().fetch(request))
        use_cases = Mock()
        use_cases.authenticated.return_value = True
        use_cases.my_pay.return_value = {"estimated_yen": 1200}
        app = create_app({"TESTING": True}, use_cases)
        environ = EnvironBuilder(path="/api/pay/estimate", method="POST",
                                 data=bounded.body, content_type="application/json").get_environ()
        environ["CONTENT_LENGTH"] = bounded.headers["content-length"]
        with app.request_context(environ):
            response = app.full_dispatch_request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"estimated_yen": 1200})
        use_cases.my_pay.assert_called_once()
