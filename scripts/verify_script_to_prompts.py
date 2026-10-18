"""愿景 §15 端到端验证：**剧本 → 人物/场景/物品/分镜提示词 + 视频提示词**。

## 愿景原文（完整方案 第一部分 三）

> 我给 DeepSeek 一段剧本，它自动拆解人物，根据我的系统提示词和相关 skill，
> 输出**人物图、角色图、场景图、物品图、分镜图**的提示词，
> 以及**最终视频生成提示词**。**提示词和素材使用特殊分隔符标记，程序识别并自动分段。**
>
> 程序读取当前项目素材清单，**将类型、名称、版本和 LOCK/状态注入模型上下文**。

## 这套代码在哪

全部在编排层 `bettercomfyui/prompt/` 里，已有单元测试：

| 模块 | 职责 |
|---|---|
| `gateway.py` | 提示词模型网关（外部 API / 本地千问 / 开发者自配） |
| `inject.py` | 把素材清单（类型/名称/版本/LOCK状态）注入模型上下文 |
| `validate.py` | 输出解析 + 结构校验（缺字段/无效引用一律拦截） |
| `match.py` | 素材确定性匹配 + 疑似重复队列 |

## 这个脚本验证什么

把分段的单元测试**连成整条链**，并且**两条输出格式都验**：

    A) 规范 JSON          （流程图 C4：「输出格式 结构化JSON + 特殊分隔符」的前半）
    B) 特殊分隔符 + 中文标签（同一节点的后半；也是愿景强调的形态）

    剧本文本
      -> build_prompt_messages（含素材清单注入）
      -> gateway.complete（**桩传输层，不联网**）
      -> parse_script_output
      -> validate_script
      -> 人物/场景/物品/分镜提示词 + 视频提示词

## 已确认的两条契约事实（实测得出，不是猜的）

1. **规范 JSON 只认规范英文键**（`assets` / `chapters` / `segments` / `id` / `asset_type`…）。
   用中文键（`人物` / `章节`）走 JSON 会被判 `missing_field: assets`。
2. **中文标签只在「分隔符文本」格式生效**（`_SECTION_ALIASES` / `_FIELD_ALIASES`
   只被分隔符解析器使用）。所以「特殊分隔符标记」不是 JSON 的替代写法，
   而是**另一条独立支持的格式**。

## 替代验证方法（为什么可以这样验）

真实调用大模型需要 API 密钥（本项目规定**不得把真实密钥写进配置与文档**），
且结果不确定、不可重复。所以这里**注入桩传输层**：

* 桩返回两份**真实形状**的模型响应（规范 JSON / 中文分隔符文本）；
* 验证的是**我这边的管线**（注入、解析、校验、中文标签归一化、既有素材复用），
  **不是模型质量** —— 模型质量本来就不可测；
* **不联网、不花钱、可重复**。真实那次调用需要你配置密钥后跑同一条链。

运行：`python scripts/verify_script_to_prompts.py`
"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))


# ---------------------------------------------------------------------------
# 两份桩响应：同一个剧本的两种输出格式
# ---------------------------------------------------------------------------

#: A) 规范 JSON。键名必须是规范英文键（见 docstring 的契约事实 1）。
REPLY_JSON = """{
  "title": "第一章 雪夜",
  "output_language": "中文",
  "chapters": [
    {
      "chapter_id": "CH001",
      "title": "雪夜",
      "summary": "林澈雪夜独行，遭遇伏击。",
      "segments": [
        {"segment_index": 1, "prompt": "雪夜长街，林澈独行，镜头缓慢推进",
         "asset_refs": ["A1", "A2", "A4"], "duration_s": 10},
        {"segment_index": 2, "prompt": "林澈拔剑回身，雪花四溅，特写",
         "asset_refs": ["A1", "A3"], "duration_s": 10},
        {"segment_index": 3, "prompt": "巷口，阿岩接应，两人对视",
         "asset_refs": ["已锁定角色-阿岩"], "duration_s": 10}
      ]
    }
  ],
  "assets": [
    {"id": "A1", "asset_type": "CHARACTER", "name": "林澈",
     "prompt": "少年剑客，白衣，长发束起，冷峻，全身像"},
    {"id": "A2", "asset_type": "SCENE", "name": "雪夜长街",
     "prompt": "大雪，青石板路，两侧灯笼，冷蓝色调，无人"},
    {"id": "A3", "asset_type": "PROP", "name": "青锋剑",
     "prompt": "三尺青锋，剑身有冰纹，特写，黑色背景"},
    {"id": "A4", "asset_type": "SHOT", "name": "长街远景",
     "prompt": "俯拍长街，一个人影居中，雪幕纵深"}
  ]
}"""

#: B) 特殊分隔符 + **纯中文标签**。这是愿景里说的「特殊分隔符标记」形态。
#: 分节标签（章节 / 分镜段 / 人物 / 场景 / 物品 / 分镜）与字段名（标题 / 提示词 /
#: 素材引用 / 时长 / 编号 / 名称）都会被归一化到规范字段。
REPLY_DELIMITED = """=== 章节 ===
章节id: CH001
标题: 雪夜
摘要: 林澈雪夜独行，遭遇伏击。

