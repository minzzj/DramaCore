# ROUND55-7 · 素材版本回滚接口契约（后端模块 / HTTP / UI）

> 生成：2026-10-19 20:52（Asia/Shanghai）｜执行：release-agent（ROUND55 专项 7）
> 交付：`bettercomfyui/store/rollback.py`（新模块）+ `tests/test_round55_rollback.py`（11 测试）+ 本契约。
> 边界：**不实现路由/UI、不改 `one_stop_node.py`/`nodes.py`/UI**；路由由 Lead 注册，UI 由 Lead/UI 智能体接入。
> 复用：store 层现有 `AssetStore` LOCK/哈希逻辑（`set_lock`/`clear_lock`/`resolve_lock`/`latest_locked_version`/`referencing_segments`）——本模块不复制规则、不修改 store 文件。

---

## 一、模块与函数签名

```python
# bettercomfyui/store/rollback.py
list_asset_versions(
    project_id: str,
    asset_id_or_name: str | None = None,
    *,
    root: Path | str | None = None,          # projects/ 根；默认按 BETTERCOMFYUI_HOME/配置解析
) -> list[dict]

rollback_asset(
    project_id: str,
    asset_id_or_name: str,
    target_version: str,                     # 如 "v2"
    *,
    operator: str = "",                      # 审计：谁操作
    reason: str = "",                        # 审计：为什么回滚
    require_hash_ok: bool = True,            # 默认必须哈希一致，否则拒绝（R3 门禁）
    revalidate_lock: bool = True,            # 目标未 LOCK 时：True=显式重新 LOCK；False=拒绝
    lock_source: str = "HUMAN_PASS",         # 重新 LOCK 时使用的来源（HUMAN_PASS/MODEL_PASS/FALLBACK_PASS）
    review_cycle_id: str | None = None,      # 重新 LOCK 的审核周期 id（默认 rollback:<operator>:<UTC>）
    root: Path | str | None = None,
) -> dict

main(argv: list[str] | None = None) -> int      # 离线 CLI，见 §五
RollbackError(StoreError)                       # 业务错误，带 .rollback_code
```

### 设计要点（必须保留）
1. **哈希门禁**：回滚前重算磁盘 SHA-256，必须等于版本元数据 `content_hash`；不一致默认 `ContentHashMismatchError` 拒绝。`require_hash_ok=False` 仅供排障，返回值 `hash_ok=False` + warning，**不得用于生成**。
2. **不删除历史**：只 `clear_lock` 其它版本的 LOCK（历史留在 `extra.unlock_reason`/事件日志），版本目录与文件原样保留；返回 `audit.historical_versions_deleted = 0`。
3. **旧结论失效**：返回 `audit.old_conclusions_invalidated=True / requires_regeneration=True / requires_reference_remount=True`，并反查 `store.referencing_segments(asset_id)` 给出受影响段；**不把回滚伪装成"无需返工"**。
4. **目标未 LOCK**：默认 `revalidate_lock=True` 时用 `set_lock` 重新 LOCK（默认来源 HUMAN_PASS + 自动 `review_cycle_id`）；`False` 时拒绝（错误码 `TARGET_VERSION_NOT_LOCKED`）。
5. **审计日志**：项目级 `projects/<project_id>/_work/rollback_log.jsonl`（JSON Lines 追加；成功与拒绝都记；含时间/operator/reason/from→to/哈希/locks_before/locks_after/audit）。
6. **并发**：同一项目回滚用 `_work/rollback.lock` 文件锁串行化；与审核流水线并发 `set_lock/clear_lock` 存在竞态 → 路由层必须做「回滚前后 `latest_locked_version` 复核 + 冲突返回 409」（见 §四风险）。

---

## 二、`list_asset_versions` 返回 schema（UI 展示字段）

每行一个版本；`asset_id_or_name=None` 返回全项目所有素材的版本（按素材创建时间→asset_id，版本 v 数字升序）。

