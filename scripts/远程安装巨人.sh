#!/usr/bin/env bash
# ============================================================================
# 「剧核 DramaCore」6 个巨人节点 · 远程 4090 安装脚本（ComfyUI 0.33.0 镜像）
#
# 用法（远端以 root 执行，约 3~5 分钟）：
#     bash /root/远程安装巨人.sh                 # 默认用 /root/giants_source.zip
#     bash /root/远程安装巨人.sh /root/xxx.zip   # 指定源码包
#     DRY_RUN=1 bash /root/远程安装巨人.sh       # 只做解压/依赖/备份检查，不复制不重启
#     SKIP_RESTART=1 bash /root/远程安装巨人.sh  # 复制完不重启（自己手动重启）
#
# 它做什么：
#   1) 用 python3 zipfile 解压源码包（兼容 Windows 反斜杠条目；zip 里没有 unzip 也能跑）
#      —— 实测远端 **没有 unzip/zip/rsync**，别用 unzip -o。
#   2) 备份同名旧目录；复制 6 个巨人包到 /model/ComfyUI/custom_nodes/
#   3) 检查巨人依赖（opencv-python-headless / scenedetect / ultralytics / requests /
#      imageio-ffmpeg），缺则用 ComfyUI 自带 venv 的 pip 安装
#   4) 按「当前正在运行的命令行」原样重启 ComfyUI（保留端口 8188 / BETTERCOMFYUI_HOME /
#      --database-url），日志追加到 /model/ComfyUI/comfyui-8188.log
#   5) 等 /object_info 就绪，校验 6 个巨人节点是否注册
#   6) 任一步失败 → 自动回滚（恢复备份目录、重启、再次校验）并打印日志尾部
#
# 注意：
#   * 重启会中断正在执行的队列/生成任务，请在没人跑任务时执行；
#   * 本脚本只动 custom_nodes 里这 6 个目录，不碰 /data/BetterComfyUI 和我们的插件；
#   * 输出里所有 ✓ / ✗ 都对应真实命令结果，不猜。
# ============================================================================
set -uo pipefail

ZIP="${1:-/root/giants_source.zip}"
SRC_DIR="/root/giants_src"
CN="/model/ComfyUI/custom_nodes"
COMFY_DIR="/model/ComfyUI"
BASE="http://127.0.0.1:8188"
LOG="/model/ComfyUI/comfyui-8188.log"
BETTER_HOME="/data/BetterComfyUI"
BACKUP="/root/giants_backup_$(date +%Y%m%d_%H%M%S)"
DRY_RUN="${DRY_RUN:-0}"
SKIP_RESTART="${SKIP_RESTART:-0}"

GIANTS=(
  "ComfyUI_MiniMaxH3_Director"
  "ComfyUI_MiniMaxH3_Smart_Director"
  "ComfyUI-MiniMaxH3-ChainDirector"
  "ComfyUI-DNNodes"
  "ComfyUI-Storyboard-LLM"
  "ComfyUI-DeepSeek-API-connector"
)
# 装完必须能在 /object_info 里看到的节点类型（我们链路真正用到的 + 各包入口）
REQUIRED_NODES=(
  "MiniMaxH3Director"
  "MiniMaxH3DirectorGroupsCombine"
  "MiniMaxH3DirectorGroupReferenceToVideo"
  "MiniMaxH3SmartDirector"
  "MiniMaxH3ChainDirector"
  "DNNodes"
  "StoryboardNode"
  "DeepSeekPromptConnector"
)

OLD_PID=""
BEFORE_COUNT=""
HAD_PROCESS=0
NEW_DIRS=()
BACKED_UP=()

log()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }

# ---------------------------------------------------------------- 基础工具
need() { command -v "$1" >/dev/null 2>&1 || { bad "缺少命令：$1"; exit 1; }; }

object_info_count() {
  curl -s --max-time 20 "$BASE/object_info" | python3 -c \
    'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0
}

