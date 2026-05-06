"""元进化器：监控各进化器效果 → 自动调优阈值/策略 → 生成周报。

- 收集各进化器统计指标
- 自动调整触发阈值
- 生成进化报告
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..engine.analyzer import ConversationAnalyzer
from ..engine.decider import EvolutionDecider
from ..engine.validator import EvolutionValidator
from ..storage.repository import EvolutionRepository
from .base import BaseEvolver


class MetaEvolver(BaseEvolver):
    """元进化器 - "进化"进化系统自身。"""

    def __init__(
        self,
        context,
        config: dict,
        data_dir: str,
        repository: EvolutionRepository,
        analyzer: ConversationAnalyzer,
        decider: EvolutionDecider,
        validator: EvolutionValidator,
        notify_user=None,
        strategy_manager=None,
    ):
        super().__init__(context, config, data_dir)
        self.repo = repository
        self.analyzer = analyzer
        self.decider = decider
        self.validator = validator
        self._notify_user = notify_user
        self.strategy_manager = strategy_manager

    def get_config(self) -> dict:
        return self.config.get("meta_evolver", {})

    def is_enabled(self) -> bool:
        return self.get_config().get("enabled", True)

    # ── 入口 ──

    async def evaluate_and_adjust(self) -> dict:
        """评估所有进化器效果，自动调整阈值。"""
        if not self.is_enabled():
            return {}

        stats = self.validator.get_evolution_stats()
        adjustments = self.decider.should_adjust_thresholds(stats)
        if self.strategy_manager:
            adjustments.update(self.strategy_manager.optimize_from_stats(stats))

        if adjustments:
            event_id = self.decider.log_event(
                "meta", "adjust_thresholds", trigger_source="cron",
                decision_detail=json.dumps(adjustments, ensure_ascii=False),
            )
            self.repo.update_event(event_id, status="completed",
                                   completed_at=time.time())

            # 持久化调整
            self.save_runtime_config(adjustments)
            # 同步到内存 config（浅层更新）
            for key, adj in adjustments.items():
                parts = key.split(".")
                if len(parts) == 2:
                    section, field = parts
                    if section in self.config and field in self.config.get(section, {}):
                        self.config[section][field] = adj["to"]

            await self.validator.validate_threshold_adjusted(adjustments)
            logger.info(f"[进化引擎] 元进化: {len(adjustments)} 项阈值调整")

        return adjustments

    async def generate_weekly_report(self) -> str:
        """生成进化周报。"""
        if not self.is_enabled():
            return ""

        stats = self.validator.get_evolution_stats()
        provider_id = self.get_config().get("report_model", "")
        report = await self.analyzer.generate_weekly_summary(stats, provider_id)
        return report or ""

    async def cron_run(self) -> None:
        """定时执行：评估 + 可选的周报。"""
        adjustments = await self.evaluate_and_adjust()

        if self.get_config().get("weekly_report", True):
            report = await self.generate_weekly_report()
            if report and self._notify_user:
                await self._notify_user(f"📊 [进化引擎-周报]\n{report}")

        return adjustments
