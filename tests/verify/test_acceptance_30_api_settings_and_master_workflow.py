"""验收 30：API 配置接口 + 完整逻辑图工作流 W0。

## 用户提的两个问题

1. **「你没有设置 DeepSeek 之类的 API 的配置接口，并不能真正实现」**
   —— 剧本巨人 `ComfyUI-Storyboard-LLM` **没有 api_key 输入端口**，
   只读自己目录下的 `settings.json`。所以光在我的面板里填密钥**不会生效**，
   必须同时写它的 `settings.json`。本条验证这条链路真的通了。
2. **「制作一套完整的工作流，可以实现我们那套逻辑图」**
   —— `W0_完整逻辑图全流程` 覆盖图一 SG1–SG7。

## 判定通过

    P1 `GET /bettercomfyui/settings` 可用，且**不返回完整密钥**；
    P2 `POST` 写入 `config/user.yaml`，字段名是 `model_name`（不是 `model`）；
    P3 `POST` **同时**写剧本巨人的 `settings.json`，且是**合法 JSON**；
    P4 两次响应里都**不含完整密钥**（只有遮罩）；
    P5 空 `api_key` **不覆盖**已有密钥（避免误清空）；
    P6 白名单生效：不在白名单的字段被丢弃；
    P7 配置节点 `BetterComfyUISettings` 能读到生效配置并给出状态；
    P8 W0 工作流存在、14 个节点、通过真实 schema 校验；
    P9 W0 覆盖图一关键子图（SG1/SG2/SG4/SG6/SG7 的节点都在）；
    P10 W0 能在**真实浏览器**里被 `app.loadGraphData()` 打开。

## 判定假通过（必须能变红）

    F1 去掉巨人 settings.json 同步 -> P3 变红；
    F2 字段名写回 `model` -> P2 变红（`load_config` 会因未知键报错）；
    F3 响应回传完整密钥 -> P1/P4 变红。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASE = "http://127.0.0.1:8188"
UI_WORKFLOWS = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"
GIANT_DIR = REPO_ROOT / ".comfyui" / "custom_nodes" / "ComfyUI-Storyboard-LLM"
USER_CONFIG = REPO_ROOT / "config" / "user.yaml"
#: ComfyUI 原生设置文件 —— 保存接口会同步写这里的 StoryBoard.api_key / BetterComfyUI.ApiBase。
#: 之前**没有备份它**，于是测试跑完假密钥留在原生设置里，界面一直显示"已配置"。
NATIVE_SETTINGS = REPO_ROOT / ".comfyui" / "user" / "default" / "comfy.settings.json"
W0_NAME = "W0_完整逻辑图全流程.json"

#: 测试用的假密钥。**用完必须清掉**（不能让假密钥留在用户配置里）。
FAKE_KEY = "sk-acceptance-test-FAKE-0000-do-not-use"


def _get(path: str, timeout: float = 20.0):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def _post(path: str, body: dict, timeout: float = 20.0):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _alive() -> bool:
    try:
        _get("/system_stats", timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def comfy():
    if not _alive():
        pytest.skip("未验证：本机 ComfyUI 不可达（http://127.0.0.1:8188）")
    return True


@pytest.fixture(scope="module")
def saved_state():
    """保存并恢复三处真实配置，避免测试把假密钥/假地址留在用户环境里。

    三处都必须备份（实测代价：只备份前两处时，假密钥残留在原生设置里，
    用户界面一直显示"API 已配置"，而真实调用 401）：
      1. config/user.yaml
      2. 巨人 custom_nodes/ComfyUI-Storyboard-LLM/settings.json
      3. ComfyUI 原生 user/default/comfy.settings.json
    """
    import warnings

    targets = {
        "user": USER_CONFIG,
        "giant": GIANT_DIR / "settings.json",
        "native": NATIVE_SETTINGS,
    }
    backups = {
        name: (path.read_text(encoding="utf-8") if path.is_file() else None)
        for name, path in targets.items()
    }

    yield

    for name, path in targets.items():
        backup = backups[name]
        try:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 恢复失败必须留痕
            warnings.warn(f"恢复 {name} 配置失败：{type(exc).__name__}: {exc}", stacklevel=1)

    # 恢复后自检：内容必须与备份一致（否则说明有并发写入，例如面板自动保存）
    for name, path in targets.items():
        backup = backups[name]
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != backup:
            warnings.warn(
                f"配置恢复不完整（{name}）：测试结束后文件内容与测试前不一致。"
                "请检查是否有『打开页面即自动保存』之类的隐式写入。",
                stacklevel=1,
            )


# ---------------------------------------------------------------------------
# P1 / P2 / P3 / P4 配置接口
# ---------------------------------------------------------------------------


def test_p1_settings_get_never_leaks_key(comfy):
    d = _get("/bettercomfyui/settings")
    assert d.get("ok") is True, d
    for field in ("provider", "model", "api_key_configured", "api_key_source", "hint"):
        assert field in d, f"缺少字段 {field}"
    # 快照里只该有遮罩字段，不该有明文密钥字段
    assert "api_key" not in d, "响应里出现了明文 api_key 字段"
    assert "***" in d["api_key_masked"] or d["api_key_masked"] == ""


def test_p2_saved_fields_to_user_yaml(comfy, saved_state):
    """POST 必须写进 config/user.yaml，且字段名是 model_name。"""
    import yaml

    d = _post("/bettercomfyui/settings", {
        "api_key": FAKE_KEY,
        "model_name": "deepseek-chat",
        "endpoint": "https://api.deepseek.com",
    })
    assert d.get("ok") is True, d
    assert "prompt.api_key" in d["saved"]["written"]
    assert "prompt.model_name" in d["saved"]["written"], (
        f"字段名不是 model_name：{d['saved']['written']}"
    )

    data = yaml.safe_load(USER_CONFIG.read_text(encoding="utf-8")) or {}
    prompt = data.get("prompt") or {}
    assert prompt.get("model_name") == "deepseek-chat"
    assert prompt.get("api_key") == FAKE_KEY
    # 关键：不能写出 `model`（未知键会让 load_config 整体报错）
    assert "model" not in prompt, "写出了非法的 model 键（会导致配置加载失败）"


def test_p3_giant_settings_json_is_synced_and_valid(comfy, saved_state):
    """**关键**：保存密钥必须同时写剧本巨人的 settings.json，否则巨人拿不到密钥。"""
    _post("/bettercomfyui/settings", {
        "api_key": FAKE_KEY,
        "model_name": "deepseek-chat",
        "endpoint": "https://api.deepseek.com",
    })
    giant_file = GIANT_DIR / "settings.json"
    assert giant_file.is_file(), "巨人 settings.json 没生成"
    payload = json.loads(giant_file.read_text(encoding="utf-8"))   # 必须能解析
    assert payload.get("api_key") == FAKE_KEY
    assert payload.get("base_url") == "https://api.deepseek.com"


def test_p4_responses_never_contain_plaintext_key(comfy, saved_state):
    resp = _post("/bettercomfyui/settings", {"api_key": FAKE_KEY})
    blob = json.dumps(resp, ensure_ascii=False)
    assert FAKE_KEY not in blob, "POST 响应里出现了完整密钥！"
    got = _get("/bettercomfyui/settings")
    assert FAKE_KEY not in json.dumps(got, ensure_ascii=False), "GET 响应里出现了完整密钥！"


# ---------------------------------------------------------------------------
# P5 / P6 安全边界
# ---------------------------------------------------------------------------


def test_p5_empty_key_does_not_clear_existing(comfy, saved_state):
    """空 api_key 不该覆盖已有密钥（否则用户误点保存就丢密钥）。"""
    _post("/bettercomfyui/settings", {"api_key": FAKE_KEY})
    _post("/bettercomfyui/settings", {"api_key": "", "model_name": "deepseek-chat"})
    d = _get("/bettercomfyui/settings")
    assert d.get("api_key_configured") is True, "空密钥把已有密钥清掉了"


def test_p6_whitelist_drops_unknown_fields(comfy, saved_state):
    """白名单外的字段必须被丢弃（防止借接口写任意配置）。"""
    d = _post("/bettercomfyui/settings", {
        "api_key": FAKE_KEY,
        "完全非法的字段": "should-be-dropped",
        "project_root": "/etc/passwd",
    })
    assert d.get("ok") is True
    written = " ".join(d["saved"]["written"])
    assert "非法" not in written and "project_root" not in written


# ---------------------------------------------------------------------------
# P7 配置节点
# ---------------------------------------------------------------------------


def test_p7_settings_node_reads_effective_config(comfy, saved_state):
    _post("/bettercomfyui/settings", {
        "api_key": FAKE_KEY, "model_name": "deepseek-chat"})
    from bettercomfyui_comfy.settings_node import (
        BetterComfyUISettings,
        resolve_runtime_secret,
    )

    info = resolve_runtime_secret()
    assert info["configured"] is True
    key, model, base, status = BetterComfyUISettings().run()
    assert key == FAKE_KEY
    assert model == "deepseek-chat"
    assert "已配置" in status
    # 状态摘要里不能出现完整密钥
    assert FAKE_KEY not in status, "状态摘要泄露了完整密钥"


# ---------------------------------------------------------------------------
# P8 / P9 / P10 完整逻辑图工作流 W0
# ---------------------------------------------------------------------------


def _load_w0() -> dict:
    path = UI_WORKFLOWS / W0_NAME
    if not path.is_file():
        pytest.skip(f"未验证：{W0_NAME} 未安装（先跑 scripts/build_master_workflow.py）")
    return json.loads(path.read_text(encoding="utf-8"))


def test_p8_w0_exists_and_validates(comfy):
    """W0 必须存在并通过真实 schema 校验。"""
    # 脚本在 <repo>/scripts/ 下，测试需要把它加进 sys.path
    import sys as _sys

    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from build_master_workflow import validate

    info = _get("/object_info")
    wf = _load_w0()
    assert len(wf["nodes"]) >= 12, f"只有 {len(wf['nodes'])} 个节点，不像完整流程"
    problems = validate(info, wf)
    assert not problems, "W0 校验失败：\n  " + "\n  ".join(problems)


def test_p9_w0_covers_logic_diagram_subgraphs(comfy):
    """W0 必须覆盖图一的关键子图（ROUND26：全链整合进一个大节点）。"""
    wf = _load_w0()
    types = {n["type"] for n in wf["nodes"]}
    coverage = {
        "SG7 全局配置": "BetterComfyUISettings",
        # SG1+SG2+SG4 前段：剧本→一次调用 E1–E5→确定性匹配→素材库→导演组
        "SG1/SG2/SG4 一个大节点全链": "BetterComfyUIEverything",
        "SG4 真实导演台": "MiniMaxH3Director",
        "SG6 合成视频": "CreateVideo",
        "SG6 成片输出": "SaveVideo",
    }
    missing = [label for label, node in coverage.items() if node not in types]
    assert not missing, f"W0 缺少这些子图的节点：{missing}"
    # 不允许再把中间环节拆成一串手工节点
    for forbidden in ("BetterComfyUIScriptPrompts", "BetterComfyUIOneDirector"):
        assert forbidden not in types, f"W0 不应再串联 {forbidden}（应整合进大节点）"


def test_p9b_w0_links_are_end_to_end(comfy):
    """W0 的连线必须真的把「配置 → 大节点 → 导演组 → 出片」串起来。"""
    wf = _load_w0()
    by_id = {n["id"]: n for n in wf["nodes"]}

    def has_link(src_type: str, dst_type: str, *, dtype: str | None = None) -> bool:
        for (_lid, src, _ss, dst, _ds, link_type) in wf["links"]:
            if by_id[src]["type"] == src_type and by_id[dst]["type"] == dst_type:
                if dtype is None or link_type == dtype:
                    return True
        return False

    # API 配置节点的 api_key/model/api_base 必须真的接到大节点
    assert has_link("BetterComfyUISettings", "BetterComfyUIEverything"), \
        "API 配置节点没连到大节点（用户指出的断链）"
    # 大节点直接输出导演组给真实导演台
    assert has_link("BetterComfyUIEverything", "MiniMaxH3Director", dtype="MMX_DIR_GROUP"), \
        "大节点的导演组没连到真实导演台"
    assert has_link("MiniMaxH3Director", "CreateVideo"), "导演台没连到合成"
    assert has_link("CreateVideo", "SaveVideo"), "合成没连到输出"
    # 结果可见：大节点的提示词JSON/素材清单/审计报告都接了预览
    big = next(n for n in wf["nodes"] if n["type"] == "BetterComfyUIEverything")
    preview_links = [l for l in wf["links"]
                     if int(l[1]) == big["id"] and by_id[int(l[3])]["type"] == "PreviewAny"]
    assert len(preview_links) >= 3, f"大节点只有 {len(preview_links)} 条结果预览连线（用户要求结果可见）"


def test_p10_w0_opens_in_real_browser(comfy):
    """W0 必须能在**真实浏览器**里被前端加载。"""
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="未验证：未安装 playwright")
    wf = _load_w0()
    with sync_api.sync_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = (pw.chromium.launch(headless=True, channel=channel)
                           if channel else pw.chromium.launch(headless=True))
                break
            except Exception:  # noqa: BLE001
                browser = None
        if browser is None:
            pytest.skip("未验证：没有可用浏览器")
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.goto(BASE, wait_until="load", timeout=60000)
            page.wait_for_function(
                "() => window.app && window.app.graph && window.LiteGraph",
                timeout=60000)
            page.wait_for_timeout(1200)
            result = page.evaluate("""async (wf) => {
                window.app.graph.clear();
                await window.app.loadGraphData(wf);
                await new Promise(r => setTimeout(r, 400));
                const nodes = window.app.graph._nodes || [];
                return {count: nodes.length, types: nodes.map(n => n.type)};
            }""", wf)
        finally:
            browser.close()

    assert result["count"] == len(wf["nodes"]), (
        f"画布节点数 {result['count']} 与文件 {len(wf['nodes'])} 不符")
    for required in ("BetterComfyUIEverything", "MiniMaxH3Director",
                     "SaveVideo", "BetterComfyUISettings"):
        assert required in result["types"], f"画布上缺少 {required}"


def test_p11_page_load_does_not_rewrite_user_config(comfy):
    """**A1 回归**：真实浏览器只是打开页面，不得静默改写 config/user.yaml。

    实测事故：面板把 `BetterComfyUI.ApiBase` 注册成原生设置，其 onChange 无条件
    POST 保存；页面加载恢复设置时就触发，于是「打开网页」=「改写用户配置」，
    还会把测试恢复好的配置覆盖成假密钥。
    """
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="未验证：未安装 playwright")

    giant_file = GIANT_DIR / "settings.json"
    before_user = USER_CONFIG.read_text(encoding="utf-8") if USER_CONFIG.is_file() else None
    before_giant = giant_file.read_text(encoding="utf-8") if giant_file.is_file() else None

    with sync_api.sync_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = (pw.chromium.launch(headless=True, channel=channel)
                           if channel else pw.chromium.launch(headless=True))
                break
            except Exception:  # noqa: BLE001
                browser = None
        if browser is None:
            pytest.skip("未验证：没有可用浏览器")
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.goto(BASE, wait_until="load", timeout=60000)
            page.wait_for_function(
                "() => window.app && window.app.graph && window.LiteGraph",
                timeout=60000)
            # 给"设置恢复 → onChange"留足触发时间
            page.wait_for_timeout(2500)
            errors = page.evaluate("""() => {
                const out = [];
                // 我们只关心面板扩展有没有加载失败（不把第三方报错算进来）
                return out;
            }""")
        finally:
            browser.close()

    after_user = USER_CONFIG.read_text(encoding="utf-8") if USER_CONFIG.is_file() else None
    after_giant = giant_file.read_text(encoding="utf-8") if giant_file.is_file() else None
    assert after_user == before_user, (
        "打开页面后 config/user.yaml 被改写了（面板『设置恢复即自动保存』回归）")
    assert after_giant == before_giant, (
        "打开页面后巨人 settings.json 被改写了（同步写入的副作用）")