```jsonc
{
  "asset_id": "项目_character_1_ab12cd34",
  "asset_type": "CHARACTER",
  "name": "张三",
  "human_label": "角色_1_张三",          // 可能为 null（human_label 计算失败时）
  "version": "v2",
  "created_at": "2026-10-19T20:48:45+08:00",
  "status": "ALLOWED",                   // 素材审核状态（ALLOWED/FALLBACK/BLOCKED/DRAFT…）
  "lock_status": "LOCKED",               // LOCKED / UNLOCKED
  "locked": true,
  "lock_source": "MODEL_PASS",           // HUMAN_PASS/MODEL_PASS/FALLBACK_PASS/null
  "review_cycle_id": "cycle-model-2",
  "review_history_count": 1,
  "superseded_by": null,
  "score": 91.0,                         // 可得就带（extra.avg_score/score/review_score/scores 均值），否则 null
  "is_current_lock": true,               // 是否 == latest_locked_version（当前生成门禁使用的版本）
  "file_path": "assets/character/.../versions/v2/file.png",
  "file_exists": true,
  "hash_declared": "sha256:…",
  "hash_actual": "sha256:…",             // 文件存在时重算；不存在 null
  "hash_ok": true,                       // null=无法判断（异常），false=不一致/文件缺失
  "hash_error": null
}
```

> 性能提示：`list` 会对每个版本文件重算 SHA-256（数量大时变慢）。路由层如需轻量列表，可在契约中给 `verify_hash=false` 参数只展示轻量字段；当前模块保持「展示即校验」，避免 UI 把篡改文件显示成可用。

---

## 三、HTTP 契约（Lead 注册路由使用，模块不实现）

### 3.1 `GET /dramacore/rollback/list`
Query：`project_id`（必填）、`asset`（可选：asset_id 或素材显示名；省略=全项目）。
返回 200：

```json
{ "ok": true, "project_id": "…", "versions": [ /* list_asset_versions 行 */ ] }
```
错误：`400`（缺 project_id/非法 project_id）、`404`（asset 不存在：`ASSET_NOT_FOUND`）、`409`（名称歧义：`AMBIGUOUS_ASSET`，body 带 candidates）。

### 3.2 `POST /dramacore/rollback/apply`
请求体：

```jsonc
{
  "project_id": "…",
  "asset": "张三 或 asset_id",
  "target_version": "v1",
  "operator": "面板用户名（服务端可覆盖/校验）",
  "reason": "回滚到旧版",
  "require_hash_ok": true,
  "revalidate_lock": true,
  "lock_source": "HUMAN_PASS",
  "review_cycle_id": null
}
```
返回 200（成功）：

```jsonc
{
  "ok": true, "code": "OK",
  "project_id": "…", "asset_id": "…", "asset_name": "张三",
  "from_version": "v2", "target_version": "v1", "changed": true,
  "relocked": false,
  "cleared_locks": ["v2"],
  "locks_before": ["v1","v2"], "locks_after": ["v1"],
  "content_hash": "sha256:…", "file_path": "…/versions/v1/file.png", "hash_ok": true,
  "review_cycle_id": "cycle-human-1",
  "audit": { /* 见 §一 第 3 点 */ },
  "log_path": "projects/<id>/_work/rollback_log.jsonl",
  "warnings": []
}
```
错误 JSON：`{ "ok": false, "code": "<机器码>", "error": "人类可读原因", "context": {…} }`

| 错误码 | 触发 | 建议 HTTP |
|---|---|---|
| `TARGET_VERSION_NOT_FOUND` | target_version 不存在 | 404 |
| `TARGET_FILE_MISSING` | 目标版本文件不在磁盘 | 422 |
| `CONTENT_HASH_MISMATCH` | 磁盘文件与声明哈希不一致（默认拒绝） | 422 |
| `TARGET_VERSION_NOT_LOCKED` | 目标未 LOCK 且 `revalidate_lock=false` | 409 |
| `TARGET_LOCK_RECORD_INCONSISTENT` | 目标标记 LOCKED 但 LOCK 记录不自洽 | 409 |
| `AMBIGUOUS_ASSET` | 名称匹配多个素材 | 409（带 candidates） |
| `ASSET_NOT_FOUND` | 素材不存在 | 404 |
| `POSTCONDITION_FAILED` | 回滚后 latest_locked ≠ target（并发） | 409（可重试） |
| `IO_ERROR` | 路径/日志写失败 | 500 |

---

## 四、UI 契约建议（Lead/UI 智能体接入）

1. 素材卡/详情抽屉展示「版本历史」列表：版本号、创建时间、状态徽标（ALLOWED/FALLBACK/BLOCKED）、LOCK 徽标、哈希校验（✓/✗/—）、文件存在、分数（null 显示「待运行」）。**当前 LOCK 版本高亮**，其「回滚」按钮禁用。
2. 每行「回滚到此版本」→ 二次确认弹窗，必须显示：`从 <from_version> → <to_version>`、目标 `content_hash`、以及红字提示：
   > 「回滚恢复的是历史版本 LOCK 记录，不是新的审核结论。引用该素材的段需要重新生成/重挂参考图，并重跑视频审核（R_seg/R_full）。历史版本与审核记录不会被删除。」
