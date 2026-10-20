# Better ComfyUI · 接口契约（CONTRACTS）v1.2 — 冻结版

> **本文件是全员唯一接口真源。** 任何模块之间的交互只能通过本文件定义的
> 名称、签名和语义。若发现契约有缺陷：**先在本文件提出修订并说明理由，
> 由 Lead 判定是否 v2**；严禁私下改签名或新增同义字段。
>
> **v1.2 修订摘要**（2026-10-16，Lead 裁定，均由 teammate 报出的真实歧义触发）：
> 1. **人工审核周期的初始状态 = `AWAITING_REVIEW`**。方案状态转移表中
>    **不存在** target=`AWAITING_REVIEW` 的边，因此它是初始状态而非转移目标。
>    → **人工周期不经过 `SCANNING`/`CLASSIFYING`**（那是模型模式的路径）。
>    → 严禁为图省事伪造 `VALIDATING → AWAITING_REVIEW` 的边。
>    → 人工模式下确定性校验失败走真实边 `RECEIVED → VALIDATING → FAILED`。
> 2. **`FALLBACK` 时 `ReviewRecord.version` = 被选中的最高分候选版本**
>    （即 LOCK 授权的那个版本），**不是**当前落选版本；
>    且 `record.content_hash` 必须等于该被选中版本的 content_hash。
>    理由：`version + content_hash + lock_source` 必须与 LOCK 三者自洽，否则违反 R3。
>    若「当前版本」与「被选中候选」是两个不同版本号，它们是**各自独立的
>    `review_cycle_id`**，FALLBACK 结论只提交到被选中版本的那个 cycle。
> 3. `GlobalParams` 新增 `comfyui_base_path` / `comfyui_endpoint`（默认空字符串）。
>    **禁止在代码中硬编码 ComfyUI 绝对路径**；机器相关路径写 `config/user.yaml`，
>    保持 `config/default.yaml` 可移植。未配置时必须如实报告「未配置」，不得猜路径。
> 4. `FAILED` 的终态语义澄清：`FAILED → RECEIVED`（技术恢复/重新提交）是
>    《方案》第七部分·六 表格明确列出的边，属**允许例外**。
>    终态约束的正确表述是「终态不得有**质量重试**出边」。
>
> **v1.1 修订摘要**（同日，先前裁定）：
> 1. 原子写临时文件名唯一化：`{name}.{pid}.{token}.tmp`（原固定 `{name}.tmp` 会两进程互踩）；
>    并明确「唯一 tmp **不能替代**文件锁」。
> 2. 新增 `AssetStore.record_suspect/list_suspects/resolve_suspect`，
>    疑似重复落 **独立文件 `suspects.json`**（保证 `meta.json` 字节不变，使「绝不覆盖」可字节断言）。
> 3. 澄清类型归属：`ProjectPaths` 在 `store/paths.py`；`Config` 定义在 `models/entities.py`、
>    加载层在 `config.py`。
> 4. **新增 R9**：`stage → ResumeTarget` 映射表 + 检查点自描述要求。
> 5. 事件日志 schema 新增必填 `namespace` 字段。
> 6. 新增「顶层包相对导入陷阱」警告（子包用 `..`，`bettercomfyui/` 下用 `.`）。
>
> 依据：《BetterComfyUI完整方案.docx》第七部分（标识体系/状态码/状态转移/并发安全/
> 枚举/元数据）、《BetterComfyUI_AI_Agent开发任务书.md》第 4、6、9 节、
> 《BetterComfyUI_完整总流程图.dot》节点名。
>
> 枚举真源：`bettercomfyui/models/enums.py`。本文件不重复定义枚举取值。

---

## 0. 五条硬规则（违反即视为缺陷，验收不通过）

| # | 规则 | 后果 |
|---|---|---|
| R1 | **存在性判断只能由程序做。** AI 只允许输出「新增素材提示词」，不得输出「已存在/不存在」结论。 | 误判导致重复生成或覆盖角色，跨章节身份漂移 |
| R2 | **素材库只追加不覆盖。** 任何路径下不得出现「原地覆盖旧素材文件」的代码。新内容一律 = 新版本目录。 | 破坏版本可追溯与审核结论有效性 |
| R3 | **LOCK 绑定 `asset_id + version + content_hash`。** 新版本绝不继承旧 LOCK/旧审核结论。 | 未审核素材进入生成 |
| R4 | **CNT1/CNT2/CNT3 语义隔离。** 三者互不累加、互不重置；技术失败（`ErrorClass != QUALITY_REVIEW`）绝不消耗 CNT2/CNT3。 | 计数器口径污染，静默掩盖真实重做次数 |
| R5 | **项目隔离。** 任何读写素材/章节/进度/输出的路径必须包含 `project_id`。跨项目查找一律禁止（公共库除外，且必须显式标注 `LibraryScope.PUBLIC`）。 | 数据串用 |

---

## 1. 目录与文件布局（唯一合法布局）

