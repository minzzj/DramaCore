"""测试辅助（prompt 层专用，避免与其他任务共享的 conftest.py 冲突）。"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def valid_script() -> dict:
    """一份完全合法的剧本（可直接通过 validate_script）。"""
    return {
        "title": "第一章 雪夜",
        "output_language": "中文",
        "chapters": [
            {
                "chapter_id": "CH001",
                "title": "雪夜",
                "summary": "林澈雪夜独行，遭遇伏击。",
                "segments": [
                    {
                        "segment_index": 1,
                        "prompt": "雪夜长街，林澈独行，镜头缓慢推进",
                        "asset_refs": ["A1", "A2"],
                        "duration_s": 10,
                        "first_frame_ref": "A2",
                        "last_frame_ref": "A2",
                    },
                    {
                        "segment_index": 2,
                        "prompt": "林澈拔剑回身，雪花四溅",
                        "asset_refs": ["A1"],
                    },
                ],
            }
        ],
        "assets": [
            {
                "id": "A1",
                "asset_type": "CHARACTER",
                "name": "林澈",
                "prompt": "少年剑客，白衣，长发束起，冷峻",
            },
            {
                "id": "A2",
                "asset_type": "SCENE",
                "name": "雪夜长街",
                "prompt": "大雪，青石板路，灯笼，冷色调",
            },
        ],
    }


def deep_update(base: Any, **changes: Any) -> Any:
    """复制 base 后按顶层键覆盖（不改动原对象）。"""
    import copy

    result = copy.deepcopy(base)
    result.update(changes)
    return result


def min_gateway_config(provider: str = "external_api", **prompt: Any) -> dict:
    """最小可用网关配置（可覆盖 prompt 段任意键）。"""
    section: dict = {"model_provider": provider, "api_base": "http://127.0.0.1:9999/v1"}
    if provider in ("external_api", "local_qwen"):
        section["model_name"] = "test-model"
    if provider == "external_api":
        section["api_key"] = "test-key"
    section.update(prompt)
    return {"prompt": section}
