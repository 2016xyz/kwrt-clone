#!/usr/bin/env bash
# =============================================================================
#  Kwrt 在线定制编译站 —— 更新代码并重启
#
#  用法：
#     sudo ./update.sh              # git pull + 装依赖 + 重启
#     sudo ./update.sh --no-pull    # 只重装依赖并重启（代码已手动更新）
#
#  **绝不触碰**：users.db / store/ / data/ / kwrt.env
#  这些是运行时数据与凭据，更新代码不应该动它们。
# =============================================================================
set -euo pipefail

SERVICE_NAME="kwrt"
DO_PULL=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) DO_PULL=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

if [ -t 1 ]; then C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""; fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; exit 1; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

cd "$ROOT"
[ -f app/main.py ] || die "请在项目根目录运行本脚本"

# ---- 1. 拉取代码 ---------------------------------------------------------- #
if [ "$DO_PULL" = "1" ]; then
  command -v git >/dev/null 2>&1 || die "未安装 git"
  [ -d .git ] || die "当前目录不是 git 仓库（可用 --no-pull 跳过拉取）"
  # 有未提交改动时先明确告知，避免 pull 失败得莫名其妙
  if [ -n "$(git status --porcelain)" ]; then
    warn "工作区有未提交改动，git pull 可能失败或被拒绝。"
    warn "建议先 git stash 或 git checkout -- . （不会动 users.db / store/）"
  fi
  info "拉取最新代码"
  git pull --ff-only || die "git pull 失败（非快进或有冲突，需人工处理）"
else
  info "按要求跳过 git pull"
fi

# ---- 2. 重装依赖 ---------------------------------------------------------- #
VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
if [ -x "$VPY" ] && [ -f requirements.txt ]; then
  info "更新 Python 依赖"
  "$VPY" -m pip install -q -r requirements.txt \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || "$VPY" -m pip install -q -r requirements.txt \
    || warn "依赖更新失败，继续重启（可能仍可运行）"
else
  warn "未找到虚拟环境或 requirements.txt，跳过依赖更新"
  warn "如需完整安装请执行 ./install.sh"
fi

# ---- 3. 重启（更新完自动重启，服务缺失就自动装上） ------------------------ #
#
#   ★ 原实现在服务缺失时只打印一句「未检测到 systemd 服务 kwrt，请手动重启」，
#     把问题丢回给用户。但「更新完成自动重启」是明确的诉求，而且服务之所以缺失，
#     通常只是当初用 run.sh 或 --no-systemd 装的 —— 这恰好是可自动修好的情况。
#   → 统一交给 scripts/systemd-install.sh：
#       服务存在 → restart（并校验 active）
#       服务缺失 → 顺带把 unit 装好、enable 开机自启、再启动
#   exit 2 表示环境不具备（无 systemctl / 无 venv），此时才回落到提示用户手动重启。
#
if [ "${SKIP_RESTART:-0}" = "1" ]; then
  warn "按要求跳过重启（SKIP_RESTART=1）"
else
  SYSTEMD_ARGS=(--name "$SERVICE_NAME")
  if command -v systemctl >/dev/null 2>&1 \
     && systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    SYSTEMD_ARGS+=(--enable-only)   # 已有服务：不动 unit，只 enable + restart
    info "重启服务"
  else
    info "未检测到 systemd 服务 ${SERVICE_NAME} —— 自动安装并启用（含开机自启）"
  fi

  set +e
  bash "$ROOT/scripts/systemd-install.sh" "${SYSTEMD_ARGS[@]}"
  RC=$?
  set -e

  case "$RC" in
    0) : ;;
    2) warn "环境不具备 systemd（无 systemctl 或未创建 venv），请手动重启："
       dim "PORT=${PORT:-8443} $ROOT/run.sh"
       dim "装上 systemd 服务：sudo $ROOT/scripts/systemd-install.sh" ;;
    *) warn "服务启动未完成，请查看：journalctl -u $SERVICE_NAME -n 100 --no-pager" ;;
  esac
fi

info "更新完成。运行时数据（users.db / store/ / data/ / kwrt.env）未被改动。"