```
<repo root>/
  config/
    default.yaml            # 默认配置（随包发布，代码不得修改）
    user.yaml               # 用户覆盖（可缺省；若存在，优先级 > default）
  projects/
    <project_id>/           # project_id = 小写 UUID4 hex（32 字符，无连字符）
      project.json          # 项目元数据（显示名、创建时间、剧本 id、配置快照）
      config.yaml           # 项目级配置覆盖（优先级 > user.yaml）
      chapters/
        <chapter_id>.json   # chapter_id = "CH{序号，3位补零}"，如 CH001
      assets/
        <asset_type 小写>/
          <asset_id>/
            asset.json      # 素材条目元数据（不含版本内容）
            versions/
              v<int>/       # v1, v2, ... 只追加
                file.<ext>  # 素材文件本体
                meta.json   # 该版本元数据（含 content_hash / review / lock）
      meta.json             # 素材索引（读写必须经过 store/atomic.py 的文件锁）
      suspects.json         # （v1.1）疑似重复待确认队列，独立文件 + 独立锁
      segments/
        <segment_id>.json   # segment_id = "{project_id}_CH{n}_SEG{m}" 逻辑段 ID，重生成不变
      output/
      checkpoints/
        <stage>.<seq>.json
      logs/
        events.jsonl        # 追加式事件日志（每行一个 JSON 对象）
```

**绝对禁止**：在 `projects/<project_id>/` 之外写任何项目数据；
在代码中硬编码绝对路径（一律经由 `Config` 解析）。

---

## 2. 标识符规范（照搬方案第七部分 · 三/四/五）

| 名称 | 格式 | 生成函数（唯一入口） |
|---|---|---|
| `project_id` | 小写 UUID4 hex（32 字符） | `store.ids.new_project_id()` |
| `asset_id` | `{项目名}_{类型}_{序号}_{uuid8}` | `store.ids.new_asset_id()` |
| `version` | `v{int}`（字符串，含 v） | `store.ids.next_version(v)` |
| `content_hash` | `sha256:{64位小写hex}` | `store.ids.content_hash_of(path)` |
| `lock_id` | `LOCK_{asset_id}_{version}` | `store.ids.lock_id()` |
| `suspect_id` | `SUSPECT_{归一化名}_{时间戳}_{uuid8}` | `store.ids.new_suspect_id()` |
| `chapter_id` | `CH{3位序号}` | `store.ids.chapter_id()` |
| `segment_id` | `{project_id}_CH{n}_SEG{m}` | `store.ids.segment_id()` |
| `segment_version` | `v{int}` | 同上 `next_version` |
| `attempt_id` | `ATTEMPT_{segment_id}_{version}_{序号}` | `store.ids.attempt_id()` |
| `audit_id` | `AUDIT_{segment_id}_{version}` | `store.ids.audit_id()` |
| `review_cycle_id` | `{对象类型}:{对象ID}:{version}` | `store.ids.review_cycle_id()` |
| `mod_id` | `MOD_{对象类型}_{对象ID}_{uuid8}` | `store.ids.mod_id()` |
| `cascade_id` | `CASCADE_{segment_id}_{uuid8}` | `store.ids.new_cascade_id()` |
| `cnt_id` | `CNT_{对象ID}_{计数类型}` | `store.ids.cnt_id()` |
| `arb_id` | `ARB_{人工审核ID}_{模型审核ID}` | `store.ids.arb_id()` |
| `abort_id` | `ABORT_{审核记录ID}` | `store.ids.abort_id()` |
| `finalization_token` | 小写 UUID4 hex | `store.ids.new_finalization_token()` |
| `idempotency_key` | `{namespace}:{sha256(canonical_json)}` | `store.ids.idempotency_key()` |

**R6：短码必须有冲突检测。** `new_asset_id()`/`new_suspect_id()` 等生成短码后，
必须由调用方在真实索引中查重；重复则重新生成（方案明确要求）。

**R7：ID 里不得包含可变状态。** `待验证/已通过/已降级` 一律是独立字段。

---

## 3. 原子写入与并发（`store/atomic.py`）

```python
def canonical_json(obj) -> str
    """确定性 JSON：sort_keys=True, separators=(',',':'), ensure_ascii=False,
    float 归一化（禁止 NaN/Inf）。同一逻辑对象两次调用必须得到完全相同的字符串。"""

def atomic_write_json(path: Path, obj, *, fsync: bool = True) -> None
    """先写同目录临时文件 -> flush -> fsync -> os.replace() 覆盖正式文件。
    （v1.1 修订）临时文件名必须唯一：{name}.{pid}.{token}.tmp，仍以 .tmp 结尾，
    且必须与目标同文件系统。理由：固定名 {name}.tmp 在两进程同写时会互相踩坏。
    注意：唯一 tmp 名**不能替代**文件锁；locked_update_json 仍须整周期持锁。"""

def locked_update_json(path: Path, mutator: Callable[[dict], dict], *, timeout_s: float = 30.0) -> dict
    """跨平台文件锁（锁文件 .{文件名}.lock，由 store/lockfile.py 提供）。
    在整个 read-modify-write 周期内持锁（方案第七部分·七 明确要求）。
    mutator 返回新 dict；返回写入后的内容。超时抛 StoreError。"""

def file_lock(lock_path: Path, *, timeout_s: float = 30.0)
    """上下文管理器。需在 Windows 与 POSIX 均可工作；同一进程内可重入。"""
```

**要求**：`meta.json` 的所有更新（新增素材、追加版本、写 LOCK）
**必须**走 `locked_update_json`；任何直接 `json.dump(open(meta.json,'w'))` 都是缺陷。

---

