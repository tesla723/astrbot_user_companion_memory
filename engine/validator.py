"""验证器模块：进化操作后的效果评估 + 回滚支持。

每个进化操作完成后记录结果，元进化器用这些数据做策略调优。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..storage.repository import EvolutionRepository


class EvolutionValidator:
    """进化效果验证与审计。"""

    def __init__(self, config: dict, repository: EvolutionRepository):
        self.config = config
        self.repo = repository

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_lvl = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if lvls.get(cfg_lvl, 1) >= lvls.get(level, 1):
            logger.debug(f"[进化引擎-验证器] {msg}")

    async def validate_knowledge_stored(
        self, event_id: int, stored_count: int, entries: list[dict]
    ) -> None:
        self.repo.update_event(
            event_id,
            status="completed" if stored_count > 0 else "completed",
            decision_detail=f"成功写入 {stored_count}/{len(entries)} 条知识",
            completed_at=time.time(),
        )
        self._dbg(
            f"知识写入验证 event={event_id} stored={stored_count}/{len(entries)}",
            "verbose",
        )

    async def validate_skill_created(
        self, event_id: int, task_id: str, success: bool, result: dict
    ) -> None:
        if success:
            self.repo.update_event(
                event_id, status="completed",
                decision_detail=f"Skill 创建成功 task={task_id}",
                completed_at=time.time(),
            )
            self.repo.log_artifact(
                event_id, "skill_proposal",
                json.dumps(result, ensure_ascii=False), "active",
            )
            self._dbg(f"Skill创建验证成功 event={event_id} task={task_id}", "basic")
        else:
            err = result.get("error", "未知错误")
            self.repo.update_event(
                event_id, status="failed",
                error_message=err, completed_at=time.time(),
            )
            logger.warning(f"[进化引擎-验证器] Skill创建失败 event={event_id}: {err}")
            self._dbg(f"Skill创建验证失败 event={event_id} error={err}", "basic")

    async def validate_profile_updated(self, event_id: int, profile_delta: dict) -> None:
        self.repo.update_event(
            event_id, status="completed",
            decision_detail=f"画像维度更新: {list(profile_delta.keys())}",
            completed_at=time.time(),
        )
        self._dbg(
            f"画像更新验证 event={event_id} dims={list(profile_delta.keys())}",
            "verbose",
        )

    async def validate_threshold_adjusted(self, adjustments: dict) -> None:
        if not adjustments:
            self._dbg("阈值调整验证：无调整", "verbose")
            return
        for key, adj in adjustments.items():
            logger.info(
                f"[进化引擎-验证器] 阈值调整: {key} {adj['from']} → {adj['to']} ({adj['reason']})"
            )
        self._dbg(f"阈值调整验证: {len(adjustments)} 项", "basic")

    def get_evolution_stats(self) -> dict:
        """收集各进化器的统计数据（供元进化器使用）。"""
        def _stats(evolver_type: str) -> dict:
            events = self.repo.list_events(evolver_type=evolver_type, limit=200)
            total = len(events)
            completed = sum(1 for e in events if e["status"] == "completed")
            failed = sum(1 for e in events if e["status"] == "failed")
            rejected = sum(1 for e in events if e["status"] == "rejected")
            return {
                "total": total,
                "completed": completed,
                "failed": failed,
                "rejected": rejected,
                "success_rate": (completed / total) if total > 0 else 1.0,
                "rejection_rate": (rejected / total) if total > 0 else 0.0,
                "deletion_rate": 0.0,
            }

        stats = {
            "knowledge": _stats("knowledge"),
            "capability": _stats("capability"),
            "learning": _stats("learning"),
            "meta": _stats("meta"),
            "knowledge_count": self.repo.get_knowledge_count(),
            "timestamp": time.time(),
        }
        self._dbg(
            f"统计收集: k={stats['knowledge_count']} "
            f"evts(k={stats['knowledge']['total']} c={stats['capability']['total']} "
            f"l={stats['learning']['total']} m={stats['meta']['total']})",
            "verbose",
        )
        return stats
