"""Better ComfyUI 命令行入口。

设计原则：
1. **懒加载**：每个子命令在需要时才 import 对应模块。这样在某个模块尚未就绪时，
   其它子命令仍可正常工作，而不是整个 CLI 直接崩掉。
2. **绝不假成功**：依赖缺失时给出可操作的错误信息，并以非零退出码结束。
   任何「看起来成功但什么都没做」的行为都是缺陷。
3. **`doctor` 是一等公民**：本项目的失败大多是环境性的（ComfyUI 版本、模型缺失、
   节点缺失）。把这些检查做成命令，让「能不能跑」变成可复现的一句话。

用法：
    python -m bettercomfyui.cli doctor
    python -m bettercomfyui.cli project list
    python -m bettercomfyui.cli project create --name "我的短剧"
    python -m bettercomfyui.cli asset list --project <project_id>
    python -m bettercomfyui.cli run --project <project_id> --chapter chapters/CH001.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

#: 仓库根目录 = 本文件的上上级（bettercomfyui/cli.py -> bettercomfyui -> 仓库根）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 期望存在的子模块 -> 负责的契约章节。用于 doctor 与懒加载报错。
EXPECTED_MODULES = [
    ("bettercomfyui.config", "配置加载"),
    ("bettercomfyui.models.enums", "枚举真源"),
    ("bettercomfyui.models.entities", "数据模型"),
    ("bettercomfyui.store.ids", "标识符生成"),
    ("bettercomfyui.store.atomic", "原子写/文件锁"),
    ("bettercomfyui.store.paths", "项目路径与越界校验"),
    ("bettercomfyui.store.events", "事件日志"),
    ("bettercomfyui.store.projects", "项目管理"),
    ("bettercomfyui.store.assets", "素材库"),
    ("bettercomfyui.store.checkpoints", "检查点"),
    ("bettercomfyui.prompt.gateway", "提示词模型网关"),
    ("bettercomfyui.prompt.validate", "剧本输出校验"),
    ("bettercomfyui.prompt.match", "确定性素材匹配"),
    ("bettercomfyui.review.state", "审核状态机"),
    ("bettercomfyui.review.counters", "CNT1/2/3 计数器"),
    ("bettercomfyui.review.commit", "唯一最终提交点"),
    ("bettercomfyui.director.plan", "段计划"),
    ("bettercomfyui.director.cascade", "级联返工"),
    ("bettercomfyui.executors.base", "执行器抽象"),
    ("bettercomfyui.executors.mock", "mock 执行器"),
    ("bettercomfyui.pipeline.core", "拼接/整片审核/交付"),
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_READY = 2
EXIT_NOT_VERIFIED = 3


class CliError(Exception):
    """命令级错误：信息面向使用者，必须可操作。"""


def _err(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def _require(module: str, *, purpose: str):
    """懒加载模块。缺失时给出明确错误，绝不返回占位实现。"""
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name and module.startswith(exc.name):
            raise CliError(
                f"模块 {module} 尚未就绪（{purpose}）。"
                f"该模块可能仍在开发中；请运行 `python -m bettercomfyui.cli doctor` 查看全貌。"
            ) from exc
        raise CliError(f"导入 {module} 时缺少依赖 {exc.name}：{exc}") from exc


def _load_config():
    cfg_mod = _require("bettercomfyui.config", purpose="配置加载")
    cfg = cfg_mod.load_config(repo_root=REPO_ROOT)
    # 图一 SG7 Z4：把 global.log_level 真正应用到根 logger。
    # 此前这个配置项只被校验、从没生效 —— 用户把级别调成 DEBUG 也没用。
    try:
        logging_mod = _require("bettercomfyui.logging_setup", purpose="日志初始化")
        logging_mod.apply_logging(cfg)
    except Exception:  # noqa: BLE001 - 日志没配上不该挡住 CLI 干活
        pass
    return cfg


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

#: 真正的模型权重扩展名。用于把「权重」与「yaml 配置 / 占位文件」区分开。
#: 早期实现只排除 put_* 占位文件，结果把 `models/configs/` 下 11 个 yaml 当成了模型，
#: `doctor` 一度输出「模型权重文件数: 11」，差点得出「有模型可用」的错误结论。
#: 宁可少报，不可多报。
WEIGHT_SUFFIXES = frozenset(
    {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft"}
)


def _probe_comfyui(
    base_path: str | Path | None, endpoint: str | None = None
) -> dict[str, Any]:
    """探测 ComfyUI 实例：路径、版本、custom_nodes、模型权重。

    `base_path` 来自配置 `global.comfyui_base_path` —— **不得硬编码绝对路径**
    （CONTRACTS.md 第 1 节）。未配置时如实报告，不去猜路径。
    """
    result: dict[str, Any] = {
        "found": False,
        "configured": bool(base_path),
        "path": str(base_path) if base_path else None,
        "version": None,
        "custom_nodes": [],
        "model_files": 0,
        "weight_samples": [],
        "notes": [],
    }
    # 图一 SG7 global.comfyui_endpoint：HTTP 地址与安装目录**是两件事**
    # （base_path 找权重，endpoint 提交任务）。这里先探测 HTTP 可达性 ——
    # 修前这个配置项产品代码零读取，用户改了端口不生效。
    result["endpoint"] = endpoint or None
    if endpoint:
        try:
            import urllib.request as _u

            with _u.urlopen(f"{str(endpoint).rstrip('/')}/system_stats", timeout=8) as resp:
                result["http_status"] = int(getattr(resp, "status", 200))
                result["http_reachable"] = True
        except Exception as exc:  # noqa: BLE001
            result["http_reachable"] = False
            result["notes"].append(f"comfyui_endpoint 不可达（{endpoint}）：{type(exc).__name__}: {exc}")
    else:
        result["http_reachable"] = None
    if not base_path:
        result["notes"].append(
            "未配置 global.comfyui_base_path —— 无法探测 ComfyUI；"
            "请在 config/user.yaml 中填写实例根目录"
        )
        return result

    cand = Path(base_path)
    if not cand.exists():
        result["notes"].append(f"配置的 ComfyUI 路径不存在：{cand}")
        return result

    result["found"] = True

    ver_file = cand / "comfyui_version.py"
    if ver_file.exists():
        text = ver_file.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if "__version__" in line and "=" in line:
                result["version"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    cn = cand / "custom_nodes"
    if cn.exists():
        result["custom_nodes"] = sorted(
            p.name
            for p in cn.iterdir()
            if p.is_dir() or (p.is_file() and p.suffix == ".py" and not p.name.endswith(".example"))
        )

    models = cand / "models"
    if models.exists():
        # 只认真正的权重扩展名，并排除 put_* 占位文件。
        weights = [
            p
            for p in models.rglob("*")
            if p.is_file()
            and p.suffix.lower() in WEIGHT_SUFFIXES
            and not p.name.startswith("put_")
        ]
        result["model_files"] = len(weights)
        result["weight_samples"] = [str(p.relative_to(models)) for p in weights[:10]]
    return result


def _probe_ffmpeg() -> dict[str, Any]:
    exe = shutil.which("ffmpeg")
    if exe:
        return {"available": True, "source": exe, "kind": "PATH"}
    try:
        import imageio_ffmpeg  # type: ignore

        return {"available": True, "source": imageio_ffmpeg.get_ffmpeg_exe(), "kind": "imageio-ffmpeg"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "source": None, "kind": None, "error": str(exc)}


def _probe_gpu() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
            result: dict[str, Any] = {
                "available": True, "name": parts[0], "vram": parts[1], "driver": parts[2],
            }
            result.update(detect_vram_tier(parts[1]))
            return result
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}
    return {"available": False, "error": "nvidia-smi 未返回可用数据"}


#: 显存档位阈值（MiB）。方案要求「显存档位自动检测（低/中/高）」。
#: 阈值依据：MiniMax H3 六段生产实测峰值约 22,016 MiB（约 21.5 GiB），
#: 见 H3-Chinese-Director/docs/CAPABILITY_CATALOG.md §4.1。
VRAM_TIERS: tuple[tuple[int, str, str], ...] = (
    (24_000, "high", "充足：可跑 H3 六段生产（实测峰值约 21.5 GiB）"),
    (16_000, "mid", "勉强：需要开启全部显存节省策略（剪枝/tiled/unload），长段可能 OOM"),
    (8_000, "low", "紧张：只能短段 + 全套节省策略；建议降分辨率"),
    (0, "minimal", "不足：无法运行 H3 视频模型，请换 >=24GB 显存的机器"),
)


def _probe_compute_capability(cfg: Any) -> dict:
    """探测「真实 H3 生成」所需的计算设备能力。

    依据 ComfyUI `/system_stats` 的 `devices`：必须存在**非 CPU** 设备，
    且显存 ≥ H3 最低要求（`H3_MIN_VRAM_MIB`，24GiB）。
    探测不到（未配置 endpoint / 服务不可达 / 无 devices）时按**不可用**处理，
    绝不因为「本机有 nvidia-smi 输出」就假定 ComfyUI 能用上它
    （实测：服务以 --cpu 启动，而机器上插着 4GB GTX1050Ti —— 两者都不是能跑 H3 的状态）。
    """
    from bettercomfyui.pipeline.core import evaluate_compute_devices

    endpoint = None
    try:
        from bettercomfyui.executors.comfyui_http import resolve_comfyui_endpoint

        endpoint = resolve_comfyui_endpoint(cfg)
    except Exception:  # noqa: BLE001 - 配置非法时按不可用处理
        endpoint = None
    if not endpoint:
        result = evaluate_compute_devices(None)
        result["reason"] = "未配置可访问的 ComfyUI endpoint，无法确认计算设备 —— 真实生成不可用"
        return result
    try:
        import json as _json
        import urllib.request as _u

        with _u.urlopen(f"{str(endpoint).rstrip('/')}/system_stats", timeout=8) as resp:
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
        return evaluate_compute_devices(payload.get("devices") or [])
    except Exception as exc:  # noqa: BLE001
        result = evaluate_compute_devices(None)
        result["reason"] = (
            f"探测 {endpoint}/system_stats 失败（{type(exc).__name__}: {exc}）"
            "—— 无法确认计算设备，真实生成不可用"
        )
        return result


def detect_vram_tier(vram_text: str) -> dict[str, Any]:
    """把 nvidia-smi 的显存文本解析成档位与建议。

    解析不出数字时返回 ``tier="unknown"`` 并说明原因 —— **不猜档位**。
    猜档位的后果是给用户一个"看起来能用"的错误建议。
    """
    import re

    m = re.search(r"(\d+)", vram_text or "")
    if not m:
        return {"tier": "unknown", "vram_mib": None,
                "tier_note": f"无法从 {vram_text!r} 解析显存容量"}
    mib = int(m.group(1))
    for threshold, tier, note in VRAM_TIERS:
        if mib >= threshold:
            return {"tier": tier, "vram_mib": mib, "tier_note": note}
    return {"tier": "unknown", "vram_mib": mib, "tier_note": "未匹配到任何档位"}


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境与模块体检。这是本项目最重要的一条命令。"""
    report: dict[str, Any] = {"repo_root": str(REPO_ROOT)}

    print("=" * 68)
    print("Better ComfyUI · 环境体检 (doctor)")
    print("=" * 68)
    print(f"仓库根目录: {REPO_ROOT}")

    # --- 模块就绪情况 ---
    print("\n[模块就绪情况]")
    ready, missing = [], []
    for mod, purpose in EXPECTED_MODULES:
        try:
            importlib.import_module(mod)
            ready.append(mod)
            mark = "OK  "
        except Exception as exc:  # noqa: BLE001 - 这里要展示任何失败原因
            missing.append((mod, purpose, f"{type(exc).__name__}: {exc}"))
            mark = "缺失"
        print(f"  [{mark}] {mod:<40} {purpose}")
    report["modules"] = {"ready": ready, "missing": [m[0] for m in missing]}

    # --- 配置 ---
    print("\n[配置]")
    cfg = None
    try:
        cfg = _load_config()
        print(f"  OK   默认配置加载成功；审核模式={cfg.asset_review.default_mode.value}，"
              f"CNT2/CNT3 上限={cfg.video_review.cnt2_limit}/{cfg.video_review.cnt3_limit}")
        if cfg.video_review.full_review_policy is None:
            print("  WARN video_review.full_review_policy 未配置 —— 整片审核未通过时会报错（这是有意设计）")
        report["config"] = "ok"
    except Exception as exc:  # noqa: BLE001
        print(f"  失败 {type(exc).__name__}: {exc}")
        report["config"] = f"error: {exc}"

    # --- ComfyUI ---
    print("\n[ComfyUI 实例]")
    _endpoint = None
    if cfg:
        try:
            from bettercomfyui.executors.comfyui_http import resolve_comfyui_endpoint

            _endpoint = resolve_comfyui_endpoint(cfg)
        except Exception:  # noqa: BLE001 - 配置非法不该让 doctor 崩
            _endpoint = None
    comfy = _probe_comfyui(cfg.global_.comfyui_base_path if cfg else None, _endpoint)
    report["comfyui"] = comfy
    if comfy["found"]:
        print(f"  路径   : {comfy['path']}")
        print(f"  版本   : {comfy['version']}")
        print(f"  自定义节点: {comfy['custom_nodes'] or '（无第三方节点）'}")
        print(f"  模型权重文件数: {comfy['model_files']}")
    if comfy.get("endpoint"):
        reach = comfy.get("http_reachable")
        tag = "OK  " if reach else ("WARN" if reach is False else "INFO")
        print(f"  HTTP 端点: {comfy['endpoint']}  [{tag} {('可达' if reach else '不可达' if reach is False else '未探测')}]")
        if comfy["version"]:
            try:
                major, minor = (int(x) for x in comfy["version"].split(".")[:2])
                if (major, minor) < (0, 30):
                    print(f"  WARN 版本 {comfy['version']} 低于 MiniMaxH3 导演台要求的 >=0.30.0；")
                    print("       真实视频生成路径不可用（upstream 已达 v0.41.0，升级即可解锁）")
            except (ValueError, TypeError):
                pass
        if comfy["model_files"] == 0:
            print("  WARN 没有任何模型权重 -> 无法进行真实出图/出片；只能以 mock 执行器验证编排闭环")
    else:
        print("  未找到 ComfyUI 实例")

    # --- GPU ---
    print("\n[GPU]")
    gpu = _probe_gpu()
    report["gpu"] = gpu
    if gpu["available"]:
        print(f"  {gpu['name']} / {gpu['vram']} / 驱动 {gpu['driver']}")
        print("  提示：本机为开发机；目标机显存 ≥24GB。不要以本机显存推断目标机能力。")
    else:
        print(f"  不可用：{gpu.get('error')}")

    # --- ffmpeg ---
    print("\n[ffmpeg]")
    ff = _probe_ffmpeg()
    report["ffmpeg"] = ff
    if ff["available"]:
        print(f"  可用（{ff['kind']}）：{ff['source']}")
        print("  -> 拼接/封装可进行真实验证")
    else:
        print(f"  不可用：{ff.get('error')}")

    # --- 结论 ---
    print("\n" + "=" * 68)
    print("[结论]")
    if missing:
        print(f"  模块未就绪 {len(missing)} 个，命令可能部分不可用。")
    else:
        print("  全部期望模块就绪。")

    # ⚠️ 版本比较必须按**数字段**做，不能字符串比较：
    # `"0.5" >= "0.30"` 在字符串语义下是 True，但 0.5 < 0.30 在版本语义下应判 False。
    # 这条 bug 真实存在过（cli 与 executor 两处），现在统一走 meets_min_comfyui()。
    from bettercomfyui.executors.comfyui_http import meets_min_comfyui

    # ⚠️ 第二个坑：**只数权重文件个数是不够的**。
    # 实测踩到：往 models/loras 放了 LoRA 后 `model_files` 非零，
    # 于是报出「真实生成：环境看起来具备条件」——
    # 但那只是 LoRA，**没有扩散模型 / 文本编码器 / VAE**，根本出不了片。
    # 「有文件」不等于「能出片」。
    #
    # 判据统一走 `probe_model_kinds()`（唯一真源，插件路由也用同一份）。
    from bettercomfyui.pipeline.core import evaluate_compute_devices, probe_model_kinds

    kinds = probe_model_kinds(Path(comfy["path"]) / "models" if comfy.get("path") else None)
    missing_kinds = kinds["missing"]

    # ⚠️ 第三个坑：**权重齐全 + 版本够，也不等于能出片** —— 还需要显存足够的 GPU。
    # 实测假阳性：本机权重齐全、ComfyUI 0.41，但服务以 --cpu 启动、唯一 GPU 只有
    # 4GB 显存，旧版 doctor 却报「环境看起来具备条件」。真实 H3 生成要求 ≥24GiB。
    compute = _probe_compute_capability(cfg)
    report["compute"] = compute

    real_generation_ok = bool(
        comfy["found"]
        and kinds["available"]
        and meets_min_comfyui(comfy["version"])
        and compute["available"]
    )
    if real_generation_ok:
        print("  真实生成：环境看起来具备条件（仍需实测确认，未实测前标『未验证』）")
        print(f"    计算设备: {compute['device']}（{compute['vram_mib']} MiB）")
        for kind, files in kinds["present"].items():
            print(f"    {kind}: {len(files)} 个")
    else:
        reasons: list[str] = []
        if not comfy["found"]:
            reasons.append("未找到可用的 ComfyUI 实例")
        elif kinds["total"] > 0 and missing_kinds:
            reasons.append(
                f"虽然 models/ 有 {kinds['total']} 个权重文件，但**缺少必需类别**："
                f"{'、'.join(missing_kinds)}（有文件 ≠ 能出片）"
            )
        elif not kinds["available"]:
            reasons.append("models/ 缺少构成一次最小生成所需的权重类别")
        if comfy["found"] and not meets_min_comfyui(comfy["version"]):
            reasons.append(
                f"ComfyUI 版本 {comfy['version'] or '未知'} 低于导演台要求 "
                f"{'0.30.0'}；真实视频生成路径不可用"
            )
        if not compute["available"]:
            reasons.append(compute["reason"])
        print(f"  真实生成：不可用 —— {'；'.join(reasons) if reasons else '前置条件不满足'}")
        print("  纪律：mock 产出必须标注，绝不能当作真实生成通过的证据。")
    report["real_generation_available"] = real_generation_ok
    report["real_generation_missing_kinds"] = missing_kinds

    if args.json:
        out = json.dumps(report, ensure_ascii=False, indent=2)
        print("\n[JSON]")
        print(out)

    return EXIT_NOT_READY if missing else EXIT_OK


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