## 4. `store/` 公开接口（负责人：teammate `store-project`）

```python
# bettercomfyui/store/projects.py
class ProjectStore:
    def __init__(self, root: Path, config: Config) -> None: ...
    def create(self, display_name: str) -> dict            # 建 project_id + 目录 + 空素材库 + 默认配置
    def list(self) -> list[dict]                           # 扫描 projects/ 下 project.json
    def open(self, project_id: str) -> dict                # 加载配置/章节/素材库/进度/最近检查点
    def close(self, project_id: str) -> dict               # 保存状态后再关闭
    def delete(self, project_id: str, *, confirm_token: str) -> None
                                                           # 二次确认；有运行任务或未保存状态必须拒绝
    def switch(self, from_id: str | None, to_id: str) -> dict
                                                           # 先保存 from 再打开 to
```

```python
# bettercomfyui/store/assets.py
class AssetStore:
    def __init__(self, project: ProjectPaths) -> None: ...
    def load_index(self) -> dict                 # 读 meta.json（持锁读）
    def list_assets(self, *, asset_type=None, scope=None) -> list[dict]
    def find_by_normalized_name(self, normalized: str, *, scope=None) -> list[dict]
                                                 # 可能返回多个 -> 调用方据此判 SUSPECT
    def add_version(self, asset_id, *, source, file_path, status, extra=None) -> dict
                                                 # 只追加；自动 version+1 + content_hash
    def create_asset(self, *, asset_type, name, source, library_scope) -> dict
    def set_version_status(self, asset_id, version, status, **fields) -> dict
    def set_lock(self, asset_id, version, *, lock_source, content_hash) -> dict
                                                 # 必须校验 content_hash 与磁盘一致
    def clear_lock(self, asset_id, version) -> dict
    def get_version(self, asset_id, version) -> dict | None
    def resolve_lock(self, asset_id, version) -> dict | None
                                                 # 返回 None 表示该版本不可用于生成
    def references_of(self, asset_id) -> list[dict]
    def record_reference(self, asset_id, version, ref) -> None

    # --- v1.1 新增：疑似重复记录（prompt/ 需要，供 build_suspect_queue 使用） ---
    def record_suspect(self, *, normalized_name, candidate_name, candidate_type,
                       candidates: list[str], reason: str) -> dict
        """写入 projects/<pid>/suspects.json（独立文件 + 独立锁）。
        **必须幂等**：同一 (normalized_name, candidate_name, candidates 集合)
        重复调用不得产生重复记录。
        独立文件的原因：保证 meta.json 字节不变，使「歧义时绝不覆盖」可被字节断言证明。"""
    def list_suspects(self, *, resolved: bool | None = None) -> list[dict]
    def resolve_suspect(self, suspect_id, *, resolution: str, note: str = "") -> dict
        """resolution ∈ {"reuse", "create_new", "discarded"}。
        discarded 必须写理由 —— 疑似重复不允许静默丢弃。"""
```

**store 的类型归属（v1.1 澄清）**
- `ProjectPaths` 定义在 `bettercomfyui/store/paths.py`（含 R5 路径越界硬校验），由
  `store/__init__.py` 与 `store/projects.py` 再导出。
- `Config` 的**定义**在 `bettercomfyui/models/entities.py`；**加载层**在 `bettercomfyui/config.py`
  （`load_config` / `config_from_dict` / `ConfigError` / `resolve_secret` / `dump_config`）。
  store 直接 `from ..config import Config, ConfigError`，无循环依赖。
- `AssetStore.__init__` 接受 `ProjectPaths | Path | str`（超集，不算改签名）。

**⚠️ 顶层包相对导入陷阱（v1.1，实测踩过）**
`bettercomfyui` 是**顶层包**。因此：
- 子包内的文件（`store/`、`review/`、`prompt/`、`director/`、`executors/`、`pipeline/`）
  用**双点**：`from ..models.enums import ...` ← 正确。
- 直接位于 `bettercomfyui/` 下的文件（如 `config.py`）用**单点**：
  `from .models.enums import ...` ← 正确；写双点会报
  `ImportError: attempted relative import beyond top-level package`。
每个模块都必须带一条 import 冒烟测试。

**R9：`stage` → `ResumeTarget` 映射（v1.1 新增）**

`ResumeTarget` 表示**从哪个子流程重新进入**，不是「检查点本身是审核阶段」。
`store/checkpoints.py` 必须公开 `STAGE_TO_RESUME_TARGET`：

| ResumeTarget | 覆盖的 Stage |
|---|---|
| `G`（素材） | `PROJECT`, `SCRIPT_PARSE`, `ASSET_MATCH`, `ASSET_GENERATE`, `ASSET_REVIEW` |
| `P`（导演台） | `DIRECTOR_PLAN`, `SEGMENT_GENERATE` |
| `R`（审核） | `SEGMENT_REVIEW`, `CASCADE`, `SPLICE`, `FULL_REVIEW`, `OUTPUT` |

无检查点 → 回退 `G`（保守，最安全）。
**额外要求**：`save()` 必须把当时的 `resume_target` 一并写入检查点（self-describing）；
`resolve_resume_target()` 优先读检查点内存档的值，缺失时才回退到该映射表。
这样以后调整映射表不会改变历史检查点的恢复语义。

**并发安全标识（v1.1 对齐第 3 节）**