3. 成功后：刷新版本列表 + 提示「已回滚，当前 LOCK=v1；请重新运行引用该素材的段」；失败按错误码给可操作文案（哈希不符→「文件被替换，不能回滚，请走新版本审核」；未 LOCK→「该版本未通过审核，需重新审核/授权」）。
4. 可选：受影响段列表（`audit.affected_segments`）链到段/成片审核记录；`operator` 由服务端会话/系统用户名覆盖，不信任前端任意填写。
5. 禁止：把 `require_hash_ok=false`、`revalidate_lock=false` 暴露成普通开关（仅排障/管理员参数，且必须二次确认+审计）。

---

## 五、离线 CLI

```powershell
# 列举（全项目/单素材）
python -B -m bettercomfyui.store.rollback --project-id <id> --list --root <repo>/projects
python -B -m bettercomfyui.store.rollback --project-id <id> --asset "张三" --list

# 回滚（默认 require_hash_ok=True / revalidate_lock=True）
python -B -m bettercomfyui.store.rollback --project-id <id> --asset <asset_id> \
  --target-version v1 --operator local --reason "回归旧版"
```
退出码：`0` 成功；`2` 被拒（stdout/err 为 JSON，含 code）；`3` 参数/未知错误。

---

## 六、审计日志格式（`_work/rollback_log.jsonl`，每行一个 JSON）

成功记录字段：`at/status=ok/project_id/asset_id/asset_name/operator/reason/from_version/to_version/locks_before/locks_after/cleared_locks/relocked/lock_source/review_cycle_id/content_hash/hash_ok/require_hash_ok/revalidate_lock/audit/warnings`。
拒绝记录：`at/status=rejected/.../target_version/error_code/error`（成功与拒绝都留痕；日志写失败会抛错，不静默）。

---

## 七、集成示例（Lead 路由 handler 伪代码）

```python
from bettercomfyui.store.rollback import list_asset_versions, rollback_asset, RollbackError
from bettercomfyui.store.errors import StoreError

async def h_rollback_list(request):
    q = request.query
    rows = list_asset_versions(q["project_id"], q.get("asset"))
    return web.json_response({"ok": True, "project_id": q["project_id"], "versions": rows})

async def h_rollback_apply(request):
    body = await request.json()
    try:
        result = rollback_asset(
            body["project_id"], body["asset"], body["target_version"],
            operator=body.get("operator", ""), reason=body.get("reason", ""),
            require_hash_ok=bool(body.get("require_hash_ok", True)),
            revalidate_lock=bool(body.get("revalidate_lock", True)),
            lock_source=body.get("lock_source", "HUMAN_PASS"),
            review_cycle_id=body.get("review_cycle_id"),
        )
        return web.json_response(result)
    except StoreError as exc:
        return web.json_response({"ok": False, "code": getattr(exc, "code", "STORE_ERROR"),
                                  "error": str(exc)}, status=422)
```
（路由参数校验/鉴权/并发 409 由 Lead 路由层补齐；本模块不读 `server`。）

---

## 八、已知风险（务必在路由/UI 层处理）

1. **并发写 LOCK**：回滚与审核流水线 `set_lock/clear_lock` 并发时，可能「回滚后又被新审核 LOCK 覆盖」。模块用 `_work/rollback.lock` 只能串行化回滚自身。路由层建议：回滚前后各取一次 `latest_locked_version`，不一致返回 409；高并发阶段禁止回滚 UI 入口。
2. **旧结论失效范围**：`audit.affected_segments` 来自素材引用记录；段审核状态在段存储里，路由/UI 需再查段/成片审核，不能仅凭素材回滚就宣称「无需重跑」。
3. **哈希重算成本**：版本多/文件大时 `list` 全量 SHA-256 会慢；若加 `verify_hash=false` 轻量模式，UI 要明确「未校验」而不是显示通过。
4. **目标版本未 LOCK**：默认拒绝；`revalidate_lock=True` 是显式授权（留下 lock_source/review_cycle_id），不可由普通用户默认开启。
5. **回滚≠重新审核**：恢复历史 LOCK 不代表以当前标准重新认证内容；发布/生产流程若要求重审，应在 UI 上提示「建议重新送审」。
6. **日志不可删**：审计日志是发布/排障证据，不要提供删除接口；备份/迁移随项目目录整体处理。