wait_ready() {  # $1=最多等待秒数
  local limit="${1:-180}" waited=0
  while [ "$waited" -lt "$limit" ]; do
    if curl -s -o /dev/null --max-time 3 "$BASE/object_info"; then
      return 0
    fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

restart_comfyui() {
  step "重启 ComfyUI"
  if [ "$HAD_PROCESS" = "1" ]; then
    local args cwd
    args="$(ps -o args= -p "$OLD_PID" 2>/dev/null | sed 's/^ *//')"
    cwd="$(readlink -f "/proc/$OLD_PID/cwd" 2>/dev/null || echo "$COMFY_DIR")"
    [ -n "$args" ] || { bad "拿不到旧进程命令行，改用默认命令"; args="$COMFY_DIR/.venv/bin/python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --database-url sqlite:////tmp/comfyui.db"; }
    log "  旧命令：$args"
    log "  工作目录：$cwd"
    kill -TERM "$OLD_PID" 2>/dev/null
    for _ in $(seq 1 30); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 1; done
    kill -0 "$OLD_PID" 2>/dev/null && { bad "旧进程未退出，强杀"; kill -KILL "$OLD_PID" 2>/dev/null; sleep 2; }
  else
    log "  当前没有运行中的 ComfyUI，用默认命令启动"
    args="$COMFY_DIR/.venv/bin/python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --database-url sqlite:////tmp/comfyui.db"
    cwd="$COMFY_DIR"
  fi
  cd "$cwd" || exit 1
  ( BETTERCOMFYUI_HOME="$BETTER_HOME" nohup bash -c "$args" >> "$LOG" 2>&1 & )
  log "  已启动，日志：$LOG"
  if wait_ready 180; then
    ok "ComfyUI /object_info 就绪（$(object_info_count) 个节点类型）"
    return 0
  fi
  bad "180 秒内 /object_info 未就绪"
  return 1
}

verify_nodes() {  # 输出缺失节点；有缺失返回 1
  local missing
  missing="$(curl -s --max-time 30 "$BASE/object_info" | python3 - "${REQUIRED_NODES[@]}" <<'PY'
import json, sys
need = sys.argv[1:]
try:
    info = json.load(sys.stdin)
except Exception as exc:  # noqa: BLE001
    print("__OBJECT_INFO_PARSE_FAIL__: %s" % exc)
    raise SystemExit(0)
print("\n".join(n for n in need if n not in info))
PY
)"
  if [ -z "$missing" ]; then
    ok "6 个巨人包关键节点全部注册"
    return 0
  fi
  bad "以下节点未注册："
  printf '    - %s\n' $missing
  return 1
}

rollback() {
  step "回滚"
  local newpid
  newpid="$(pgrep -f "main.py --listen" | head -1 || true)"
  [ -n "$newpid" ] && kill -TERM "$newpid" 2>/dev/null && sleep 3
  for d in "${NEW_DIRS[@]:-}"; do
    [ -n "$d" ] && [ -d "$CN/$d" ] && rm -rf "$CN/$d" && log "  删除新装目录：$d"
  done
  for d in "${BACKED_UP[@]:-}"; do
    [ -n "$d" ] && [ -d "$BACKUP/$d" ] && mv "$BACKUP/$d" "$CN/$d" && log "  恢复备份目录：$d"
  done
  restart_comfyui || true
  if [ -n "$BEFORE_COUNT" ]; then
    local now; now="$(object_info_count)"
    [ "$now" = "$BEFORE_COUNT" ] && ok "节点数已恢复到安装前：$now" \
      || bad "节点数 $now（安装前 $BEFORE_COUNT），请人工看日志"
  fi
  step "日志尾部（$LOG）"
  tail -60 "$LOG" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'
  bad "安装失败，已回滚。上面日志能看出是哪个包 import 失败。"
  exit 1
}

# ============================================================================
step "0. 前置检查"
[ "$(id -u)" = "0" ] || { bad "请用 root 执行"; exit 1; }
need curl; need python3; need pgrep
[ -d "$CN" ] || { bad "custom_nodes 不存在：$CN"; exit 1; }
[ -f "$ZIP" ] || { bad "源码包不存在：$ZIP"; exit 1; }
log "  源码包：$ZIP（$(du -h "$ZIP" | cut -f1)）"
OLD_PID="$(pgrep -f "main.py --listen" | head -1 || true)"
if [ -n "$OLD_PID" ]; then
  HAD_PROCESS=1
  log "  当前 ComfyUI PID=$OLD_PID"
else
  bad "没检测到运行中的 ComfyUI（main.py --listen）；仍可继续，但重启那步会新起一个"
fi
BEFORE_COUNT="$(object_info_count)"
log "  安装前 /object_info 节点类型数：${BEFORE_COUNT:-取不到}"
ls "$CN" | grep -qxF "bettercomfyui_comfy" && ok "我们的插件目录存在" \
  || log "  （未检测到 bettercomfyui_comfy，不影响本脚本）"
EXISTING=()
for d in "${GIANTS[@]}"; do [ -e "$CN/$d" ] && EXISTING+=("$d"); done
[ "${#EXISTING[@]}" -gt 0 ] && log "  已存在同名目录（会先备份）：${EXISTING[*]}" || log "  6 个巨人包均未安装（符合预期）"

