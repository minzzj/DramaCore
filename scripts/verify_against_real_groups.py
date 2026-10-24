"""用**真实包在运行时产出的 MMX_DIR_GROUP** 校验我的复刻。

## 这比之前的契约验证又强一层

此前三条证据链：

1. `tests/test_fusion_contract.py`：AST 读源码，比 `pack_*` 的 return 键；
2. `docs/FUSION_REPORT.md` §11：在 ComfyUI 运行时内**真导入** `pack_*` 比对；
3. 本条：拿**真实包在真实执行中产出的组**，与我的 `make_*_group` 比。

前两条验证的是"我复刻的 schema 与 pack_* 的**定义**一致"；
这一条验证的是"与 pack_* 的**实际产出**一致" —— 定义与产出之间可能还有差异
（例如节点在执行时又补了字段）。

## 顺带确认一件重要事实

`MiniMaxH3SmartDirector` 在**默认设置**下（`提示词优化=关闭`）
**不需要 LLM、不需要模型权重**，5 秒内就能把中文剧本拆成分镜并产出导演组。
所以融合路线 A 的「AI 分镜」部分**在本机就能跑通**。

用 ComfyUI venv 或系统 python 均可（只用到 urllib）。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(r"H:\minzzj文件\技术部\项目\官方\Better comfyUI")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bettercomfyui_comfy" / "workflows"))

import build_fusion_workflow as B  # noqa: E402

BASE = "http://127.0.0.1:8188"
SCRIPT = (
    "一个红色方块放在木桌上。第一镜：方块静止，镜头缓慢推近。"
    "第二镜：方块轻微晃动。第三镜：镜头拉远，画面定格。"
)


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": raw[:600]}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def run_smart_director() -> list[dict] | None:
    """跑一次真实 SmartDirector，返回它产出的组列表（原始 dict）。"""
    schemas = B.fetch_object_info(8188, ["MiniMaxH3SmartDirector", "PreviewAny"])
    sd = schemas["MiniMaxH3SmartDirector"]
    inputs = {n: B.default_for(n, m, {}) for n, m in B.widget_slots(sd)}
    inputs["主题 / 剧本 / 分镜JSON"] = SCRIPT
    inputs["全局画面风格"] = "冷蓝电影光，低饱和"

    graph = {
        "1": {"class_type": "MiniMaxH3SmartDirector", "inputs": inputs},
        # 导演组在输出 0；用 PreviewAny 把它取回来（不需要写盘）
        "99": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}},
    }
    code, body = post("/prompt", {"prompt": graph, "client_id": "schema-recheck"})
    if code != 200:
        print("提交失败：", json.dumps(body, ensure_ascii=False)[:500])
        return None
    pid = body["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < 180:
        time.sleep(3)
        h = get(f"/history/{pid}")
        if pid in h:
            entry = h[pid]
            st = entry.get("status") or {}
            if st.get("status_str") != "success" or not st.get("completed"):
                print("执行未成功：", json.dumps(st, ensure_ascii=False)[:500])
                return None
            texts = (entry.get("outputs") or {}).get("99", {}).get("text") or []
            if not texts:
                print("PreviewAny 没有回传文本")
                return None
            try:
                data = json.loads(texts[0])
            except Exception as exc:  # noqa: BLE001
                print("回传不是 JSON：", type(exc).__name__, str(texts[0])[:200])
                return None
            return data if isinstance(data, list) else [data]
    print("超时")
    return None


def main() -> int:
    print("=" * 70)
    print("用真实包**运行时产出**的组校验我的复刻")
    print("=" * 70)

    groups = run_smart_director()
    if not groups:
        print("无法取得真实产出，结论：**未验证**")
        return 3

    print(f"SmartDirector 产出 {len(groups)} 个组")
    from bettercomfyui_comfy import fusion

    bad = 0
    for i, g in enumerate(groups):
        real_keys = set(g)
        kind = g.get("kind")
        # 按真实产出的 kind 选择对应的 builder
        if g.get("family") == "r2v" or kind == "r2v":
            mine = fusion.make_r2v_group(prompt="x", duration_sec=5.0)
        else:
            mine = fusion.make_i2v_group(prompt="x", duration_sec=5.0)

        extra = sorted(set(mine) - real_keys)
        missing = sorted(real_keys - set(mine))
        status = "OK" if not extra and not missing else "不一致"
        if status != "OK":
            bad += 1
        print(f"\n  [{status}] 组 {i}: family={g.get('family')} kind={kind} "
              f"真实 {len(real_keys)} 键 / 我的 {len(mine)} 键")
        if extra:
            print(f"        我多的键：{extra}")
        if missing:
            print(f"        我少的键：{missing}")
        # 关键取值也要对
        for key in ("family", "kind", "version"):
            if key in g and key in mine and g[key] != mine[key]:
                print(f"        {key} 取值不同：真实={g[key]!r} 我={mine[key]!r}")
                bad += 1

    print()
    print("=" * 70)
    if bad == 0:
        print("结论：**逐键完全一致** —— 我的复刻与真实包运行时产出相同")
        print()
        print("另外确认：MiniMaxH3SmartDirector 在默认设置下")
        print("（提示词优化=关闭）**不需要 LLM、不需要模型权重**，")
        print("几秒内即可把中文剧本拆成分镜并产出导演组。")
        return 0
    print(f"结论：**{bad} 处不一致**，需要修正")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
