"""LLM-backed extraction for categorized companion memories."""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .sanitizer import ConversationSanitizer


DEFAULT_EXTRACTION_PROMPT = """你是一个“用户相关记忆整理器”。请从下面这段对话里，只提取以后值得长期参考的、和用户直接相关的简短记忆。

只允许提取这六类：
- profile: 用户稳定信息、偏好、禁忌、习惯、设备、项目背景
- agreement: 用户和助手之间明确约定、称呼、规则、禁令、偏好要求
- slang: 用户个人黑话、圈内简称、群友梗、特殊语义映射、说法习惯
- event: 最近简短事件，例如今天做了什么、刚发生了什么
- fact: 对用户有用的简短事实，不是通用百科
- knowledge_ref: 某类知识、文件、目录、插件、页面、说明存放在哪里

规则：
1. 只记用户相关内容，不记工具调用，不记助手自己的内在想法
2. 每条都要简短、独立、可直接注入
3. 不要重复同义改写
4. 不确定就不要编
5. event 类尽量短时有效；agreement/profile/slang 类更稳定
6. 如果用户给某个词、缩写、梗、句式赋予了特定含义，优先记为 slang

输出严格 JSON 数组：
[
  {{
    "category": "profile|agreement|slang|event|fact|knowledge_ref",
    "content": "一条简短记忆",
    "priority": 0.0,
    "confidence": 0.0,
    "pinned": false,
    "note": "可选补充说明"
  }}
]

当前已有记忆（用于避免重复）：
{existing}

对话：
{conversation}
"""

DEFAULT_SLANG_IMPORT_PROMPT = """你是一个“用户黑话词典整理器”。下面是一段长文本，请从中提取用户可能长期会使用的黑话、简称、圈内梗、特殊含义映射。

目标：
1. 只提取以后值得长期记住的黑话/术语
2. 一条一条独立输出，适合直接写进长期记忆
3. 如果文本里出现“某词 = 某意思 / 某说法代表什么 / 在我们这里是什么意思”，优先提取
4. 不要提取一次性的情绪话、废话、普通口头禅
5. 不确定就不要编

输出严格 JSON 数组：
[
  {{
    "category": "slang",
    "content": "黑话本体 + 简短释义",
    "priority": 0.0,
    "confidence": 0.0,
    "pinned": false,
    "note": "适用语境、来源、补充说明"
  }}
]

已有黑话（避免重复）：
{existing}

原始文本：
{conversation}
"""