# ---------------------------------------------------------------- 1. 解压
step "1. 解压源码包 → $SRC_DIR"
rm -rf "$SRC_DIR"; mkdir -p "$SRC_DIR"
python3 - "$ZIP" "$SRC_DIR" <<'PY'
import os, sys, zipfile
zip_path, dest = sys.argv[1], sys.argv[2]
zf = zipfile.ZipFile(zip_path)
n = 0
for info in zf.infolist():
    if info.is_dir():
        continue
    raw = info.filename.replace("\\", "/")          # Windows 反斜杠条目 → POSIX
    parts = [p for p in raw.split("/") if p not in ("", ".", "..")]
    if not parts:
        continue
    # 去掉 .git 与 __pycache__（省空间、避免踩坑）
    if any(p in (".git", "__pycache__", ".github", ".idea") for p in parts):
        continue
    target = os.path.join(dest, *parts)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with zf.open(info) as src, open(target, "wb") as out:
        out.write(src.read())
    n += 1
print(f"  已解压 {n} 个文件")
PY
[ "$?" = "0" ] || rollback
FOUND=()
for d in "${GIANTS[@]}"; do [ -d "$SRC_DIR/$d" ] && FOUND+=("$d"); done
if [ "${#FOUND[@]}" -ne "${#GIANTS[@]}" ]; then
  bad "解压后只找到 ${#FOUND[@]}/${#GIANTS[@]} 个巨人目录：$(ls "$SRC_DIR" 2>/dev/null | head -20 | tr '\n' ' ')"
  exit 1
fi
ok "6 个巨人目录齐全：${FOUND[*]}"
du -sh "$SRC_DIR" | sed 's/^/  解压后大小：/'

# ---------------------------------------------------------------- 2. 依赖
step "2. 检查巨人依赖（ComfyUI venv）"
VENV_PY="$COMFY_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || { bad "找不到 ComfyUI venv：$VENV_PY"; exit 1; }
MISSING_PKGS="$("$VENV_PY" - <<'PY'
import importlib.util
need = {
    "opencv-python-headless": "cv2",
    "scenedetect": "scenedetect",
    "ultralytics": "ultralytics",
    "requests": "requests",
    "imageio-ffmpeg": "imageio_ffmpeg",
}
print(" ".join(pkg for pkg, mod in need.items() if importlib.util.find_spec(mod) is None))
PY
)"
if [ -n "${MISSING_PKGS// /}" ]; then
  log "  缺少：$MISSING_PKGS → 用 venv pip 安装（失败只告警，不阻断）"
  # shellcheck disable=SC2086
  "$VENV_PY" -m pip install --no-input $MISSING_PKGS || bad "pip 安装失败（网络？）——巨人包可能 import 失败，继续但请留意"
else
  ok "巨人包依赖已全部就绪"
fi

if [ "$DRY_RUN" = "1" ]; then
  step "DRY_RUN=1：停在复制/重启之前"
  log "  解压目录：$SRC_DIR"; log "  试运行结束，未改动 custom_nodes / 未重启"
  exit 0
fi

# ---------------------------------------------------------------- 3. 备份
step "3. 备份已有同名目录 → $BACKUP"
mkdir -p "$BACKUP"
for d in "${EXISTING[@]:-}"; do
  [ -n "$d" ] || continue
  mv "$CN/$d" "$BACKUP/$d" && BACKED_UP+=("$d") && log "  已备份：$d"
done
ok "备份完成（${#BACKED_UP[@]} 个）"

# ---------------------------------------------------------------- 4. 复制
step "4. 复制 6 个巨人包 → $CN"
for d in "${GIANTS[@]}"; do
  cp -a "$SRC_DIR/$d" "$CN/$d" || { bad "复制失败：$d"; rollback; }
  NEW_DIRS+=("$d")
  chown -R root:root "$CN/$d" 2>/dev/null
  find "$CN/$d" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
  ok "已安装：$d"
done

# ---------------------------------------------------------------- 5. 重启
if [ "$SKIP_RESTART" = "1" ]; then
  log "\nSKIP_RESTART=1：请手动重启 ComfyUI 后执行："
  log "  curl -s $BASE/object_info | python3 -c 'import json,sys;d=json.load(sys.stdin);print(\"MiniMaxH3Director\" in d)'"
  exit 0
fi
restart_comfyui || rollback

# ---------------------------------------------------------------- 6. 校验
step "5. 校验节点注册"
COUNT_AFTER="$(object_info_count)"
log "  安装后节点类型数：$COUNT_AFTER（安装前 $BEFORE_COUNT）"
verify_nodes || rollback
ok "安装成功：6 个巨人包已注册"

step "6. 完成"
log "  · 回滚备份保留在：$BACKUP（确认稳定后可 rm -rf）"
log "  · 下一步：远端 Web UI 导入 《00_剧核总流程_4090适配版.json》"
log "  · 导入后仍要：① 右侧 Better ComfyUI 面板填 DeepSeek 密钥 ② 确认项目=剧核试用"
log "  · 若要回滚：rm -rf $CN/{${GIANTS[*]// /,}} 后从 $BACKUP 恢复并重启"
exit 0
