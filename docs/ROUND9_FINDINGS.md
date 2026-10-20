# 第九轮进展记录（2026-10-17 凌晨）

> 目标轮次 7/60。本轮补齐**最后一个功能性缺口**：音画参考链路
> （方案要求「参考主体 / 音画参考」，三个导演台的 r2v 组也支持
> `ref_audios` / `ref_videos`）。过程中抓到 3 个真 bug。

---

## 一、缺口：写完了但**忘记接线**

`fusion.locked_ref_media`（把已 LOCK 的音/视频素材解析成 `ref_audios`/`ref_videos`）
在上一轮就写好了，但 **`grep` 显示没有任何节点调用它**。

这正是"代码存在 ≠ 功能可用"的典型：函数有实现、有文档、
看起来完成了，但用户永远碰不到它。

### 表现

`MiniMaxH3Director` 的 r2v 组支持音色参考与参考视频，
但我的「融合·段计划→导演组」节点只会解析**图片**引用，
`ref_audios` / `ref_videos` 永远是空字典。

---

## 二、补齐的内容

### 1. 融合·段计划→导演组：解析音画引用

剧本里可以这样写：

```json
{
  "segment_index": 1,
  "prompt": "主角开口说话",
  "asset_refs": [{"asset_id": "...", "version": "v1"}],
  "audio_refs": [{"asset_id": "...", "version": "v1"}],
  "video_refs": [{"asset_id": "...", "version": "v1"}]
}
```

审计输出新增 `audio_refs` / `video_refs` 与 `ref_counts`（图/音/视各几个），
让"到底几个参考真的进了组"一目了然。

**只在 r2v 模式生效** —— 这是真实包结构决定的：
`pack_i2v_group` 的 `ref_audios`/`ref_videos` 是**固定空字典**，
传进去会被丢掉。所以节点**明确告知**而不是"看起来支持实际无效"。

### 2. 融合·单组打包：新增 6 个音画参考输入

`ref_audio_1..3`（AUDIO）+ `ref_video_1..3`（IMAGE 帧序列）。
在 i2v 模式下若用户连了音画参考，摘要里会**明确写出"已忽略 N 个，请改用参考 r2v"** ——
静默忽略比不支持更糟。

### 3. 新增素材节点：`BetterComfyUI 素材·取已锁定音/视频`

`asset_id(+version)` + 类型（音频/视频）→ `AUDIO` / `IMAGE`，
可直接接单组打包的音画参考口。

### 4. 扩展 `AssetType`：新增 `AUDIO` / `VIDEO`

方案的 `AssetType` 只有人物/场景/物品/分镜四类，
**音视频参考无处归类**。硬塞进 `PROP` 会让素材库分不清
"参考视频"与"道具图"，审核阈值、清单注入、节点槽位都会跟着糊掉。

枚举 docstring 本身就写着「允许按需求扩展」，所以这是合规扩展。
原有四类保留（方案要求"至少四类"）。

---

## 三、本轮抓到的 3 个真 bug

### B-1 插件里的跨顶级包相对导入（**同一个坑第二次**）

```python
from ..pipeline.core import _ffmpeg_exe     # ❌ ImportError
```

`bettercomfyui_comfy` 是**独立顶层包**（ComfyUI 的 custom_nodes 不在 `sys.path` 上），
所以 `..` 直接抛 `attempted relative import beyond top-level package`。

**这个坑在 `bettercomfyui/config.py` 里踩过一次**，这次在 `fusion.py` 又踩了。
已改为绝对导入，并补了一条 **AST 静态扫描**测试，
把整个插件里所有 `level >= 2` 的导入都禁掉 —— 这类问题不该靠记性。

### B-2 参考视频槽位**没有越界检查**

实测：

| 槽位越界 | 结果 |
|---|---|
| 参考图 9 | ✅ 报错 |
| 参考音频 3 | ✅ 报错 |
| **参考视频 3** | ❌ **放行** |
| **视频音轨 3** | ❌ **放行** |

