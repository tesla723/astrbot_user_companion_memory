"""能力进化器：分析需求模式 → 生成 Skill 建议 → 审批 → 创建。"""
from __future__ import annotations

import json
import time
from typing import Any
from astrbot.api import logger
from ..engine.analyzer import ConversationAnalyzer
from ..engine.decider import EvolutionDecider
from ..engine.scheduler import EvolutionScheduler
from ..engine.validator import EvolutionValidator
from ..storage.repository import EvolutionRepository
from .base import BaseEvolver


class CapabilityEvolver(BaseEvolver):
    def __init__(self, context, config, data_dir, repository, analyzer, decider, scheduler, validator, notify_user=None):
        super().__init__(context, config, data_dir)
        self.repo = repository
        self.analyzer = analyzer
        self.decider = decider
        self.scheduler = scheduler
        self.validator = validator
        self._notify_user = notify_user
        self._pending_approvals: dict[str, dict] = {}
        self._recently_rejected: dict[str, float] = {}
        self._conversation_buffer: list[str] = []

    def get_config(self) -> dict:
        return self.config.get("capability_evolver", {})

    def is_enabled(self) -> bool:
        return self.get_config().get("enabled", True)

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        if lvls.get(self.config.get("debug_settings", {}).get("debug_level", "basic"), 1) >= lvls.get(level, 1):
            logger.debug(f"[进化引擎-能力] {msg}")

    # ── 入口 ──

    async def accumulate_conversation(self, message: str) -> None:
        if not self.is_enabled():
            return
        self._conversation_buffer.append(message)
        trigger_mode = self.get_config().get("trigger_mode", "time")
        if trigger_mode not in ("rounds", "both"):
            return
        buf_size = self.get_config().get("conversation_buffer_size", 50)
        if len(self._conversation_buffer) >= buf_size:
            self._dbg(f"轮数触发模式分析 buf={len(self._conversation_buffer)}", "basic")
            hist = "\n".join(self._conversation_buffer[-100:])
            self._conversation_buffer = []
            await self._do_analyze(hist)

    async def cron_analyze(self) -> None:
        """定时分析（时间触发 + 兜底扫描共用）。"""
        if not self.is_enabled():
            return
        trigger_mode = self.get_config().get("trigger_mode", "time")
        if trigger_mode not in ("time", "both"):
            return
        hist = "\n".join(self._conversation_buffer[-100:]) if self._conversation_buffer else ""
        if not hist.strip():
            self._dbg("定时分析：无对话数据", "verbose")
            return
        self._dbg(f"定时模式分析 | buf={len(self._conversation_buffer)}", "basic")
        await self._do_analyze(hist)
        self._conversation_buffer = []

    async def _do_analyze(self, conversation_history: str) -> list[dict]:
        """执行分析 + 提案生成。"""
        started = time.monotonic()
        provider_id = self.get_config().get("analysis_model", "")
        patterns = await self.analyzer.detect_capability_patterns(conversation_history, provider_id)
        self._dbg(f"LLM 返回 {len(patterns)} 个模式 | {(time.monotonic()-started)*1000:.0f}ms", "basic")

        proposals = self.decider.should_propose_skill(patterns, self._get_excluded_names())
        self._dbg(f"决策过滤: {len(patterns)} → {len(proposals)} 个提案", "basic")

        for pattern in proposals:
            await self.propose_skill(pattern)
        # 清理 buffer（时间触发模式下不清空，保留给下一次分析窗口）
        return proposals

    def _get_excluded_names(self) -> set[str]:
        now = time.time()
        cooldown_s = self.get_config().get("reject_cooldown_hours", 72) * 3600
        excluded = set()
        for name, rt in list(self._recently_rejected.items()):
            if now - rt > cooldown_s:
                del self._recently_rejected[name]
            else:
                excluded.add(name)
        excluded.update(self._pending_approvals.keys())
        return excluded

    # ── 提案/审批 ──

    async def propose_skill(self, pattern: dict) -> str:
        approval_id = f"approval_{int(time.time())}_{pattern.get('pattern_name', 'unknown')}"
        provider_id = self.get_config().get("creation_model", "")
        proposal = await self.analyzer.generate_skill_proposal(pattern, provider_id)
        proposal["pattern"] = pattern
        proposal["approval_id"] = approval_id
        self._pending_approvals[approval_id] = proposal

        event_id = self.decider.log_event("capability", "propose", trigger_source="pattern_analysis",
            analysis_summary=pattern.get("description", ""), decision_detail=json.dumps(proposal, ensure_ascii=False))
        self.decider.log_artifact(event_id, "skill_proposal", json.dumps(proposal, ensure_ascii=False), status="pending_approval")

        if self._notify_user:
            await self._notify_user(self._format_msg(approval_id, proposal))
        logger.info(f"[进化引擎-能力] Skill提案: {proposal.get('skill_name')} (id={approval_id})")
        return approval_id

    async def approve(self, approval_id: str) -> bool:
        proposal = self._pending_approvals.pop(approval_id, None)
        if not proposal:
            return False
        event_id = self.decider.log_event("capability", "create", trigger_source="user_approval", decision_detail=f"approval_id={approval_id}")
        await self.scheduler.dispatch_skill_creation(proposal, on_complete_callback=lambda tid, ok, r: self.validator.validate_skill_created(event_id, tid, ok, r))
        self._update_artifact_status(approval_id, "active")
        logger.info(f"[进化引擎-能力] 已批准 {approval_id}")
        return True

    async def reject(self, approval_id: str, reason: str = "") -> bool:
        proposal = self._pending_approvals.pop(approval_id, None)
        if not proposal:
            return False
        pattern_name = proposal.get("pattern", {}).get("pattern_name", approval_id)
        self._recently_rejected[pattern_name] = time.time()
        event_id = self.decider.log_event("capability", "reject", trigger_source="user_reject", decision_detail=f"approval_id={approval_id} reason={reason}")
        self.repo.update_event(event_id, status="rejected", completed_at=time.time())
        self._update_artifact_status(approval_id, "deleted")
        logger.info(f"[进化引擎-能力] 已拒绝 {approval_id}")
        return True

    def get_pending_approvals(self) -> list[dict]:
        return list(self._pending_approvals.values())

    def _update_artifact_status(self, approval_id: str, status: str) -> None:
        for a in self.repo.list_artifacts(artifact_type="skill_proposal", status="pending_approval", limit=10):
            if a.get("content") and approval_id in str(a.get("content", "")):
                self.repo.update_artifact(a["id"], status=status)
                break

    def _format_msg(self, approval_id: str, proposal: dict) -> str:
        return (
            f"[进化引擎] Skill 创建建议\n"
            f"名称：{proposal.get('display_name')} ({proposal.get('skill_name')})\n"
            f"描述：{proposal.get('description')}\n"
            f"原因：{proposal.get('pattern', {}).get('description', '')}\n"
            f"ID：{approval_id}\n"
            f"/evo approve {approval_id} | /evo reject {approval_id}"
        )