=== 分镜段 ===
序号: 1
提示词: 雪夜长街，林澈独行，镜头缓慢推进
素材引用: A1, A2, A4
时长: 10

=== 分镜段 ===
序号: 2
提示词: 林澈拔剑回身，雪花四溅，特写
素材引用: A1, A3
时长: 10

=== 分镜段 ===
序号: 3
提示词: 巷口，阿岩接应，两人对视
素材引用: 已锁定角色-阿岩
时长: 10

=== 人物 ===
编号: A1
名称: 林澈
提示词: 少年剑客，白衣，长发束起，冷峻，全身像

=== 场景 ===
编号: A2
名称: 雪夜长街
提示词: 大雪，青石板路，两侧灯笼，冷蓝色调，无人

=== 物品 ===
编号: A3
名称: 青锋剑
提示词: 三尺青锋，剑身有冰纹，特写，黑色背景

=== 分镜 ===
编号: A4
名称: 长街远景
提示词: 俯拍长街，一个人影居中，雪幕纵深
"""


class StubTransport:
    """实现 `HttpTransport` 协议：记录调用、返回预设响应，**不联网**。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def post_json(self, *, url, headers, payload, timeout_s) -> Any:
        self.calls.append({"url": url, "payload": payload, "timeout_s": timeout_s})
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}]}


class _Cfg:
    """最小只读配置包装（网关只按属性取 prompt 段）。"""

    def __init__(self, raw: dict) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return self._raw.get(name, {})


def _run_path(label: str, reply: str, fmt: str, cfg, store, existing_names) -> bool:
    """跑一条链：组装消息 -> 网关 -> 解析 -> 校验 -> 汇总。返回是否通过。"""
    from bettercomfyui.prompt.gateway import build_gateway
    from bettercomfyui.prompt.inject import build_prompt_messages
    from bettercomfyui.prompt.validate import parse_script_output, validate_script

    print("\n" + "=" * 74)
    print(label)
    print("=" * 74)

    messages = build_prompt_messages(
        script_text="林澈雪夜独行长街，遭遇伏击，拔剑回身。",
        cfg=cfg, store=store, chapter_hint="第一章")
    manifest = messages.get("manifest") or ""
    injected = [ln.strip() for ln in manifest.splitlines() if "阿岩" in ln]
    print(f"  素材清单注入: {messages.get('manifest_injected')}"
          f" | 清单含已 LOCK 素材: {'是' if injected else '否'}")
    for ln in injected[:1]:
        print(f"    · {ln[:92]}")

    stub = StubTransport(reply)
    gateway = build_gateway(_Cfg(cfg._gateway_raw), transport=stub)
    text = gateway.complete(system=messages["system"], user=messages["user"])
    print(f"  网关: {type(gateway).__name__}（桩，不联网）→ 响应 {len(text)} 字")

    parsed = parse_script_output(text, output_format=fmt)
    validated = validate_script(parsed, existing_assets=existing_names)

    chapters = validated.get("chapters") or []
    assets = validated.get("assets") or []
    segs = (chapters[0].get("segments") or []) if chapters else []

    labels = {"CHARACTER": "人物图提示词", "SCENE": "场景图提示词",
              "PROP": "物品图提示词", "SHOT": "分镜图提示词"}
    by_type: dict[str, list] = {}
    for a in assets:
        t = getattr(a.get("asset_type"), "value", a.get("asset_type"))
        by_type.setdefault(str(t), []).append(a)

    reuse = 0
    # ⚠️ 判「复用既有」必须同时比对本次新增素材的**编号与名称**。
    # 第一版只比名称，于是 `A1` 被判成"复用"—— 但 A1 是本次新生成的素材，
    # 这条判据是错的，会把"新生成"误报成"复用"。名称和编号都要看。
    new_ids = {str(a.get("id")) for a in assets}
    new_names = {str(a.get("name")) for a in assets}
    print(f"\n  产出：")
    for t in ("CHARACTER", "SCENE", "PROP", "SHOT"):
        rows = by_type.get(t) or []
        names = "、".join(str(a.get("name")) for a in rows) or "—"
        print(f"    {labels[t]}：{len(rows)} 个 → {names}")
    print(f"    视频提示词 / 分段：{len(segs)} 段")
    for s in segs:
        refs = s.get("asset_refs") or []
        tags = []
        for r in refs:
            key = str(r)
            if key in new_ids or key in new_names:
                tags.append(f"{key}(本次新生成)")
            else:
                tags.append(f"{key}(复用既有)")
                reuse += 1
        print(f"      第{s.get('segment_index')}段 [{'、'.join(tags)}] "
              f"{str(s.get('prompt'))[:38]}")

    ok = (all(by_type.get(t) for t in ("CHARACTER", "SCENE", "PROP", "SHOT"))
          and len(segs) == 3 and reuse >= 1)
    print(f"\n  => {'✅ 通过' if ok else '❌ 不通过'}（复用既有素材 {reuse} 处）")
    return ok


