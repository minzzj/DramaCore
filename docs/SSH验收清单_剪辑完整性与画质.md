# SSH 实机验收清单：成片是否「剪辑过 / 完整 / 质量高」

> 用户要求（2026-10-18 凌晨）：今晚 SSH 的机器在**视频生成出来之后**，需要检查：
> ① 有没有经过剪辑；② 视频是否完整；③ 质量是否高。
> 本清单可直接照做；每项都有命令或机器可算的指标，结论区分「机器可证 / 人工判断」。

---

## A0. 最快方式：一条命令出报告

```bash
python -B scripts/quality_audit.py --video FINAL.mp4 \
    --segments-dir 段目录 --expect-segments N --expect-duration X --out 质检报告.md
```
输出 Markdown + JSON：完整性（时长/段数/音轨）、剪辑性（场景切换/黑帧/冻帧/静音）、
质量四维（本地启发式）、占位检测、结论（通过/不通过）与边界说明；退出码 0=通过/1=不通过。
下面 B~F 是逐项命令版（想手动核对时用）。

## A. 先决条件（到机器先做）

```bash
nvidia-smi                                  # GPU/显存/驱动
curl -s http://127.0.0.1:8188/system_stats  # ComfyUI 是否在跑、device 是否 cuda
python -B scripts/verify_install.py         # 环境自检（节点/工作流/巨人）
```

* 若 `device=cpu` → 说明 ComfyUI 没用 GPU 启动，先修启动参数再谈出片；
* 若 4090 上文本编码器加载失败（NVFP4 不支持）→ 按环境清单换 FP8/CPU 方案，单独记录为**环境问题**。

## B. 成片基本信息（判断"有没有剪辑/是不是单文件"）

```bash
# 把成片路径替换成实际文件
ffprobe -v error -show_entries format=duration,size,bit_rate,format_name \
        -show_entries stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate \
        -of json FINAL.mp4
```

| 检查 | 合格标准（建议） | 判定 |
|---|---|---|
| 是单文件 mp4（不是一堆段文件） | format_name 含 mp4 | 机器可证 |
| 时长 ≈ 计划段数 × 每段秒数 | 误差 ≤2% | 机器可证 |
| 视频流分辨率/帧率符合设置 | 如 1280×720@24 或 1920×1080@24 | 机器可证 |
| 有音频流且覆盖整段 | audio 时长 ≥ 视频时长×99% | 机器可证 |
| 码率合理（不是占位纯色） | 1080p24 实拍级 ≈3–6Mbps+；纯色占位会极低 | 机器可证 |
| 关键帧/切片数 | 片段数 ≈ 段数（剪辑拼接痕迹） | 机器可证（见 C） |

> ⚠️ 若码率极低、时长对但画面是纯色/测试图 → 说明是**占位视频冒充真实生成**（这是必须立刻修的产品缺陷）。

## C. 是否真正「剪辑/拼接」过

```bash
# 1) 场景切换点数量（拼接痕迹；近似段数-1 说明是逐段拼接）
ffmpeg -i FINAL.mp4 -filter:v "select='gt(scene,0.35)',showinfo" -f null - 2>&1 | grep -c showinfo
# 2) 黑帧（段间空档）
ffmpeg -i FINAL.mp4 -vf blackdetect=d=0.05:pix_th=0.10 -an -f null - 2>&1 | grep blackdetect
# 3) 冻帧（卡住/重复帧）
ffmpeg -i FINAL.mp4 -vf freezedetect=n=0.003:d=0.5 -an -f null - 2>&1 | grep freezedetect
# 4) 静音段（缺音频/空档）
ffmpeg -i FINAL.mp4 -af silencedetect=n=-50dB:d=1.0 -f null - 2>&1 | grep silence
```

| 检查 | 合格标准 | 判定 |
|---|---|---|
| 段数与计划一致 | 场景切换数 ≈ 段数-1（容差 ±2） | 机器可证 |
| 无黑帧空档 | blackdetect 命中 ≤ 1 处且 <0.5s | 机器可证 |
| 无长时间冻帧 | freezedetect 命中时长 <总时长 1% | 机器可证 |
| 无长静音 | 静音段累计 <总时长 5%（除非剧情需要） | 机器可证 |
| 段边界衔接 | 相邻段尾帧/首帧相似度 ≥55（用本项目评分器） | 机器可证 |

