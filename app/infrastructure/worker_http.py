"""Rakushifu HTTP client for the Python Workers runtime.

The local Flask process uses requests. Workers use fetch, and carry only
serializable cookies between requests and Durable Object activations.
"""

import calendar
import json
import re
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urljoin, urlparse

from app.application.errors import (
    CredentialsUnavailable, InvalidCredentials, UpstreamError,
)
from app.domain.models import Viewer

from .rakushifu_mapper import map_schedule
from .worker_state import viewer_from_dict, viewer_to_dict


ALLOWED_HOSTS = {"api.accounts.rakushifu.com", "accounts.rakushifu.com",
                 "skylark.enterprise.rakushifu.com"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class HttpReply:
    status_code: int
    url: str
    text: str

    def json(self):
        return json.loads(self.text)


class WorkerCookieJar:
    def __init__(self, values=None):
        self.values = list(values or [])

    def read(self, headers, url):
        try:
            raw_cookies = list(headers.getSetCookie())
        except (AttributeError, TypeError):
            single = headers.get("set-cookie")
            raw_cookies = [single] if single else []
        host = urlparse(url).hostname
        for raw in raw_cookies:
            cookie = SimpleCookie()
            cookie.load(str(raw))
            for item in cookie.values():
                domain = (item["domain"] or host).lstrip(".").lower()
                if (domain != "rakushifu.com"
                        and not domain.endswith(".rakushifu.com")):
                    continue
                if host != domain and not host.endswith("." + domain):
                    continue
                request_path = urlparse(url).path or "/"
                default_path = request_path.rsplit("/", 1)[0] or "/"
                path = item["path"] or default_path
                self.values = [old for old in self.values if not (
                    old["name"] == item.key and old["domain"] == domain
                    and old["path"] == path)]
                max_age = item["max-age"]
                if item.value and (not max_age or int(max_age) > 0):
                    self.values.append({"name": item.key, "value": item.value,
                                        "domain": domain, "path": path})

    def header(self, url):
        parsed = urlparse(url)
        host = parsed.hostname
        path = parsed.path or "/"
        return "; ".join(
            f'{item["name"]}={item["value"]}' for item in self.values
            if (host == item["domain"] or host.endswith("." + item["domain"]))
            and (path == item["path"] or path.startswith(item["path"].rstrip("/") + "/"))
        )

    def has(self, name, domain):
        return any(item["name"] == name and
                   (item["domain"] == domain or domain.endswith("." + item["domain"]))
                   for item in self.values)

    def clear(self):
        self.values.clear()


async def worker_fetch(url, *, method="GET", headers=None, body=None,
                       cookies=None, redirects=True):
    from js import Object, fetch
    from pyodide.ffi import to_js

    for _ in range(9):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise UpstreamError("らくしふの転送先が不正です")
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        cookie_header = cookies.header(url) if cookies else ""
        if cookie_header:
            request_headers["Cookie"] = cookie_header
        options = {"method": method, "headers": request_headers,
                   "redirect": "manual"}
        if body is not None:
            options["body"] = body
        try:
            response = await fetch(url, to_js(options, dict_converter=Object.fromEntries))
            if cookies:
                cookies.read(response.headers, url)
            if redirects and response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise UpstreamError("らくしふの転送先が不正です")
                url = urljoin(url, str(location))
                if response.status == 303:
                    method, body = "GET", None
                continue
            return HttpReply(int(response.status), url, await response.text())
        except UpstreamError:
            raise
        except Exception as error:
            raise UpstreamError("らくしふへ接続できません") from error
    raise UpstreamError("らくしふの転送回数が多すぎます")


class WorkerRakushifuConnection:
    SCHEDULES_URL = "https://skylark.enterprise.rakushifu.com/ajax/admin/v2/schedules"

    def __init__(self, viewer, cookies, csrf_token=""):
        self.viewer = viewer
        self.cookies = cookies
        self.csrf_token = csrf_token

    def export_state(self):
        return {"viewer": viewer_to_dict(self.viewer),
                "cookies": self.cookies.values, "csrf_token": self.csrf_token}

    @classmethod
    def from_state(cls, state):
        return cls(viewer_from_dict(state["viewer"]),
                   WorkerCookieJar(state["cookies"]), state["csrf_token"])

    async def fetch_async(self, year, month, store_id, genre_id):
        last_day = calendar.monthrange(year, month)[1]
        params = urlencode({
            "page_ctx_name": "staff", "store_id": store_id,
            "genre_ids[]": genre_id,
            "start_date": f"{year}-{month:02d}-01",
            "end_date": f"{year}-{month:02d}-{last_day:02d}",
            "is_staff_print_page": "false",
        })
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": "https://skylark.enterprise.rakushifu.com/staff/v2/schedules/confirmed",
        }
        if self.csrf_token:
            headers["x-csrf-token"] = self.csrf_token
        reply = await worker_fetch(self.SCHEDULES_URL + "?" + params,
                                   headers=headers, cookies=self.cookies)
        if (reply.status_code in (401, 403) or "/sign_in" in reply.url
                or "/sign-in" in reply.url):
            raise CredentialsUnavailable("らくしふのログインが期限切れです")
        if reply.status_code != 200:
            raise UpstreamError("らくしふからシフトを取得できません")
        try:
            payload = reply.json()
        except ValueError as error:
            raise UpstreamError("らくしふの応答形式が不正です") from error
        if not isinstance(payload, dict):
            raise UpstreamError("らくしふの応答形式が不正です")
        return map_schedule(payload, year, month)

    def fetch(self, year, month, store_id, genre_id):
        from pyodide.ffi import run_sync
        return run_sync(self.fetch_async(year, month, store_id, genre_id))

    def close(self):
        self.cookies.clear()


