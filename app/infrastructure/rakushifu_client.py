import calendar
import re
import requests

from app.application.errors import (
    CredentialsUnavailable, InvalidCredentials, UpstreamError,
)
from app.domain.models import Viewer
from .rakushifu_mapper import map_schedule


class RakushifuConnection:
    SCHEDULES_URL = "https://skylark.enterprise.rakushifu.com/ajax/admin/v2/schedules"

    def __init__(self, http_session: requests.Session, viewer: Viewer,
                 csrf_token: str = ""):
        self.http_session = http_session
        self.viewer = viewer
        self.csrf_token = csrf_token

    def fetch(self, year: int, month: int, store_id: int, genre_id: int):
        last_day = calendar.monthrange(year, month)[1]
        params = {
            "page_ctx_name": "staff",
            "store_id": str(store_id),
            "genre_ids[]": str(genre_id),
            "start_date": f"{year}-{month:02d}-01",
            "end_date": f"{year}-{month:02d}-{last_day:02d}",
            "is_staff_print_page": "false",
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": "https://skylark.enterprise.rakushifu.com/staff/v2/schedules/confirmed",
        }
        if self.csrf_token:
            headers["x-csrf-token"] = self.csrf_token
        try:
            response = self.http_session.get(
                self.SCHEDULES_URL, params=params, headers=headers, timeout=20,
            )
        except requests.RequestException as error:
            raise UpstreamError("らくしふからシフトを取得できません") from error
        if response.status_code in (401, 403) or "/sign_in" in response.url or "/sign-in" in response.url:
            raise CredentialsUnavailable("らくしふのログインが期限切れです")
        if response.status_code != 200:
            raise UpstreamError("らくしふからシフトを取得できません")
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamError("らくしふの応答形式が不正です") from error
        if not isinstance(payload, dict):
            raise UpstreamError("らくしふの応答形式が不正です")
        return map_schedule(payload, year, month)

    def close(self) -> None:
        self.http_session.cookies.clear()
        self.http_session.close()


class RakushifuAuthenticator:
    ACCOUNT_LOGIN_URL = "https://api.accounts.rakushifu.com/sign_in_with_employee_code/browser"
    ENTERPRISE_LOGIN_URL = (
        "https://skylark.enterprise.rakushifu.com/authenticated_users"
    )
    PROFILE_URL = "https://skylark.enterprise.rakushifu.com/ajax/organizations"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, session_factory=requests.Session):
        self.session_factory = session_factory

    def login(self, employee_code: str, password: str) -> RakushifuConnection:
        http = self.session_factory()
        try:
            # The account API rejects requests with requests' default User-Agent
            # before checking credentials, returning an HTML 403 response.
            http.headers.update({"User-Agent": self.USER_AGENT})
            response = http.post(
                self.ACCOUNT_LOGIN_URL,
                json={
                    "enterprise_code": "skylark",
                    "employee_code": employee_code,
                    "password": password,
                },
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "origin": "https://accounts.rakushifu.com",
                    "referer": "https://accounts.rakushifu.com/skylark/staff/sign-in",
                },
                timeout=20,
                allow_redirects=False,
            )
            try:
                error_type = response.json().get("type") if response.status_code >= 400 else None
            except (ValueError, AttributeError):
                error_type = None
            if error_type == "employee_code_account_unauthenticated":
                raise InvalidCredentials("従業員IDまたはパスワードが正しくありません")
            if not 200 <= response.status_code < 300:
                raise UpstreamError("らくしふへログインできません")

            # The account service issues its own cookies. This handoff issues the
            # enterprise session needed by the shift API.
            handoff = http.get(
                self.ENTERPRISE_LOGIN_URL,
                params={"role": "staff", "enterprise_code": "skylark"},
                timeout=20,
            )
            if (handoff.status_code != 200
                    or not handoff.url.startswith("https://skylark.enterprise.rakushifu.com/")
                    or not any(
                        cookie.name == "_Rakushifu_session" for cookie in http.cookies)):
                raise UpstreamError("らくしふのセッションを確立できません")
            profile_response = http.get(
                self.PROFILE_URL, headers={"accept": "application/json"},
                timeout=20,
            )
            if profile_response.status_code != 200:
                raise UpstreamError("らくしふの利用者情報を取得できません")
            try:
                current_user = profile_response.json()["current_user"]
                viewer = Viewer(
                    account_id=employee_code,
                    staff_id=int(current_user["id"]),
                    store_id=int(current_user["current_belong_store_id"]),
                    genre_id=int(current_user["current_belong_genre_id"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise UpstreamError("らくしふの利用者情報の形式が不正です") from error
            match = re.search(
                r'data-csrf-token=["\']([^"\']+)',
                handoff.text,
            )
            return RakushifuConnection(http, viewer,
                                       match.group(1) if match else "")
        except requests.RequestException as error:
            http.close()
            raise UpstreamError("らくしふへ接続できません") from error
        except (InvalidCredentials, UpstreamError):
            http.close()
            raise
