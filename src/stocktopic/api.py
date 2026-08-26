from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import re
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
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.service = service

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        public_path = (
            request.url.path in {"/", "/health", "/favicon.ico"}
            or request.url.path.startswith("/static/")
        )
        if not public_path and not _authorized(request, settings):
            response = JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="StockTopic"'},
            )
        elif (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("Authorization", "").startswith("Basic ")
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
        return response

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/health")
    async def health():
        return await asyncio.to_thread(service.health)

    @app.get("/")
    async def dashboard_page():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/v1/dashboard")
    async def dashboard():
        latest_anomaly_date = service.database.latest_anomaly_trade_date()
        anomalies = (
            service.database.anomalies_for_trade_date(latest_anomaly_date)
            if latest_anomaly_date
            else []
        )
        return {
            "health": await asyncio.to_thread(service.health),
            "themes": service.database.list_themes(),
            "anomalies": anomalies,
            "alerts": service.database.recent_alerts(100),
            "data_context": {
                "anomaly_trade_date": latest_anomaly_date,
                "has_intraday_data": bool(latest_anomaly_date),
            },
        }

    @app.get("/api/v1/themes")
    async def themes(status: str | None = None):
        if status not in {None, "pending", "confirmed", "rejected", "merged"}:
            raise HTTPException(status_code=400, detail="Invalid theme status")
        return {"items": service.database.list_themes(status)}

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
        service.database.set_suggested_name(theme_id, item["suggested_name"])
        return {key: value for key, value in item.items() if key != "raw"}

    @app.post("/api/v1/admin/run-once")
    async def run_once():
        # This endpoint never bypasses the market clock or trading calendar.
        return await asyncio.to_thread(service.collect_once)

    @app.post("/api/v1/admin/wecom-test")
    async def wecom_test():
        if not service.notifier.enabled:
            raise HTTPException(status_code=503, detail="WeCom not configured")
        try:
            await asyncio.to_thread(
                service.notifier.send_text,
                "StockTopic连接测试",
                "Mac mini题材情绪系统已成功连接企业微信。",
            )
        except Exception as error:
            logger.warning("WeCom connection test failed: %s", _safe_integration_error(error))
            raise HTTPException(
                status_code=502,
                detail=f"企业微信发送失败：{_safe_integration_error(error)}",
            ) from error
        return {"ok": True}

    return app


def _authorized(request: Request, settings: Settings) -> bool:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        supplied = header[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, settings.app_api_token)
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
            password, settings.admin_password
        )
    return False


def _safe_integration_error(error: Exception) -> str:
    message = str(error)
    message = re.sub(r"(?i)(access_token=)[^&\s]+", r"\1***", message)
    message = re.sub(r"(?i)(corpsecret=)[^&\s]+", r"\1***", message)
    return message[:500] or type(error).__name__
