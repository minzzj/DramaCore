"""接缝验证：`prompt/`（剧本拆解）的规范输出能否被 `demo.py`（消费方）正确消费。

## 为什么要单独验这一条

两半各自都有测试且都绿：

* `tests/verify/test_acceptance_02*` 验 `validate_script`（返回**顶层** `assets`）；
* `tests/verify/test_acceptance_15*` 验端到端，但它用的是 `demo.py` **自带的
  `DEMO_SCRIPT`**（assets 在**章节级**）。

**接缝没人验** —— 这类"两半各自绿、接缝断了"最难发现。

## 本次实测发现

| 环节 | `assets` 在哪一级 |
|---|---|
| `validate_script` 规范输出 | **顶层**（章节级为 0） |
| `demo.py` 读取 | **章节级** `chapter_obj.get("assets", [])` |
| 内置 `DEMO_SCRIPT` | 章节级（因此**通不过** `validate_script`，7 条错误） |

**影响**：把真实（经校验的）剧本喂给 `run_demo`，
`for cand in chapter_obj.get("assets", [])` 会**一次都不进循环** ——
素材的「匹配 / 复用 / 疑似队列 / 生成 / 审核 LOCK」**整段被静默跳过**。

而下游导演台需要 `locked_assets`，于是要么空跑、要么在别处才报错，
**错误信息不会指向真正的根因**。

## 本脚本做什么

把两种形状分别喂给 `run_demo`，对比 `assets_created / assets_reused / assets_locked`，
用**数字**证明跳过确实发生。
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def canonical_script() -> dict:
    """`validate_script` 认可的形状：**顶层** assets。"""
    return {
        "title": "接缝验证",
        "output_language": "中文",
        "chapters": [
            {
                "chapter_id": "CH001",
                "title": "雪夜",
                "summary": "林澈雪夜独行。",
                "segments": [
                    {"segment_index": 1, "prompt": "雪夜长街，林澈独行",
                     "asset_refs": ["A1", "A2"], "duration_s": 10},
                    {"segment_index": 2, "prompt": "林澈拔剑回身",
                     "asset_refs": ["A1"], "duration_s": 10},
                ],
            }
        ],
        "assets": [
            {"id": "A1", "asset_type": "CHARACTER", "name": "林澈",
             "prompt": "少年剑客，白衣，长发束起，冷峻"},
            {"id": "A2", "asset_type": "SCENE", "name": "雪夜长街",
             "prompt": "大雪，青石板路，两侧灯笼，冷色调"},
        ],
    }


def chapter_level_script(*, use_canonical_key: bool = False) -> dict:
    """`demo.py` 期望的形状：**章节级** assets。

    `use_canonical_key=False` 用 `type`（demo.py 读的键）；
    `True` 用 `asset_type`（validate_script 规范输出的键）——
    用来暴露**第二个**不一致：键名也不同。
    """
    s = copy.deepcopy(canonical_script())
    assets = s.pop("assets")
    if not use_canonical_key:
        assets = [{("type" if k == "asset_type" else k): v for k, v in a.items()}
                  for a in assets]
    s["chapters"][0]["assets"] = assets
    return s


def run_one(label: str, script: dict, tmp: Path) -> dict:
    from bettercomfyui.config import load_config
    from bettercomfyui.demo import run_demo

    work = tmp / label
    work.mkdir(parents=True, exist_ok=True)
    script_file = work / "script.json"
    script_file.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")

    cfg = load_config(repo_root=REPO)
    cfg.global_.project_root = str(work / "projects")
    (work / "projects").mkdir(parents=True, exist_ok=True)

    # project=None -> 让 run_demo 自己创建项目（它负责生成合法 UUID）。
    # 第一版我传了中文名当 project_id，直接撞上
    # ProjectNotFoundError: project_id 必须是 32 位小写 UUID4 hex。
    rep = run_demo(repo_root=REPO, config=cfg, project=None,
                   chapter=script_file, executor="mock", workdir=work)
    return rep


def main() -> int:
    print("=" * 76)
    print("接缝验证：validate_script 的规范输出 → demo.py 能否正确消费")
    print("=" * 76)

    # 先确认规范形状确实通过校验、且章节级 assets 为空
    from bettercomfyui.prompt.validate import validate_script
    canon_ok = validate_script(canonical_script())
    top = len(canon_ok.get("assets") or [])
    ch_lvl = sum(len(c.get("assets") or []) for c in canon_ok.get("chapters") or [])
    print(f"\n[A] validate_script 规范输出：顶层 assets={top}，章节级 assets={ch_lvl}")

    # 内置演示剧本能否通过校验
    from bettercomfyui.demo import DEMO_SCRIPT
    try:
        validate_script(copy.deepcopy(DEMO_SCRIPT))
        demo_valid = "通过"
    except Exception as exc:  # noqa: BLE001
        n = str(exc).count("[") or 1
        demo_valid = f"不通过（{type(exc).__name__}，{n} 条错误）"
    print(f"[B] 内置 DEMO_SCRIPT 过 validate_script：{demo_valid}")

    tmp = Path(tempfile.mkdtemp())
    print("\n[C] 三种形状分别喂给 run_demo，看素材阶段计数：\n")
    rows = []
    for label, script in (
        ("① 规范形状：顶层 assets + asset_type 键", canonical_script()),
        ("② 章节级 + type 键（demo 真正期望的）", chapter_level_script()),
        ("③ 章节级 + asset_type 键（键名不一致）",
         chapter_level_script(use_canonical_key=True)),
    ):
        rep = run_one(f"case{len(rows)}", script, tmp)
        row = {
            "shape": label,
            "ok": rep.get("ok"),
            "created": rep.get("assets_created"),
            "reused": rep.get("assets_reused"),
            "suspect": rep.get("assets_suspect"),
            "locked": rep.get("assets_locked"),
            "segments": rep.get("segments_generated"),
            "error": (rep.get("error") or "")[:60],
        }
        rows.append(row)
        print(f"  {label}")
        print(f"      ok={row['ok']}  素材 新建={row['created']} 复用={row['reused']} "
              f"疑似={row['suspect']} 已LOCK={row['locked']}  段={row['segments']}")
        if row["error"]:
            print(f"      error: {row['error']}")

    print("\n" + "=" * 76)
    a, b, c = rows[0], rows[1], rows[2]
    skipped = (a["created"] or 0) == 0 and (a["reused"] or 0) == 0
    if skipped and (b["created"] or 0) > 0:
        print("结论：**接缝断了，而且是两处不一致**")
        print()
        print(f"  ① 规范形状（validate_script 的输出）：")
        print(f"     ok={a['ok']} 素材新建={a['created']} 复用={a['reused']} "
              f"疑似={a['suspect']} LOCK={a['locked']} 段={a['segments']}")
        print("     -> **ok=True 却一个素材都没建** —— 假成功，整个素材阶段被静默跳过")
        print()
        print(f"  ② 章节级 + type 键：新建={b['created']} 复用={b['reused']} "
              f"LOCK={b['locked']} ok={b['ok']}")
        print("     -> 这才是 demo.py 期望的形状（assets 在章节级 + 用 type 键）")
        print()
        print(f"  ③ 章节级 + asset_type 键：ok={c['ok']}  error={c['error']}")
        print("     -> 键名不同也不行（demo.py 读 cand[\"type\"]）")
        print()
        print("  两处不一致：")
        print("    位置：validate_script 放**顶层** / demo.py 读**章节级**")
        print("    键名：validate_script 用 asset_type / demo.py 用 type")
        print()
        print("  后果：真实（经校验的）剧本走这条路时，")
        print("        素材匹配/复用/疑似队列/生成/LOCK **全部不发生**，")
        print("        而 run_demo 仍返回 ok=True（段还能生成）—— 是**假成功**。")
        print("        导演台拿不到 locked_assets，身份一致性直接失去依据。")
        return 1
    if not skipped:
        print("结论：三种形状都能被消费，**接缝正常**")
        return 0
    print("结论：都没产出素材，需进一步排查")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
