"""缺口分析：三份文档的第一阶段要求 vs 当前实现。

**为什么要写这个模块而不是写个 md**

方案里有大量"参数建议值"表格（提示词/素材/审核/导演台/成片/视频审核/全局/项目管理）
与 46 项问题清单。人肉比对一定会漏，而且漏了不会报错。
把它做成**可执行的检查**，缺口就变成一个可运行、可回归的清单。

本模块只做一件事：**声明每条要求的状态与证据位置**，并提供 `report()` 输出。
状态只有三种，与项目一贯纪律一致：

* `DONE`        —— 已实现，并能指出**实现位置**（不是"应该有吧"）
* `DELEGATED`   —— 已实现，但由**导演台/依赖节点**承担（融合层负责接线），并指出接线点
* `TODO`        —— 未实现，写明缺什么
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class Gap(NamedTuple):
    area: str          # 方案章节
    item: str          # 要求
    status: str        # DONE / DELEGATED / TODO
    where: str         # 证据位置（文件或节点）
    note: str = ""


#: 逐条盘点。维护规则：改成 DONE 之前必须能指出文件/节点，否则保持 TODO。
GAPS: tuple[Gap, ...] = (
    # ---------------- 一、提示词生成参数（方案第三部分·一） ----------------
    Gap("提示词", "提示词生成模型可选（外部/本地千问）", "DONE",
        "prompt/gateway.py build_gateway()", "外部 API / local_qwen / custom 三 provider"),
    Gap("提示词", "输出语言 中文（可切中/英）", "DONE", "models/entities.py PromptParams.output_language"),
    Gap("提示词", "输出格式 结构化JSON+分隔符", "DONE", "prompt/validate.py parse_script_output()"),
    # ---- 接缝缺口（ROUND16 实测发现；两半各自有测试且全绿，接缝没人验）----
    Gap("提示词", "界面路径的结构校验（无效引用不得进入素材生成）", "DONE",
        "demo._run_flow 进素材生成前过 validate_script 闸门（不合法抛异常）",
        "ROUND20 验收 16-P5 实测：无效引用 -> ok=False、素材 0、段 0"),
    Gap("提示词", "剧本形状接缝（顶层 vs 章节级、asset_type vs type）", "DONE",
        "canonicalize_script() 在边界归一 + demo 改读规范形状 + DEMO_SCRIPT 合规",
        "ROUND20 验收 16-P2 实测：规范剧本 -> 素材新建>0、LOCK>0（修前全为 0）"),
    Gap("提示词", "分镜粒度 按镜头（可按场景）", "DONE", "PromptParams.story_granularity"),
    Gap("提示词", "系统提示词模板预置 5 套", "DONE",
        "prompt/inject.py SYSTEM_PROMPT_PRESETS",
        "5 套：default / strict_json / local_qwen / chinese_drama / shot_by_shot"),
    Gap("提示词", "已有素材注入（多章节必须开启）", "DONE", "store/inject.py build_existing_asset_manifest()"),
    Gap("提示词", "存在性判断由程序做（不由 AI）", "DONE", "prompt/match.py match_candidate()", "R1 落地，有静态检查 C5B"),
    Gap("提示词", "名称归一化（去空格/统一小写/统一标点）", "DONE", "prompt/normalize.py normalize_name()"),
    Gap("提示词", "疑似重复标记待人工确认（绝不自动覆盖）", "DONE", "prompt/match.py + store suspects.json"),
    Gap("提示词", "本地模型适配（千问简化约束指令）", "DONE", "prompt/inject.py 本地模型分支"),
    # ROUND17：图七 GF 的「引用次数」与图四 F2B 都依赖「素材→段」反向索引，
    # 而写入它的 record_reference() 全工程零调用（实测跑完 demo 后 references 全空）。
    Gap("素材生成", "引用关系记录（素材被哪些镜头/视频段引用）", "DONE",
        "plan_segments 调 record_segment_asset_references；新增 referencing_segments / "
        "segments_depending_on_asset（支持中程失败与图四 F2B）",
        "ROUND20 验收 17 实测：跑完 demo 得 6 条引用记录；标 FAILED 后只返回该段"),

    # ---------------- 二、素材生成参数（方案第三部分·二） ----------------
    Gap("素材生成", "分辨率 1024x1024（512-2048）", "DELEGATED",
        "融合节点 → MiniMaxH3Director 采样参数", "实际生成由导演台承担"),
    Gap("素材生成", "采样步数 25（15-50）", "DELEGATED", "MiniMaxH3Director.steps"),
    Gap("素材生成", "采样器 DPM++ 2M Karras", "DELEGATED", "MiniMaxH3Director 采样器输入"),
    Gap("素材生成", "CFG 7（5-12）", "DELEGATED", "MiniMaxH3Director.cfg"),
    # ROUND18：下面两条**不是采样参数**，是素材库级约束，不该 DELEGATED 给导演台。
    # 实测两者在配置里都有值，但产品代码里零读取/零判断 —— 配了不用。
    Gap("素材生成", "素材库统一分辨率 / 自动缩放（图一 G3）", "DONE",
        "AssetStore.add_version(unify_resolution=...) 最长边等比缩放",
        "ROUND20 验收 18 实测：3000x1200 -> 1024x410（等比，不变形）"),
    Gap("素材生成", "每类数量上限 per_class_limit=9（图一 G4）", "DONE",
        "AssetStore.create_asset(per_class_limit=...) 在持锁 mutator 里判断",
        "ROUND20 验收 18 实测：上限 9，第 10 个被拒并说明原因"),
    Gap("素材生成", "种子 随机/固定（可复现）", "DONE",
        "bettercomfyui/seed.py + 融合节点 base_seed",
        "跨进程稳定（SHA-256 推导，不用 hash()）"),
    Gap("素材生成", "批量数量 4 / 每类上限 9", "DELEGATED",
        "采样节点批量 + 素材库每类 9 槽约束",
        "方案原文写「每类上限9 节点限制」——该限制本就是执行节点的能力"),

    # ---------------- 三、素材审核参数（方案第三部分·三） ----------------
    Gap("素材审核", "默认审核模式 模型审核（可切人工）", "DONE", "review/asset_review.py"),
    Gap("素材审核", "人工超时 10 分钟（按无人有效响应计）", "DONE", "review/asset_review.py is_timed_out()"),
    Gap("素材审核", "有效操作期间暂停超时；闲置仍超时", "DONE", "review/asset_review.py record_human_activity()"),
    Gap("素材审核", "模型候选上限 3 轮（CNT1）", "DONE", "review/counters.py"),
    Gap("素材审核", "评分阈值 70（可调）", "DONE", "config AssetReviewParams.score_threshold"),
    Gap("素材审核", "评分维度 画质/一致性/合规/构图", "DONE", "AssetReviewParams.score_dimensions"),
    Gap("素材审核", "通知方式 弹窗+声音+QQ/飞书/钉钉", "DONE", "review/notify.py", "失败只 WARN，不阻断"),
    Gap("素材审核", "超时后行为 切模型（可挂起/跳过）", "DONE", "review/asset_review.py on_human_timeout()"),
    Gap("素材审核", "仲裁规则 统一后端时间戳；平局模型胜", "DONE", "review/arbiter.py arbitrate()"),
    Gap("素材审核", "版本规则 新版本必重审、重算 SHA-256", "DONE", "store/assets.py add_version()"),
    Gap("素材审核", "兜底规则 三轮最高分 + FALLBACK_PASS", "DONE", "review/asset_review.py FALLBACK 分支"),

    # ---------------- 四、导演台参数（方案第三部分·四） ----------------
    Gap("导演台", "分段时长 10 秒（5/10/15）", "DELEGATED", "ChainDirector 分段方式 / Director 时间轴"),
    Gap("导演台", "分辨率 1080P（720P/4K）", "DELEGATED", "ChainDirector 分辨率预设"),
    Gap("导演台", "帧率 24fps（24/30/60）", "DELEGATED", "ChainDirector ref_video_fps / Director fps"),
    Gap("导演台", "尾部帧数 22（可调）", "DELEGATED", "ChainDirector 内部 prev_last 接力"),
    Gap("导演台", "首帧第一帧 / 尾帧最后一帧（可手动）", "DELEGATED", "融合·单组打包 first_frame/last_frame"),
    Gap("导演台", "显存档位自动检测（低/中/高）", "DONE",
        "cli.py detect_vram_tier() + _probe_comfyui(base_path)",
        "按实测显存给出 low/mid/high 档位与建议；解析不出则 unknown，不猜"),
    Gap("导演台", "剪枝模型开启", "DELEGATED", "由 checkpoint 文件名（pruned_int8）决定"),
    Gap("导演台", "参考模式 自动/手动", "DELEGATED", "融合节点 family 选择（r2v/i2v）"),
    Gap("导演台", "音画参考（音色/参考视频）接入素材库", "DONE",
        "fusion.locked_ref_media + 融合节点 + AssetMedia 节点",
        "AssetType 扩展 AUDIO/VIDEO；实测 wav->(1,2,44100)、mp4->8 帧；未 LOCK 一律拒绝"),
    Gap("导演台", "显存节省策略全开", "DELEGATED", "ChainDirector 模式=4 旁路 / Director 段间清理显存"),
    Gap("导演台", "高级接缝（二采/SelfLift/SemanticBridge/FaceRefine）能力检测", "DONE",
        "fusion_nodes.check_advanced_modes + 融合·体检",
        "4/4 接缝实测可接；按**选项**报告可用性；权重目录真的去查（含 ultralytics/bbox）"),

    # ---------------- 五、成片封装参数（方案第三部分·五） ----------------
    Gap("成片封装", "音频拼接 自动", "DONE", "pipeline/core.py encode()"),
    Gap("成片封装", "转场 无/淡入淡出", "DONE", "pipeline/core.py encode() fade 分支"),
    Gap("成片封装", "字幕 不加（可启用）", "DONE",
        "pipeline/core.py encode(subtitle_file=...)",
        "真烧字幕；开启但未给文件 -> 显式报错"),
    Gap("成片封装", "水印 不加（可启用）", "DONE",
        "pipeline/core.py encode(watermark_file=..., watermark_position=...)",
        "真叠加，支持四角定位；开启但未给文件 -> 显式报错"),
    Gap("成片封装", "输出格式 MP4/MOV/WebM", "DONE", "EncodeParams.format"),
    Gap("成片封装", "编码 H.264/H.265", "DONE", "EncodeParams.codec"),
    Gap("成片封装", "CRF 18（0-51）", "DONE", "EncodeParams.crf"),
    Gap("成片封装", "封面 第一帧", "DONE", "pipeline/core.py write_output() 封面提取"),

    # ---------------- 六、视频审核参数（方案第三部分·六） ----------------
    Gap("视频审核", "分段级+整片级（R_seg / R_full）", "DONE", "director/review.py + pipeline/core.py"),
    Gap("视频审核", "R1a 四维（画面连贯/角色一致/动作合理/无闪烁）", "DONE", "VideoReviewParams.r1a_dimensions"),
    Gap("视频审核", "R1b 四维（叙事流畅/音画同步/节奏合理/无跳变）", "DONE", "VideoReviewParams.r1b_dimensions"),
    Gap("视频审核", "分段级与整片级阈值可分别设置", "DONE", "threshold_segment / threshold_full"),
    Gap("视频审核", "CNT2/CNT3 各 3 次、分离计数", "DONE", "review/counters.py + director/rework.py"),
    Gap("视频审核", "只重跑不合格段（段级返工）", "DONE", "director/rework.py handle_quality_failure()"),
    Gap("视频审核", "重生成后新版本重新审核", "DONE", "director/review.py StaleSegmentVersion 门禁"),
    Gap("视频审核", "级联标记 + CNT3 独立", "DONE", "director/cascade.py"),
    Gap("视频审核", "最终整片审核只执行一次", "DONE", "pipeline/core.py full_review()", "段级路径调用即抛 ReviewScopeViolation"),
    Gap("视频审核", "整片审核失败策略显式配置", "DONE", "pipeline/core.py apply_full_policy()", "未配置即报错"),

    # ---------------- 七、全局配置（方案第三部分·七） ----------------
    Gap("全局配置", "默认配置 config/default.yaml", "DONE", "config/default.yaml（由 dataclass 生成）"),
    Gap("全局配置", "用户配置覆盖默认（用户>默认）", "DONE", "config.py load_config() 三层优先级"),
    Gap("全局配置", "配置导入导出（JSON）", "DONE", "config.py dump_config()", "导出时清除密钥"),
    Gap("全局配置", "日志级别可调（INFO/DEBUG/…）", "DONE", "GlobalParams.log_level"),
    Gap("全局配置", "记录所有手动操作与自动切换", "DONE", "store/events.py + 各模块 emit"),
    Gap("全局配置", "提示词/评分模型默认配置（用户可覆盖）", "DONE", "PromptParams / VideoReviewParams"),
    Gap("全局配置", "本地提示词模型 千问", "DONE", "prompt/gateway.py local_qwen provider"),
    Gap("全局配置", "全局参数统一注入；单段覆盖必须显式记录", "DONE",
        "models/entities.py SegmentOverrides + director/plan.py",
        "审核类覆盖必填原因；审核器经 threshold_for()/audit_scope_for() 真消费；"
        "边界 N1 取两段更严者"),

    # ---------------- 八、项目管理（方案第三部分·八） ----------------
    Gap("项目管理", "项目根目录 projects/（可改）", "DONE", "GlobalParams.project_root"),
    Gap("项目管理", "项目切换支持", "DONE", "store/projects.py switch()"),
    Gap("项目管理", "项目隔离（素材/章节/进度/输出独立）", "DONE", "store/paths.py R5 越界校验"),
    Gap("项目管理", "素材库累积（只追加不重建）", "DONE", "store/assets.py O_EXCL + 版本目录"),
    Gap("项目管理", "meta.json 加文件锁串行化", "DONE", "store/atomic.py locked_update_json()"),
    Gap("项目管理", "检查点每章节边界落盘", "DONE", "store/checkpoints.py"),
    Gap("项目管理", "段版本落盘（重启不重复建版本）", "DONE",
        "store/segments.py SegmentStore",
        "只追加+同版本同指纹幂等+指纹冲突报错；segments/ 路径此前声明但无人写入"),
    Gap("项目管理", "增量更新 / 已有素材注入 开启", "DONE", "prompt/match.py + store/inject.py"),

    # ---------------- 核心原则里的可执行项 ----------------
    Gap("核心原则", "素材审核前置（未 LOCK 不得进生成）", "DONE",
        "fusion.py locked_asset_ref_images()", "融合层同样执行该门禁"),
    Gap("核心原则", "连贯性主策略：素材库参考图为主，尾帧为辅", "DONE",
        "融合节点 r2v ref_images 来自已 LOCK 素材", "尾帧仅作辅助（ChainDirector 内部）"),
    Gap("核心原则", "人物不在尾部 22 帧时必须由素材库锁身份", "DONE",
        "融合·段计划→导演组：每段都带 ref_images", "不依赖上一帧"),
    Gap("核心原则", "异常分素材/视频两条线（ERR1a/ERR1b）", "DONE", "models/enums.py ErrorClass + 各模块分类"),
    Gap("核心原则", "断点续传恢复到最近检查点（G/P/R）", "DONE", "store/checkpoints.py resolve_resume_target()"),
    Gap("核心原则", "手动添加素材不审核、直接入库", "DONE", "fusion/asset_nodes + nodes.py ManualAsset", "标记 MANUAL_UNREVIEWED"),
    Gap("核心原则", "手动替换素材走当前项目审核模式", "DONE", "nodes.py BetterComfyUIManualAsset 替换分支"),
    Gap("核心原则", "第一阶段只跑通主流程", "DONE", "—"),
    Gap("核心原则", "第二阶段（自动抓剧本/发布）预留不实现", "DONE",
        "bettercomfyui_comfy/phase2.py", "接口保留、UI 不展示"),
)


def counts() -> dict[str, int]:
    out = {"DONE": 0, "DELEGATED": 0, "TODO": 0}
    for g in GAPS:
        out[g.status] = out.get(g.status, 0) + 1
    return out


def report(*, only: str | None = None) -> str:
    """生成人类可读清单。`only='TODO'` 只看缺口。"""
    rows = [g for g in GAPS if only is None or g.status == only]
    lines = [
        "=== 第一阶段覆盖盘点（方案第三部分 + 核心原则） ===",
        f"合计 {len(GAPS)} 条：DONE {counts()['DONE']} / "
        f"DELEGATED {counts()['DELEGATED']} / TODO {counts()['TODO']}",
        "",
    ]
    area = None
    for g in rows:
        if g.area != area:
            area = g.area
            lines.append(f"[{area}]")
        mark = {"DONE": "OK ", "DELEGATED": "委派", "TODO": "缺口"}[g.status]
        lines.append(f"  [{mark}] {g.item}")
        if g.status != "DONE":
            lines.append(f"         位置/说明：{g.where}" + (f" —— {g.note}" if g.note else ""))
    todos = [g for g in GAPS if g.status == "TODO"]
    if todos:
        lines.append("")
        lines.append("待补齐（TODO）：")
        for g in todos:
            lines.append(f"  - [{g.area}] {g.item} —— {g.note or g.where}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
