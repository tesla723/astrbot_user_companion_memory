"""进化器基类。"""
from __future__ import annotations

import json
import os
import time
from abc import ABC
from typing import Any

from astrbot.api import logger


class BaseEvolver(ABC):

    def __init__(self, context, config: dict, data_dir: str):
        self.context = context
        self.config = config
        self.data_dir = data_dir

    def _get_profile_path(self) -> str:
        return os.path.join(self.data_dir, "user_profile.json")

    def load_profile(self) -> dict:
        path = self._get_profile_path()
        default = {
            "language_style": "未知",
            "tech_stack": [],
            "time_pattern": "未知",
            "topic_interests": [],
            "memory_preferences": "未知",
            "relationship_preferences": "未知",
            "last_updated": 0,
        }
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return self._sanitize_profile(data, default)
                logger.warning(f"[进化引擎] 画像文件损坏(非dict) size={os.path.getsize(path)}，使用默认值")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[进化引擎] 画像文件读取失败: {e}，使用默认值")
        return default

    @staticmethod
    def _sanitize_profile(data: dict, defaults: dict) -> dict:
        """确保画像每个字段类型正确，修复 LLM 可能的输出异常。"""
        list_keys = {"tech_stack", "topic_interests", "knowledge_domains"}
        str_keys = {
            "language_style",
            "time_pattern",
            "memory_preferences",
            "relationship_preferences",
            "persona_style",
            "response_preferences",
            "conversation_habits",
        }
        result = dict(defaults)
        for k, v in data.items():
            if k in list_keys:
                if isinstance(v, list):
                    result[k] = [str(x) for x in v if x]
                elif isinstance(v, str):
                    result[k] = [x.strip() for x in v.split(",") if x.strip()]
                else:
                    result[k] = []
            elif k in str_keys:
                result[k] = str(v) if v and v != "未知" else defaults.get(k, "未知")
            elif k == "last_updated":
                result[k] = float(v) if isinstance(v, (int, float)) else 0
            else:
                result[k] = v
        return result

    def save_profile(self, profile: dict) -> None:
        profile["last_updated"] = time.time()
        path = self._get_profile_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _get_config_path(self) -> str:
        return os.path.join(self.data_dir, "evolution_config.json")

    def save_runtime_config(self, updates: dict) -> None:
        path = self._get_config_path()
        existing = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        existing.update(updates)
        existing["last_updated"] = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
