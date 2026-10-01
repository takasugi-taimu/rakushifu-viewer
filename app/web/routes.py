import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, url_for)

from app.application.errors import (
    CredentialsUnavailable, InvalidCredentials,
    InvalidScheduleData, SessionExpired, StaffNotFound, StorageUnavailable,
    UpstreamError,
)


routes = Blueprint("routes", __name__)


def _use_cases():
    return current_app.extensions["shift_use_cases"]


def _token() -> str:
    return request.cookies.get(current_app.config["APP_COOKIE_NAME"], "")


def _login_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        if not _use_cases().authenticated(_token()):
            if request.path.startswith("/api/"):
                return jsonify({"error": "ログインが必要です"}), 401
            return redirect(url_for("routes.login_page"))
        return function(*args, **kwargs)
    return decorated


def _year_month():
    try:
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
        date(year, month, 1)
        return year, month
    except (TypeError, ValueError):
        raise ValueError("year and month parameters are required")


@routes.after_request
def prevent_shift_cache(response):
    if request.path.startswith("/api/") or request.path == "/login":
        response.headers["Cache-Control"] = "no-store"
    return response


@routes.errorhandler(SessionExpired)
@routes.errorhandler(CredentialsUnavailable)
def expired_session(error):
    _use_cases().logout(_token())
    response = jsonify({"error": "ログインの有効期限が切れました"})
    response.status_code = 401
    response.delete_cookie(current_app.config["APP_COOKIE_NAME"], path="/")
    return response


@routes.errorhandler(UpstreamError)
def upstream_error(error):
    return jsonify({"error": str(error)}), 502


@routes.errorhandler(InvalidScheduleData)
def invalid_schedule(error):
    return jsonify({"error": str(error)}), 502


@routes.errorhandler(StorageUnavailable)
def storage_unavailable(error):
    return jsonify({"error": str(error)}), 503


@routes.get("/login")
def login_page():
    if _use_cases().authenticated(_token()):
        return redirect(url_for("routes.index"))
    return render_template("login.html")


@routes.post("/login")
def login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "リクエストが不正です"}), 400
    employee_code = data.get("employee_code")
    password = data.get("password")
    if (not isinstance(employee_code, str) or not employee_code.strip()
            or not isinstance(password, str) or not password):
        return jsonify({"error": "従業員IDとパスワードを入力してください"}), 400
    if len(employee_code) > 50 or len(password) > 500:
        return jsonify({"error": "入力が長すぎます"}), 400
    employee_code = employee_code.strip()
    client_ip = (request.headers.get("CF-Connecting-IP")
                 if current_app.config["APP_ENV"] == "production" else None)
    if not current_app.extensions["login_limiter"].allow(
            client_ip or request.remote_addr or "unknown", employee_code):
        return jsonify({"error": "ログイン試行が多すぎます。しばらく待ってください"}), 429
    try:
        token = _use_cases().login(employee_code, password)
    except InvalidCredentials as error:
        return jsonify({"error": str(error)}), 401
    if _token():
        _use_cases().logout(_token())
    response = jsonify({"message": "ログイン成功"})
    response.set_cookie(
        current_app.config["APP_COOKIE_NAME"], token,
        max_age=current_app.config["APP_SESSION_SECONDS"],
        httponly=True, secure=current_app.config["APP_COOKIE_SECURE"],
        samesite="Lax", path="/",
    )
    return response


@routes.post("/logout")
def logout():
    _use_cases().logout(_token())
    response = redirect(url_for("routes.login_page"))
    response.delete_cookie(current_app.config["APP_COOKIE_NAME"], path="/")
    return response


@routes.get("/")
@_login_required
def index():
    return render_template("index.html")


@routes.get("/api/shifts")
@_login_required
def shifts_by_date():
    value = request.args.get("date", "")
    if not value:
        return jsonify({"error": "date parameter is required"}), 400
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        target = date.fromisoformat(value)
    except ValueError:
        return jsonify({"error": "date format must be YYYY-MM-DD"}), 400
    return jsonify(_use_cases().day(target, _token()))


@routes.get("/api/calendar")
@_login_required
def calendar_data():
    try:
        year, month = _year_month()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(_use_cases().calendar(year, month, _token()))


@routes.get("/api/staff/<int:user_id>")
@_login_required
def staff_detail(user_id):
    try:
        year, month = _year_month()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        return jsonify(_use_cases().staff(user_id, year, month, _token()))
    except StaffNotFound as error:
        return jsonify({"error": str(error)}), 404


@routes.get("/api/staff")
@_login_required
def search_staff():
    try:
        year, month = _year_month()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    query = request.args.get("q", "")
    if len(query) > 100:
        return jsonify({"error": "検索語が長すぎます"}), 400
    return jsonify(_use_cases().search_staff(query, year, month, _token()))


@routes.post("/api/pay/estimate")
@_login_required
def pay_estimate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "リクエストが不正です"}), 400
    try:
        year = int(data["year"])
        month = int(data["month"])
        date(year, month, 1)
        wage = Decimal(str(data["hourly_wage"]))
        bonus = Decimal(str(data["night_bonus_percent"]))
        if (not wage.is_finite() or wage < 0 or wage > 100000
                or not bonus.is_finite() or bonus < 0 or bonus > 500):
            raise ValueError
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return jsonify({"error": "給与設定を確認してください"}), 400
    return jsonify(_use_cases().my_pay(year, month, _token(), wage, bonus))
