"""触发器模块：对话事件钩子 + Cron 定时任务。

负责感知 AstrBot 的对话完成事件，并通过 AsyncIOScheduler 执行定时扫描。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from astrbot.api import logger


class TriggerManager:
    """管理进化引擎的所有触发源。"""

    def __init__(
        self,
        scheduler,
        config: dict,
        on_conversation_end: Callable,
    ):
        self._scheduler = scheduler
        self._config = config
        self._on_conversation_end = on_conversation_end
        self._cron_jobs: dict[str, str] = {}
        self._trigger_count: dict[str, int] = {}  # evolver -> count
        self._last_trigger: dict[str, float] = {}

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_lvl = self._config.get("debug_settings", {}).get("debug_level", "basic")
        if lvls.get(cfg_lvl, 1) >= lvls.get(level, 1):
            logger.debug(f"[进化引擎-触发器] {msg}")

    async def handle_llm_response(self, event, assistant_text: str = "") -> None:
        """在 LLM 响应后异步触发分析。"""
        try:
            session_id = event.unified_msg_origin or ""
            message_text = event.message_str or ""
            if not session_id or not message_text:
                return

            # 活跃时段检查
            if not self._is_active_hours():
                self._dbg("跳过触发：不在活跃时段", "trace")
                return

            # 分析冷却检查
            if not self._check_cooldown():
                self._dbg("跳过触发：冷却中", "trace")
                return

            self._trigger_count["llm_response"] = self._trigger_count.get("llm_response", 0) + 1
            now = time.time()
            self._last_trigger["llm_response"] = now
            self._dbg(
                f"LLM响应触发 | session={session_id[:20]}... "
                f"msg_len={len(message_text)} 累计={self._trigger_count['llm_response']}",
                "trace",
            )

            await self._on_conversation_end(session_id, message_text, event, assistant_text)
        except Exception:
            logger.debug("[进化引擎-触发器] LLM响应处理异常", exc_info=True)

    def _is_active_hours(self) -> bool:
        """检查当前是否在配置的活跃时段内。"""
        ts = self._config.get("time_settings", {})
        start = ts.get("active_hours_start", 0)
        end = ts.get("active_hours_end", 0)
        if start == end:
            return True
        now_h = time.localtime().tm_hour
        if start <= end:
            return start <= now_h < end
        else:
            return now_h >= start or now_h < end

    def _check_cooldown(self) -> bool:
        """检查是否已过分析冷却时间。"""
        cooldown_s = self._config.get("time_settings", {}).get("analysis_cooldown_seconds", 60)
        if cooldown_s <= 0:
            return True
        last = self._last_trigger.get("llm_response", 0)
        return (time.time() - last) >= cooldown_s

    def schedule_cron(self, evolver_name: str, interval_hours: int, callback: Callable):
        """为某个进化器注册定时扫描。"""
        if not self._scheduler:
            logger.warning(f"[进化引擎-触发器] 调度器未初始化，跳过 {evolver_name}")
            return

        job_id = f"evolution_{evolver_name}"
        if job_id in self._cron_jobs:
            self._scheduler.remove_job(job_id)
            self._dbg(f"移除旧定时任务 {job_id}", "verbose")

        self._scheduler.add_job(
            lambda: asyncio.create_task(callback()),
            "interval",
            hours=interval_hours,
            id=job_id,
            replace_existing=True,
        )
        self._cron_jobs[job_id] = evolver_name
        logger.info(f"[进化引擎-触发器] 已注册 {evolver_name} Cron 每{interval_hours}h")

    def schedule_cron_interval_minutes(
        self, evolver_name: str, interval_minutes: int, callback: Callable
    ):
        """为某个进化器注册分钟级的定时扫描。"""
        if not self._scheduler:
            logger.warning(f"[进化引擎-触发器] 调度器未初始化，跳过 {evolver_name}")
            return
        job_id = f"evolution_{evolver_name}"
        if job_id in self._cron_jobs:
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            lambda: asyncio.create_task(callback()),
            "interval",
            minutes=interval_minutes,
            id=job_id,
            replace_existing=True,
        )
        self._cron_jobs[job_id] = evolver_name
        logger.info(f"[进化引擎-触发器] 已注册 {evolver_name} Cron 每{interval_minutes}min")

    def get_stats(self) -> dict:
        return {
            "cron_jobs": dict(self._cron_jobs),
            "trigger_counts": dict(self._trigger_count),
            "last_triggers": dict(self._last_trigger),
        }
