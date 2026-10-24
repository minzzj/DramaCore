"""本地评分器（图像 / 视频帧 / 整片）—— 真实计算，绝不返回常量。

## 定位（重要，不要误解成"语义审核模型"）

本模块是**确定性本地启发式评分器**：

* 每个分数都来自**实际像素/音频统计**（清晰度、对比度、曝光、边缘分布、帧间差异、
  闪烁尖峰、直方图相似度、音频时长对齐等），同一输入必得同一分数；
* 它**不是**语义模型：判断不了"这个角色是不是林秀""提示词合规不合规"。
  所以 :func:`image_scores` 只产出它能计算的维度，其余维度会**明确列在 skipped 里**，
  绝不编一个"80 分"来冒充（项目明令禁止：不允许用常量分数假装审核通过）。

## 与逻辑图的对应

* 素材审核 `H5` 评分维度「画质/一致性/合规/构图」：
  本地启发式可算 `画质`、`构图`；`一致性`/`合规` 需要语义/参考模型 → 进 ``skipped``。
* 视频段审核 `R1a`「画面连贯/角色一致/动作合理/无闪烁」：
  本地启发式用帧序列统计近似计算四维（见 :func:`frame_scores`，输出里带
  ``mode="local_heuristic"`` 标记，使用方必须如实展示）。
* 整片审核 `R1b`「叙事流畅/音画同步/节奏合理/无明显跳变」：
  :func:`video_scores` 用镜头切分率、时长对齐、帧差尖峰等计算（同样标注为启发式）。

要「真正的语义审核」，请注入自己的评分模型（方案：评分模型由项目作者提供，
用户可覆盖）；本模块只负责在没有语义模型时给出**诚实、可复现、非造假**的基础分。
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "IMAGE_SUPPORTED_DIMENSIONS",
    "R1A_SUPPORTED_DIMENSIONS",
    "R1B_SUPPORTED_DIMENSIONS",
    "LocalScorerError",
    "clamp",
    "image_scores",
    "frame_scores",
    "video_scores",
    "frames_from_video",
    "score_average",
]

#: 本地图像启发式能计算的素材评分维度（其余进 skipped）
IMAGE_SUPPORTED_DIMENSIONS: tuple[str, ...] = ("画质", "构图")
#: 本地视频启发式能计算的段级 R1a 维度（四维全部可近似）
R1A_SUPPORTED_DIMENSIONS: tuple[str, ...] = ("画面连贯", "角色一致", "动作合理", "无闪烁")
#: 本地视频启发式能计算的整片 R1b 维度（四维全部可近似）
R1B_SUPPORTED_DIMENSIONS: tuple[str, ...] = ("叙事流畅", "音画同步", "节奏合理", "无明显跳变")


class LocalScorerError(RuntimeError):
    """本地评分无法完成（文件损坏/ffmpeg 缺失/解码失败）。

    **绝不回退到常量分数**：调用方必须看到真实失败原因。
    """


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return float(max(low, min(high, value)))


def score_average(scores: dict[str, float]) -> float:
    if not scores:
        raise LocalScorerError("没有可用的评分数值，无法计算平均分")
    return round(sum(float(v) for v in scores.values()) / len(scores), 3)


def _ffmpeg() -> str:
    """定位 ffmpeg：优先 PATH，其次 imageio-ffmpeg 自带二进制。

    注意：**不 import pipeline/**（依赖方向单向：review 层不得依赖 pipeline 层，
    `check_contracts.py::C5_FORBIDDEN_IMPORTS` 会拦截）。
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate:
            return str(candidate)
    except Exception:  # noqa: BLE001 - 没有 imageio-ffmpeg 时继续找
        pass
    for candidate in (
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ):
        if candidate.is_file():
            return str(candidate)
    raise LocalScorerError("找不到 ffmpeg（PATH 与 imageio-ffmpeg 都没有可用的二进制）")


# ---------------------------------------------------------------------------
# 图像：清晰度 / 对比度 / 曝光 / 构图（真实像素统计）
# ---------------------------------------------------------------------------


def _image_metrics(rgb: Any) -> dict[str, float]:
    """从 PIL RGB 图像计算基础指标（0-100 之间归一化后返回原始指标）。"""
    import numpy as np

    arr = np.asarray(rgb, dtype=np.float64)
    gray = arr.mean(axis=2)
    # 对比度：标准差（0-127.5 归一化到 0-100）
    contrast = float(gray.std()) / 127.5 * 100.0
    # 清晰度：相邻像素梯度均值（0-255 归一化）
    gy = abs(np.diff(gray, axis=0)).mean() if gray.shape[0] > 1 else 0.0
    gx = abs(np.diff(gray, axis=1)).mean() if gray.shape[1] > 1 else 0.0
    sharpness = (gx + gy) / 255.0 * 100.0 * 4.0  # 经验放大，避免普遍偏低
    # 曝光：均值离 128 的偏差
    exposure = 100.0 - abs(float(gray.mean()) - 128.0) / 128.0 * 100.0
    # 色彩丰富度（Hasler-Süsstrunk 简化）
    rg = arr[..., 0] - arr[..., 1]
    yb = 0.5 * (arr[..., 0] + arr[..., 1]) - arr[..., 2]
    colorfulness = math.sqrt(float(rg.std()) ** 2 + float(yb.std()) ** 2)
    # 构图：三分线附近边缘能量占比 vs 中心（0-100）
    if gray.shape[0] >= 9 and gray.shape[1] >= 9:
        edge = np.abs(np.gradient(gray)[0]) + np.abs(np.gradient(gray)[1])
        h, w = edge.shape
        third_h, third_w = h // 3, w // 3
        bands = np.zeros_like(edge, dtype=bool)
        bands[:third_h, :] = True
        bands[-third_h:, :] = True
        bands[:, :third_w] = True
        bands[:, -third_w:] = True
        total = float(edge.sum())
        composition = (
            100.0 * float(edge[bands].sum()) / total if total > 1e-9 else 50.0
        )
    else:
        composition = 50.0
    return {
        "contrast": clamp(contrast),
        "sharpness": clamp(sharpness),
        "exposure": clamp(exposure),
        "colorfulness": colorfulness,
        "composition": clamp(composition),
    }


def _map_image_dims(metrics: dict[str, float]) -> dict[str, float]:
    # 画质 = 清晰度 55% + 对比度 30% + 曝光 15%（权重显式写死，便于复现）
    quality = (
        0.55 * metrics["sharpness"]
        + 0.30 * metrics["contrast"]
        + 0.15 * metrics["exposure"]
    )
    # 构图 = 三分线边缘能量占比（裁剪过度/主体贴边会明显偏低）
    composition = metrics["composition"]
    return {"画质": round(clamp(quality), 3), "构图": round(clamp(composition), 3)}


def _consistency_vs_reference(reference_path: Path, rgb: Any) -> float | None:
    """一致性：与同素材上一 LOCK 版本的灰度直方图+边缘相关性（确定性像素统计）。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(reference_path) as ref:
            ref_rgb = ref.convert("RGB")
            if max(ref_rgb.size) > 256:
                ratio = 256.0 / max(ref_rgb.size)
                ref_rgb = ref_rgb.resize((max(1, int(ref_rgb.size[0] * ratio)),
                                          max(1, int(ref_rgb.size[1] * ratio))))
            ref_arr = np.asarray(ref_rgb, dtype=np.float64)
    except Exception:  # noqa: BLE001
        return None
    cur = rgb
    if max(cur.size) > 256:
        ratio = 256.0 / max(cur.size)
        cur = cur.resize((max(1, int(cur.size[0] * ratio)), max(1, int(cur.size[1] * ratio))))
    cur_arr = np.asarray(cur, dtype=np.float64)
    h = min(ref_arr.shape[0], cur_arr.shape[0])
    w = min(ref_arr.shape[1], cur_arr.shape[1])
    if h < 8 or w < 8:
        return None
    a = ref_arr[:h, :w].mean(axis=2)
    b = cur_arr[:h, :w].mean(axis=2)
    # 灰度直方图相关系数
    ha, _ = np.histogram(a, bins=64, range=(0, 255))
    hb, _ = np.histogram(b, bins=64, range=(0, 255))
    denom = float(np.linalg.norm(ha) * np.linalg.norm(hb))
    hist_corr = float(np.dot(ha, hb) / denom) if denom > 1e-9 else 0.0
    # 梯度结构相关性
    ga = np.abs(np.gradient(a)[0]) + np.abs(np.gradient(a)[1])
    gb = np.abs(np.gradient(b)[0]) + np.abs(np.gradient(b)[1])
    gd = float(np.linalg.norm(ga) * np.linalg.norm(gb))
    edge_corr = float(np.dot(ga.ravel(), gb.ravel()) / gd) if gd > 1e-9 else 0.0
    score = 100.0 * max(0.0, 0.6 * hist_corr + 0.4 * edge_corr)
    return round(clamp(score), 3)


#: 合规规则（确定性文本规则，不是语义模型）：命中扣分并写明命中词。
_COMPLIANCE_BANNED = (
    "色情", "裸露", "暴力", "血腥", "恐怖主义", "政治敏感", "毒品", "赌博",
    "未成年", "虐杀", "仇恨", "枪击",
)


def _compliance_rule_score(prompt_text: str) -> float | None:
    text = str(prompt_text or "").strip()
    if not text:
        return None
    hits = [w for w in _COMPLIANCE_BANNED if w in text]
    return round(clamp(100.0 - 25.0 * len(hits)), 3)


def image_scores(path: Path | str, *, reference_path: Path | str | None = None,
                 prompt_text: str = "") -> tuple[dict[str, float], list[str]]:
    """对一张素材图做本地启发式评分。

    返回 ``(scores, skipped_dimensions)``：
    - 画质/构图：真实像素统计；
    - 一致性：传了同素材上一 LOCK 版本时按直方图+边缘相关性计算（无参考则跳过）；
    - 合规：传了素材提示词时按确定性违禁词规则计算（非语义模型；无提示词则跳过）。
    """
    path = Path(path)
    if not path.is_file():
        raise LocalScorerError(f"素材文件不存在：{path}")
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise LocalScorerError(f"缺少 Pillow，无法做图像评分：{exc}") from exc
    try:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            if max(rgb.size) > 768:
                ratio = 768.0 / max(rgb.size)
                rgb = rgb.resize((max(1, int(rgb.size[0] * ratio)),
                                  max(1, int(rgb.size[1] * ratio))))
            metrics = _image_metrics(rgb)
    except LocalScorerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LocalScorerError(f"图像解码失败（{path.name}）：{type(exc).__name__}: {exc}") from exc
    scores = _map_image_dims(metrics)
    if reference_path:
        cons = _consistency_vs_reference(Path(reference_path), rgb)
        if cons is not None:
            scores["一致性"] = cons
    comp = _compliance_rule_score(prompt_text)
    if comp is not None:
        scores["合规"] = comp
    skipped = [d for d in ("画质", "一致性", "合规", "构图") if d not in scores]
    if metrics["colorfulness"] < 2.0:
        scores["画质"] = round(clamp(scores["画质"] - 5.0), 3)
        skipped.append("（画面近乎单色，画质分已按规则下调）")
    return scores, skipped


# ---------------------------------------------------------------------------
# 视频帧序列（R1a）
# ---------------------------------------------------------------------------


def frames_from_video(path: Path | str, *, fps: float = 2.0, max_frames: int = 120,
                      width: int = 160) -> Any:
    """用 ffmpeg 抽帧为 ``uint8 [N,H,W,3]`` 数组（N 可能小于 max_frames）。"""
    import numpy as np

    path = Path(path)
    if not path.is_file():
        raise LocalScorerError(f"视频文件不存在：{path}")
    if fps <= 0:
        raise LocalScorerError("抽帧 fps 必须 > 0")
    with tempfile.TemporaryDirectory(prefix="bcu_frames_") as tmp:
        out_pattern = str(Path(tmp) / "f_%04d.png")
        cmd = [
            _ffmpeg(), "-v", "error", "-i", str(path),
            "-vf", f"fps={fps},scale={width}:-2",
            "-frames:v", str(max_frames),
            out_pattern,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        files = sorted(Path(tmp).glob("f_*.png"))
        if not files:
            raise LocalScorerError(
                f"ffmpeg 抽帧失败（{path.name}）：{ (proc.stderr or '')[-500:] }")
        try:
            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            raise LocalScorerError(f"缺少 Pillow，无法读取抽帧：{exc}") from exc
        frames = []
        for file in files:
            with Image.open(file) as im:
                frames.append(np.asarray(im.convert("RGB"), dtype=np.uint8))
        return np.stack(frames, axis=0)


def frame_scores(frames: Any) -> dict[str, float]:
    """对帧序列计算 R1a 四维（真实统计，非常量）。

    公式（显式、可复现）：

    * ``画面连贯`` = 100 × (1 − 相邻帧平均差/0.35)，差越大越不连贯；
    * ``无闪烁``   = 100 × (1 − 突变帧占比/0.25)，突变 = 帧差 > 均值+3×标准差；
    * ``角色一致`` = 100 × 首尾帧灰度直方图余弦相似度；
    * ``动作合理`` = 100 × (1 − 帧差序列的离散度)，动作越不连续分越低。
    """
    import numpy as np

    arr = np.asarray(frames, dtype=np.float64)
    if arr.ndim != 4 or arr.shape[0] < 2:
        raise LocalScorerError(
            f"帧序列形状不合法（需要 [N>=2,H,W,3]），实际 {arr.shape}")
    gray = arr.mean(axis=3) / 255.0
    diffs = np.abs(np.diff(gray, axis=0)).mean(axis=(1, 2))
    mean_diff = float(diffs.mean())
    std_diff = float(diffs.std())

    coherence = 100.0 * (1.0 - min(1.0, mean_diff / 0.35))
    if std_diff > 1e-9:
        spikes = float((diffs > mean_diff + 3.0 * std_diff).sum()) / max(1, len(diffs))
    else:
        spikes = 0.0
    flicker = 100.0 * (1.0 - min(1.0, spikes / 0.25))

    def _hist(frame: Any) -> Any:
        hist, _ = np.histogram(frame, bins=32, range=(0.0, 1.0))
        return hist.astype(np.float64)

    h0, h1 = _hist(gray[0]), _hist(gray[-1])
    denom = float(np.linalg.norm(h0) * np.linalg.norm(h1))
    similarity = float(np.dot(h0, h1) / denom) if denom > 1e-9 else 0.0
    identity = 100.0 * max(0.0, min(1.0, similarity))

    if mean_diff > 1e-9:
        dispersion = std_diff / mean_diff
    else:
        dispersion = 0.0
    motion = 100.0 * (1.0 - min(1.0, dispersion / 2.0))

    return {
        "画面连贯": round(clamp(coherence), 3),
        "角色一致": round(clamp(identity), 3),
        "动作合理": round(clamp(motion), 3),
        "无闪烁": round(clamp(flicker), 3),
    }


# ---------------------------------------------------------------------------
# 整片（R1b）
# ---------------------------------------------------------------------------


def _probe_media(path: Path) -> dict[str, Any]:
    """用 ffmpeg -i 解析时长与是否含音频（不依赖 ffprobe）。"""
    proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    text = (proc.stderr or "") + (proc.stdout or "")
    duration = None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        h, m, s = match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
    fps = None
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s+fps", text)
    if fps_match:
        try:
            fps = float(fps_match.group(1))
        except ValueError:  # pragma: no cover - 正则已限数字
            fps = None
    has_audio = "Audio:" in text
    has_video = "Video:" in text
    return {"duration_s": duration, "has_audio": has_audio,
            "has_video": has_video, "video_fps": fps}


def _stream_end_seconds(path: Path, spec: str) -> float | None:
    """测量某一流的**最后一帧/包的时间戳**（秒）。

    用 `-c copy -f null -` 只走封装/时间戳，不解码像素或音频，长片也很快。
    没有该流（例如无音轨）时返回 None —— 调用方必须如实处理 None，
    绝不允许用常量顶替。

    注意：`time=` 报的是最后一帧的 PTS，比容器时长少约一帧；
    调用方用 `1/fps` 的容差修正（见 :func:`video_scores`）。
    """
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path),
         "-map", spec, "-c", "copy", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    matches = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not matches:
        return None
    h, m, s = matches[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def video_scores(path: Path | str) -> dict[str, float]:
    """整片 R1b 四维本地启发式评分（真实统计，非常量）。

    * ``无明显跳变`` = 100 × (1 − 帧差尖峰占比/0.25)；
    * ``叙事流畅``   = 基于镜头切分率：片段级（≤90s）只在机械式快切时扣分
      （单镜头生成是常态，实测真实 H3 片段若用"理想切换率"会天然判 50）；
      整片级（>90s）切得太密/太稀都扣分；
    * ``节奏合理``   = 100 × (1 − 帧差离散度归一化)，节奏是否稳定；
    * ``音画同步``   = **真实测量音频流与视频流的时长**：
      两流各自用 `-c copy -f null` 取最后时间戳（视频按 1/fps 容差修正），
      时长差占长流时长的比例映射到 0–100；无音轨记 0 分。

    诚实边界（必须如实展示，不能冒充语义审核）：
    ``音画同步`` 只检测**流时长对齐**（整体错位/截断/音轨缺失），
    不做帧级唇形同步检测；要精确同步检测请注入语义评分模型。
    """
    import numpy as np

    path = Path(path)
    info = _probe_media(path)
    if not info["has_video"]:
        raise LocalScorerError(f"文件里没有视频流（{path.name}）")
    duration = info["duration_s"] or 0.0
    if duration <= 0:
        raise LocalScorerError(f"无法解析视频时长（{path.name}）")

    frames = frames_from_video(path, fps=1.0, max_frames=180)
    gray = np.asarray(frames, dtype=np.float64).mean(axis=3) / 255.0
    diffs = np.abs(np.diff(gray, axis=0)).mean(axis=(1, 2))
    mean_diff = float(diffs.mean()) if len(diffs) else 0.0
    std_diff = float(diffs.std()) if len(diffs) else 0.0

    if std_diff > 1e-9:
        spikes = float((diffs > mean_diff + 3.0 * std_diff).sum()) / max(1, len(diffs))
    else:
        spikes = 0.0
    no_jump = 100.0 * (1.0 - min(1.0, spikes / 0.25))

    # 镜头切分率（每分钟突变次数）。
    # 片段级（≤90s）：单镜头生成是常态，0~30 切/分钟都算正常，只在机械式快切时扣分
    # （2026-10-18 真实 4090 机器采样：12/12 个真实 H3 片段按旧公式恒为 50，属误判）。
    # 整片级（>90s）：使用"理想切换率"区间，切得太密/太稀都扣分。
    minutes = max(duration / 60.0, 1e-6)
    cut_rate = spikes * len(diffs) / minutes if len(diffs) else 0.0
    if duration <= 90.0:
        excess = max(0.0, cut_rate - 30.0)
        narrative = 100.0 * (1.0 - min(1.0, excess / 60.0))
    else:
        narrative = 100.0 * (1.0 - min(1.0, abs(cut_rate - 12.0) / 24.0))

    dispersion = std_diff / mean_diff if mean_diff > 1e-9 else 0.0
    rhythm = 100.0 * (1.0 - min(1.0, dispersion / 2.0))

    # 音画同步：真实测量两条流的时长差（此前这里是恒 80.0 的占位分，
    # 无论音频是否截断/错位都恒定通过阈值，属「常量分冒充审核」，已修）。
    sync = _av_sync_score(path, info)
    return {
        "叙事流畅": round(clamp(narrative), 3),
        "音画同步": round(clamp(sync), 3),
        "节奏合理": round(clamp(rhythm), 3),
        "无明显跳变": round(clamp(no_jump), 3),
    }


def _av_sync_score(path: Path, info: dict[str, Any]) -> float:
    """按音频流/视频流真实时长对齐度给「音画同步」分（0–100）。

    规则（显式、可复现）：
    * 无音轨 -> 0.0（整片应有同步音轨；缺失是明确缺陷）；
    * 视频流时长 = 最后时间戳 + 1/fps（最后一帧 PTS 比真实时长少一帧）；
    * 视频/音频任一测得 None -> 0.0（测不到就如实给 0，绝不编分）；
    * 容差 = max(1/fps, 一帧)；时长差先减容差，剩余按「长流时长 10% 归零」线性映射。
    """
    if not info.get("has_audio"):
        return 0.0
    video_end = _stream_end_seconds(path, "0:v:0")
    audio_end = _stream_end_seconds(path, "0:a:0")
    if video_end is None or audio_end is None:
        return 0.0
    fps = float(info.get("video_fps") or 24.0)
    if fps <= 0:
        fps = 24.0
    frame = 1.0 / fps
    video_duration = video_end + frame
    tolerance = frame
    delta = max(0.0, abs(video_duration - audio_end) - tolerance)
    longest = max(video_duration, audio_end, 1e-9)
    ratio = delta / longest
    return 100.0 * max(0.0, 1.0 - min(1.0, ratio / 0.10))


def split_video_into_segments(path: Path | str, *, segment_seconds: float,
                              out_dir: Path) -> list[Path]:
    """把整片按固定时长切成段文件（用于段级 R_seg）。返回段文件路径列表。"""
    path = Path(path)
    if segment_seconds <= 0:
        raise LocalScorerError("每段时长必须 > 0")
    duration = (_probe_media(path)["duration_s"] or 0.0)
    if duration <= 0:
        raise LocalScorerError(f"无法解析视频时长（{path.name}）")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, int(math.ceil(duration / segment_seconds)))
    files: list[Path] = []
    for index in range(count):
        out = out_dir / f"seg_{index + 1:04d}.mp4"
        cmd = [
            _ffmpeg(), "-v", "error", "-y",
            "-ss", f"{index * segment_seconds:.3f}",
            "-i", str(path),
            "-t", f"{segment_seconds:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0 or not out.is_file():
            raise LocalScorerError(
                f"切分第 {index + 1} 段失败：{(proc.stderr or '')[-300:]}")
        files.append(out)
    return files
