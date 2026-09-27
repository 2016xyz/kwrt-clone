#!/usr/bin/env bash
# =============================================================================
#  Kwrt —— systemd 服务安装 / 启用（install.sh 与 update.sh 共用）
#
#  为什么单独抽出来：
#    原先 install.sh 与 update.sh 各写一份 systemd 逻辑，结果两边的行为
#    不一致 —— install.sh 会装服务，update.sh 在服务缺失时**只打印一句警告**
#    （「[!] 未检测到 systemd 服务 kwrt，请手动重启」），于是「装的机器没有
#    开机自启、更新的机器不知道怎么办」。共用一份逻辑，才不会再次跑偏。
#
#  用法：
#     sudo scripts/systemd-install.sh              # 装/更新 unit + enable + restart
#     sudo scripts/systemd-install.sh --enable-only# 只 enable + restart（不动 unit）
#     sudo scripts/systemd-install.sh --dry-run    # 只打印，不改动
#
#  退出码：
#     0  服务已 active 且已加入开机自启
#     1  操作失败（调用方应据此报错，而不是继续往下走）
#     2  环境不具备（没有 systemctl / 没有 venv）—— 调用方据此决定是否降级
#
#  幂等：重复执行安全。**绝不触碰** users.db / store/ / data/ / kwrt.env。
# =============================================================================
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-kwrt}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8443}"
HOST_BIND="${HOST_BIND:-0.0.0.0}"
ENABLE_ONLY=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --enable-only) ENABLE_ONLY=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --port)        PORT="$2"; shift 2 ;;
    --host)        HOST_BIND="$2"; shift 2 ;;
    --name)        SERVICE_NAME="$2"; shift 2 ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

if [ -t 1 ]; then C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""; fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
err()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }
run_root() {
  if [ "$DRY_RUN" = "1" ]; then dim "[dry-run] (root) $*"; return 0; fi
  if [ "$(id -u)" = "0" ]; then "$@"; else sudo "$@"; fi
}

# ---- 0. 前置检查 ---------------------------------------------------------- #
if ! command -v systemctl >/dev/null 2>&1; then
  warn "系统没有 systemctl，无法安装 systemd 服务"
  exit 2
fi
# 容器里常见：有 systemctl 但没有可用的 init（会报 "System has not been booted"）
if ! systemctl is-system-running >/dev/null 2>&1 && \
   [ "$(systemctl is-system-running 2>/dev/null || true)" = "offline" ]; then
  warn "systemd 未运行（可能是容器），跳过服务安装"
  exit 2
fi

if [ "$ENABLE_ONLY" = "0" ]; then
  # venv 必须真实存在，否则装出来的服务一启动就失败
  VPY=""
  for cand in "$ROOT/.venv/bin/python" "$ROOT/venv/bin/python"; do
    if [ -x "$cand" ]; then VPY="$cand"; break; fi
  done
  if [ -z "$VPY" ]; then
    err "找不到虚拟环境（.venv/bin/python）—— 请先执行 ./install.sh 创建环境"
    exit 2
  fi
  if ! "$VPY" -c 'import uvicorn, fastapi' >/dev/null 2>&1; then
    err "虚拟环境里缺少 fastapi/uvicorn —— 请先执行：$VPY -m pip install -r requirements.txt"
    exit 2
  fi
fi

UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
ENVFILE="$ROOT/kwrt.env"

# ---- 1. 写入 unit --------------------------------------------------------- #
if [ "$ENABLE_ONLY" = "0" ]; then
  VPY="${VPY:-$ROOT/.venv/bin/python}"
  info "安装 systemd 服务: $UNIT"
  if [ "$DRY_RUN" = "1" ]; then
    dim "[dry-run] 写入 $UNIT"
  else
    TMPU="$(mktemp)"
    cat > "$TMPU" <<UNITEOF
[Unit]
Description=Kwrt Online OpenWrt Firmware Builder
Documentation=file://$ROOT/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
# WorkingDirectory 必须显式指定：app 内部有按相对路径打开的文件，
# 缺失它会让「验证邮件」等链路在 systemd 下静默失效。
WorkingDirectory=$ROOT
# kwrt.env 只含初始管理员密码，权限 600；用 - 前缀允许文件不存在
EnvironmentFile=-$ENVFILE
Environment=PYTHONUNBUFFERED=1
# --forwarded-allow-ips 留空：uvicorn 默认会信任来自 127.0.0.1 的
# X-Forwarded-For 并改写 request.client，导致应用层的可信代理判定被架空
# （实测可让封禁被一个请求头绕过）。真实 IP 判定统一交给应用层。
Environment=FORWARDED_ALLOW_IPS=
ExecStart=$VPY -m uvicorn app.main:app --host $HOST_BIND --port $PORT --log-level warning --forwarded-allow-ips=
# 崩溃自动拉起；开机后由 multi-user.target 拉起（见 [Install]）
Restart=on-failure
RestartSec=5
# 构建 ImageBuilder 需要较长时间，给足退出宽限
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
UNITEOF
    run_root install -m 644 "$TMPU" "$UNIT"
    rm -f "$TMPU"
    run_root systemctl daemon-reload
  fi
else
  if [ "$DRY_RUN" != "1" ] && [ ! -f "$UNIT" ]; then
    err "服务不存在：$UNIT（--enable-only 不会创建它，请去掉该参数重新执行）"
    exit 1
  fi
  dim "跳过 unit 写入（--enable-only）"
fi

# ---- 2. 开机自启（这一步失败必须报出来，不能吞） -------------------------- #
#   ★ 原实现是 `systemctl enable ... >/dev/null 2>&1 || true`
#     —— 失败了也不出声，机器重启后服务不会起来，而安装时显示的是一切正常。
#     「安装完成就自动设置开机启动」这条承诺，必须在这里真的兑现并验证。
info "设置开机自启"
if [ "$DRY_RUN" = "1" ]; then
  dim "[dry-run] systemctl enable $SERVICE_NAME"
else
  if run_root systemctl enable "$SERVICE_NAME" >/dev/null 2>&1; then
    dim "已 enable：开机后会自动启动"
  else
    err "systemctl enable 失败 —— 开机不会自动启动"
    dim "排查：systemctl enable $SERVICE_NAME 2>&1"
    exit 1
  fi
  # 独立验证一次，不靠上面的返回值
  if ! systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    err "enable 后 is-enabled 仍不是 enabled，开机不会自启"
    exit 1
  fi
fi

# ---- 3. 启动 / 重启 ------------------------------------------------------- #
info "启动服务"
if [ "$DRY_RUN" = "1" ]; then
  dim "[dry-run] systemctl restart $SERVICE_NAME"
else
  if ! run_root systemctl restart "$SERVICE_NAME"; then
    err "systemctl restart 失败"
    dim "查看日志：journalctl -u $SERVICE_NAME -n 100 --no-pager"
    exit 1
  fi
  sleep 2
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "服务已启动并处于 active（开机自启已开启）"
  else
    err "服务未处于 active，启动失败"
    dim "查看日志：journalctl -u $SERVICE_NAME -n 100 --no-pager"
    exit 1
  fi
fi

dim "状态：systemctl status $SERVICE_NAME   日志：journalctl -u $SERVICE_NAME -f"
exit 0
