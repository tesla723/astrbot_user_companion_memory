"""FastAPI WebUI for user companion memory plugin."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from astrbot.api import logger


class WebUIServer:
    def __init__(self, plugin, config: dict[str, Any]):
        self.plugin = plugin
        self.config = config
        webui = config.get("webui_settings", {})
        self.host = str(webui.get("host", "127.0.0.1"))
        self.port = int(webui.get("port", 6187))
        self.session_timeout = int(webui.get("session_timeout", 3600))
        password = str(webui.get("access_password", "")).strip()
        if not password:
            password = secrets.token_urlsafe(12)
            logger.info(f"[用户记忆-WebUI] 未设置密码，已自动生成: {password}")
        self._access_password = password
        self._tokens: dict[str, float] = {}
        self._token_lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._app = FastAPI(title="User Companion Memory", version="1.0.0")
        self._setup_routes()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        cfg = uvicorn.Config(self._app, host=self.host, port=self.port, log_level="info", loop="asyncio")
        self._server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(50):
            if getattr(self._server, "started", False):
                logger.info(f"[用户记忆-WebUI] 已启动 http://{self.host}:{self.port}")
                return
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            await self._task
        logger.info("[用户记忆-WebUI] 已停止")

    def _setup_routes(self) -> None:
        static_dir = Path(__file__).resolve().parent.parent / "static"
        index_path = static_dir / "index.html"
        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )
        self._app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @self._app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))

        @self._app.get("/api/health")
        async def health():
            return {"status": "ok"}

        @self._app.post("/api/login")
        async def login(payload: dict[str, Any]):
            if str(payload.get("password", "")).strip() != self._access_password:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="认证失败")
            token = secrets.token_urlsafe(32)
            async with self._token_lock:
                self._tokens[token] = time.time()
            return {"token": token}

        @self._app.post("/api/logout")
        async def logout(request: Request):
            token = self._extract_token(request)
            async with self._token_lock:
                self._tokens.pop(token, None)
            return {"ok": True}

        @self._app.get("/api/dashboard")
        async def dashboard(token: str = Depends(self._auth())):
            return {
                "stats": self.plugin.repo.get_dashboard_stats(),
                "events": self.plugin.repo.list_events(limit=12),
                "uptime_minutes": int((time.time() - self.plugin._started_at) / 60),
            }

        @self._app.get("/api/memories")
        async def memories(
            category: str = "",
            status_filter: str = "",
            token: str = Depends(self._auth()),
        ):
            items = self.plugin.repo.list_memories(
                category=category,
                status=status_filter,
                limit=500,
                include_expired=True,
            )
            now = time.time()
            for item in items:
                item["forgetting"] = self.plugin.repo.explain_forgetting(item, self.plugin.config, now=now)
            return {
                "items": items
            }

        @self._app.post("/api/memories")
        async def add_memory(payload: dict[str, Any], token: str = Depends(self._auth())):
            try:
                status_text, item_id = await self.plugin._store_memory(
                    category=str(payload.get("category", "")).strip(),
                    content=str(payload.get("content", "")).strip(),
                    priority=float(payload.get("priority", 0.7)),
                    confidence=float(payload.get("confidence", 0.9)),
                    pinned=bool(payload.get("pinned", False)),
                    source=str(payload.get("source", "webui")),
                    note=str(payload.get("note", "")).strip(),
                    ttl_days=int(payload.get("ttl_days", 0)),
                )
            except ValueError as e:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
            return {"status": status_text, "id": item_id}

        @self._app.put("/api/memories/{item_id}")
        async def update_memory(item_id: int, payload: dict[str, Any], token: str = Depends(self._auth())):
            current = self.plugin.repo.get_memory(item_id)
            if not current:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="记忆不存在")
            content = str(payload.get("content", current.get("content", ""))).strip()
            priority = float(payload.get("priority", current.get("priority", 0.7)))
            pinned = self.plugin._normalize_pinned_request(
                bool(payload.get("pinned", current.get("pinned", False))),
                source="webui_update",
                category=str(payload.get("category", current.get("category", ""))),
                content=content,
                new_priority=priority,
                current_item_id=item_id,
            )
            ok = self.plugin.repo.update_memory(
                item_id,
                category=payload.get("category", current.get("category")),
                content=content,
                priority=priority,
                pinned=pinned,
                confidence=float(payload.get("confidence", current.get("confidence", 0.9))),
                note=payload.get("note", current.get("note", "")),
                status=payload.get("status", current.get("status", "active")),
                ttl_days=int(payload.get("ttl_days", current.get("ttl_days", 0))),
            )
            if not ok:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="无可更新字段")
            await self.plugin._refresh_memory_embedding(item_id, content)
            return {"ok": True}

        @self._app.delete("/api/memories/{item_id}")
        async def archive_memory(item_id: int, token: str = Depends(self._auth())):
            ok = self.plugin.repo.update_memory(item_id, status="archived")
            return {"ok": ok}

        @self._app.post("/api/search")
        async def search(payload: dict[str, Any], token: str = Depends(self._auth())):
            query = str(payload.get("query", "")).strip()
            return {"items": await self.plugin._search_memories(query, None, 20)}

        @self._app.post("/api/injection-preview")
        async def preview(payload: dict[str, Any], token: str = Depends(self._auth())):
            text, used_ids = await self.plugin._build_injection(str(payload.get("query", "")).strip())
            return {"text": text, "used_ids": used_ids}

        @self._app.get("/api/config")
        async def get_config(token: str = Depends(self._auth())):
            return self.plugin.config

        @self._app.get("/api/config/schema")
        async def get_schema(token: str = Depends(self._auth())):
            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            return json.loads(schema_path.read_text(encoding="utf-8"))

        @self._app.put("/api/config")
        async def put_config(payload: dict[str, Any], token: str = Depends(self._auth())):
            return {"ok": True, "config": self.plugin.update_runtime_config(payload)}

        @self._app.post("/api/forgetting/run")
        async def run_forgetting(token: str = Depends(self._auth())):
            return self.plugin.repo.run_forgetting(self.plugin.config)

    def _extract_token(self, request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return ""

    async def _validate_token(self, token: str) -> None:
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未认证")
        async with self._token_lock:
            last = self._tokens.get(token)
            if not last:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token无效")
            if time.time() - last > self.session_timeout:
                self._tokens.pop(token, None)
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="会话超时")
            self._tokens[token] = time.time()

    def _auth(self):
        async def dep(request: Request) -> str:
            token = self._extract_token(request)
            await self._validate_token(token)
            return token
        return dep
