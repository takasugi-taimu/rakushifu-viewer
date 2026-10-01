import os
from pathlib import Path

from flask import Flask, Response, request

from .application.use_cases import ShiftUseCases
from .settings import AppSettings
from .web.routes import routes


PROJECT_DIR = Path(__file__).resolve().parent.parent


def create_app(config=None, use_cases=None, *, worker_runtime=False) -> Flask:
    settings = AppSettings.load((config or {}).get("APP_ENV"))
    app = Flask(__name__, template_folder=str(PROJECT_DIR / "templates"),
                static_folder=(str(PROJECT_DIR / "static")
                               if settings.environment == "test" else None))
    app.config.update(
        APP_ENV=settings.environment,
        APP_COOKIE_NAME="app_session",
        APP_COOKIE_SECURE=(settings.environment == "production" or
                           os.environ.get("APP_COOKIE_SECURE", "false").lower() == "true"),
        APP_SESSION_SECONDS=settings.session_seconds,
        CACHE_SECONDS=settings.cache_seconds,
        MAX_CACHED_MONTHS=settings.max_cached_months,
        MAX_SESSIONS=500,
    )
    if config:
        app.config.update(config)
    if settings.environment == "production":
        if not app.config["APP_COOKIE_SECURE"]:
            raise ValueError("production requires APP_COOKIE_SECURE=true")
        if not worker_runtime:
            raise RuntimeError("production requires the Python Workers runtime")
    if use_cases is None:
        if settings.environment == "test":
            from .infrastructure.memory_sessions import MemorySessionStore
            from .infrastructure.rakushifu_client import RakushifuAuthenticator
            authenticator = RakushifuAuthenticator()
            sessions = MemorySessionStore(
                lifetime_seconds=app.config["APP_SESSION_SECONDS"],
                max_sessions=app.config["MAX_SESSIONS"],
            )
        else:
            from .infrastructure.durable_sessions import DurableSessionStore
            from .infrastructure.worker_http import WorkerRakushifuAuthenticator
            authenticator = WorkerRakushifuAuthenticator()
            sessions = DurableSessionStore(
                lifetime_seconds=app.config["APP_SESSION_SECONDS"],
                cache_seconds=app.config["CACHE_SECONDS"],
                max_cached_months=app.config["MAX_CACHED_MONTHS"],
            )
        use_cases = ShiftUseCases(
            authenticator, sessions,
            cache_seconds=app.config["CACHE_SECONDS"],
            max_cached_months=app.config["MAX_CACHED_MONTHS"],
        )
    app.extensions["shift_use_cases"] = use_cases
    if settings.environment == "test":
        from .infrastructure.login_limiter import MemoryLoginLimiter
        app.extensions["login_limiter"] = MemoryLoginLimiter()
    else:
        from .infrastructure.durable_sessions import DurableLoginLimiter
        app.extensions["login_limiter"] = DurableLoginLimiter()
        @app.get("/static/<path:filename>", endpoint="static")
        def worker_static(filename):
            from pyodide.ffi import run_sync
            try:
                assets = request.environ["workers.env"].ASSETS
            except (KeyError, AttributeError) as error:
                raise RuntimeError("production requires the ASSETS binding") from error
            asset = run_sync(assets.fetch(f"https://assets.local/static/{filename}"))
            return Response(run_sync(asset.bytes()), status=asset.status,
                            headers=asset.headers)
    app.register_blueprint(routes)
    return app