def cmd_project(args: argparse.Namespace) -> int:
    mod = _require("bettercomfyui.store.projects", purpose="项目管理")
    ProjectStore = mod.ProjectStore
    cfg = _load_config()
    store = ProjectStore(REPO_ROOT / cfg.global_.project_root, cfg)

    if args.action == "list":
        rows = store.list()
        if not rows:
            print("（还没有项目）")
            return EXIT_OK
        print(f"{'project_id':<34} {'显示名':<24} {'创建时间'}")
        for row in rows:
            print(f"{row.get('project_id',''):<34} {row.get('display_name',''):<24} {row.get('created_at','')}")
        return EXIT_OK

    if args.action == "create":
        if not args.name:
            raise CliError("project create 需要 --name")
        info = store.create(args.name)
        print(f"已创建项目 {info.get('project_id')}（{args.name}）")
        return EXIT_OK

    if args.action == "show":
        if not args.project:
            raise CliError("project show 需要 --project")
        info = store.open(args.project)
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK

    if args.action == "delete":
        if not args.project:
            raise CliError("project delete 需要 --project")
        if not args.confirm:
            raise CliError(
                f"删除项目 {args.project} 需要二次确认：加 --confirm。"
                "（方案要求删除需二次确认）"
            )
        store.delete(args.project, confirm_token=args.project)
        print(f"已删除项目 {args.project}")
        return EXIT_OK

    raise CliError(f"未知 action: {args.action}")


