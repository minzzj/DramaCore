"""扫描配置项「候选未接线」清单（裸词口径 + 明确排除项）。

## 口径说明（为什么是"候选"而不是"结论"）

两种自动判据都有偏差：

| 判据 | 偏差方向 | 例子 |
|---|---|---|
| **裸字段名**（`\\bfield\\b`） | 假阳性：会把无关同名**误判已接线**（掩盖缺口） | `assets.resolution` 被 `cfg.director.resolution` 命中 |
| **完整路径**（`.group.field`） | 假阴性：中间变量就读不到（夸大缺口） | `p = cfg.director; p.tail_frames` |

所以这里用**裸词**做"候选筛选"（宁可多报），再由人逐条定性。
`tests/test_config_wiring_ratchet.py` 的 `KNOWN_UNWIRED` 就是定性结果。

用法：`python scripts/scan_config_wiring.py`
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bettercomfyui.models.entities import Config  # noqa: E402

#: 这些都**不算产品代码使用**，按**具体路径**排除（不按文件名 —— 见下）：
#: 顶层 config.py 只校验边界/解析枚举；entities.py 是字段定义；gap_analysis 是清单。
#:
#: ⚠️ 不能用文件名 {"config.py"} 排除：ettercomfyui/review/config.py
#: 会被一起排掉，而 score_dimensions 正是在那里被读的 -> 误报未接线。
SKIP = {
    REPO / "bettercomfyui" / "config.py",
    REPO / "bettercomfyui" / "models" / "entities.py",
    REPO / "bettercomfyui" / "gap_analysis.py",
}


def main() -> int:
    chunks: list[tuple[str, str]] = []
    for d in ("bettercomfyui", "bettercomfyui_comfy"):
        for p in (REPO / d).rglob("*.py"):
            if p.resolve() in SKIP:
                continue
            chunks.append((p.name, p.read_text(encoding="utf-8")))
    print(
        f"扫描产品代码 {len(chunks)} 个文件（已排除 "
        f"{', '.join(sorted(str(p.relative_to(REPO)) for p in SKIP))}）"
    )

    fields: list[tuple[str, str]] = []
    for gf in dataclasses.fields(Config):
        gobj = gf.default_factory()
        if not dataclasses.is_dataclass(gobj):
            continue
        for f in dataclasses.fields(gobj):
            fields.append((gf.name, f.name))

    def hits(name: str) -> tuple[int, set[str]]:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        n = 0
        where: set[str] = set()
        for fname, text in chunks:
            found = pat.findall(text)
            if found:
                n += len(found)
                where.add(fname)
        return n, where

    zero: list[tuple[str, str]] = []
    for g, f in fields:
        n, where = hits(f)
        if n == 0:
            zero.append((g, f))

    print(f"\n配置字段 {len(fields)} 个；**候选未接线（裸词零命中）{len(zero)} 个**：\n")
    cur = None
    for g, f in zero:
        if g != cur:
            print(f"  [{g}]")
            cur = g
        print(f"    {f}")
    print("\n注意：这是**候选**清单，需要人工定性（可能是设计上由别层承担）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
