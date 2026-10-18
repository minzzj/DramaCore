"""Better ComfyUI 独立验证共享工具（仅 tests/verify 使用）。

设计原则（对应 task-6 硬要求 —— 三态结论 通过/失败/未验证）：

1. **只有「目标模块文件不存在」才算未验证。** 模块存在但内部 ImportError、
   缺契约函数、签名不符，一律按 **失败** 处理——真缺陷不得被 skip 吞掉。
   这是本文件最重要的纪律：skip 只能表达「被测对象尚未存在」，
   绝不能表达「被测对象有问题但我不想判失败」。
2. 不允许断言恒真的空测试。每条用例至少有一个能因实现变化而变红的断言。
3. 提供 `tree_digest()` 做「文件字节未变」级别的证据，用于 R2/R3
   与「迟到结果不覆盖」类断言（只比较哈希，不依赖 mtime）。

用法：
    from _harness import need, make_config, tree_digest
    store_assets = need("bettercomfyui.store.assets")
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 由 conftest 保证，双保险
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# 任务书第 9 节 15 条验收清单原文（逐字摘录，作为报告对照基准）
# 来源：文档/BetterComfyUI_AI_Agent开发任务书.md 第 181-199 行
# ---------------------------------------------------------------------------

ACCEPTANCE_ITEMS: dict[int, str] = {
    1: "两个项目的数据和配置互不串用，切换后可恢复。",
    2: "有效剧本得到结构化数据；无效 JSON 被拦截，不启动生成。",
    3: "已有素材正确注入；明确匹配时复用、不重复生成。",
    4: "疑似重名进入确认队列，不覆盖旧素材。",
    5: "新素材生成后创建版本和内容指纹；旧版本保留。",
    6: "新版本不继承旧版 LOCK/审核结论。",
    7: "人工审核超时后按配置切换模型，且记录事件。",
    8: "CNT1/CNT2/CNT3 与技术重试计数互不混淆。",
    9: "并发审核只产生一个有效结论；迟到结果不能覆盖新版本。",
    10: "不合格视频段可局部重做；受影响后续段进入衔接复核。",
    11: "级联超限可停止并按策略处理，不会无限循环。",
    12: "服务重启后从检查点恢复，不重复创建版本或拼接。",
    13: "`segment/full/both` 审核策略实际生效。",
    14: "第三方节点缺失/不兼容时明确报错，不出现假成功。",
    15: "至少完成一次端到端演示：剧本→素材→视频段→审核→拼接→输出。",
    # 16 不是任务书原文，是 ROUND16 审计发现的**接缝**回归项。
    # 加它的理由：前 15 条两半各自绿，但接缝没人验 ——
    # 实测「规范剧本 → run_demo」返回 ok=True 却零素材（假成功）。
    16: "接缝：剧本形状归一 + 结构校验闸门 + 每类上限（ROUND19 新增）。",
    # 17 同样是审计产物：图一 SG8「中程失败」/ 图四 F2B 依赖「素材→段」反向索引，
    # 而 record_reference() 曾经零调用，这条能力根本算不出来（ROUND17 发现）。
    17: "素材→段反向索引与中程失败爆炸半径（图四 F2B）。",
    # 18：图一 SG2 G3/G4 两条素材库级约束，此前"配了不执行"（ROUND18 实测）。
    18: "素材库级约束执行：每类上限 + 统一分辨率（图一 G3/G4）。",
    # 19：图一 SG7 Z3/Z4/Z5 + SG4 ENC1，四项都曾「配了不读」（ROUND18 棘轮扫描）。
    19: "日志级别/人工切换日志/配置导入导出/音频拼接三态（Z3-Z5 + ENC1）。",
    # 20：图一 SG4 O7 背景音乐，愿景自己标注的待办，此前零实现。
    20: "背景音乐：混音/循环补齐/三种音频模式组合/配置校验（图一 O7）。",
    # 21：用户要求的 5090 32G 阶段化显存调度（单卡靠卸载而非堆硬件）。
    21: "阶段化显存调度：批量生成/卸载换模型/统一审核/预算门禁。",
    # 22：图一 SG5 X 返工加料，此前零实现。
    22: "返工提示词加料：失败维度 + @素材标记（图一 SG5 X）。",
    # 23：任务超时看门狗 + ComfyUI 地址（两个曾配了不读的配置项）
    23: "任务超时看门狗与 ComfyUI 地址解析（task_timeout_s / comfyui_endpoint）。",
    # 24：用户要求的模拟浏览器操作端到端测试（HTTP 等价层 + 模型边界打桩）
    24: "模拟浏览器操作 E2E：object_info/提交/执行/取消/清队列/释放显存。",
    # 25：无法真跑项的理论计算（权重体积/显存峰值/加载次数/码率）
    25: "理论计算：显存峰值证明阶段化是必需、加载次数与时长对比。",
    # 26：A4 三项（图一 C6 风格标签库 / G2 素材命名 / H5 分维度评分）
    26: "风格标签库、素材可读命名、分维度评分（图一 C6/G2/H5）。",
    # 27：图一 SG1 界面接通（装 DeepSeek 两巨人 + 桥接节点 + 真实 ComfyUI 链路）
    27: "剧本巨人→桥接→导演台：格式转换 + 真 ComfyUI 链路贯通（图一 SG1）。",
    # 28：成品安装验证 + 4 套新工作流 + 愿景对齐矩阵
    28: "成品安装验证、4 套新工作流 schema 校验、愿景对齐矩阵。",
    # 29：用户要求的**真实浏览器**点击测试（Playwright + 系统 Edge）
    29: "真实浏览器点击：页面渲染/拖节点/点队列/打开工作流/无前端错误。",
    # 30：API 配置接口（含同步给剧本巨人）+ 完整逻辑图工作流 W0
    30: "API 配置接口（写 user.yaml + 巨人 settings.json）+ W0 完整逻辑图。",
    # 31：ROUND25/26 用户基准 —— 一个大节点完成「一次 API 调用 → E1–E5 全量提示词 →
    #    预设分隔符拆分 → 确定性匹配登记素材库 → 导演组」，并把中间节点全收进节点内部。
    #    （旧版界面只出镜头、素材提示词缺失；W0/W0L 占位开关恒真、W2 连线非法 —— 用户报障。）
    31: "一个大节点：一次调用产出 E1–E5、预设分隔符、素材库确定性登记、直出导演组。",
    # 32：用户第三次补充 —— 逻辑图不只有视频生成，还有「图片/素材生成」「素材审核」「视频审核」。
    #     ROUND27 补：素材生成取任务+入库节点、素材审核走完整状态机、视频审核段级+整片
    #     （并修掉回灌节点用常量 80 分假通过的真缺陷）。
    32: "素材生成/入库/素材审核(LOCK)/视频审核(R_seg+R_full)完整闭环。",
    # 33：ROUND28 —— 第一阶段「总流程」工作流：人工输入一段剧本 → 全部内容；
    #     普通用户看得懂（说明区+分组+少量节点）；视频审核必须可见。
    33: "总流程工作流：一段剧本→全部内容（本机可跑/GPU 全链）+ 视频审核可见。",
    # 34：ROUND29 —— 逻辑图 SG6 的 SPLICE→ENC→R_full：成片剪辑/封装/自检节点 +
    #     本机可跑「段视频→剪好成片」工作流；真机 3 段合并、完整性、质量、占位检测。
    34: "成片剪辑（拼接+封装+完整性/剪辑性/质量自检）+ 视频审核闭环。",
    # 35：ROUND33 —— 32GB 生产显存控制：自动拆两段（素材/出片）+ 段间 /free；
    #     结构可本地验证，显存峰值必须在 GPU 机器实测（如实标注）。
    35: "32G 分两段执行编排（素材段/出片段 + /free + dry-run 结构验证）。",
    # 36：ROUND35 —— 一条命令回答「剪辑过吗/完整吗/质量高吗」：
    #     成片质量审核 CLI（完整性/剪辑性/质量/占位），到机后直接用。
    36: "成片质量审核 CLI：剪辑性/完整性/质量/占位 一条命令出报告。",
}

#: 任务书第 199 行原文，所有真实 vs mock 结论都必须服从这一条
REAL_VS_MOCK_RULE = (
    "若真实视频模型暂不可用，必须明确区分真实集成与 mock 测试，"
    "不能把模拟结果说成真实生成已通过。"
)


# ---------------------------------------------------------------------------
# 模块装载：区分「尚无实现」与「实现有缺陷」
# ---------------------------------------------------------------------------


def _is_missing_target(exc: ModuleNotFoundError, dotted: str) -> bool:
    """判断 ModuleNotFoundError 是否就是「目标模块不存在」。

    只有当缺失名恰好等于目标模块、或是目标模块的父包时，才算未验证。
    实现内部缺第三方依赖（例如缺 requests）不算——那是真缺陷。
    """
    missing = (exc.name or "").strip()
    if not missing:
        return False
    if missing == dotted:
        return True
    # 目标模块的父包不存在（例如 bettercomfyui.store 整个包没建）
    return dotted.startswith(missing + ".")


def load(*dotted: str):
    """导入一个或多个模块；任一目标模块文件不存在 -> pytest.skip（= 未验证）。"""
    mods = []
    for name in dotted:
        try:
            mods.append(importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if _is_missing_target(exc, name):
                pytest.skip(
                    f"未验证/UNVERIFIED：{name} 尚未实现（找不到模块文件 {name}）。"
                    f"通过该条验收需要先交付 {name}。"
                )
            raise
    return mods[0] if len(mods) == 1 else tuple(mods)


def load_optional(dotted: str):
    """导入可选模块，不存在返回 None（不 skip）。用于探测类检查。"""
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:
        if _is_missing_target(exc, dotted):
            return None
        raise


def need(name: str):
    """`need("bettercomfyui.store.assets")` 的别名，语义 = load。"""
    return load(name)


def contract_attr(mod: Any, name: str, *, kind: str = "auto") -> Any:
    """取契约要求的成员；缺失 -> 断言失败（这是缺陷，不是未验证）。"""
    if not hasattr(mod, name):
        raise AssertionError(
            f"契约违背：{mod.__name__} 缺少 {name!r}"
            f"（见 docs/CONTRACTS.md，模块 {mod.__name__}）"
        )
    obj = getattr(mod, name)
    if kind == "function" and not callable(obj):
        raise AssertionError(f"契约违背：{mod.__name__}.{name} 应为可调用对象，实际 {type(obj)!r}")
    if kind == "class" and not isinstance(obj, type):
        raise AssertionError(f"契约违背：{mod.__name__}.{name} 应为类，实际 {type(obj)!r}")
    return obj


def module_source_path(dotted: str) -> Path:
    return REPO_ROOT / (dotted.replace(".", os.sep) + ".py")


# ---------------------------------------------------------------------------
# 配置对象：CONTRACTS.md 第 9 节的默认值（Config 类模块路径契约未定义，
# 见 docs/VERIFY.md 的契约缺口 G-1；测试用鸭子类型配置推进）
# ---------------------------------------------------------------------------


class DotDict(dict):
    """支持属性访问的 dict，供被测代码以 cfg.x.y 或 cfg['x']['y'] 取配置。"""

    def __getattr__(self, item: str):
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(item) from exc

    def __setattr__(self, key: str, value: Any) -> None:  # pragma: no cover
        self[key] = value


#: 逐字照搬 docs/CONTRACTS.md 第 9 节（v1 冻结）
CONTRACT_CONFIG_DEFAULTS: dict[str, dict[str, Any]] = {
    "prompt": {
        "model_provider": "external_api",
        "output_language": "中文",
        "output_format": "json",
        "story_granularity": "shot",
        "system_prompt_template": "default",
        "inject_existing_assets": True,
        "normalize_names": True,
    },
    "assets": {
        "resolution": "1024x1024",
        "steps": 25,
        "sampler": "DPM++ 2M Karras",
        "cfg": 7,
        "seed": None,
        "batch_count": 4,
        "per_class_limit": 9,
    },
    "asset_review": {
        "default_mode": "MODEL",
        "human_timeout_minutes": 10,
        "timeout_pause_on_active_op": True,
        "model_candidate_limit": 3,
        "score_threshold": 70,
        "score_dimensions": ["画质", "一致性", "合规", "构图"],
        "on_timeout": "switch_to_model",
        "arbitration": "backend_timestamp_sole",
        "new_version_requires_review": True,
        "fallback": "highest_of_candidates",
    },
    "director": {
        "segment_seconds": 10,
        "resolution": "1080P",
        "fps": 24,
        "tail_frames": 22,
        "first_frame": "first",
        "last_frame": "last",
        "vram_tier": "auto",
        "pruned_model": True,
        "reference_mode": "auto",
    },
    "video_review": {
        "audit_scope": "both",
        "threshold_segment": 70,
        "threshold_full": 70,
        "cnt2_limit": 3,
        "cnt3_limit": 3,
        "regenerate_granularity": "segment",
        "max_cascade_depth": 8,
        "full_review_policy": None,
    },
    "encode": {
        "audio_join": "auto",
        "transition": "none",
        "subtitle": False,
        "watermark": False,
        "format": "MP4",
        "codec": "H.264",
        "crf": 18,
        "cover": "first_frame",
    },
    "global": {
        "log_level": "INFO",
        "log_all_manual_and_auto_switch": True,
        "tech_retry_limit": 3,
        "checkpoint_every_stage": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def make_config(**overrides: Any):
    """构造配置对象。

    优先使用 **真实实现** 的 `bettercomfyui.config.load_config`（契约缺口 G-1 已由
    Lead 落地为 `bettercomfyui/config.py`），再用 `config_from_dict` 叠加覆盖，
    这样被测代码拿到的是它自己的 Config 类型（避免了「用假配置测出假失败」）。
    只有在该模块尚未实现时才退回到契约默认值的 DotDict。
    """
    cfg_mod = load_optional("bettercomfyui.config")
    if cfg_mod is not None and hasattr(cfg_mod, "load_config"):
        base = cfg_mod.load_config(repo_root=REPO_ROOT)
        if not overrides:
            return base
        data = _deep_merge(base.to_dict(), overrides)
        return cfg_mod.config_from_dict(data)
    return DotDict(_deep_merge(CONTRACT_CONFIG_DEFAULTS, overrides))


# ---------------------------------------------------------------------------
# 证据工具：文件树摘要（字节级）
# ---------------------------------------------------------------------------


def tree_digest(root: Path) -> dict[str, str]:
    """返回 {相对路径: sha256hex}，只含文件，不含 mtime。目录不存在返回 {}。"""
    root = Path(root)
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
    return out


def assert_tree_unchanged(before: dict[str, str], after: dict[str, str], *, what: str) -> None:
    """断言文件树字节级未变；打印差异便于定位。"""
    if before == after:
        return
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    raise AssertionError(
        f"{what}：文件树被改动 —— 新增={added} 删除={removed} 内容变化={changed}"
    )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def events_of(path: Path, event_name: str) -> list[dict]:
    """从 logs/events.jsonl 里筛出指定事件（用于「必须记录事件」类断言）。"""
    return [e for e in read_jsonl(path) if e.get("event") == event_name]


# ---------------------------------------------------------------------------
# 契约要求的枚举取值（真源 = docs/CONTRACTS.md，最终真源 = 方案第七部分）
# 硬编码于此，任何实现偏离都会让 test_contracts_parity.py 变红。
# ---------------------------------------------------------------------------


def enums_mod():
    return load("bettercomfyui.models.enums")


def values_of(enum_cls) -> set[str]:
    return {m.value for m in enum_cls}