| 标识 | 格式 | 作用 |
|---|---|---|
| 文件锁标识 | `.{文件名}.lock` | 防止并发读改写冲突，整周期持锁 |
| 原子写入标识 | `{文件名}.{pid}.{token}.tmp` | 避免半写入文件被当成有效数据，`os.replace()` 收尾 |

```python
# bettercomfyui/store/inject.py
def build_existing_asset_manifest(store: AssetStore, *, library_scope=None) -> str
    """R1 落地：程序读取素材库清单，输出「类型 + 序号 + 名称 + 锁定状态」文本，
    原样注入系统提示词（A1 -> A2 -> A3 -> A4）。
    素材库为空时返回空字符串，调用方据此走 A1B 的「空」分支跳过注入。"""
```

```python
# bettercomfyui/store/checkpoints.py
class CheckpointStore:
    def save(self, stage: Stage, payload: dict) -> Path   # 只追加，含 seq 与时间戳
    def latest(self, *, at_or_before: Stage | None = None) -> dict | None
    def resolve_resume_target(self) -> ResumeTarget       # 恢复目标：G / P / R
```

**store 不得 import `prompt/`、`review/`、`director/`、`pipeline/`**（依赖方向单向）。

---

## 5. `prompt/` 公开接口（负责人：teammate `prompt-assets`）

```python
# bettercomfyui/prompt/gateway.py
class PromptModelGateway(Protocol):
    def complete(self, *, system: str, user: str, **params) -> str: ...

class GatewayError(Exception):
    """接口未配置 / 认证失败 / 超时。必须携带 ErrorClass。"""

def build_gateway(cfg: Config) -> PromptModelGateway
    """按 cfg.model_provider 选择 external_api / local_qwen / custom。
    未配置必要字段（如 API key、endpoint）时必须抛 GatewayError 并给出
    可操作的错误信息——绝不静默回退到 mock。"""
```

```python
# bettercomfyui/prompt/normalize.py
def normalize_name(name: str) -> str
    """去首尾与内部空格、统一小写、统一标点（全角->半角，中英标点统一）。
    必须是纯函数、幂等：normalize(normalize(x)) == normalize(x)。"""

def is_suspect(candidate: str, existing: list[str]) -> bool
    """归一化后不相等但高度相近（如仅差标点/序数词/繁简）时返回 True。"""
```

```python
# bettercomfyui/prompt/validate.py
class ScriptValidationError(Exception):
    def __init__(self, errors: list[dict]) -> None: ...   # [{"path","code","message"}]

def parse_script_output(raw: str, *, output_format: str = "json") -> dict
    """解析模型输出：优先结构化 JSON，兼容特殊分隔符文本。
    解析失败抛 ScriptValidationError —— 绝不返回部分结果。"""

def validate_script(parsed: dict) -> dict
    """结构校验：缺字段、无效引用、重复 ID、格式错误一律抛 ScriptValidationError。
    校验通过前不得进入素材生成（任务书第 9 节验收 2）。"""
```

```python
# bettercomfyui/prompt/match.py
class MatchResult(NamedTuple):
    outcome: MatchOutcome
    asset_id: str | None
    version: str | None
    suspect_id: str | None
    reason: str

def match_candidate(candidate: dict, store: AssetStore) -> MatchResult
    """R1 落地：三分支确定性匹配，纯程序逻辑，不允许调用 AI。
    EXISTS_REUSE -> 复用既有版本，不重复生成
    NEW_GENERATE -> 进入生成队列
    SUSPECT_DUPLICATE -> 建 SUSPECT_* 记录，禁止覆盖、禁止静默丢弃"""

def build_suspect_queue(store: AssetStore) -> list[dict]
```

**prompt 依赖 `store`，不得依赖 `review`/`director`/`pipeline`。**

---

## 6. `review/` 公开接口（负责人：teammate `review-lock`）

### ⚠️ v1.2：人工审核周期的状态路径（规格缺口，已裁定）

方案第七部分·六 的转移表里**没有任何 target=`AWAITING_REVIEW` 的边**。
这不是遗漏，而是设计：`AWAITING_REVIEW` 是**初始状态**，初始状态不属于转移。

| 模式 | 状态路径 |
|---|---|
| 人工 | `AWAITING_REVIEW`(初始) → `ALLOWED` / `BLOCKED` / `TIMEOUT`；`TIMEOUT → SCANNING`(自动切模型) 或 `→ ARBITRATING` |
| 模型 | `RECEIVED → VALIDATING → SCANNING → CLASSIFYING → ALLOWED/BLOCKED/FALLBACK` |
| 人工 + 校验失败 | `RECEIVED → VALIDATING → FAILED`（真实边，不得绕行） |

**禁止**伪造 `VALIDATING → AWAITING_REVIEW` 的边。验证员应显式断言
「转移表中不存在 target=`AWAITING_REVIEW` 的边」，防止后人加假边。

### FALLBACK 的版本指向（v1.2）

`FALLBACK` 时 `ReviewRecord.version` = **被选中的最高分候选版本**（LOCK 授权的那一个），
`record.content_hash` 必须等于该版本的 content_hash。
「当前落选版本」与「被选中候选版本」若是两个版本号，则各有独立的 `review_cycle_id`；
FALLBACK 结论**只**提交到被选中版本的那个 cycle。

