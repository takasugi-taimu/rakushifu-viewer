import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from requests.cookies import RequestsCookieJar

from app import create_app
from app.application.errors import (
    CredentialsUnavailable, InvalidCredentials, UpstreamError,
)
from app.application.use_cases import JAPAN_TIME, ShiftUseCases
from app.application.session import ActiveSession
from app.domain.models import Shift, TimeOfDay, Viewer
from app.domain.services import estimate_pay, overlaps
from app.infrastructure.memory_sessions import MemorySessionStore
from app.infrastructure.rakushifu_client import RakushifuAuthenticator
from app.infrastructure.rakushifu_mapper import map_schedule


def sample_payload(code, name, year, month, store_id=10):
    day = f"{year}-{month:02d}-10"
    return {
        "users": [
            {"id": 1, "name": name, "employee_code": code,
             "belonging_store_id": store_id},
            {"id": 2, "name": "Colleague", "employee_code": "other",
             "belonging_store_id": store_id},
            {"id": 3, "name": "External", "employee_code": "external",
             "belonging_store_id": 999},
        ],
        "shared": [
            {"date": day, "user_id": 1, "attending_store_id": store_id,
             "start_time": {"hour": 23, "min": 0},
             "end_time": {"hour": 25, "min": 0}},
            {"date": day, "user_id": 2, "attending_store_id": store_id,
             "start_time": {"hour": 24, "min": 0},
             "end_time": {"hour": 26, "min": 0}},
        ],
    }


class FakeConnection:
    def __init__(self, code, name, store_id):
        self.code = code
        self.name = name
        self.viewer = Viewer(code, 1, store_id, 2 if store_id == 10 else 3)
        self.calls = []
        self.closed = False

    def fetch(self, year, month, store_id, genre_id):
        self.calls.append((year, month, store_id, genre_id))
        return map_schedule(sample_payload(self.code, self.name, year, month,
                                           store_id),
                            year, month)

    def close(self):
        self.closed = True


class FakeAuthenticator:
    def __init__(self):
        self.connections = {}

    def login(self, employee_code, password):
        if password != "test-password":
            raise InvalidCredentials("認証に失敗しました")
        store_id = 10 if employee_code == "0000000001" else 20
        connection = FakeConnection(employee_code, f"Staff {employee_code}", store_id)
        self.connections[employee_code] = connection
        return connection


