"""收尾探测：SmartDirector 在**没有 LLM** 的情况下到底是什么行为。

## 为什么要问这个问题

`MiniMaxH3SmartDirector` 是融合路线 A 的「AI 分镜」入口。它的关键控件：

| 控件 | 默认 | 说明 |
|---|---|---|
| `生成模式` | `auto 智能判断` | 决定 t2v/i2v/fl2v/r2v |
| `提示词优化` | **`关闭`** | 只有本地LLM/API 两种开启方式 |
| `本地主模型` | `Qwen3.8-27B-...gguf` | **磁盘上没有**（models/LLM 为空） |
| `API密钥` | `''` | 未配置 |

节点还存在一个「主题 / 剧本 / 分镜JSON」输入 —— 可以直接喂结构化分镜。

所以三种可能：

1. **默认设置下不需要 LLM**：直接把输入解析成分镜 -> 输出导演组（可用！）
2. 需要 LLM 但缺权重 -> 明确报错（诚实，可接受）
3. 需要 LLM 却**静默出假结果**（最糟）

本脚本就是要区分这三种。**必须实测，不能推断。**
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(r"[PROJECT-ROOT]")
sys.path.insert(0, str(REPO / "bettercomfyui_comfy" / "workflows"))

import build_fusion_workflow as B  # noqa: E402

BASE = "http://127.0.0.1:8188"


def post(path: str, payload: dict):
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
            return exc.code, {"raw": raw[:900]}


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def main() -> int:
    schemas = B.fetch_object_info(8188, ["MiniMaxH3SmartDirector", "PreviewAny"])
    sd = schemas.get("MiniMaxH3SmartDirector")
    if sd is None:
        print("[!] SmartDirector 不在本机")
        return 2

    # 用 schema 默认值填满所有控件（这是 safest 的做法：模拟用户直接点 Queue）
    inputs: dict = {}
    for name, meta in B.widget_slots(sd):
        inputs[name] = B.default_for(name, meta, {})
    print("提交的控件（全部取 schema 默认值）：")
    for k, v in inputs.items():
        shown = str(v)
        print(f"   {k} = {shown[:70]}")
    print()

    # 给一个明确的中文剧本（3 段），让分镜有内容可拆
    inputs["主题 / 剧本 / 分镜JSON"] = (
        "一个红色方块放在木桌上。第一镜：方块静止，镜头缓慢推近。"
        "第二镜：方块轻微晃动。第三镜：镜头拉远，画面定格。"
    )
    inputs["全局画面风格"] = "冷蓝电影光，低饱和"

    graph = {
        "1": {"class_type": "MiniMaxH3SmartDirector", "inputs": inputs},
        "99": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}},
    }
    code, body = post("/prompt", {"prompt": graph, "client_id": "sd-final-probe"})
    print(f"提交结果 HTTP {code}")
    if code != 200:
        print(json.dumps(body, ensure_ascii=False, indent=2)[:1600])
        if body.get("node_errors"):
            print()
            print("=> 结论：**缺必需输入**（UI 里那些控件必须都填）")
        return 1

    pid = body.get("prompt_id")
    print(f"已入队 prompt_id = {pid}")
    print("等待执行（最多 5 分钟；若它要加载 LLM 会失败或很慢）……")

    t0 = time.time()
    while time.time() - t0 < 300:
        time.sleep(5)
        h = get(f"/history/{pid}")
        if pid in h:
            entry = h[pid]
            st = entry.get("status") or {}
            print()
            print(f"耗时 {time.time()-t0:.0f}s")
            print(f"status_str = {st.get('status_str')}  completed = {st.get('completed')}")
            for m in (st.get("messages") or [])[-16:]:
                print("   ", json.dumps(m, ensure_ascii=False)[:320])
            outs = entry.get("outputs") or {}
            print(f"输出节点: {list(outs)}")
            for nid, o in outs.items():
                for k, v in o.items():
                    print(f"   节点{nid}.{k} => {json.dumps(v, ensure_ascii=False)[:400]}")
            if st.get("status_str") == "success" and st.get("completed"):
                print()
                print("=> 结论：**默认设置下不需要 LLM，SmartDirector 正常工作**")
                return 0
            print()
            print("=> 结论：执行失败 —— 上面的 messages 就是确切原因")
            return 1
    print()
    print("=> 结论：5 分钟未完成（可能在等 LLM 或极慢）")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
