"""契约一致性检查脚本（task-6 阶段一第 2 项）。

把《BetterComfyUI完整方案.docx》第七部分的**表格值硬编码为期望集合**，
再去比对实现与枚举真源，任何偏离都是可复现的发现（附 file:line）。

用法：
    python tests/verify/check_contracts.py            # 人类可读报告
    python tests/verify/check_contracts.py --json     # 机器可读（同时写 _contracts_report.json）

退出码：0 = 无 BLOCKER；1 = 存在 BLOCKER 发现。

覆盖状态说明（防止「真空通过」被当成通过）：
    PASS        检查已执行且目标文件存在，未发现问题
    FAIL        检查已执行并发现 BLOCKER/MAJOR 级问题
    UNVERIFIED  目标模块尚未实现，本次**没有**验证任何东西 —— 不得写成通过
    VACUOUS     目标存在但没有可检查的内容（0 个文件/0 条记录），等同未验证
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PKG = REPO_ROOT / "bettercomfyui"


def _make_console_safe() -> None:
    """让脚本在非 UTF-8 控制台（例如 Windows GBK）也能跑完并给出正确退出码。

    `render()` 用 ✔/✘/∅ 等非 GBK 字符；在 cp936 控制台上 `print()` 会抛
    `UnicodeEncodeError`，于是「契约检查没有 BLOCKER」却让验收命令退出 1 ——
    这是审计 R1 用默认控制台复现到的验收基础设施缺陷（PYTHONIOENCODING=utf-8
    时退出码才是 0）。

    处理策略：
    * 输出被重定向（CI/管道/文件）时：改用 UTF-8，保证证据文件内容正确；
    * 真实终端（TTY）时：保持终端现有编码，只把不可编码字符替换掉（不乱码不崩溃）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            isatty = bool(getattr(stream, "isatty", lambda: False)())
            if isatty:
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 非 TextIOWrapper
            pass

# ---------------------------------------------------------------------------
# 期望真源：逐条抄自 文档/BetterComfyUI完整方案.docx 第七部分
# （已提取文本：第七部分 八、版本/来源与降级枚举；五、审核流程标识体系与状态码；
#   六、状态转移规则）。硬编码于此，**不从实现里读**，否则就是自证。
# ---------------------------------------------------------------------------

#: 第七部分 · 八 · LOCK来源
EXP_LOCK_SOURCE = {"HUMAN_PASS", "MODEL_PASS", "FALLBACK_PASS"}
#: 第七部分 · 八 · 段来源
EXP_SEGMENT_SOURCE = {"NORMAL", "REGENERATED", "REVIEW_FALLBACK", "CASCADE_FALLBACK"}
#: 第七部分 · 八 · 审核模式
EXP_REVIEW_MODE = {"HUMAN", "MODEL", "AUTO_SWITCHED"}
#: 第七部分 · 八 · 降级标记
EXP_DEGRADE_FLAG = {"NONE", "CASCADE_DEGRADED", "FALLBACK_DEGRADED", "FULL_REVIEW_DEGRADED"}
#: 第七部分 · 五 · 审核状态码（12 个）
EXP_REVIEW_STATES = {
    "RECEIVED",
    "VALIDATING",
    "SCANNING",
    "CLASSIFYING",
    "AWAITING_REVIEW",
    "TIMEOUT",
    "ARBITRATING",
    "ALLOWED",
    "BLOCKED",
    "FALLBACK",
    "FAILED",
    "ABORTED",
}

#: 第七部分 · 六 · 状态转移规则（当前状态, 事件中文原文, 目标状态）
#: 事件名在实现里允许用英文标识符，因此比对时只比对 (状态, 目标状态) 的**多重集合**。
EXP_TRANSITIONS = [
    ("RECEIVED", "启动审核", "VALIDATING"),
    ("VALIDATING", "确定性校验通过", "SCANNING"),
    ("VALIDATING", "确定性校验失败", "FAILED"),
    ("SCANNING", "扫描完成", "CLASSIFYING"),
    ("SCANNING", "扫描技术失败", "FAILED"),
    ("CLASSIFYING", "评分达到阈值", "ALLOWED"),
    ("CLASSIFYING", "评分未达阈值且仍可重试", "BLOCKED"),
    ("CLASSIFYING", "评分未达阈值且已达到候选上限", "FALLBACK"),
    ("AWAITING_REVIEW", "人工通过", "ALLOWED"),
    ("AWAITING_REVIEW", "人工拒绝", "BLOCKED"),
    ("AWAITING_REVIEW", "超时条件满足", "TIMEOUT"),
    ("TIMEOUT", "自动切换模型启动", "SCANNING"),
    ("TIMEOUT", "迟到人工结果需要与模型结果裁决", "ARBITRATING"),
    ("SCANNING", "模型任务进行中收到迟到人工结果", "ARBITRATING"),
    ("CLASSIFYING", "模型结果已产生且有待裁决人工结果", "ARBITRATING"),
    ("ARBITRATING", "人工结果时间戳早于模型完成时间戳且人工通过", "ALLOWED"),
    ("ARBITRATING", "人工结果时间戳早于模型完成时间戳且人工拒绝", "BLOCKED"),
    ("ARBITRATING", "模型完成时间早于或等于人工结果时间且模型通过", "ALLOWED"),
    ("ARBITRATING", "模型完成时间早于或等于人工结果时间且模型未通过、CNT1未到上限", "BLOCKED"),
    ("ARBITRATING", "模型完成时间早于或等于人工结果时间且模型未通过、CNT1已到上限", "FALLBACK"),
    ("BLOCKED", "优化完成并生成新版本", "VALIDATING"),
    ("FAILED", "技术恢复/重新提交", "RECEIVED"),
]
#: 第七部分 · 六 · 「任意可运行状态 + 用户主动取消 -> ABORTED」
EXP_ABORTABLE_EXCLUDED = {"ALLOWED", "FALLBACK", "FAILED", "ABORTED"}

