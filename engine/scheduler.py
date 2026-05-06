"""调度器模块：将进化任务打包并通过 subagent 或直接执行。

能力进化（创建 Skill）需要后台执行，设置 background_task=true。
其他进化操作为轻量操作，由插件直接执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from astrbot.api import logger


class EvolutionScheduler:
    """进化任务调度器。"""

    def __init__(self, context, config: dict, data_dir: str):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self._max_concurrent = config.get("engine_settings", {}).get(
            "max_concurrent_tasks", 2
        )
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_lvl = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if lvls.get(cfg_lvl, 1) >= lvls.get(level, 1):
            logger.debug(f"[进化引擎-调度器] {msg}")

    async def dispatch_knowledge_store(self, entries: list[dict], store_callback) -> int:
        """调度知识写入（轻量，直接执行）。"""
        self._dbg(f"知识写入调度 | {len(entries)} 条 | 并发槽 {self._max_concurrent}", "verbose")
        async with self._semaphore:
            stored = 0
            for entry in entries:
                try:
                    await store_callback(entry)
                    stored += 1
                except Exception as e:
                    self._dbg(f"知识写入失败: {e}", "basic")
            self._dbg(f"知识写入完成: {stored}/{len(entries)}", "verbose")
            return stored

    async def dispatch_profile_update(self, profile_delta: dict, update_callback) -> bool:
        """调度画像更新（轻量，直接执行）。"""
        self._dbg(f"画像更新调度 | 维度={list(profile_delta.keys())}", "verbose")
        async with self._semaphore:
            try:
                await update_callback(profile_delta)
                self._dbg("画像更新成功", "verbose")
                return True
            except Exception as e:
                logger.warning(f"[进化引擎-调度器] 画像更新失败: {e}")
                return False

    async def dispatch_skill_creation(
        self, proposal: dict, on_complete_callback,
    ) -> str:
        """调度 Skill 创建（重量，通过 background task 执行）。

        使用 AstrBot subagent 机制，设置 background_task=true。
        返回 task_id 用于追踪。
        """
        task_id = f"skill_create_{int(time.time())}_{proposal.get('skill_name', 'unknown')}"
        self._dbg(f"Skill创建调度 task_id={task_id}", "basic")

        async def _background_create():
            tb = time.monotonic()
            async with self._semaphore:
                try:
                    skill_name = proposal.get("skill_name", "unknown")
                    logger.info(f"[进化引擎-调度器] 开始后台创建 Skill: {skill_name}")
                    self._dbg(f"后台任务启动 task={task_id}", "basic")

                    skill_code = await self._generate_skill_content(proposal)
                    if skill_code:
                        result = await self._save_generated_skill(proposal, skill_code)
                        ms = (time.monotonic() - tb) * 1000
                        logger.info(
                            f"[进化引擎-调度器] ✅ Skill 创建成功 {skill_name} | {ms:.0f}ms"
                        )
                        await on_complete_callback(task_id, True, result)
                    else:
                        self._dbg(f"Skill代码生成失败: 空结果", "basic")
                        await on_complete_callback(task_id, False, {"error": "代码生成为空"})
                except Exception as e:
                    logger.error(f"[进化引擎-调度器] Skill 创建失败: {e}", exc_info=True)
                    await on_complete_callback(task_id, False, {"error": str(e)})

        task = asyncio.create_task(_background_create())
        self._running_tasks[task_id] = task
        task.add_done_callback(lambda t: self._running_tasks.pop(task_id, None))
        self._dbg(f"后台任务已创建 task={task_id} 运行中={len(self._running_tasks)}", "verbose")
        return task_id

    async def _generate_skill_content(self, proposal: dict) -> str:
        """用 LLM 生成 Skill 代码。"""
        skill_name = proposal.get("skill_name", "generated_skill")
        description = proposal.get("description", "")
        impl_notes = proposal.get("implementation_notes", "")

        self._dbg(f"生成Skill代码 name={skill_name}", "basic")

        prompt = f"""基于以下 Skill 方案，生成完整的 AstrBot 插件代码。

Skill 名称：{skill_name}
描述：{description}
实现要点：{impl_notes}

要求：
1. 生成一个独立的 AstrBot 插件，包含 main.py、metadata.yaml、requirements.txt
2. main.py 继承 Star 类，使用 @register 装饰器
3. metadata.yaml 的 name 必须是合法 Python 标识符

请以 JSON 格式输出：

{{
  "metadata.yaml": "文件内容...",
  "main.py": "文件内容...",
  "requirements.txt": "文件内容..."
}}

只输出 JSON，不要有其他解释。"""

        provider_id = self.config.get("capability_evolver", {}).get("creation_model", "")
        self._dbg(f"调用模型 provider={provider_id or '(auto)'}", "verbose")

        tb = time.monotonic()
        try:
            if provider_id:
                result = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=prompt,
                )
                text = result.completion_text.strip() if hasattr(result, "completion_text") else str(result)
                ms = (time.monotonic() - tb) * 1000
                self._dbg(f"Skill代码生成 LLM 完成 | {ms:.0f}ms | len={len(text)}", "basic")
                return self._parse_skill_json(text)
            provider = self.context.get_using_provider()
            if provider:
                result = await provider.text_chat(prompt=prompt)
                text = result.completion_text.strip() if hasattr(result, "completion_text") else str(result)
                ms = (time.monotonic() - tb) * 1000
                self._dbg(f"Skill代码生成 LLM 完成 | {ms:.0f}ms | len={len(text)}", "basic")
                return self._parse_skill_json(text)
        except Exception as e:
            logger.warning(f"[进化引擎-调度器] Skill 代码生成失败: {e}")
        return ""

    async def _save_generated_skill(self, proposal: dict, code_json: str) -> dict:
        """保存生成的 Skill 代码到插件 data 目录。"""
        gen_dir = os.path.join(self.data_dir, "generated_skills")
        skill_name = proposal.get("skill_name", "unnamed")
        skill_dir = os.path.join(gen_dir, f"astrbot_plugin_{skill_name}")
        os.makedirs(skill_dir, exist_ok=True)

        try:
            files = json.loads(code_json) if isinstance(code_json, str) else code_json
            for filename, content in files.items():
                filepath = os.path.join(skill_dir, filename)
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
            self._dbg(f"Skill文件已保存 dir={skill_dir} files={list(files.keys())}", "basic")
            return {
                "skill_name": skill_name,
                "skill_dir": skill_dir,
                "files": list(files.keys()),
            }
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[进化引擎-调度器] Skill 保存失败: {e}")
            return {"error": str(e)}

    @staticmethod
    def _parse_skill_json(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            try:
                start = text.index("{")
                end = text.rindex("}") + 1
                return text[start:end]
            except (ValueError, json.JSONDecodeError):
                return ""