# ---------------------------------------------------------------------------
# asset
# ---------------------------------------------------------------------------


def cmd_asset(args: argparse.Namespace) -> int:
    store_mod = _require("bettercomfyui.store.projects", purpose="项目管理")
    assets_mod = _require("bettercomfyui.store.assets", purpose="素材库")
    paths_mod = _require("bettercomfyui.store.paths", purpose="项目路径")
    cfg = _load_config()

    if not args.project:
        raise CliError("asset 子命令需要 --project")
    store = store_mod.ProjectStore(REPO_ROOT / cfg.global_.project_root, cfg)
    store.open(args.project)
    paths = paths_mod.ProjectPaths(root=REPO_ROOT / cfg.global_.project_root, project_id=args.project)
    assets = assets_mod.AssetStore(paths)

    if args.action == "list":
        rows = assets.list_assets()
        if not rows:
            print("（项目素材库为空）")
            return EXIT_OK
        print(f"{'asset_id':<40} {'类型':<10} {'名称':<20} {'版本':<6} {'状态':<18} {'LOCK'}")
        for row in rows:
            entries = row.get("versions") or []
            latest = entries[-1] if entries else {}
            print(
                f"{row.get('asset_id',''):<40} {str(row.get('asset_type','')):<10} "
                f"{str(row.get('name','')):<20} {str(latest.get('version','')):<6} "
                f"{str(latest.get('status','')):<18} {str(latest.get('lock_status',''))}"
            )
        return EXIT_OK

    if args.action == "suspects":
        rows = assets.list_suspects(resolved=False)
        if not rows:
            print("（无疑似重复待确认项）")
            return EXIT_OK
        for row in rows:
            print(f"{row.get('suspect_id')}  {row.get('normalized_name')} -> {row.get('candidates')}")
        return EXIT_OK

    raise CliError(f"未知 action: {args.action}")