class ShiftArchitectureTests(unittest.TestCase):
    def test_staff_list_orders_employee_codes_before_limiting_results(self):
        payload = sample_payload('0000000001', 'Viewer', 2026, 9)
        payload['users'] = [payload['users'][0]] + [
            {'id': number + 10, 'name': f'Fixture {200 - number:03d}',
             'employee_code': str(number), 'belonging_store_id': 10}
            for number in range(105, 1, -1)
        ] + [
            {'id': 200, 'name': 'Fixture padded', 'employee_code': '0002',
             'belonging_store_id': 10},
            {'id': 201, 'name': 'Special text', 'employee_code': 'A1',
             'belonging_store_id': 10},
            {'id': 202, 'name': 'Special missing', 'belonging_store_id': 10},
        ]
        month = map_schedule(payload, 2026, 9)
        with patch.object(FakeConnection, 'fetch', return_value=month):
            cases = ShiftUseCases(FakeAuthenticator(), MemorySessionStore())
            token = cases.login('0000000001', 'test-password')
            results = cases.search_staff('', 2026, 9, token)
            self.assertEqual(len(results), 100)
            self.assertEqual([int(item['employee_code']) for item in results],
                             [2, 2] + list(range(3, 101)))
            filtered = cases.search_staff('Fixture ', 2026, 9, token)
            self.assertEqual(filtered, results)
            special = cases.search_staff('Special', 2026, 9, token)
            self.assertEqual([item['user_id'] for item in special], [201, 202])

    def test_pay_estimate_handles_overnight_night_hours_and_break(self):
        shifts = [
            Shift(date(2026, 9, 10), 1, 10, TimeOfDay(21, 0), TimeOfDay(25, 0),
                  ((TimeOfDay(21, 15), TimeOfDay(21, 45)),
                   (TimeOfDay(22, 30), TimeOfDay(23, 0)))),
            Shift(date(2026, 9, 11), 1, 10, TimeOfDay(9, 0), TimeOfDay(12, 0)),
        ]
        result = estimate_pay(shifts, Decimal('1200'), Decimal('25'))
        self.assertEqual(result['scheduled_minutes'], 420)
        self.assertEqual(result['break_minutes'], 60)
        self.assertEqual(result['worked_minutes'], 360)
        self.assertEqual(result['night_minutes'], 150)
        self.assertEqual(result['estimated_yen'], 7950)

    def test_pay_estimate_merges_overlapping_breaks_across_midnight(self):
        shifts = [Shift(date(2026, 9, 10), 1, 10,
                        TimeOfDay(23, 0), TimeOfDay(25, 0),
                        ((TimeOfDay(23, 30), TimeOfDay(24, 30)),
                         (TimeOfDay(0, 0), TimeOfDay(0, 45))))]
        result = estimate_pay(shifts, Decimal('1000'), Decimal('25'))
        self.assertEqual(result['break_minutes'], 75)
        self.assertEqual(result['worked_minutes'], 45)
        self.assertEqual(result['night_minutes'], 45)
        self.assertEqual(result['estimated_yen'], 938)

    def test_mapper_reads_june_8_rest_times(self):
        payload = sample_payload('code', 'Viewer', 2026, 6)
        payload['shared'][0]['date'] = '2026-06-08'
        payload['shared'][0]['rest_times'] = [
            {'start_hour': 23, 'start_minute': 30,
             'end_hour': 24, 'end_minute': 0},
        ]
        shift = map_schedule(payload, 2026, 6).shifts[0]
        self.assertEqual(shift.rest_minutes, 30)
        self.assertEqual(shift.work_minutes, 90)

    def test_staff_search_and_pay_api_use_authenticated_store(self):
        cases = ShiftUseCases(FakeAuthenticator(), MemorySessionStore())
        client = create_app({'TESTING': True}, cases).test_client()
        today = datetime.now(JAPAN_TIME).date()
        self.assertEqual(client.get(
            f'/api/staff?year={today.year}&month={today.month}').status_code, 401)
        client.post('/login', json={
            'employee_code': '0000000001', 'password': 'test-password'})
        result = client.get(
            f'/api/staff?year={today.year}&month={today.month}&q=other')
        self.assertEqual(result.status_code, 200)
        self.assertEqual([person['user_id'] for person in result.get_json()], [2])
        self.assertEqual(client.get(
            f'/api/staff?year={today.year}&month={today.month}&q=Staff').get_json(), [])
        self.assertEqual(client.get(
            f'/api/staff?year={today.year}&month={today.month}&q=External').get_json(), [])
        self.assertEqual(client.get(
            f'/api/staff/3?year={today.year}&month={today.month}').status_code, 404)
        self.assertEqual(client.get(
            f'/api/staff/2?year={today.year}&month={today.month}').status_code, 200)
        pay = client.post('/api/pay/estimate', json={
            'year': today.year, 'month': today.month, 'hourly_wage': 1000,
            'night_bonus_percent': 25,
        })
        self.assertEqual(pay.status_code, 200)
        self.assertEqual(pay.get_json()['worked_minutes'], 120)
        self.assertEqual(pay.get_json()['night_minutes'], 120)
        self.assertEqual(pay.get_json()['estimated_yen'], 2500)
        self.assertEqual(client.post('/api/pay/estimate', json={
            'year': today.year, 'month': today.month, 'hourly_wage': -1,
            'night_bonus_percent': 25,
        }).status_code, 400)

    def test_overnight_domain_rule(self):
        first = Shift(date(2026, 9, 10), 1, 10,
                      TimeOfDay(23, 0), TimeOfDay(25, 0))
        second = Shift(date(2026, 9, 10), 2, 10,
                       TimeOfDay(24, 0), TimeOfDay(26, 0))
        self.assertEqual(first.duration_minutes, 120)
        self.assertTrue(overlaps(first, second))

    def test_login_cookie_and_per_user_memory_cache(self):
        auth = FakeAuthenticator()
        cases = ShiftUseCases(auth, MemorySessionStore())
        app = create_app({"TESTING": True, "APP_COOKIE_SECURE": False}, cases)
        first = app.test_client()
        second = app.test_client()
        today = datetime.now(JAPAN_TIME).date()
        calendar_url = f"/api/calendar?year={today.year}&month={today.month}"
        day_url = f"/api/shifts?date={today.year}-{today.month:02d}-10"

        self.assertEqual(first.get(calendar_url).status_code, 401)
        login_a = first.post("/login", json={
            "employee_code": "0000000001", "password": "test-password"})
        login_b = second.post("/login", json={
            "employee_code": "0000000002", "password": "test-password"})
        self.assertEqual(login_a.status_code, 200)
        self.assertEqual(login_b.status_code, 200)
        index_response = first.get("/")
        self.assertEqual(index_response.status_code, 200)
        self.assertIn(b'method="post" action="/logout"', index_response.data)
        cookie = login_a.headers["Set-Cookie"]
        self.assertIn("app_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("test-password", cookie)
        self.assertNotIn("_Rakushifu_session", cookie)

        day_a = first.get(day_url).get_json()
        day_b = second.get(day_url).get_json()
        self.assertEqual(day_a["workers"][0]["name"], "Staff 0000000001")
        self.assertEqual(day_b["workers"][0]["name"], "Staff 0000000002")
        self.assertEqual(auth.connections["0000000001"].calls[0][2], 10)
        self.assertEqual(auth.connections["0000000002"].calls[0][2], 20)
        self.assertEqual(auth.connections["0000000002"].calls[0][3], 3)
        self.assertTrue(day_a["workers"][1]["is_overlapping"])
        self.assertEqual(len(auth.connections["0000000001"].calls), 1)
        self.assertEqual(first.get(calendar_url).headers["Cache-Control"], "no-store")
        self.assertEqual(len(auth.connections["0000000001"].calls), 1)
        self.assertEqual(first.get("/logout").status_code, 405)
        self.assertEqual(first.get(calendar_url).status_code, 200)
        first.post("/logout")
        self.assertTrue(auth.connections["0000000001"].closed)
        self.assertEqual(first.get(calendar_url).status_code, 401)
        self.assertEqual(second.get(calendar_url).status_code, 200)

    def test_invalid_credentials_do_not_create_an_app_session(self):
        cases = ShiftUseCases(FakeAuthenticator(), MemorySessionStore())
        client = create_app({"TESTING": True}, cases).test_client()
        response = client.post("/login", json={
            "employee_code": "0000000001", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("app_session=", response.headers.get("Set-Cookie", ""))

    def test_login_attempts_are_limited(self):
        cases = ShiftUseCases(FakeAuthenticator(), MemorySessionStore())
        client = create_app({"TESTING": True}, cases).test_client()
        for _ in range(5):
            self.assertEqual(client.post("/login", json={
                "employee_code": "0000000001", "password": "wrong"}).status_code, 401)
        self.assertEqual(client.post("/login", json={
            "employee_code": "0000000001", "password": "wrong"}).status_code, 429)

    def test_expired_session_closes_upstream_connection(self):
        connection = FakeConnection("0000000001", "Viewer", 10)
        sessions = MemorySessionStore(lifetime_seconds=0)
        token = sessions.create(ActiveSession(connection.viewer, connection))
        self.assertIsNone(sessions.get(token))
        self.assertTrue(connection.closed)

    def test_month_cache_has_a_per_user_limit(self):
        sessions = MemorySessionStore()
        cases = ShiftUseCases(FakeAuthenticator(), sessions, max_cached_months=2)
        token = cases.login("0000000001", "test-password", date(2026, 9, 10))
        cases.calendar(2026, 10, token)
        cases.calendar(2026, 11, token)
        self.assertEqual(set(sessions.get(token).cache), {(2026, 10), (2026, 11)})

    def test_expired_month_cache_is_fetched_again(self):
        auth = FakeAuthenticator()
        cases = ShiftUseCases(auth, MemorySessionStore(), cache_seconds=0)
        token = cases.login("0000000001", "test-password", date(2026, 9, 10))
        cases.calendar(2026, 9, token)
        self.assertEqual(len(auth.connections["0000000001"].calls), 2)

    def test_login_prefetch_uses_japan_date_at_utc_month_boundary(self):
        auth = FakeAuthenticator()
        cases = ShiftUseCases(auth, MemorySessionStore())
        utc_instant = datetime(2026, 9, 30, 15, 30, tzinfo=timezone.utc)
        with patch("app.application.use_cases.datetime") as clock:
            clock.now.side_effect = lambda zone: utc_instant.astimezone(zone)
            cases.login("0000000001", "test-password")
        self.assertEqual(auth.connections["0000000001"].calls[0][:2], (2026, 10))

    def test_upstream_session_expiry_clears_app_cookie(self):
        class ExpiringConnection(FakeConnection):
            def fetch(self, year, month, store_id, genre_id):
                if self.calls:
                    raise CredentialsUnavailable("expired")
                return super().fetch(year, month, store_id, genre_id)

        class ExpiringAuthenticator:
            def login(self, employee_code, password):
                self.connection = ExpiringConnection(employee_code, "Viewer", 10)
                return self.connection

        auth = ExpiringAuthenticator()
        cases = ShiftUseCases(auth, MemorySessionStore())
        client = create_app({"TESTING": True}, cases).test_client()
        self.assertEqual(client.post("/login", json={
            "employee_code": "0000000001", "password": "test-password"}).status_code, 200)
        today = datetime.now(JAPAN_TIME).date()
        response = client.get(
            f"/api/calendar?year={today.year + 1}&month={today.month}")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Expires=Thu, 01 Jan 1970", response.headers["Set-Cookie"])
        self.assertTrue(auth.connection.closed)

    def test_upstream_login_handoff_and_schedule_fetch(self):
        class FakeResponse:
            def __init__(self, status, url, payload=None, text=""):
                self.status_code = status
                self.url = url
                self.payload = payload
                self.text = text

            def json(self):
                return self.payload

        class FakeHttpSession:
            def __init__(self):
                self.cookies = RequestsCookieJar()
                self.headers = {}
                self.calls = []
                self.closed = False

            def post(self, url, **kwargs):
                self.calls.append(("post", url, kwargs))
                return FakeResponse(200, url)

            def get(self, url, **kwargs):
                self.calls.append(("get", url, kwargs))
                if url.endswith("/authenticated_users"):
                    self.cookies.set("_Rakushifu_session", "upstream-only")
                    return FakeResponse(200, url,
                                        text='<div data-csrf-token="dummy-csrf"></div>')
                if url.endswith("/ajax/organizations"):
                    return FakeResponse(200, url, {
                        "current_user": {"id": 1, "current_belong_store_id": 20,
                                         "current_belong_genre_id": 3}
                    })
                return FakeResponse(200, url, sample_payload(
                    "0000000001", "Viewer", 2026, 9, 20))

            def close(self):
                self.closed = True

        http = FakeHttpSession()
        connection = RakushifuAuthenticator(lambda: http).login(
            "0000000001", "test-password")
        self.assertEqual(connection.viewer.store_id, 20)
        self.assertIn("Mozilla/5.0", http.headers["User-Agent"])
        month = connection.fetch(2026, 9, connection.viewer.store_id,
                                 connection.viewer.genre_id)
        self.assertEqual(month.staff[1].employee_code, "0000000001")
        self.assertEqual(http.calls[0][2]["json"]["employee_code"], "0000000001")
        self.assertEqual(http.calls[-1][2]["params"]["store_id"], "20")
        self.assertEqual(http.calls[-1][2]["params"]["genre_ids[]"], "3")
        self.assertEqual(http.calls[-1][2]["headers"]["x-csrf-token"], "dummy-csrf")
        connection.close()
        self.assertTrue(http.closed)

    def test_authentication_distinguishes_invalid_credentials_from_rejected_request(self):
        class FakeResponse:
            def __init__(self, status, payload=None):
                self.status_code = status
                self.payload = payload

            def json(self):
                if self.payload is None:
                    raise ValueError("HTML response")
                return self.payload

        class FakeHttpSession:
            def __init__(self, response):
                self.response = response
                self.headers = {}
                self.closed = False

            def post(self, url, **kwargs):
                return self.response

            def close(self):
                self.closed = True

        blocked = FakeHttpSession(FakeResponse(403))
        with self.assertRaises(UpstreamError):
            RakushifuAuthenticator(lambda: blocked).login("0000000001", "password")
        self.assertTrue(blocked.closed)
        blocked_app = create_app(
            {"TESTING": True},
            ShiftUseCases(
                RakushifuAuthenticator(lambda: FakeHttpSession(FakeResponse(403))),
                MemorySessionStore(),
            ),
        )
        blocked_result = blocked_app.test_client().post("/login", json={
            "employee_code": "0000000001", "password": "password",
        })
        self.assertEqual(blocked_result.status_code, 502)

        denied = FakeHttpSession(FakeResponse(401, {
            "type": "employee_code_account_unauthenticated",
        }))
        with self.assertRaises(InvalidCredentials):
            RakushifuAuthenticator(lambda: denied).login("0000000001", "password")
        self.assertTrue(denied.closed)


if __name__ == "__main__":
    unittest.main()
