"""LLM-backed extraction for categorized companion memories."""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .sanitizer import ConversationSanitizer


DEFAULT_EXTRACTION_PROMPT = """你是一个“用户相关记忆整理器”。请从下面这段对话里，只提取以后值得长期参考的、和用户直接相关的简短记忆。

只允许提取这五类：
- profile: 用户稳定信息、偏好、禁忌、习惯、设备、项目背景
- agreement: 用户和助手之间明确约定、称呼、规则、禁令、偏好要求
- event: 最近简短事件，例如今天做了什么、刚发生了什么
- fact: 对用户有用的简短事实，不是通用百科
- knowledge_ref: 某类知识、文件、目录、插件、页面、说明存放在哪里

规则：
1. 只记用户相关内容，不记工具调用，不记助手自己的内在想法
2. 每条都要简短、独立、可直接注入
3. 不要重复同义改写
4. 不确定就不要编
5. event 类尽量短时有效；agreement/profile 类更稳定

输出严格 JSON 数组：
[
  {{
    "category": "profile|agreement|event|fact|knowledge_ref",
    "content": "一条简短记忆",
    "priority": 0.0,
    "confidence": 0.0,
    "ttl_days": 0,
    "pinned": false,
    "note": "可选补充说明"
  }}
]

当前已有记忆（用于避免重复）：
{existing}

对话：
{conversation}
"""


class ConversationAnalyzer:
    def __init__(self, context, config: dict[str, Any]):
        self.context = context
        self.config = config

    def _dbg(self, msg: str) -> None:
        levels = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if levels.get(cfg, 1) >= 2:
            logger.info(f"[用户记忆-分析器] {msg}")

    async def extract_memories(
        self,
        conversation: str,
        existing_memories: list[dict[str, Any]],
        provider_id: str = "",
    ) -> list[dict[str, Any]]:
        conversation = ConversationSanitizer.clean_text(conversation, self.config).strip()
        if not conversation:
            return []
        existing = [
            {
                "category": item.get("category"),
                "content": item.get("content"),
                "status": item.get("status"),
            }
            for item in existing_memories[:80]
        ]
        custom = self.config.get("prompt_settings", {}).get("round_summary_prompt", "")
        prompt = (custom or DEFAULT_EXTRACTION_PROMPT).format(
            existing=json.dumps(existing, ensure_ascii=False, indent=2),
            conversation=conversation[-4000:],
        )
        self._dbg(f"提取开始 conv_len={len(conversation)} existing={len(existing)} provider={provider_id or '(auto)'}")
        raw = await self._call_llm(prompt, provider_id)
        items = self._parse_json_array(raw)
        self._dbg(f"提取完成 count={len(items)}")
        return [self._normalize_item(item) for item in items if self._normalize_item(item)]

    async def _call_llm(self, prompt: str, provider_id: str = "") -> str:
        started = time.monotonic()
        result_text = ""
        try:
            if provider_id:
                result = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            else:
                provider = self.context.get_using_provider()
                result = await provider.text_chat(prompt=prompt)
            result_text = (
                result.completion_text.strip()
                if hasattr(result, "completion_text")
                else str(result).strip()
            )
        except Exception as e:
            logger.warning(f"[用户记忆-分析器] LLM 提取失败: {e}")
            return "[]"
        logger.info(
            f"[用户记忆-分析器] LLM提取完成 prompt_len={len(prompt)} result_len={len(result_text)} elapsed={(time.monotonic()-started)*1000:.0f}ms"
        )
        return result_text

    @staticmethod
    def _parse_json_array(text: str) -> list[dict[str, Any]]:
        if not text:
            return []
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception:
            try:
                start = text.index("[")
                end = text.rindex("]") + 1
                data = json.loads(text[start:end])
                return data if isinstance(data, list) else []
            except Exception:
                logger.warning(f"[用户记忆-分析器] JSON 解析失败: {text[:300]}")
                return []

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        category = str(item.get("category", "")).strip()
        content = " ".join(str(item.get("content", "")).split())
        if not category or not content:
            return None
        if category not in {"profile", "agreement", "event", "fact", "knowledge_ref"}:
            return None
        try:
            priority = float(item.get("priority", 0.6))
        except Exception:
            priority = 0.6
        try:
            confidence = float(item.get("confidence", 0.8))
        except Exception:
            confidence = 0.8
        try:
            ttl_days = int(item.get("ttl_days", 0))
        except Exception:
            ttl_days = 0
        return {
            "category": category,
            "content": content[:240],
            "priority": min(1.0, max(0.0, priority)),
            "confidence": min(1.0, max(0.0, confidence)),
            "ttl_days": max(0, ttl_days),
            "pinned": bool(item.get("pinned", False)),
            "note": str(item.get("note", "")).strip()[:240],
        }