#: 第七部分 · 七 · 并发安全
EXP_LOCKFILE_FORMAT = ".{文件名}.lock"
EXP_TMP_FORMAT = "{文件名}.tmp"

#: CONTRACTS.md 第 2 节 · 标识符格式
ID_FORMAT_SPECS = {
    "new_project_id": r"^[0-9a-f]{32}$",
    "new_finalization_token": r"^[0-9a-f]{32}$",
    "next_version": r"^v\d+$",
    "chapter_id": r"^CH\d{3}$",
}

FORBIDDEN_IMPORTS = {
    "store": ("prompt", "review", "director", "pipeline"),
    "prompt": ("review", "director", "pipeline"),
    "review": ("prompt", "director", "pipeline"),
    "director": ("pipeline",),
    "models": ("store", "prompt", "review", "director", "pipeline", "executors"),
}


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in str(p)]


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:  # pragma: no cover
        return str(p)


def _finding(file: str, line: int | None, message: str, suggestion: str = "") -> dict:
    return {"file": file, "line": line, "message": message, "suggestion": suggestion}


def _check(id_: str, title: str, severity: str, status: str, coverage: dict, findings: list[dict]) -> dict:
    return {
        "id": id_,
        "title": title,
        "severity": severity,
        "status": status,
        "coverage": coverage,
        "findings": findings,
    }