# ---------------------------------------------------------------------------
# run（端到端入口，第一阶段以 mock 执行器验证编排闭环）
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    demo = _require("bettercomfyui.demo", purpose="端到端演示流程")
    cfg = _load_config()
    result = demo.run_demo(
        repo_root=REPO_ROOT,
        config=cfg,
        project=args.project,
        chapter=Path(args.chapter) if args.chapter else None,
        executor=args.executor,
        workdir=Path(args.workdir) if args.workdir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok"):
        _err("端到端流程未成功完成，详见上面的输出。")
        return EXIT_ERROR
    if result.get("used_mock"):
        print(
            "\n注意：本次使用 mock 执行器，**输出不是真实生成结果**，"
            "只能证明编排逻辑与版本/审核/计数语义闭环。"
        )
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """配置导入 / 导出（图一 SG7 Z3「配置导入导出支持 JSON」）。

    导出**必然脱敏**：`api_key` / `api_key_env` 被替换成 `***`。
    任务书第 10 节明确要求「配置样例不得写入真实 API 密钥」，
    所以不提供"导出真实密钥"的开关。
    """
    logs = _require("bettercomfyui.logging_setup", purpose="配置导入导出")
    cfg = _load_config()

    if args.action == "export":
        out = Path(args.path) if args.path else (
            REPO_ROOT / "config" / "exported_config.json")
        text = logs.export_config_json(cfg, out)
        print(f"已导出配置到：{out}（{len(text)} 字节，密钥已脱敏为 ***）")
        return EXIT_OK

    if args.action == "import":
        if not args.path:
            _err("import 需要 --path 指定要导入的配置 JSON")
            return EXIT_NOT_READY
        merged = logs.import_config_json(Path(args.path))
        target = REPO_ROOT / "config" / "user.yaml"
        try:
            import yaml  # noqa: PLC0415

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except ImportError:
            _err("导入需要 PyYAML（用于写回 config/user.yaml）。")
            return EXIT_NOT_READY
        print(f"已导入到：{target}（脱敏占位 *** 已丢弃，不会写成字面量密钥）")
        return EXIT_OK

    _err(f"未知的 config action：{args.action}")
    return EXIT_NOT_READY


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bettercomfyui",
        description="Better ComfyUI —— 剧本到成片的自动化流程（第一阶段）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="环境与模块体检")
    d.add_argument("--json", action="store_true", help="额外输出 JSON 报告")
    d.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("project", help="项目管理")
    pr.add_argument("action", choices=["list", "create", "show", "delete"])
    pr.add_argument("--name", help="项目显示名（create 用）")
    pr.add_argument("--project", help="project_id")
    pr.add_argument("--confirm", action="store_true", help="删除的二次确认")
    pr.set_defaults(func=cmd_project)

    a = sub.add_parser("asset", help="素材库")
    a.add_argument("action", choices=["list", "suspects"])
    a.add_argument("--project", help="project_id")
    a.set_defaults(func=cmd_asset)

    r = sub.add_parser("run", help="端到端流程（仅占位媒体路径，无真实生成执行器）")
    r.add_argument("--project", help="project_id；缺省则自动建一个演示项目")
    r.add_argument("--chapter", help="章节剧本 JSON 路径")
    r.add_argument("--executor", default="mock", choices=["mock", "comfyui"],
                   help="mock=占位媒体路径（唯一支持）；comfyui 会显式报错——"
                        "本命令没有真实 ComfyUI 生成执行器，真实出片请在 ComfyUI 里跑"
                        "导演台/融合工作流（S2/S3/W3/00_总流程 GPU），"
                        "绝不把占位媒体标成真实生成")
    r.add_argument("--workdir", help="工作目录（默认使用仓库内 projects/）")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("config", help="配置导入 / 导出（JSON，导出必然脱敏）")
    c.add_argument("action", choices=["export", "import"])
    c.add_argument("--path", help="导出目标 / 导入来源（JSON 路径）")
    c.set_defaults(func=cmd_config)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        _err(str(exc))
        return EXIT_NOT_READY
    except KeyboardInterrupt:
        _err("已中断")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，必须让人看到原因
        _err(f"{type(exc).__name__}: {exc}")
        if getattr(args, "verbose", False):
            raise
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
