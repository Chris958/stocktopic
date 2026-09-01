from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .service import StockTopicService

logger = logging.getLogger(__name__)


class ConfirmRequest(BaseModel):
    final_name: str | None = Field(default=None, max_length=40)
    catalyst_strength: float | None = Field(default=None, ge=0, le=100)
    catalyst_duration: str | None = Field(default=None, max_length=20)


class MergeRequest(BaseModel):
    source_ids: list[int] = Field(min_length=1)


class SplitRequest(BaseModel):
    member_codes: list[str] = Field(min_length=1)
    new_name: str = Field(min_length=1, max_length=40)


class PinRequest(BaseModel):
    pinned: bool


class TrackStockRequest(BaseModel):
    theme_id: int = Field(gt=0)
    code: str = Field(min_length=6, max_length=16)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


SESSION_COOKIE = "stocktopic_session"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = StockTopicService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await asyncio.to_thread(service.initialize)
        scheduler = asyncio.create_task(service.run_scheduler(), name="stocktopic-scheduler")
        try:
            yield
        finally:
            service.stop()
            await scheduler

    app = FastAPI(
        title="StockTopic API",
        version="0.9.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.service = service

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        public_path = request.url.path in {
            "/",
            "/health",
            "/favicon.ico",
            "/api/v1/auth/login",
        } or request.url.path.startswith("/static/")
        auth_kind = _authorization_kind(request, settings)
        if not public_path and not auth_kind:
            response = JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
            )
        elif (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path != "/api/v1/auth/login"
            and auth_kind in {"basic", "session"}
            and request.headers.get("X-StockTopic-Request") != "1"
        ):
            response = JSONResponse({"detail": "CSRF check failed"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/health")
    async def health():
        return await asyncio.to_thread(service.health)

    @app.get("/")
    async def dashboard_page():
        return FileResponse(static_dir / "index.html")

    @app.post("/api/v1/auth/login")
    async def login(credentials: LoginRequest):
        valid = hmac.compare_digest(
            credentials.username, settings.admin_username
        ) and hmac.compare_digest(credentials.password, settings.admin_password)
        if not valid:
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        max_age = settings.session_cookie_days * 24 * 60 * 60
        response = JSONResponse({"ok": True, "expires_in_days": settings.session_cookie_days})
        response.set_cookie(
            SESSION_COOKIE,
            _make_session_token(settings, credentials.username, int(time.time()) + max_age),
            max_age=max_age,
            httponly=True,
            secure=settings.public_base_url.startswith("https://"),
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=settings.public_base_url.startswith("https://"),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/v1/dashboard")
    async def dashboard():
        return {
            "health": await asyncio.to_thread(service.health),
            "themes": service.database.list_themes(),
            "alerts": service.database.recent_alerts(100),
            "backtest": service.test_pool.dashboard(service.clock.china_now()),
        }

    @app.get("/api/v1/test-pool")
    async def test_pool():
        return service.test_pool.dashboard(service.clock.china_now())

    @app.post("/api/v1/test-pool")
    async def track_stock(request: TrackStockRequest):
        try:
            entry, created = service.add_test_pool_stock(request.theme_id, request.code)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "created": created, "entry": entry}

    @app.get("/api/v1/themes")
    async def themes(status: str | None = None):
        if status not in {
            None,
            "pending",
            "watching",
            "confirmed",
            "rejected",
            "merged",
            "archived",
        }:
            raise HTTPException(status_code=400, detail="Invalid theme status")
        return {"items": service.database.list_themes(status)}

    @app.post("/api/v1/themes/{theme_id}/pin")
    async def pin_theme(theme_id: int, request: PinRequest):
        try:
            service.database.set_theme_pin(theme_id, request.pinned)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"ok": True, "theme": service.database.get_theme(theme_id)}

    @app.post("/api/v1/themes/{theme_id}/archive")
    async def archive_theme(theme_id: int):
        try:
            service.database.archive_theme(theme_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"ok": True}

    @app.post("/api/v1/themes/{theme_id}/restore")
    async def restore_theme(theme_id: int):
        try:
            service.database.restore_theme(theme_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"ok": True, "theme": service.database.get_theme(theme_id)}

    @app.post("/api/v1/themes/{theme_id}/confirm")
    async def confirm_theme(theme_id: int, request: ConfirmRequest):
        try:
            service.discovery.confirm(
                theme_id,
                request.final_name,
                request.catalyst_strength,
                request.catalyst_duration,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        score = service.scorer.calculate(
            service.database.get_theme(theme_id) or {}, service.clock.china_now()
        )
        if score:
            service.database.save_score(theme_id, score)
        return {"ok": True, "theme": service.database.get_theme(theme_id)}

    @app.post("/api/v1/themes/{theme_id}/reject")
    async def reject_theme(theme_id: int):
        try:
            service.discovery.reject(theme_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"ok": True}

    @app.post("/api/v1/themes/{theme_id}/merge")
    async def merge_theme(theme_id: int, request: MergeRequest):
        try:
            service.discovery.merge(theme_id, request.source_ids)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "theme": service.database.get_theme(theme_id)}

    @app.post("/api/v1/themes/{theme_id}/split")
    async def split_theme(theme_id: int, request: SplitRequest):
        try:
            new_id = service.discovery.split(theme_id, request.member_codes, request.new_name)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "new_theme": service.database.get_theme(new_id)}

    @app.post("/api/v1/themes/{theme_id}/explain")
    async def explain_theme(theme_id: int):
        theme = service.database.get_theme(theme_id)
        if not theme:
            raise HTTPException(status_code=404, detail="Theme not found")
        if not service.explainer.enabled:
            raise HTTPException(status_code=503, detail="OpenAI API not configured")
        names = [
            item.get("final_name") or item.get("suggested_name") or item["provisional_name"]
            for item in service.database.list_themes()
        ]
        item = await asyncio.to_thread(service.explainer.explain, theme, names)
        service.database.save_ai_explanation(theme_id, item)
        service.database.save_theme_catalysts(theme_id, item.get("catalysts", []))
        service.database.set_suggested_name(theme_id, item["suggested_name"])
        return {key: value for key, value in item.items() if key != "raw"}

    @app.post("/api/v1/admin/refresh-catalysts")
    async def refresh_catalysts():
        if not service.explainer.enabled:
            raise HTTPException(status_code=503, detail="OpenAI API not configured")
        return await asyncio.to_thread(service.refresh_theme_catalysts)

    @app.post("/api/v1/admin/refresh-test-pool")
    async def refresh_test_pool():
        return await asyncio.to_thread(service.refresh_test_pool_prices)

    @app.post("/api/v1/admin/run-once")
    async def run_once():
        # This endpoint never bypasses the market clock or trading calendar.
        return await asyncio.to_thread(service.collect_once)

    @app.post("/api/v1/admin/backfill-discovery")
    async def backfill_discovery():
        return await asyncio.to_thread(
            service.backfill_recent_trade_days,
            service.clock.china_now(),
            refresh_sources=True,
            source="manual",
        )

    @app.post("/api/v1/admin/wecom-test")
    async def wecom_test():
        if not service.notifier.enabled:
            raise HTTPException(status_code=503, detail="WeCom group robot not configured")
        try:
            await asyncio.to_thread(
                service.notifier.send_text,
                "StockTopic连接测试",
                "Mac mini题材情绪系统已成功连接企业微信群机器人。",
            )
        except Exception as error:
            safe = _safe_integration_error(error)
            service.database.set_metadata("last_wecom_error", safe)
            logger.warning("WeCom connection test failed: %s", safe)
            raise HTTPException(
                status_code=502,
                detail=f"企业微信群机器人发送失败：{safe}",
            ) from error
        service.database.set_metadata("last_wecom_error", "")
        service.database.set_metadata(
            "last_wecom_success_at",
            service.clock.china_now().isoformat(timespec="seconds"),
        )
        return {"ok": True}

    return app