def _import_optional(dotted: str):
    import importlib

    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        return {"__import_error__": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# C1/C2/C3：枚举与状态转移（真源 = 方案第七部分）
# ---------------------------------------------------------------------------


def check_enums() -> list[dict]:
    checks = []
    enums = _import_optional("bettercomfyui.models.enums")
    if enums is None or isinstance(enums, dict):
        err = "" if enums is None else enums["__import_error__"]
        return [
            _check(
                "C1_ENUMS",
                "枚举取值与方案第七部分八逐项一致",
                "BLOCKER",
                "UNVERIFIED",
                {"module": "bettercomfyui/models/enums.py", "exists": (PKG / "models" / "enums.py").exists(), "files_scanned": 0},
                [_finding("bettercomfyui/models/enums.py", None, f"模块不可用/未实现 {err}", "先交付 models/enums.py")],
            )
        ]

    expectations = [
        ("LockSource", EXP_LOCK_SOURCE),
        ("SegmentSource", EXP_SEGMENT_SOURCE),
        ("ReviewMode", EXP_REVIEW_MODE),
        ("DegradeFlag", EXP_DEGRADE_FLAG),
        ("ReviewState", EXP_REVIEW_STATES),
    ]
    for cls_name, expected in expectations:
        cls = getattr(enums, cls_name, None)
        if cls is None:
            checks.append(
                _check(
                    f"C1_{cls_name.upper()}",
                    f"{cls_name} 取值 == 方案第七部分期望集合",
                    "BLOCKER",
                    "FAIL",
                    {"files_scanned": 1, "expected": len(expected)},
                    [_finding("bettercomfyui/models/enums.py", None, f"缺少枚举类 {cls_name}", "补齐枚举")],
                )
            )
            continue
        actual = {m.value for m in cls}
        findings = []
        if actual != expected:
            findings.append(
                _finding(
                    "bettercomfyui/models/enums.py",
                    _line_of(f"class {cls_name}"),
                    f"{cls_name} 取值与方案不一致：多={sorted(actual - expected)} 缺={sorted(expected - actual)}",
                    "以《方案》第七部分为准修正；如需扩展必须先改 CONTRACTS.md 与 enums.py 并说明理由",
                )
            )
        checks.append(
            _check(
                f"C1_{cls_name.upper()}",
                f"{cls_name} 取值 == 方案第七部分期望集合",
                "BLOCKER",
                "FAIL" if findings else "PASS",
                {"files_scanned": 1, "expected": sorted(expected), "actual": sorted(actual)},
                findings,
            )
        )
    return checks


def check_transitions() -> dict:
    enums = _import_optional("bettercomfyui.models.enums")
    if enums is None or isinstance(enums, dict):
        return _check(
            "C3_TRANSITIONS",
            "状态转移表与方案第七部分六逐行一致",
            "BLOCKER",
            "UNVERIFIED",
            {"files_scanned": 0, "expected_rows": len(EXP_TRANSITIONS)},
            [_finding("bettercomfyui/models/enums.py", None, "REVIEW_TRANSITIONS 不可用", "先交付 enums.py")],
        )

    table = getattr(enums, "REVIEW_TRANSITIONS", None)
    if not isinstance(table, dict):
        return _check(
            "C3_TRANSITIONS",
            "状态转移表与方案第七部分六逐行一致",
            "BLOCKER",
            "FAIL",
            {"files_scanned": 1, "expected_rows": len(EXP_TRANSITIONS)},
            [_finding("bettercomfyui/models/enums.py", _line_of("REVIEW_TRANSITIONS"), "缺少 REVIEW_TRANSITIONS", "补齐状态转移表")],
        )

    def norm(x):
        return getattr(x, "value", x)

    exp_pairs = [(s, t) for s, _e, t in EXP_TRANSITIONS]
    act_pairs = [(norm(k[0]), norm(v)) for k, v in table.items()]

    findings = []
    if len(act_pairs) != len(exp_pairs):
        findings.append(
            _finding(
                "bettercomfyui/models/enums.py",
                _line_of("REVIEW_TRANSITIONS"),
                f"转移条数不一致：方案 {len(exp_pairs)} 行，实现 {len(act_pairs)} 行",
                "逐行比对方案第七部分六的表格；缺失或多出的转移都必须显式说明",
            )
        )
    from collections import Counter

    ce, ca = Counter(exp_pairs), Counter(act_pairs)
    for pair, n in (ce - ca).items():
        findings.append(
            _finding(
                "bettercomfyui/models/enums.py",
                _line_of("REVIEW_TRANSITIONS"),
                f"缺少方案要求的转移：{pair[0]} -> {pair[1]}（应出现 {n} 次）",
                "按方案第七部分六补上该行",
            )
        )
    for pair, n in (ca - ce).items():
        findings.append(
            _finding(
                "bettercomfyui/models/enums.py",
                _line_of("REVIEW_TRANSITIONS"),
                f"实现多出方案未定义的转移：{pair[0]} -> {pair[1]}（出现 {n} 次）",
                "删除或先在 CONTRACTS.md 说明理由",
            )
        )

    # 可取消状态 = 全部状态 - 终态集合（方案：任意可运行状态 + 用户主动取消 -> ABORTED）
    abortable = getattr(enums, "REVIEW_ABORTABLE_STATES", None)
    if abortable is None:
        findings.append(
            _finding("bettercomfyui/models/enums.py", None, "缺少 REVIEW_ABORTABLE_STATES", "补上可取消状态集合")
        )
    else:
        actual_abort = {norm(x) for x in abortable}
        expected_abort = EXP_REVIEW_STATES - EXP_ABORTABLE_EXCLUDED
        if actual_abort != expected_abort:
            findings.append(
                _finding(
                    "bettercomfyui/models/enums.py",
                    _line_of("REVIEW_ABORTABLE_STATES"),
                    f"可取消状态集合不一致：多={sorted(actual_abort - expected_abort)} "
                    f"缺={sorted(expected_abort - actual_abort)}",
                    "方案第七部分六：任意可运行状态可取消；ALLOWED/FALLBACK/FAILED/ABORTED 不在此列",
                )
            )

    return _check(
        "C3_TRANSITIONS",
        "状态转移表与方案第七部分六逐行一致",
        "BLOCKER",
        "FAIL" if findings else "PASS",
        {
            "files_scanned": 1,
            "expected_rows": len(exp_pairs),
            "actual_rows": len(act_pairs),
            "note": "事件名中文/英文映射无法自动比对，仅比对(状态,目标状态)多重集合与条数",
        },
        findings,
    )


def _line_of(token: str, path: Path | None = None) -> int | None:
    p = path or (PKG / "models" / "enums.py")
    if not p.exists():
        return None
    for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if token in line:
            return i
    return None


# ---------------------------------------------------------------------------
# C4：重复枚举定义
# ---------------------------------------------------------------------------


def check_duplicate_enums() -> dict:
    findings = []
    files = _py_files(PKG)
    enums_path = (PKG / "models" / "enums.py").resolve()
    scanned = 0
    for p in files:
        if p.resolve() == enums_path:
            continue
        scanned += 1
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            findings.append(_finding(_rel(p), exc.lineno, f"语法错误，无法解析：{exc.msg}", "修复语法"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                if any(b in ("Enum", "IntEnum", "StrEnum", "_StrEnum") or b.endswith("Enum") for b in bases):
                    findings.append(
                        _finding(
                            _rel(p),
                            node.lineno,
                            f"模块内自行定义了枚举 {node.name}({', '.join(bases)})",
                            "枚举真源只有 bettercomfyui/models/enums.py；如需扩展先改该文件并同步契约",
                        )
                    )
    status = "VACUOUS" if scanned == 0 else ("FAIL" if findings else "PASS")
    return _check(
        "C4_NO_DUPLICATE_ENUMS",
        "除 models/enums.py 外不得自行定义枚举",
        "BLOCKER",
        status,
        {"files_scanned": scanned, "root": "bettercomfyui/"},
        findings,
    )


# ---------------------------------------------------------------------------
# C5：禁止的跨层 import（依赖方向单向）
# ---------------------------------------------------------------------------


def check_forbidden_imports() -> list[dict]:
    checks = []
    findings = []
    scanned = 0
    for p in _py_files(PKG):
        rel = p.relative_to(PKG).parts
        layer = rel[0] if len(rel) > 1 else ""
        if layer not in FORBIDDEN_IMPORTS:
            continue
        scanned += 1
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module is None:
                    continue
                if node.module:
                    mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for m in mods:
                for forbidden in FORBIDDEN_IMPORTS[layer]:
                    if m == f"bettercomfyui.{forbidden}" or m.startswith(f"bettercomfyui.{forbidden}."):
                        findings.append(
                            _finding(
                                _rel(p),
                                node.lineno,
                                f"{layer}/ 不得 import {forbidden}/（依赖方向单向）",
                                f"删除该 import；若确需数据，改为参数注入或下沉到 store/",
                            )
                        )
    checks.append(
        _check(
            "C5_FORBIDDEN_IMPORTS",
            "禁止的跨层 import（store/prompt/review/director/models）",
            "BLOCKER",
            "VACUOUS" if scanned == 0 else ("FAIL" if findings else "PASS"),
            {"files_scanned": scanned, "rules": {k: list(v) for k, v in FORBIDDEN_IMPORTS.items()}},
            findings,
        )
    )

    # R1：存在性判断必须纯程序 —— prompt/match.py 不得依赖模型网关
    match_py = PKG / "prompt" / "match.py"
    m_findings = []
    if match_py.exists():
        text = match_py.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bgateway\b|\bPromptModelGateway\b|build_gateway", line) and not line.strip().startswith("#"):
                m_findings.append(
                    _finding(_rel(match_py), i, f"R1 违背：match.py 出现 AI 网关依赖：{line.strip()}", "存在性判断必须纯程序确定性匹配")
                )
    checks.append(
        _check(
            "C5B_MATCH_NO_AI",
            "R1：prompt/match.py 不得依赖 AI 网关",
            "BLOCKER",
            "UNVERIFIED" if not match_py.exists() else ("FAIL" if m_findings else "PASS"),
            {"files_scanned": 1 if match_py.exists() else 0, "target": "bettercomfyui/prompt/match.py"},
            m_findings,
        )
    )
    return checks


# ---------------------------------------------------------------------------
# C6/C7：R2 静态检查 —— 原地覆盖素材 / meta.json 直接写
# ---------------------------------------------------------------------------

_OVERWRITE_PATTERNS = [
    (r"""open\s*\([^)]*,\s*["']w""", "BLOCKER", "以 'w' 模式打开文件（可能原地覆盖）"),
    (r"\.write_text\s*\(", "MAJOR", "write_text 会原地覆盖目标文件"),
    (r"\.unlink\s*\(|\.rmtree\s*\(|os\.remove\s*\(", "INFO", "删除文件（若目标是既有素材版本则违反 R2，需人工确认）"),
    (r"shutil\.(copy|copyfile|move)\s*\(", "MAJOR", "拷贝/移动可能覆盖既有素材"),
    (r"os\.replace\s*\(", "INFO", "os.replace 是原子写的正确做法，但目标不得是既有素材版本文件"),
]
_ALLOWLIST_OVERWRITE = {"bettercomfyui/store/atomic.py", "bettercomfyui/store/lockfile.py"}
#: 已知合法的删除/替换点：(相对路径, 函数名) -> 理由
#: 这些是「清理自己刚创建的文件」或「契约第 4 节定义的 delete 语义」，不是 R2 违背。
_EXEMPT_DELETION = {
    ("bettercomfyui/store/assets.py", "_copy_exclusive"): "O_EXCL 创建失败后清理自己刚建的文件",
    ("bettercomfyui/store/projects.py", "delete"): "契约第 4 节 ProjectStore.delete 的既定语义（含路径隔离校验）",
}


def _func_at(lines: list[str], idx: int) -> str:
    """向上找出 idx 行所属的函数名（缩进 <= 4 的 def）。"""
    for j in range(idx, -1, -1):
        m = re.match(r"^(\s*)def\s+(\w+)", lines[j])
        if m and len(m.group(1)) <= 4:
            return m.group(2)
    return ""


def _func_span(lines: list[str], idx: int) -> tuple[int, int]:
    """返回 idx 行所属函数的 [起, 止) 行号（顶格/四空格缩进的 def 边界）。"""
    start = None
    for j in range(idx, -1, -1):
        m = re.match(r"^(\s*)def\s+\w+", lines[j])
        if m and len(m.group(1)) <= 4:
            start = j
            break
    if start is None:
        return 0, len(lines)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(\s*)def\s+\w+", lines[j]) and len(re.match(r"^(\s*)", lines[j]).group(1)) <= 4:
            end = j
            break
        if re.match(r"^class\s+\w+", lines[j]):
            end = j
            break
    return start, end


def check_inplace_overwrite() -> dict:
    """R2 静态检查：素材**版本**不得原地覆盖/删除。

    Lead 复核裁定（2026-10-16）：
      * `assets.py::_copy_new_file` 的 `os.unlink` 是「O_EXCL 创建后写入失败的回滚」——
        目标在本次调用前必然不存在（O_EXCL 保证），不违反 R2，反而是 R2 最严格实现。
        故：函数体内出现 `O_EXCL` 守卫时，该函数内的删除操作一律豁免。
      * `projects.py::delete` 是《方案》明确要求的项目声明式删除，且有三道路径/运行态守卫，
        不属于「素材版本原地覆盖」。故：`ProjectStore.delete` 整体排除在 R2 语义之外。
      * 删除类命中的默认级别为 INFO（需人工确认），只有明确的覆盖式写入才是 BLOCKER/MAJOR。
    """
    findings = []
    scanned = 0
    asset_ctx = re.compile(r"version|asset|素材|file_path|target|dest", re.IGNORECASE)
    for p in _py_files(PKG):
        rel = _rel(p)
        in_store_atomic = rel in _ALLOWLIST_OVERWRITE
        scanned += 1
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            fname = _func_at(lines, i - 1)
            fstart, fend = _func_span(lines, i - 1)
            fbody = "\n".join(lines[fstart:fend])
            for pattern, sev, why in _OVERWRITE_PATTERNS:
                if not re.search(pattern, line):
                    continue
                if in_store_atomic and pattern.startswith(r"os\.replace"):
                    continue  # 原子写的唯一合法实现位置
                if in_store_atomic and pattern.startswith(r"open\s*\("):
                    if ".tmp" in line or "tmp" in line:
                        continue
                if sev == "INFO":
                    # 已知合法的清理/删除点（附裁定理由，避免下次又被当成噪音）
                    if (rel, fname) in _EXEMPT_DELETION:
                        continue
                    if "O_EXCL" in fbody:
                        continue  # O_EXCL 守卫：目标路径此前必然不存在，删除的是本次自己的半成品
                if not asset_ctx.search(line):
                    continue
                findings.append(
                    _finding(
                        rel,
                        i,
                        f"[{sev}] {why}：{stripped}"
                        + (f"（函数 {fname}）" if fname else ""),
                        "R2：素材只追加；新内容一律新版本目录，用 store.atomic.atomic_write_json",
                    )
                )
    hard = [f for f in findings if not f["message"].startswith("[INFO]")]
    status = "VACUOUS" if scanned == 0 else ("FAIL" if hard else "PASS")
    return _check(
        "C6_R2_NO_INPLACE_OVERWRITE",
        "R2 静态检查：不得原地覆盖素材文件",
        "BLOCKER",
        status,
        {
            "files_scanned": scanned,
            "allowlist": sorted(_ALLOWLIST_OVERWRITE),
            "note": "启发式：命中含 version/asset/file_path 等上下文的写操作；需人工确认",
        },
        findings,
    )


def check_meta_json_writes() -> dict:
    findings = []
    scanned = 0
    for p in _py_files(PKG):
        rel = _rel(p)
        if rel in _ALLOWLIST_OVERWRITE:
            continue
        scanned += 1
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if "meta.json" not in line:
                continue
            window = "\n".join(lines[max(0, i - 4) : i + 4])
            if re.search(r"json\.dump\s*\(", window) and not re.search(r"locked_update_json|atomic_write_json", window):
                findings.append(
                    _finding(
                        rel,
                        i,
                        f"meta.json 直接写（未经 locked_update_json / atomic_write_json）：{line.strip()}",
                        "CONTRACTS.md 第 3 节：meta.json 的所有更新必须走 locked_update_json",
                    )
                )
    return _check(
        "C7_META_JSON_LOCKED",
        "meta.json 更新必须走 locked_update_json",
        "BLOCKER",
        "VACUOUS" if scanned == 0 else ("FAIL" if findings else "PASS"),
        {"files_scanned": scanned},
        findings,
    )


# ---------------------------------------------------------------------------
# C8/C9/C10：绝对路径、静默失败、mock 回退
# ---------------------------------------------------------------------------


def check_absolute_paths() -> dict:
    findings = []
    scanned = 0
    pat = re.compile(r"""["'](?:[A-Za-z]:\\\\|[A-Za-z]:\\|/home/|/Users/|/mnt/)""")
    for p in _py_files(PKG):
        scanned += 1
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or "example" in s.lower() or "http" in s.lower():
                continue
            if pat.search(line):
                findings.append(
                    _finding(_rel(p), i, f"硬编码绝对路径：{s}", "CONTRACTS.md 第 1 节：一律经由 Config 解析，不得硬编码绝对路径")
                )
    return _check(
        "C8_NO_ABSOLUTE_PATHS",
        "不得硬编码绝对路径",
        "MAJOR",
        "VACUOUS" if scanned == 0 else ("FAIL" if findings else "PASS"),
        {"files_scanned": scanned},
        findings,
    )


#: 文件系统清理类异常：捕获后 pass/continue 属于 best-effort 清理，
#: 不改变返回值语义，按 Lead 裁定（2026-10-16）降为 INFO。
_FS_CLEANUP_EXC = {
    "OSError",
    "FileNotFoundError",
    "PermissionError",
    "FileExistsError",
    "IsADirectoryError",
    "NotADirectoryError",
    "WindowsError",
}


def check_silent_failure() -> dict:
    """不得静默吞异常（假成功温床）。

    分级规则（Lead 复核裁定 2026-10-16，避免真问题与噪音混在一个计数里）：
      * **INFO** — 文件系统 best-effort 清理（`except OSError: pass`），
        典型位置 `store/atomic.py`、`store/lockfile.py` 的临时文件/锁释放；
        强行要求打日志会在锁实现里引入重入风险。
      * **MAJOR** — 领域异常被吞后返回默认值，尤其是 `except StoreError: -> return None`：
        `resolve_lock()` 返回 None 表示「该版本不可用于生成」，是授权门禁；
        若把「读取失败/文件损坏」也变成 None，调用方无法区分「未审核」与「读不出来」。
        必须至少写 WARN 日志 + 保留 ErrorClass。
    """
    findings = []
    scanned = 0
    for p in _py_files(PKG):
        scanned += 1
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if not re.match(r"except\b.*:\s*$", s) and not re.match(r"except\b.*:\s*(pass|continue)\b", s):
                continue
            if s.startswith("#"):
                continue
            tail = lines[i : i + 4]
            body = [t.strip() for t in tail[1:] if t.strip()]
            if not body:
                continue
            caught = ""
            m = re.match(r"except\s*([\w.,()\s]+?)(?:\s+as\s+\w+)?\s*:", s)
            if m:
                caught = m.group(1)
            caught_names = {x.strip() for x in re.split(r"[,\s()]+", caught) if x.strip()}
            is_cleanup = bool(caught_names & _FS_CLEANUP_EXC) and all(
                re.match(r"(pass|continue)\s*$", b) for b in body[:1]
            )

            if any(re.match(r"(pass|continue)\s*$", b) for b in body[:1]) or re.search(
                r":\s*(pass|continue)\s*$", s
            ):
                sev = "INFO" if is_cleanup else "MAJOR"
                note = (
                    "best-effort 清理（临时文件/锁释放），不改变返回值语义 —— 已按裁定降为 INFO"
                    if is_cleanup
                    else "静默吞异常：任务书第 7 节要求不得静默失败；至少写 WARN 日志或按 ErrorClass 显式分类"
                )
                findings.append(
                    _finding(_rel(p), i + 1, f"[{sev}] {('清理类' if is_cleanup else '静默吞异常')}：{s}", note)
                )
            elif any(re.match(r"return\s+(None|True|False|\{\}|\[\])\s*$", b) for b in body[:1]):
                # Lead 裁定（第三轮）：文件系统 best-effort 清理/探测函数里
                # 「捕获 FS 异常 -> 返回保守失败值」不算静默失败（失败方向是闭合的）。
                # 与之相对，`resolve_lock()` 这类**授权门禁**返回 None 必须保留 MAJOR：
                # 调用方无法区分「未审核」与「读不出来」就是假失败/假成功温床。
                fname = _func_at(lines, i)
                best_effort_func = bool(
                    re.search(r"(release|cleanup|close|dispose|rollback|unlock|_try|try_|remove|unlink|fsync|_probe)", fname, re.I)
                )
                fs_exc = bool(caught_names & _FS_CLEANUP_EXC)
                is_best_effort = fs_exc and best_effort_func
                sev = "INFO" if is_best_effort else "MAJOR"
                note = (
                    f"best-effort 清理/探测（函数 {fname}，捕获 {sorted(caught_names)} 后返回保守失败值）—— 已按裁定降为 INFO"
                    if is_best_effort
                    else "若该返回值是授权门禁（如 resolve_lock 的 None），必须写 WARN 日志 + 保留 "
                    "ErrorClass，让调用方能区分「未审核」与「读不出来」"
                )
                findings.append(
                    _finding(
                        _rel(p),
                        i + 1,
                        f"[{sev}] 异常后返回默认值（可能是假失败/假成功）：{s} -> {body[0]}"
                        + (f"（函数 {fname}）" if fname else ""),
                        note,
                    )
                )
    hard = [f for f in findings if not f["message"].startswith("[INFO]")]
    status = "VACUOUS" if scanned == 0 else ("FAIL" if hard else "PASS")
    return _check(
        "C9_NO_SILENT_FAILURE",
        "不得静默吞异常（假成功温床；清理类为 INFO，领域异常吞掉为 MAJOR）",
        "MAJOR",
        status,
        {
            "files_scanned": scanned,
            "info_cleanup": sum(1 for f in findings if f["message"].startswith("[INFO]")),
            "note": "INFO = 文件系统 best-effort 清理（Lead 裁定 2026-10-16）；MAJOR = 领域异常被吞后返回默认值",
        },
        findings,
    )


def check_mock_fallback() -> dict:
    findings = []
    scanned = 0
    for p in _py_files(PKG):
        if p.name == "mock.py" and p.parent.name == "executors":
            continue
        scanned += 1
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if "MockExecutor(" not in line:
                continue
            window = lines[max(0, i - 6) : i + 2]
            if any(re.match(r"\s*except\b", w) for w in window):
                findings.append(
                    _finding(
                        _rel(p),
                        i + 1,
                        f"疑似「异常 -> 静默回退到 MockExecutor」：{line.strip()}",
                        "禁止静默回退到 mock（契约第 5 节）；mock 只能由显式配置选择",
                    )
                )
    return _check(
        "C10_NO_SILENT_MOCK_FALLBACK",
        "不得静默回退到 mock",
        "BLOCKER",
        "VACUOUS" if scanned == 0 else ("FAIL" if findings else "PASS"),
        {"files_scanned": scanned},
        findings,
    )


# ---------------------------------------------------------------------------
# C11/C12：标识符格式与 canonical_json（运行时检查）
# ---------------------------------------------------------------------------


def check_id_formats() -> dict:
    ids = _import_optional("bettercomfyui.store.ids")
    if ids is None or isinstance(ids, dict):
        return _check(
            "C11_ID_FORMATS",
            "标识符格式 == CONTRACTS.md 第 2 节",
            "MAJOR",
            "UNVERIFIED",
            {"files_scanned": 0, "module": "bettercomfyui/store/ids.py"},
            [_finding("bettercomfyui/store/ids.py", None, "ids.py 尚未实现，未验证任何标识符格式", "先交付 store/ids.py")],
        )

    findings = []
    checked = 0
    for fn_name, pattern in ID_FORMAT_SPECS.items():
        fn = getattr(ids, fn_name, None)
        if not callable(fn):
            continue
        try:
            if fn_name == "next_version":
                val = fn("v1")
            elif fn_name == "chapter_id":
                val = fn(1)
            else:
                val = fn()
        except TypeError as exc:
            findings.append(
                _finding(
                    "bettercomfyui/store/ids.py",
                    _line_of(f"def {fn_name}", PKG / "store" / "ids.py"),
                    f"{fn_name} 调用签名与契约不符（TypeError: {exc}）",
                    "CONTRACTS.md 第 2 节给出的签名可能与实现不一致，需在契约中明确参数",
                )
            )
            continue
        checked += 1
        if not re.match(pattern, str(val)):
            findings.append(
                _finding(
                    "bettercomfyui/store/ids.py",
                    _line_of(f"def {fn_name}", PKG / "store" / "ids.py"),
                    f"{fn_name}() 返回 {val!r}，不匹配契约格式 {pattern}",
                    "按 CONTRACTS.md 第 2 节修正格式",
                )
            )

    # 需要参数但格式明确的：按契约格式校验
    spec_calls = [
        ("lock_id", ("ASSET_1", "v1"), r"^LOCK_ASSET_1_v1$"),
        ("audit_id", ("P_CH1_SEG1", "v1"), r"^AUDIT_P_CH1_SEG1_v1$"),
        ("arb_id", ("H1", "M1"), r"^ARB_H1_M1$"),
        ("abort_id", ("MOD_1",), r"^ABORT_MOD_1$"),
        ("cnt_id", ("P_CH1_SEG1", "CNT2"), r"^CNT_P_CH1_SEG1_CNT2$"),
    ]
    for fn_name, args, pattern in spec_calls:
        fn = getattr(ids, fn_name, None)
        if not callable(fn):
            continue
        try:
            val = fn(*args)
        except TypeError as exc:
            findings.append(
                _finding(
                    "bettercomfyui/store/ids.py",
                    _line_of(f"def {fn_name}", PKG / "store" / "ids.py"),
                    f"{fn_name} 调用签名与预期不符（TypeError: {exc}）",
                    "契约第 2 节未给出完整签名，需在 CONTRACTS.md 补齐",
                )
            )
            continue
        checked += 1
        if not re.match(pattern, str(val)):
            findings.append(
                _finding(
                    "bettercomfyui/store/ids.py",
                    _line_of(f"def {fn_name}", PKG / "store" / "ids.py"),
                    f"{fn_name}{args} 返回 {val!r}，期望匹配 {pattern}",
                    "按 CONTRACTS.md 第 2 节修正",
                )
            )

    status = "VACUOUS" if checked == 0 else ("FAIL" if findings else "PASS")
    return _check(
        "C11_ID_FORMATS",
        "标识符格式 == CONTRACTS.md 第 2 节",
        "MAJOR",
        status,
        {"files_scanned": 1, "functions_checked": checked},
        findings,
    )


def check_runtime_atomic() -> dict:
    atomic = _import_optional("bettercomfyui.store.atomic")
    if atomic is None or isinstance(atomic, dict):
        return _check(
            "C12_ATOMIC_JSON",
            "canonical_json 确定性 / NaN 拒绝 / 中文不转义",
            "MAJOR",
            "UNVERIFIED",
            {"files_scanned": 0, "module": "bettercomfyui/store/atomic.py"},
            [_finding("bettercomfyui/store/atomic.py", None, "atomic.py 尚未实现，未验证", "先交付 store/atomic.py")],
        )
    fn = getattr(atomic, "canonical_json", None)
    if not callable(fn):
        return _check(
            "C12_ATOMIC_JSON",
            "canonical_json 确定性 / NaN 拒绝 / 中文不转义",
            "MAJOR",
            "FAIL",
            {"files_scanned": 1},
            [_finding("bettercomfyui/store/atomic.py", None, "缺少 canonical_json", "按契约第 3 节实现")],
        )

    findings = []
    a = fn({"b": 1, "a": 2, "中文": "值"})
    b = fn({"a": 2, "中文": "值", "b": 1})
    if a != b:
        findings.append(_finding("bettercomfyui/store/atomic.py", None, f"canonical_json 对键序不确定：{a!r} != {b!r}", "sort_keys=True"))
    if "\\u" in a:
        findings.append(_finding("bettercomfyui/store/atomic.py", None, f"中文被转义（ensure_ascii 应为 False）：{a!r}", "ensure_ascii=False"))
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            out = fn({"x": bad})
        except Exception:  # noqa: BLE001 - 期望抛错
            continue
        findings.append(_finding("bettercomfyui/store/atomic.py", None, f"canonical_json 接受非法浮点 {bad!r} -> {out!r}", "禁止 NaN/Inf（契约第 3 节）"))
    return _check(
        "C12_ATOMIC_JSON",
        "canonical_json 确定性 / NaN 拒绝 / 中文不转义",
        "MAJOR",
        "FAIL" if findings else "PASS",
        {"files_scanned": 1},
        findings,
    )


# ---------------------------------------------------------------------------
# C13：语义一致性（enums 内部）
# ---------------------------------------------------------------------------


def check_semantic_consistency() -> dict:
    """枚举语义自洽性。

    规则（Lead 复核裁定 2026-10-16）：**终态不得有「质量重试」出边**；
    `FAILED -> RECEIVED`（事件「技术恢复/重新提交」）是《方案》第七部分六
    **表格最后一行明确写出的**技术恢复边，属于允许例外，不得报为问题。
    原先的「终态不得有任何出边」规则与规格真源冲突，已废弃。
    """
    enums = _import_optional("bettercomfyui.models.enums")
    if enums is None or isinstance(enums, dict):
        return _check("C13_SEMANTICS", "枚举语义自洽（终态不得有质量重试出边）", "MINOR", "UNVERIFIED", {"files_scanned": 0}, [])
    findings = []
    states = getattr(enums, "ReviewState", None)
    table = getattr(enums, "REVIEW_TRANSITIONS", {})
    allowed_exception = ("FAILED", "RECEIVED")  # 方案第七部分六 最后一行：技术恢复/重新提交
    if states is not None and isinstance(table, dict):
        for st in states:
            if not getattr(st, "is_terminal", False):
                continue
            for k, v in table.items():
                src = getattr(k[0], "value", k[0])
                dst = getattr(v, "value", v)
                if src != st.value:
                    continue
                if (src, dst) == allowed_exception:
                    continue  # 方案明确允许的技术恢复边
                findings.append(
                    _finding(
                        "bettercomfyui/models/enums.py",
                        _line_of("is_terminal"),
                        f"{st.value} 被声明为终态（is_terminal=True），却有质量重试出边 {src} -> {dst}",
                        "终态不得再走质量重试；若确需该边，请在注释中引用方案条目并调整 is_terminal 语义",
                    )
                )
    return _check(
        "C13_SEMANTICS",
        "枚举语义自洽（终态不得有质量重试出边；FAILED->RECEIVED 为方案允许的技术恢复例外）",
        "MINOR",
        "FAIL" if findings else "PASS",
        {
            "files_scanned": 1,
            "allowed_exception": f"{allowed_exception[0]} -> {allowed_exception[1]}（方案第七部分六 末行「技术恢复/重新提交」）",
        },
        findings,
    )


# ---------------------------------------------------------------------------
# C14：测试里的固定 sleep（flaky 风险）
# ---------------------------------------------------------------------------


def check_test_sleeps() -> dict:
    tests_root = REPO_ROOT / "tests"
    findings = []
    scanned = 0
    for p in _py_files(tests_root):
        if "verify" in p.parts:
            continue
        scanned += 1
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"\btime\.sleep\s*\(\s*(\d*\.?\d+)\s*\)", line):
                m = re.search(r"\btime\.sleep\s*\(\s*(\d*\.?\d+)\s*\)", line)
                if m and float(m.group(1)) >= 0.5:
                    findings.append(
                        _finding(_rel(p), i, f"固定 sleep {m.group(1)}s（并发/超时测试易 flaky）：{line.strip()}", "改用事件/条件轮询或假时钟")
                    )
    return _check(
        "C14_TEST_SLEEPS",
        "测试中避免长固定 sleep（flaky 风险）",
        "MINOR",
        "VACUOUS" if scanned == 0 else ("FAIL" if findings else "PASS"),
        {"files_scanned": scanned},
        findings,
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _package_revision() -> str:
    """整个 bettercomfyui/ 的内容摘要 —— 把报告钉在某个代码修订上。

    队友在并行写代码，没有这个摘要，findings 里的行号会随时失效（不可复现）。
    """
    h = __import__("hashlib").sha256()
    for p in _py_files(PKG):
        h.update(_rel(p).encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def run_all() -> dict:
    checks: list[dict] = []
    checks.extend(check_enums())
    checks.append(check_transitions())
    checks.append(check_duplicate_enums())
    checks.extend(check_forbidden_imports())
    checks.append(check_inplace_overwrite())
    checks.append(check_meta_json_writes())
    checks.append(check_absolute_paths())
    checks.append(check_silent_failure())
    checks.append(check_mock_fallback())
    checks.append(check_id_formats())
    checks.append(check_runtime_atomic())
    checks.append(check_semantic_consistency())
    checks.append(check_test_sleeps())

    totals = {"BLOCKER": 0, "MAJOR": 0, "MINOR": 0, "INFO": 0}
    statuses = {"PASS": 0, "FAIL": 0, "UNVERIFIED": 0, "VACUOUS": 0}
    for c in checks:
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
        for f in c["findings"]:
            sev = c["severity"]
            m = re.match(r"\[(BLOCKER|MAJOR|MINOR|INFO)\]", f["message"])
            if m:
                sev = m.group(1)
            totals[sev] = totals.get(sev, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "repo_root": str(REPO_ROOT),
        "package_revision": _package_revision(),
        "totals": totals,
        "statuses": statuses,
        "checks": checks,
    }


def render(report: dict) -> str:
    out = []
    out.append("=" * 78)
    out.append("Better ComfyUI 契约一致性检查报告")
    out.append(f"生成时间：{report['generated_at']}")
    out.append(f"代码修订摘要（bettercomfyui/ sha256 前缀16）：{report.get('package_revision')}")
    out.append(
        f"检查项状态：PASS={report['statuses'].get('PASS', 0)} "
        f"FAIL={report['statuses'].get('FAIL', 0)} "
        f"UNVERIFIED={report['statuses'].get('UNVERIFIED', 0)} "
        f"VACUOUS={report['statuses'].get('VACUOUS', 0)}"
    )
    out.append(f"发现统计：{report['totals']}")
    out.append("=" * 78)
    for c in report["checks"]:
        mark = {"PASS": "✔", "FAIL": "✘", "UNVERIFIED": "?", "VACUOUS": "∅"}.get(c["status"], "?")
        out.append(f"\n{mark} [{c['status']}] {c['id']} — {c['title']}（级别 {c['severity']}）")
        cov = c["coverage"]
        out.append(
            "    覆盖：files_scanned={files_scanned}{extra}".format(
                files_scanned=cov.get("files_scanned", "?"),
                extra="".join(
                    f" {k}={v}" for k, v in cov.items() if k not in ("files_scanned", "note")
                ),
            )
        )
        if cov.get("note"):
            out.append(f"    说明：{cov['note']}")
        for f in c["findings"]:
            loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
            out.append(f"    - {loc} :: {f['message']}")
            if f.get("suggestion"):
                out.append(f"      建议：{f['suggestion']}")
        if not c["findings"] and c["status"] in ("UNVERIFIED", "VACUOUS"):
            out.append("    （本次没有验证任何内容 —— 不得表述为通过）")
    out.append("\n" + "=" * 78)
    if blocker_findings(report):
        out.append("结论：存在 BLOCKER 级契约问题，验收不通过。")
    else:
        out.append("结论：无 BLOCKER 级契约问题（UNVERIFIED/VACUOUS 项仍需按三态纪律单列）。")
    return "\n".join(out)


def effective_severity(check: dict, finding: dict) -> str:
    """发现的真实级别：消息里的 [SEV] 前缀优先，否则取检查项级别。

    这保证了「检查项是 BLOCKER 级，但其中某条发现已被裁定为 INFO」时，
    不会被误算成 BLOCKER（Lead 复核明确要求，否则 BLOCKER 计数长期失真）。
    """
    m = re.match(r"\[(BLOCKER|MAJOR|MINOR|INFO)\]", finding.get("message", ""))
    return m.group(1) if m else check["severity"]


def blocker_findings(report: dict) -> list[tuple[str, dict]]:
    return [
        (c["id"], f)
        for c in report["checks"]
        for f in c["findings"]
        if effective_severity(c, f) == "BLOCKER"
    ]


def main(argv: list[str] | None = None) -> int:
    _make_console_safe()
    ap = argparse.ArgumentParser(description="Better ComfyUI 契约一致性检查")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    report = run_all()
    out_path = Path(__file__).resolve().parent / "_contracts_report.json"
    try:
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        print(f"[warn] 无法写入 {out_path}: {exc}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
        print(f"\n机器可读报告：{out_path}")

    return 1 if blocker_findings(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