## D. 完整性（有没有丢段/跳段/重复）

1. 拿**计划清单**（剧本拆分后的段列表/CSV/JSON）与成片对照：
   - 段数一致；每段时长符合（10s±0.5s）；
   - 顺序正确（用每段中间帧 hash 反查？至少人工抽 3 段核对内容对应剧情顺序）。
2. 总时长 = Σ段时长 − 丢帧误差（≤2%）。
3. 首尾：开头前 5 秒、结尾最后 5 秒必须正常画面+声音。
4. 用本项目 `local_scorer` 跑整片：
```bash
python -B - <<'PY'
from bettercomfyui.review import local_scorer
s = local_scorer.video_scores(r"FINAL.mp4")
print(s)   # 叙事流畅/音画同步/节奏合理/无明显跳变（本地启发式，真实统计）
PY
```

| 检查 | 合格标准 |
|---|---|
| 段数/时长 | 与计划一致（误差 ≤2%） |
| 首尾完整 | 首尾各 5s 正常 |
| 无重复段 | 相邻段内容不重复（抽检+帧差） |
| 评分（启发式） | 四维均 ≥70（阈值可配） |

## E. 质量（"质量是否高"）

机器可算（本项目评分器 + ffprobe）：
| 维度 | 指标 | 建议合格线 |
|---|---|---|
| 画质 | 清晰度/对比度/曝光（`image_scores` 对抽帧） | 均值 ≥70 |
| 稳定性 | 闪烁/跳变（视频评分器） | ≥70 |
| 音画 | 音视频流时长差 | ≤0.2s |
| 分辨率/帧率 | 与设置一致 | 一致 |
| 压缩 | 码率/CRF | 1080p24 建议 CRF 18~20 |

人工判断（必须做，抽检 3 段：开头/中间/结尾各 1 段，每段 ≥5s）：
- [ ] 角色没有变形/丢失（与素材库参考图对照）
- [ ] 动作连贯，没有明显穿模/闪烁/重影
- [ ] 场景切换自然，没有突然跳变
- [ ] 音频与画面同步，无爆音/断续
- [ ] 无字幕错误/乱码（若有字幕）
- [ ] 整体节奏可看（不像幻灯片）

## F. 资源与稳定性（生产 32GB 红线）

```bash
# 跑的时候另开终端采样
nvidia-smi --query-gpu=timestamp,memory.used,memory.total,utilization.gpu --format=csv -l 2 > vram.log &
# 结束后看峰值
python -B -c "import csv;rows=list(csv.reader(open('vram.log')))[1:];pk=max(int(r[1].split()[0]) for r in rows);print('峰值 MiB:',pk)"
```

| 检查 | 合格标准（5090-32G） |
|---|---|
| 显存峰值 | **< 30.5GB**（留 1.5GB 余量） |
| OOM 次数 | 0 |
| 是否走分阶段 | 是（文本编码器编码后卸载） |
| RAM 峰值 | < 90% 物理内存 |
| 单段耗时 / 总耗时 | 记录，回填双卡报表 |

## G. 结论模板（发给用户）

```
【成片】路径/大小/时长/分辨率/帧率/码率/音轨
【剪辑】单文件？场景切换数 ≈ 段数-1？黑帧/冻帧/静音检查结果
【完整】段数一致？时长误差？首尾正常？重复段？
【质量】启发式四维分 / 画质分 / 音画差 / 人工抽检 3 段结论
【资源】显存峰值 / OOM / 单段耗时 / 总耗时
【问题】若有：产品缺陷（立刻修）/ 环境问题（给命令）/ 模型限制（说明）
【判定】通过 / 有条件通过 / 不通过
```

> 判定为"不通过"时，先区分**环境/模型问题**与**产品问题**；产品问题立即修，
> 并补回归测试；环境问题给出预装修正命令；模型限制如实告知，不冒充通过。