class WorkerRakushifuAuthenticator:
    ACCOUNT_LOGIN_URL = "https://api.accounts.rakushifu.com/sign_in_with_employee_code/browser"
    ENTERPRISE_LOGIN_URL = "https://skylark.enterprise.rakushifu.com/authenticated_users"
    PROFILE_URL = "https://skylark.enterprise.rakushifu.com/ajax/organizations"

    async def login_async(self, employee_code, password):
        cookies = WorkerCookieJar()
        reply = await worker_fetch(
            self.ACCOUNT_LOGIN_URL, method="POST", cookies=cookies,
            redirects=False,
            body=json.dumps({"enterprise_code": "skylark",
                             "employee_code": employee_code, "password": password}),
            headers={"accept": "application/json", "content-type": "application/json",
                     "origin": "https://accounts.rakushifu.com",
                     "referer": "https://accounts.rakushifu.com/skylark/staff/sign-in"},
        )
        try:
            error_type = reply.json().get("type") if reply.status_code >= 400 else None
        except (ValueError, AttributeError):
            error_type = None
        if error_type == "employee_code_account_unauthenticated":
            raise InvalidCredentials("従業員IDまたはパスワードが正しくありません")
        if not 200 <= reply.status_code < 300:
            raise UpstreamError("らくしふへログインできません")
        handoff = await worker_fetch(
            self.ENTERPRISE_LOGIN_URL + "?" + urlencode({
                "role": "staff", "enterprise_code": "skylark"}), cookies=cookies)
        if (handoff.status_code != 200
                or not handoff.url.startswith("https://skylark.enterprise.rakushifu.com/")
                or not cookies.has("_Rakushifu_session",
                                   "skylark.enterprise.rakushifu.com")):
            raise UpstreamError("らくしふのセッションを確立できません")
        profile = await worker_fetch(self.PROFILE_URL, cookies=cookies,
                                     headers={"accept": "application/json"})
        if profile.status_code != 200:
            raise UpstreamError("らくしふの利用者情報を取得できません")
        try:
            current = profile.json()["current_user"]
            viewer = Viewer(employee_code, int(current["id"]),
                            int(current["current_belong_store_id"]),
                            int(current["current_belong_genre_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise UpstreamError("らくしふの利用者情報の形式が不正です") from error
        match = re.search(r'data-csrf-token=["\']([^"\']+)', handoff.text)
        return WorkerRakushifuConnection(viewer, cookies,
                                         match.group(1) if match else "")

    def login(self, employee_code, password):
        from pyodide.ffi import run_sync
        return run_sync(self.login_async(employee_code, password))
