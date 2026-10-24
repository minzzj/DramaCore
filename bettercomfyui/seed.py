"""种子管理：让「这一版很好看」可以被复现。

## 为什么需要单独一层

方案要求「种子 随机/固定（可复现）」。但"可复现"不是一个参数，而是三件事：

1. **要求**：用户指定固定 seed，或要求随机（`Segment.seed`）；
2. **实际**：执行层究竟用了哪个 seed（随机时二者不同，`Segment.resolved_seed`）；
3. **逐候选**：每个候选版本各自用了什么 seed（`SegmentAttempt.used_seed`）。

失败模式很常见：**只记录"要求"**。于是随机模式下跑出一个好镜头，
却永远不知道该填哪个种子去复现它 —— 素材白攒了，时间白花了。

## 设计

* `derive_seed(project_id, segment_id, version, attempt_index)` —— **确定性**推导：
  同一段同一版本同一尝试序号永远得到同一个种子。
  这样"重跑"得到相同结果，"换一版重做"自然得到不同种子（不必手动改）。
* 用户显式指定的 `seed` 优先，`derive_seed` 只作为未指定时的兜底。

推导用 SHA-256 而不是 `hash()`：Python 的 `hash()` 对 str 会随
`PYTHONHASHSEED` 变化，**跨进程不稳定**，用它做"可复现"是自欺欺人。
"""

from __future__ import annotations

import hashlib

#: 种子上限（与多个执行层一致的常见上界）
SEED_MAX = (1 << 53) - 1


def derive_seed(project_id: str, segment_id: str, version: str,
                attempt_index: int = 1) -> int:
    """确定性地推导一个种子。

    输入相同 → 输出永远相同（跨进程、跨机器稳定）。
    用法：随机模式下的兜底种子，保证"同一版本重跑得到同一结果"。
    """
    key = f"{project_id}|{segment_id}|{version}|{int(attempt_index)}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    # 取 8 字节 -> 63 位整数，再对 SEED_MAX 取模，保证落在安全范围内
    value = int.from_bytes(digest[:8], "big", signed=False)
    return value % (SEED_MAX + 1)


def resolve_seed(*, requested: int | None, project_id: str, segment_id: str,
                 version: str, attempt_index: int = 1) -> int:
    """决定这次生成该用哪个种子。

    * `requested` 不是 None -> **原样使用**（用户要求固定种子时必须尊重，不能"顺手随机化"）；
    * `requested` 是 None -> 用 `derive_seed` 兜底（确定性，可复现）。

    注意：负数或超出范围的 requested 视为"无效"，抛错而不是静默替换 ——
    静默替换会让用户以为固定种子生效了，实际没有。
    """
    if requested is None:
        return derive_seed(project_id, segment_id, version, attempt_index)
    if not isinstance(requested, int) or isinstance(requested, bool):
        raise ValueError(f"seed 必须是整数或 None，收到 {type(requested).__name__}")
    if requested < 0:
        raise ValueError(f"seed 不能为负：{requested}")
    if requested > SEED_MAX:
        raise ValueError(f"seed 超出允许范围（最大 {SEED_MAX}）：{requested}")
    return requested


def is_reproducible(segment_seed: int | None, resolved: int | None) -> bool:
    """判断该段是否**留有可复现信息**。

    只要 `resolved_seed` 有值就算可复现（哪怕用户当初要求的是随机）——
    因为我们已经知道实际用了哪个种子。
    """
    return resolved is not None