真实包对这三类都做上限裁剪（`if 0 <= i < MAX_REFERENCE_VIDEOS`）。
我漏了两个循环，后果是：**越界的视频槽会被我保留、被真实包丢掉** ——
"我这边看着有参考视频、实际导演台看不到"。这类不一致最难查。

已补上视频与视频音轨的检查（四个循环齐了）。

### B-3 `set_lock` 的守卫被我当成了障碍（其实它是特性）

写测试时我直接调 `store.set_lock(...)`，被拒绝：

> `set_lock 必须提供 review_cycle_id`：LOCKED 版本必须能追溯到授权它的审核周期。

第一反应是"补个参数绕过去"。但正确做法是**走真实审核链路**
（`AssetReviewer.review_version` → `commit_final`）。
守卫存在的意义就是防止"未经审核直接加锁"，
测试绕过它就等于把 R3 的验证变成假的。已改走真实链路。

---

## 四、实测（真实文件 + 真实审核链路）

```
wav : 176444 bytes, 44100Hz, 2ch
mp4 : 2784 bytes
locked: audio=音画参考测试_audio_1_47847060@v1
locked: video=音画参考测试_video_1_99532927@v1

ref_audios[0]: keys=['sample_rate', 'waveform'] sample_rate=44100 waveform=(1, 2, 44100)
ref_videos[0]: shape=(8, 64, 64, 3)
audit kinds: ['audio', 'video']

group kind: r2v
group ref_counts: {'ref_images': 0, 'ref_audios': 1, 'ref_videos': 1}
未 LOCK 被拒绝 : 音频素材 ...@v1 没有有效 LOCK，禁止进入导演台
缺版本被拒绝   : 音频引用缺少 asset_id 或 version

MEDIA-REF-OK
```

关键点：`waveform=(1, 2, 44100)` 与 ComfyUI 的 AUDIO 结构一致 ——
结构不符时真实包会判定无效并**静默跳过**，那会变成"以为加了参考音实际没有"。

---

## 五、新增测试（9 条，`tests/test_media_refs.py`）

| 分组 | 覆盖 |
|---|---|
| **导入纪律**（2 条） | AST 扫描禁止插件内 `level>=2` 相对导入；插件的绝对导入必须存在 |
| 音频链路 | 已 LOCK 音频 → `{"waveform","sample_rate"}` 且形状正确 |
| 视频链路 | 已 LOCK 视频 → 8 帧、末维 3 |
| R3 门禁 | 未 LOCK 报错；缺版本报错 |
| 组结构 | 音画参考进组、**只有 12 个键**（不引入真实包没有的键） |
| 槽位 | 音频/视频越界都报错 |
| 枚举 | `AUDIO`/`VIDEO` 存在，且原有四类保留 |

---

## 六、验证汇总

| 项 | 结果 |
|---|---|
| 全量测试（系统 Python） | **EXIT=0** |
| 全量测试（ComfyUI venv） | **EXIT=0** |
| 独立验收（两者） | 92 通过 / 0 失败 / 0 未验证 |
| ComfyUI 实测 | **15** 个我的节点（新增 `BetterComfyUIAssetMedia`），总 1025 |
| 权重下载 | 🔄 **40.34 / 59.38 GB**（缺项只剩 `VAE`） |

> `real_generation` 的 `missing` 从「文本编码器、VAE」变成只剩 **`VAE`** ——
> 说明文本编码器已下完，检测逻辑正确跟踪了进度。

---

## 七、仍未验证

1. **真实 H3 视频出片** —— 需 ≥24GB 显存（本机 4GB）。权重即将下齐，但仍缺 GPU。
2. 浏览器**渲染级**（像素/CSS/布局）—— DOM 级已过，本机无 headless 浏览器。
3. 导演台高级模式（二采 / Guide / SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出；3 个 bug 都是我自己写的，如实记录。_