```python
# bettercomfyui/review/state.py
class ReviewCycle:
    """一个对象版本对应一个正式审核周期。状态转移必须走 REVIEW_TRANSITIONS。"""
    state: ReviewState
    review_cycle_id: str
    def fire(self, event: str, **payload) -> ReviewState
        """非法「状态+事件」组合抛 IllegalTransition，不得静默忽略。"""

# bettercomfyui/review/counters.py
class CounterStore:
    """绑定逻辑任务 ID，不绑定版本号。"""
    def increment(self, obj_id: str, kind: CounterKind) -> int
    def get(self, obj_id: str, kind: CounterKind) -> int
    def reset(self, obj_id: str, kind: CounterKind) -> None
        """唯一合法重置场景：人工超时切换到模型模式时 CNT1 从 0 开始。
        其余任何地方调用 reset 都是缺陷。"""

# bettercomfyui/review/asset_review.py
class AssetReviewer:
    def review_version(self, asset_id, version, *, mode: ReviewMode, material: dict) -> dict
        """素材版本审核：人工/模型双模式。
        返回 {"state", "review_cycle_id", "score", "reason", "lock_source"|None}"""

    def on_human_timeout(self, cycle_id: str) -> dict
        """超时切换：写 ReviewMode.AUTO_SWITCHED 事件、CNT1 归零、发非阻断通知。
        注意：只有「持续有效审核操作」才暂停超时计时（H1B/H1C）。"""

    def submit_human_result(self, cycle_id: str, *, passed: bool, operator: str,
                            client_ts: float | None = None) -> dict
        """人工结果提交。必须写统一后端时间戳（服务端 now，不信客户端时钟）。"""

    def submit_model_result(self, cycle_id: str, *, score: float, passed: bool) -> dict

# bettercomfyui/review/arbiter.py
def arbitrate(cycle: dict) -> dict
    """H_arb：统一后端时间戳仲裁（方案第七部分·一 第 9/10 条）。
    人工时间戳 < 模型完成时间戳 -> 人工胜出 + 发 ABORT_SIGNAL
    人工时间戳 >= 模型完成时间戳 -> 模型胜出，人工结果仅写日志
    平局时模型胜出（方案参数表明确：等于时模型胜出）。
    返回 {"winner": "HUMAN"|"MODEL", "abort_signal": bool, "arb_id": str}"""

# bettercomfyui/review/commit.py
def commit_final(cycle: dict, *, store: AssetStore) -> dict
    """唯一最终结果提交点。
    条件更新：仅当 review_cycle_id 的 finalization_token 尚未被提交时才成功。
    只有首次提交者可以触发下游（写 LOCK / 更新素材库）。
    迟到结果 -> 记录为 ABORTED 并归档，绝不改变当前版本状态。"""
```

**并发与迟到结果（任务书第 9 节验收 9）**：同一 `review_cycle_id` 并发提交时，
**恰好一个**返回 `committed=True`，其余返回 `committed=False, reason="already_finalized"`，
且不得产生第二次 LOCK 写入。

**review 依赖 `store`；不得依赖 `prompt`/`director`/`pipeline`。**

---

## 7. `director/` 与 `executors/` 公开接口（负责人：teammate `director-exec`）

```python
# bettercomfyui/executors/base.py
@dataclass(frozen=True)
class JobRef:
    job_id: str
    executor: str

@dataclass(frozen=True)
class JobState:
    status: JobStatus
    progress: float | None
    outputs: list[Path]
    error: str | None
    error_class: ErrorClass | None

class Executor(Protocol):
    name: str
    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef: ...
    def poll(self, ref: JobRef) -> JobState: ...
    def cancel(self, ref: JobRef) -> bool: ...
    def capabilities(self) -> dict
        """必须如实声明：{'image': bool, 'video': bool, 'first_last_frame': bool,
        'tail_frames': int|None, 'verified': bool, 'notes': str}。
        未在本机验证的能力必须 verified=False 并在 notes 写明原因。"""
```

**执行器清单（R8：能力必须如实声明，禁止假成功）**

| 执行器 | 用途 | 本机状态 |
|---|---|---|
| `MockExecutor` | 无模型时证明编排闭环；产出确定性的占位文件 | 可用 |
| `ComfyUIHttpExecutor` | 连接本机 ComfyUI（`/prompt`、`/history`、`/interrupt`） | 0.22.3 可连；**节点与模型未就绪 -> verified=False** |
| `DirectorNodeExecutor` | 适配 ChainDirector / AIMixer / SmartDirector / H3-Chinese-Director | 节点未安装 -> verified=False |