def _authorization_kind(request: Request, settings: Settings) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        supplied = header[7:].strip()
        if supplied and hmac.compare_digest(supplied, settings.app_api_token):
            return "bearer"
        return None
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        if hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
            password, settings.admin_password
        ):
            return "basic"
        return None
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and _valid_session_token(settings, token):
        return "session"
    return None


def _session_key(settings: Settings) -> bytes:
    material = (
        f"stocktopic-session-v1\0{settings.app_api_token}\0{settings.admin_password}"
    ).encode()
    return hashlib.sha256(material).digest()


def _make_session_token(settings: Settings, username: str, expires_at: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"u": username, "exp": expires_at}, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(_session_key(settings), payload.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload}.{encoded_signature}"


def _valid_session_token(settings: Settings, token: str) -> bool:
    try:
        payload, supplied_signature = token.split(".", 1)
        expected = hmac.new(
            _session_key(settings), payload.encode("ascii"), hashlib.sha256
        ).digest()
        supplied = base64.urlsafe_b64decode(
            supplied_signature + "=" * (-len(supplied_signature) % 4)
        )
        if not hmac.compare_digest(supplied, expected):
            return False
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(decoded.decode("utf-8"))
        return (
            hmac.compare_digest(str(data.get("u") or ""), settings.admin_username)
            and int(data.get("exp") or 0) > int(time.time())
        )
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _safe_integration_error(error: Exception) -> str:
    message = str(error)
    message = re.sub(r"(?i)(access_token=)[^&\s]+", r"\1***", message)
    message = re.sub(r"(?i)(corpsecret=)[^&\s]+", r"\1***", message)
    message = re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1***", message)
    return message[:500] or type(error).__name__