def main() -> int:
    from _prompt_testutil import min_gateway_config  # tests/ 辅助

    from bettercomfyui.config import load_config
    from bettercomfyui.models.enums import (
        AssetSource, AssetStatus, AssetType, LibraryScope, ReviewMode,
    )
    from bettercomfyui.review.asset_review import AssetReviewer
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    print("=" * 74)
    print("愿景 §15 端到端验证：剧本 → 四类提示词 + 视频提示词（两种输出格式）")
    print("=" * 74)

    # --- 造项目 + 一个已 LOCK 素材（用于验证「复用既有素材」） ---
    tmp = Path(tempfile.mkdtemp())
    cfg = load_config(repo_root=REPO)
    root = tmp / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)

    pid = ProjectStore(root, cfg).create("愿景验证")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    store = AssetStore(paths)

    import numpy as np
    from PIL import Image

    img = tmp / "已锁定角色.png"
    Image.fromarray(np.full((64, 64, 3), 180, dtype=np.uint8)).save(img)
    entry = store.create_asset(asset_type=AssetType.CHARACTER, name="已锁定角色-阿岩",
                               source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    ver = store.add_version(entry["asset_id"], source=AssetSource.AI,
                            file_path=img, status=AssetStatus.PENDING_REVIEW)
    AssetReviewer(store, config=cfg, project_id=pid,
                  model_review=lambda m: {"score": 91.0, "passed": True},
                  auto_commit=True).review_version(
        entry["asset_id"], ver["version"], mode=ReviewMode.MODEL,
        material={"file_path": str(img), "content_hash": ver["content_hash"]})
    print(f"\n项目 {pid[:8]}…；已有 1 个已 LOCK 素材：已锁定角色-阿岩@{ver['version']}")

    # 网关配置：真实配置的 prompt 段（system 模板等）+ 可测的 provider
    merged = copy.deepcopy(getattr(cfg, "_raw", {}) or {})
    merged.setdefault("prompt", {})
    merged["prompt"].update(min_gateway_config()["prompt"])
    cfg._gateway_raw = merged

    names = [a.get("name") for a in store.list_assets() if a.get("name")]

    ok_a = _run_path(
        "A) 规范 JSON（键名必须规范）", REPLY_JSON, "json", cfg, store, names)
    ok_b = _run_path(
        "B) 特殊分隔符 + 纯中文标签（愿景强调的形态）", REPLY_DELIMITED,
        "delimited", cfg, store, names)

    print("\n" + "=" * 74)
    print(f"结论：规范 JSON {'✅' if ok_a else '❌'}   "
          f"分隔符+中文标签 {'✅' if ok_b else '❌'}")
    if ok_a and ok_b:
        print()
        print("两种输出格式都能产出：人物/场景/物品/分镜四类提示词 + 视频提示词，")
        print("并且引用到已有素材的那一段走的是**复用**而不是重新生成（愿景 R1）。")
        print()
        print("说明：用**桩传输层**做替代验证（不联网、不花钱、可重复），")
        print("      验证的是注入/解析/校验/中文标签归一化/既有素材复用，")
        print("      **不是模型质量**。真实调用需你配置 API 密钥或本地千问后跑同一链。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