```python
# bettercomfyui/director/plan.py
def plan_segments(script: dict, cfg: Config, *, locked_assets: dict) -> list[dict]
    """建立段计划（J_IN -> J_PARAM -> O -> P）。
    每段必须记录：segment_id、segment_index、prompt、asset_refs[]（含 version）、
    generation_params、first_frame、last_frame、dependency_refs。"""

# bettercomfyui/director/generate.py
class SegmentGenerator:
    def generate(self, segment: dict, *, executor: Executor) -> dict
        """P：分段视频生成。只允许使用 resolve_lock() 通过的素材版本（R3）。
        技术失败 -> ErrorClass 分类 + 独立技术重试计数，不触碰 CNT2/CNT3。"""

# bettercomfyui/director/review.py
class SegmentReviewer:
    def review(self, segment: dict, version: str) -> dict
        """R_seg：仅审核当前段当前版本，绑定 AUDIT_{segment_id}_{version}。
        R1a 四维：画面连贯 / 角色一致 / 动作合理 / 无闪烁。"""

    def verify_edge(self, seg_n: dict, seg_n1: dict) -> dict
        """N1_EDGE：仅验证相邻边界，不触发 R_full。"""

# bettercomfyui/director/cascade.py
class CascadeController:
    def on_segment_changed(self, segment: dict) -> dict
        """CASCADE_CHECK：该段变化是否影响后续段依赖？
        只有「参考帧/动作/时间轴依赖确实失效」才继续标记后续段。
        必须检查 max_cascade_depth —— 超限后按策略阻止交付/请求人工/降级，
        禁止无限循环（任务书第 6 节底线）。"""

    def mark_next(self, segment: dict) -> dict
        """Y2：标记第 N+1 段待验证，为该段建立独立 CNT3。"""

    def run(self, segment: dict, *, executor: Executor) -> dict
        """N1：级联段重生成。CNT3+1（与 CNT2 完全独立）。
        达上限 -> 取最高分候选 + CASCADE_FALLBACK + CASCADE_DEGRADED。"""
```

**CNT2/CNT3 边界（R4 落地，必须写单元测试）**

| 场景 | 计数 |
|---|---|
| 当前问题段质量不合格 -> REGEN | `CNT2 + 1` |
| 级联段 N1 重生成 | `CNT3 + 1` |
| API 超时 / 节点异常 / 文件损坏 | **都不计**，只加技术重试计数 |
| 版本号递增 | **都不清零** |

---

## 8. `pipeline/` 公开接口（负责人：Lead + teammate `director-exec` 协作，Lead 独占写入）

```python
# bettercomfyui/pipeline/splice.py
def select_final_versions(segments: list[dict]) -> list[dict]
    """只挑选符合项目策略的片段进入拼接；兜底段正常参与，来源写入元数据。"""

def splice(segments: list[dict], cfg: Config, *, workdir: Path) -> Path

# bettercomfyui/pipeline/encode.py
def encode(src: Path, cfg: Config, *, out_path: Path) -> Path
    """音频拼接 / 转场 / 字幕 / 水印 / 格式 / 编码 / CRF。"""

# bettercomfyui/pipeline/full_review.py
def full_review(movie: Path, cfg: Config) -> dict
    """R_full：仅在最终拼接后、S_OUT 之前执行一次（绝不因单段重生成反复触发）。
    R1b 四维：叙事流畅 / 音画同步 / 节奏合理 / 无明显跳变。"""

def apply_full_policy(result: dict, cfg: Config) -> dict
    """阈值未达 -> 按 cfg.full_review_policy 显式选择：
    BLOCK_OUTPUT（保留检查点等待人工）或 DEGRADE_OUTPUT（记录 FULL_REVIEW_DEGRADED）。
    没有配置时必须报错，不得隐式选择。"""

# bettercomfyui/pipeline/deliver.py
def write_output(movie: Path, cfg: Config, *, metadata: dict) -> dict
    """S_OUT + OUT_CFG + OUT1-4。
    输出前必须校验：文件存在、可读取、时长、音轨（方案「输出前检查」）。
    返回 {"out_path","cover_path","metadata_path","checks": {...}}"""
```

---

## 9. 配置（`config/default.yaml` + `Config` 类，Lead 独占写入）

优先级：**项目 config.yaml > 用户 user.yaml > 默认 default.yaml**（方案参数表明确）。

必须可配（不得硬编码为不可修改规则）：

```yaml
prompt:  { model_provider, output_language, output_format, story_granularity,
           system_prompt_template, inject_existing_assets, normalize_names }
assets:  { resolution, steps, sampler, cfg, seed, batch_count, per_class_limit }
asset_review: { default_mode, human_timeout_minutes: 10, timeout_pause_on_active_op: true,
                model_candidate_limit: 3, score_threshold: 70,
                score_dimensions: [画质, 一致性, 合规, 构图],
                on_timeout: switch_to_model, arbitration: backend_timestamp_sole,
                new_version_requires_review: true, fallback: highest_of_candidates }
director: { segment_seconds: 10, resolution: 1080P, fps: 24, tail_frames: 22,
            first_frame: first, last_frame: last, vram_tier: auto,
            pruned_model: true, reference_mode: auto }
video_review: { audit_scope: both, threshold_segment: 70, threshold_full: 70,
                cnt2_limit: 3, cnt3_limit: 3, regenerate_granularity: segment,
                max_cascade_depth: 8, full_review_policy: null }  # null -> 使用时必须报错
encode:  { audio_join: auto, transition: none, subtitle: false, watermark: false,
           format: MP4, codec: H.264, crf: 18, cover: first_frame }
global:  { log_level: INFO, log_all_manual_and_auto_switch: true,
           tech_retry_limit: 3, checkpoint_every_stage: true }
```

**`full_review_policy: null` 时的行为**：`apply_full_policy` 抛
`ConfigError("full_review_policy 未显式配置：必须选择 BLOCK_OUTPUT 或 DEGRADE_OUTPUT")`。
理由：方案明确「不得隐式采用未记录的行为」。