DEFAULT_FORGETTING_ORGANIZER_PROMPT = """你是一个“用户记忆库整理器”。下面给你一批已有记忆，请输出明确的整理动作，不要新增无中生有的内容。

目标：
1. 找出语义高度重复、明显可以合并的记忆
2. 找出明显过时、被新信息纠正、或与更可信记忆冲突的记忆
3. 对每个动作给出清晰 reason
4. 只有非常确定时才处理，不确定就不要动

规则：
1. merge 只能在同一 category 内执行
2. update 用于修正一条记忆本身
3. archive 用于归档重复、过时、被明确纠正或冲突失败的记忆
4. agreement/profile 要更谨慎
4. 输出里只能引用下面给出的 id
5. pinned 代表重要，不代表永远正确；如果确实过时或冲突，也可以建议 archive
6. 如果无需整理，返回空数组 []

输出严格 JSON 数组：
[
  {{
    "action": "merge",
    "target_id": 1,
    "source_ids": [2, 3],
    "content": "合并后的最终内容",
    "priority": 0.0,
    "confidence": 0.0,
    "pinned": false,
    "note": "可选补充说明",
    "reason": "为什么这样合并"
  }},
  {{
    "action": "update",
    "target_id": 4,
    "content": "更新后的最终内容",
    "priority": 0.0,
    "confidence": 0.0,
    "pinned": false,
    "note": "可选补充说明",
    "reason": "为什么这样更新"
  }},
  {{
    "action": "archive",
    "target_id": 5,
    "reason": "为什么归档"
  }}
]

已有记忆：
{existing}
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

    @staticmethod
    def _safe_format_prompt(template: str, **kwargs: Any) -> str:
        allowed = {key: f"__UCM_PLACEHOLDER_{key.upper()}__" for key in kwargs.keys()}
        text = template
        for key, token in allowed.items():
            text = text.replace("{" + key + "}", token)
        text = text.replace("{", "{{").replace("}", "}}")
        for key, token in allowed.items():
            text = text.replace(token, "{" + key + "}")
        return text.format(**kwargs)

    async def extract_memories(
        self,
        conversation: str,
        existing_memories: list[dict[str, Any]],
        provider_id: str = "",
    ) -> list[dict[str, Any]]:
        conversation = ConversationSanitizer.clean_text(conversation, self.config).strip()
        if not conversation:
            return []
        memory_cfg = self.config.get("memory_settings", {})
        existing_limit = int(memory_cfg.get("round_summary_existing_limit", 80))
        conversation_chars = int(memory_cfg.get("round_summary_conversation_chars", 4000))
        existing = [
            {
                "category": item.get("category"),
                "content": item.get("content"),
                "status": item.get("status"),
            }
            for item in existing_memories[:existing_limit] if existing_limit > 0
        ]
        custom = self.config.get("prompt_settings", {}).get("round_summary_prompt", "")
        prompt = self._safe_format_prompt(
            custom or DEFAULT_EXTRACTION_PROMPT,
            existing=json.dumps(existing, ensure_ascii=False, indent=2),
            conversation=conversation[-conversation_chars:] if conversation_chars > 0 else conversation,
        )
        self._dbg(f"提取开始 conv_len={len(conversation)} existing={len(existing)} provider={provider_id or '(auto)'}")
        raw = await self._call_llm(prompt, provider_id)
        items = self._parse_json_array(raw)
        self._dbg(f"提取完成 count={len(items)}")
        return [self._normalize_item(item) for item in items if self._normalize_item(item)]

    async def import_slang_memories(
        self,
        text: str,
        existing_memories: list[dict[str, Any]],
        provider_id: str = "",
    ) -> list[dict[str, Any]]:
        text = ConversationSanitizer.clean_text(text, self.config).strip()
        if not text:
            return []
        slang_cfg = self.config.get("slang_settings", {})
        existing_limit = int(slang_cfg.get("import_existing_limit", 120))
        text_chars = int(slang_cfg.get("import_text_chars", 12000))
        existing = [
            {
                "category": item.get("category"),
                "content": item.get("content"),
                "status": item.get("status"),
                "note": item.get("note", ""),
            }
            for item in existing_memories[:existing_limit] if existing_limit > 0
            if item.get("category") == "slang"
        ]
        custom = self.config.get("prompt_settings", {}).get("slang_import_prompt", "")
        prompt = self._safe_format_prompt(
            custom or DEFAULT_SLANG_IMPORT_PROMPT,
            existing=json.dumps(existing, ensure_ascii=False, indent=2),
            conversation=text[-text_chars:] if text_chars > 0 else text,
        )
        self._dbg(f"黑话导入开始 text_len={len(text)} existing={len(existing)} provider={provider_id or '(auto)'}")
        raw = await self._call_llm(prompt, provider_id)
        items = self._parse_json_array(raw)
        normalized = []
        for item in items:
            fixed = self._normalize_item(item)
            if fixed and fixed["category"] == "slang":
                normalized.append(fixed)
        self._dbg(f"黑话导入完成 count={len(normalized)}")
        return normalized

    async def organize_existing_memories(
        self,
        existing_memories: list[dict[str, Any]],
        provider_id: str = "",
        max_items: int = 200,
    ) -> list[dict[str, Any]]:
        existing = []
        selected_memories = existing_memories if max_items <= 0 else existing_memories[:max_items]
        for item in selected_memories:
            existing.append(
                {
                    "id": item.get("id"),
                    "category": item.get("category"),
                    "content": item.get("content"),
                    "priority": item.get("priority"),
                    "pinned": item.get("pinned"),
                    "status": item.get("status"),
                    "note": item.get("note", ""),
                }
            )
        if not existing:
            return []
        custom = self.config.get("prompt_settings", {}).get("forgetting_organizer_prompt", "")
        prompt = self._safe_format_prompt(
            custom or DEFAULT_FORGETTING_ORGANIZER_PROMPT,
            existing=json.dumps(existing, ensure_ascii=False, indent=2),
        )
        self._dbg(f"整理开始 memories={len(existing)} provider={provider_id or '(auto)'}")
        raw = await self._call_llm(prompt, provider_id)
        items = self._parse_json_array(raw)
        results = [self._normalize_organizer_item(item) for item in items]
        results = [item for item in results if item]
        self._dbg(f"整理完成 actions={len(results)}")
        return results

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
        if category not in {"profile", "agreement", "slang", "event", "fact", "knowledge_ref"}:
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

    @staticmethod
    def _normalize_organizer_item(item: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        action = str(item.get("action", "merge")).strip().lower() or "merge"
        if action not in {"merge", "update", "archive"}:
            return None
        try:
            target_id = int(item.get("target_id", 0))
        except Exception:
            return None
        if target_id <= 0:
            return None
        source_ids: list[int] = []
        source_ids_raw = item.get("source_ids", [])
        if isinstance(source_ids_raw, list):
            for value in source_ids_raw:
                try:
                    iv = int(value)
                except Exception:
                    continue
                if iv > 0 and iv != target_id:
                    source_ids.append(iv)
            source_ids = sorted(set(source_ids))
        content = " ".join(str(item.get("content", "")).split())
        if action == "merge" and (not content or not source_ids):
            return None
        if action == "update" and not content:
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
            "action": action,
            "target_id": target_id,
            "source_ids": source_ids,
            "content": content[:240],
            "priority": min(1.0, max(0.0, priority)),
            "confidence": min(1.0, max(0.0, confidence)),
            "ttl_days": max(0, ttl_days),
            "pinned": bool(item.get("pinned", False)),
            "note": str(item.get("note", "")).strip()[:240],
            "reason": str(item.get("reason", "")).strip()[:240],
        }