---

## 10. 事件日志（`logs/events.jsonl`，全模块统一）

每行一个 JSON，字段固定：

```json
{"ts":"ISO8601+08:00","namespace":"BETTERCOMFYUI|REVIEW|CASCADE","event":"...",
 "project_id":"...","stage":"...","object_id":"...","version":null,
 "mode":null,"detail":{},"severity":"INFO"}
```

（v1.1）`namespace` 为**必填**字段，用于按来源过滤事件。
追加写要求：append 模式、单次 write 写入一整行、UTF-8、不做 read-modify-write；
**写日志失败只告警，绝不中断主流程**。

**必须记录**（方案 Z4/Z5）：状态变化、参数快照、重试原因、版本关系、
人工操作、模式切换、仲裁结论、降级与兜底、通知投递。

**通知不得阻断主流程**：通知失败只写 `severity=WARN` 日志，绝不让主流程抛错。

---

## 11. v1.2 已实现 API 的精确登记（**以代码实测为准，非推测**）

以下均为各模块**实测确认**的行为，登记在此以免后来者按旧签名调用而踩坑。

### 11.1 `store/` 补充（store-project 实测）

| 项 | 实测行为 |
|---|---|
| `set_lock(asset_id, version, *, lock_source, content_hash, review_cycle_id=...)` | **`review_cycle_id` 为必填关键字**（models 层不变量：LOCKED 必须可追溯到审核周期）；返回**版本记录**，`lock_id` 在 `extra.lock_id` |
| 同版本已 LOCK 且结论不同 | 抛 `LockAlreadyExistsError`，必须先 `clear_lock(reason=...)`。这是 R3 的**第二道防线**，**不替代** `finalization_token` 的唯一提交语义 |
| `resolve_lock(asset_id, version)` | 返回**扁平** LOCK 记录：顶层含 `lock_id/asset_id/version/content_hash/lock_source/review_cycle_id/absolute_path`；`None` 表示该版本**不可用于生成** |
| **`resolve_lock` 的 `None` vs 抛错（v1.2 修订，重要）** | `None` **只**表示「没有可授权的 LOCK 记录」（无素材/无版本/未 LOCK/记录三要素不自洽/hash 与元数据不一致/**磁盘文件丢失**）。**读取或解析失败一律抛原始 `StoreError`**，并带 `context["error_class"] = ErrorClass.FILE_ENCODING`。理由：`None` 是授权门禁的正常返回值，若把「读不出来」也变成 `None`，调用方无法区分「未审核（正常拒绝）」与「技术故障（应重试/报警）」 |
| **`load_index` 的「不存在」vs「损坏」（v1.2 修订）** | 文件**确实不存在** → 返回空索引（首次写入前的合法状态）；**文件存在但为 0 字节** → 抛 `CorruptFileError`。理由：`read_json` 把「空内容」统一映射为 `default=None`，0 字节 meta.json 会伪装成「文件不存在」，最终让 `resolve_lock` 静默返回 `None` —— 把**索引损坏**误报成**未审核**，且下一次写入会永久覆盖损坏前的内容（不可恢复的数据丢失） |
| `poll` 的 `/queue` 查询失败 | 查询失败（抛错 / HTTP≥400 / 非 dict / 缺 `queue_*` 键）**不等于作业丢失**：应按「状态未知/可重试」继续轮询，重试耗尽才 `UNKNOWN`。只有「查询成功且队列确实没有该作业」才判 `FAILED` |
| `projects/<project_id>/suspects.json` | 疑似重复队列，独立文件 + 独立锁（保证 `meta.json` 字节不变） |
| 原子写 tmp 命名 | **必须唯一化，或至少 `O_EXCL` 抢占固定名并在占用时唯一化回落**。仅「固定名」不够：两进程共用 `{name}.tmp` 会互相踩坏 |
| 锁文件 `.lock` | **永不删除**（删锁文件会让竞争进程拿到不同 inode 的同名锁，属经典锁失效 bug），退出时由内核释放 |

### 11.2 `review/` 补充（review-lock 实测）

| 项 | 实测行为 |
|---|---|
| `AssetReviewer(auto_commit=True)` | **默认 `True`**：结论一到就调 `commit_final`（`review_version` 通过即写 LOCK）。`auto_commit=False` 时只到结论态，由调用方提交 |
| `commit_final(cycle, *, store)` | 输入必须是 `ReviewRecord.to_dict()` 形状，键名为 **`review_state` / `target_id`**（不是 `state` / `asset_id`）；`AssetReviewer(auto_commit=False)` 正好返回该形状 |
| `<project>/review/finalizations.json` | 跨进程 CAS 落盘物。**必须**在 `ProjectPaths.project_dir` 下（R5）；**绝不能**放 `ProjectPaths.root`（那是 `projects/` 基目录，会跨项目串用） |
| `FALLBACK` 时的版本指向 | `record.version` = **被选中的最高分候选版本**；`record.content_hash` = 该版本真实指纹；`lock_id == LOCK_{asset}_{fallback_version}`；只给被选中版本写 LOCK，落选版本保持 `UNLOCKED` |
| `_tech` 计数 key | `CNT_{obj_id}_TECH`，**不走 `store.ids.cnt_id`**（`TECH` 不是 `CounterKind`）。技术重试与 CNT1/2/3 完全分离 |

### 11.3 `director/` 与 `executors/` 补充（director-exec 实测）

| 项 | 实测行为 |
|---|---|
| `plan_segments(script, cfg, *, locked_assets, ids=None, project_id=None, store=None, as_dicts=False)` | `project_id` 有 4 种来源：显式参数 > `script["project_id"]` > `cfg.project_id` > `store=AssetStore.project_id`；**都没有则明确报错**（R5 禁止伪造 segment_id） |
| `as_dicts=True` | 只是 `Segment.to_dict()`，**`asset_refs[].version` 不丢失**（`Segment.__post_init__` 强制要求）；传 `store=` 时还会以真实 LOCK 记录补 `content_hash`/`lock_source` |
| `SegmentReviewer.review(seg, version, *, material=None)` | **优先级：显式 `material` > 注入的 `scorer`**（传了 `material` 就不调 `scorer`，避免两套分数打架）。`verify_edge` 同理（`material` > `edge_scorer`） |
| `SegmentReviewer.review()` 调用顺序 | **必须先登记候选尝试并把 `segment.current_version` 设为待审版本，再 `review()`**；返回 `result["segment"]` 是**深拷贝副本**，调用方必须用它替换本地对象。顺序写错会被 `StaleSegmentVersion` 拒绝（守卫正确） |
| `SegmentStatus` 映射 | `R_seg` 未通过 → `status=REVIEWING` + `review_status=ReviewState.BLOCKED`。**质量不合格 ≠ FAILED ≠ CASCADE_FAILED**，用 `review_status` 承载失败语义，不污染 `SegmentStatus` |
| `CascadeController(counters, *, cfg, ids, generator, reviewer, store, events, project_id, max_cascade_depth, depth_policy, segments)` | 给了 `store`/`scorer`/`edge_scorer` 就**惰性自建** generator/reviewer；缺 `store` 时抛可操作错误而非崩溃 |
| `propagate(root, *, executor, max_depth=None, policy=None)` | 级联**只在依赖确实失效时传播**：需 `CONTENT_DEPENDENCY_KINDS` + `ref_version != 上游 current_version`；裸 `segment_id` → `no_evidence` → **不传播**（正确语义）。返回含 `iterations/stopped_reason/degrade_flag/segments_degraded` |
| `capabilities()` 最小键集 | `image / video / first_last_frame / tail_frames / verified / notes(非空)`；缺任一抛 `ExecutorCapabilityError` |
| `capabilities()` 完整键 | Mock 9 键（+`mock=True`/`video_decodable=False`/`workdir`）；ComfyUIHttp 16 键（+`mock=False`/`reachable`/`verified_reason`/`comfyui_version`/`expected_comfyui_version`/`node_type_count`/`checkpoints`/`video_nodes_present`/`director_nodes_present`/`probe`）；DirectorNode 19 键（+`nodes_available`/`nodes`/`required_nodes`） |
| `JobState` | 字段 `status/progress/outputs/error/error_class` + 新增 **`mock: bool = False`**（`JobRef` 未改）。不变量：`SUCCEEDED` 不得带 `error_class`；`FAILED` 必须带明确 `error_class`；`UNKNOWN` 必须带 `error`；`is_success` 仅 `SUCCEEDED` 为真 |
| `verified=True` 现状 | **当前没有任何执行器返回 `verified=True`**；唯一放行口是 `assert_real_capabilities()`。目标机跑通后由 Lead 显式改 |

### 11.4 `MovieMetadata` 新增字段（Lead，v1.2）

| 字段 | 语义 |
|---|---|
| `generation_provenance: dict` | 生成来源声明：`{"is_mock": bool, "executor": str, "reason": str, "unverified": [str]}`。**必须随成片落盘** —— 成片文件在磁盘上无法区分「模型生成」与「占位媒体」，来源只存在于当次 stdout 就无法长期守住任务书第 9 节末句 |
| `is_mock_output` (property) | `generation_provenance["is_mock"]` 的便捷读取 |
| `SegmentManifestItem.content_hash` | 每段必须留下内容指纹（可追溯到具体文件） |

### 11.5 已知取舍（记录在案，第一阶段不改）

1. `AssetStore.add_version` 在索引锁内完成「分配版本号 + 复制文件 + 写版本 meta」：
   正确性最高，但 GB 级素材会长时间持索引锁。v2 可改「锁内预留 + 锁外复制 + 二次锁内登记」。
2. `ProjectStore.list()` 的 `created_at` 为秒级，同秒创建按 `project_id` 兜底排序（确定性，但非创建顺序）。
3. `ProjectStore.open()` 是纯读（不写 `project.json`）；`create()` 不标记「已打开」，
   否则新建后立即删除会被误判为「未保存状态」。

---

## 12. 变更流程

1. 任何人都**必须**先读本文件与 `models/enums.py` 再动手。
2. 需要新字段 -> 先在本文件对应章节补齐 + 说明理由，再改代码。
3. 契约冲突时**以《方案》第七部分「计数口径、仲裁、版本、最终输出」为准**。
4. Lead 每阶段末跑全量测试 + 契约一致性检查，作为合并门槛。

---

_本文件 v1 冻结于 2026-10-16 23:00，v1.2 更新于 2026-10-16 23:3x（Asia/Shanghai），Lead 维护。_
