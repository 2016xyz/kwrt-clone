#!/usr/bin/env bash
# =============================================================================
#  Kwrt 在线定制编译站 —— 一键安装 / 部署
#
#  用法：
#     git clone <repo> kwrt && cd kwrt
#     sudo ./install.sh                 # 全自动：装依赖 + venv + systemd + 启动
#     sudo ./install.sh --port 9000     # 指定端口
#     sudo ./install.sh --no-systemd    # 只装依赖，不起 systemd（前台跑）
#     sudo ./install.sh --dry-run       # 只打印将执行的命令，不实际改动
#
#  幂等性（重要）：
#     重复执行**不会**覆盖任何已有数据 —— users.db / store/ / data/ 里的
#     用户配置一律保留。若已存在管理员账号，也不会重置其密码。
# =============================================================================
set -euo pipefail

SERVICE_NAME="kwrt"
PORT="${PORT:-8443}"
HOST_BIND="${HOST_BIND:-0.0.0.0}"
USE_SYSTEMD=1
DRY_RUN=0
ASSUME_YES=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)        PORT="$2"; shift 2 ;;
    --host)        HOST_BIND="$2"; shift 2 ;;
    --no-systemd)  USE_SYSTEMD=0; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -y|--yes)      ASSUME_YES=1; shift ;;
    -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

# ---- 输出helpers ---------------------------------------------------------- #
if [ -t 1 ]; then C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""; fi
info()  { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()   { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; exit 1; }
dim()   { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }
run()   {
  if [ "$DRY_RUN" = "1" ]; then dim "[dry-run] $*"; return 0; fi
  "$@"
}
# 需要 root 的命令（dry-run 时同样只打印）
run_root() {
  if [ "$DRY_RUN" = "1" ]; then dim "[dry-run] (root) $*"; return 0; fi
  if [ "$(id -u)" = "0" ]; then "$@"; else sudo "$@"; fi
}

[ "$DRY_RUN" = "1" ] || [ -f "$ROOT/app/main.py" ] || die "请在项目根目录运行本脚本（找不到 app/main.py）"

# ---- 1. 识别发行版 -------------------------------------------------------- #
PKG=""
if command -v dnf >/dev/null 2>&1; then PKG="dnf"
elif command -v yum >/dev/null 2>&1; then PKG="yum"
elif command -v apt-get >/dev/null 2>&1; then PKG="apt"
fi
[ -n "$PKG" ] || die "未识别到包管理器（需要 dnf / yum / apt-get）"
info "包管理器: $PKG"

# ImageBuilder 编译固件所需的外部工具与 perl 模块
RPM_DEPS="perl-FindBin perl-IPC-Cmd perl-Digest-SHA perl-Time-Piece zstd make gcc gawk unzip tar"
DEB_DEPS="perl squashfs-tools zstd make gcc gawk unzip tar python3-venv python3-pip"

# ---- 2. 安装系统依赖 ------------------------------------------------------ #
MISSING=""
if [ "$PKG" = "apt" ]; then
  for p in $DEB_DEPS; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
  done
else
  for p in $RPM_DEPS; do
    rpm -q "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
  done
fi

if [ -n "$MISSING" ]; then
  info "安装系统依赖:$MISSING"
  if [ "$PKG" = "apt" ]; then
    run_root apt-get update -qq || true
    # --no-install-recommends：镜像/精简系统上建议包会多拉几百 MB，
    # 而构建 ImageBuilder 只需要下面这些确定性依赖。
    # shellcheck disable=SC2086
    run_root apt-get install -y --no-install-recommends $MISSING
  else
    # shellcheck disable=SC2086
    run_root "$PKG" install -y $MISSING
  fi
else
  info "系统依赖已齐全，跳过"
fi

# ---- 3. 准备 Python 虚拟环境 ---------------------------------------------- #
PY="$(command -v python3 || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  # min 版 Debian/容器里 python3 可能要先靠 apt 装出来。dry-run 不会真的装，
  # 此时用占位路径继续走完流程（只打印不执行），否则脚本会在第二步就退出，
  # 让人误以为脚本本身有问题。
  if [ "$DRY_RUN" = "1" ]; then
    warn "当前系统尚无 python3（dry-run 不安装依赖），后续以占位展示"
    PY="/usr/bin/python3"
  else
    die "未找到 python3（依赖安装后仍缺失，请手动安装 python3）"
  fi
fi
if [ -x "$PY" ]; then
  PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  info "Python: $PY ($PYV)"
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' \
    || die "需要 Python 3.9+，当前 $PYV"
else
  info "Python: $PY (dry-run 占位)"
fi

VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  info "创建虚拟环境 $VENV"
  run_root mkdir -p "$ROOT"
  run_root chown -R "$(id -un):$(id -gn)" "$ROOT" 2>/dev/null || true
  run "$PY" -m venv "$VENV"
else
  info "虚拟环境已存在，复用"
fi

VPY="$VENV/bin/python"
[ "$DRY_RUN" = "1" ] || [ -x "$VPY" ] || die "虚拟环境创建失败：$VPY 不存在"

PIP_IDX="https://pypi.tuna.tsinghua.edu.cn/simple"
REQ="$ROOT/requirements.txt"
info "安装 Python 依赖"
if run "$VPY" -m pip install --upgrade pip -q -i "$PIP_IDX"; then :; else
  warn "使用国内镜像失败，回退到官方源"
  run "$VPY" -m pip install --upgrade pip -q
fi
if [ -f "$REQ" ]; then
  if ! run "$VPY" -m pip install -q -r "$REQ" -i "$PIP_IDX"; then
    warn "国内镜像安装失败，回退官方源（若网络受限请配置代理）"
    run "$VPY" -m pip install -q -r "$REQ"
  fi
else
  warn "未找到 requirements.txt，按最小集合安装"
  run "$VPY" -m pip install -q fastapi uvicorn python-multipart jinja2 cryptography qrcode -i "$PIP_IDX"
fi

# ---- 装完必须证明「真的能导入」-------------------------------------------- #
# ★ 只检查 pip 退出码是不够的：pip 可能报告成功，但装上的版本与当前解释器
#   并不兼容（例如旧解释器 + 只支持新版 Python 的包）。那种情况下安装阶段
#   一切正常，直到第一次启动才抛出一大段裸 traceback —— 用户无从下手。
#   这里就地验证，把「装完了」变成「装对了」。
if [ "$DRY_RUN" != "1" ]; then
  if ERR="$("$VPY" -c 'import fastapi, uvicorn' 2>&1)"; then
    info "Python 依赖校验通过（fastapi / uvicorn 可导入）"
  else
    err_last="$(printf '%s' "$ERR" | tail -1)"
    warn "Python 依赖校验失败：$err_last"
    # 归类逻辑与 run.sh 保持一致（同一判据，避免两处漂移）。
    # 「No module named 'X.Y'」（带点）= 已装但坏；不带点 = 真没装。
    case "$err_last" in
      *"unsupported operand type"*|*SyntaxError*|*"from 'typing'"*|*"from 'types'"*)
        die "已安装的 fastapi/uvicorn 与本解释器（$PYV）不兼容。\
     修复：改用更新的 Python（3.10+）重建虚拟环境后重跑本脚本。" ;;
    esac
    case "$err_last" in
      *"No module named"*)
        mod="$(printf '%s' "$err_last" | sed -n "s/.*No module named '\([^']*\)'.*/\1/p")"
        case "$mod" in
          *.*) die "依赖已装但与解释器不兼容（子模块缺失 $mod）：$err_last \
     修复：$VPY -m pip install --force-reinstall -r $ROOT/requirements.txt" ;;
          *)   die "依赖未装全（缺 $mod）。\
     修复：$VPY -m pip install -r $ROOT/requirements.txt" ;;
        esac ;;
      *) die "依赖导入失败：$err_last" ;;
    esac
  fi
fi
  
# ---- 4. 运行时目录与初始凭据 ---------------------------------------------- #
info "准备运行时目录"
run mkdir -p "$ROOT/store" "$ROOT/data" "$ROOT/cache" "$ROOT/work"
[ "$DRY_RUN" = "1" ] || printf 'placeholder\n' > "$ROOT/store/.gitkeep" 2>/dev/null || true

ENVFILE="$ROOT/kwrt.env"
ADMIN_FILE="$ROOT/data/INITIAL_ADMIN.txt"
ADMIN_PW=""
if [ -f "$ADMIN_FILE" ]; then
  info "已存在管理员凭据文件，保持不变（不重置密码）"
  dim "凭据位置: $ADMIN_FILE"
else
  # 仅在**首次安装**时生成密码。应用侧 _ensure_admin() 只会在
  # 「一个管理员都没有」时才创建账号，所以重复执行不会覆盖既有密码。
  info "生成初始管理员密码"
  if [ "$DRY_RUN" != "1" ]; then
    ADMIN_PW="$("$VPY" -c 'import secrets;print(secrets.token_urlsafe(12))' 2>/dev/null \
                || "$PY" -c 'import secrets;print(secrets.token_urlsafe(12))' \
                || true)"
    umask 077
    printf 'KWRT_ADMIN_PASSWORD=%s\n' "$ADMIN_PW" > "$ENVFILE"
    chmod 600 "$ENVFILE"
    {
      echo "Kwrt 在线定制站 —— 初始管理员凭据"
      echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
      echo
      echo "  用户名: admin"
      echo "  密码  : $ADMIN_PW"
      echo
      echo "登录后请立即在「管理后台 → 用户」修改密码。"
      echo "如需重置：删除本文件与 kwrt.env，清空 users 表里的管理员后重启。"
    } > "$ADMIN_FILE"
    chmod 600 "$ADMIN_FILE"
  fi
fi

# ---- 5. systemd 服务 ------------------------------------------------------ #
if [ "$USE_SYSTEMD" = "1" ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "系统没有 systemctl，跳过 systemd 安装（用 run.sh 前台启动）"
  else
    # ---- 交给共用脚本处理（install.sh 与 update.sh 同一份逻辑）----
    # 抽出来的原因：两边原本各写一份，行为不一致 —— update.sh 在服务缺失时
    # 只打印一句「请手动重启」，而 install.sh 会把失败静默吞掉（`|| true`）。
    SYSTEMD_ARGS=(--port "$PORT" --host "$HOST_BIND" --name "$SERVICE_NAME")
    [ "$DRY_RUN" = "1" ] && SYSTEMD_ARGS+=(--dry-run)
    if bash "$ROOT/scripts/systemd-install.sh" "${SYSTEMD_ARGS[@]}"; then
      SYSTEMD_OK=1
    else
      SYSTEMD_OK=0
      warn "systemd 服务安装未完成（详见上方输出）"
      warn "可手动重试：sudo $ROOT/scripts/systemd-install.sh"
    fi
  fi
else
  info "按要求跳过 systemd；用以下命令前台启动："
  dim "PORT=$PORT $ROOT/run.sh"
fi

# ---- 6. 健康检查 ---------------------------------------------------------- #
if [ "$DRY_RUN" != "1" ] && [ "$USE_SYSTEMD" = "1" ]; then
  info "等待服务就绪…"
  OK=0
  for _ in $(seq 1 30); do
    if "$PY" - "$PORT" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
    d = json.load(r)
sys.exit(0 if d.get("ok") else 1)
PYEOF
    then OK=1; break; fi
    sleep 1
  done
  if [ "$OK" = "1" ]; then
    info "服务已就绪"
  else
    warn "30 秒内未通过健康检查，请查看日志：journalctl -u $SERVICE_NAME -n 100 --no-pager"
  fi
fi

# ---- 7. 完成 -------------------------------------------------------------- #
IP="$( (hostname -I 2>/dev/null || echo 127.0.0.1) | awk '{print $1}')"
cat <<DONE

$(info "安装完成")

  访问地址   http://${IP}:${PORT}/        （本机：http://127.0.0.1:${PORT}/）
  管理后台   http://${IP}:${PORT}/admin/
  健康检查   curl -s http://127.0.0.1:${PORT}/healthz

  管理员账号 admin
DONE
if [ -f "$ADMIN_FILE" ]; then
  echo "  管理员密码 $(sed -n 's/^  密码  : //p' "$ADMIN_FILE")"
  echo "             （同时保存在 $ADMIN_FILE，登录后请立即修改）"
fi
cat <<DONE

  查看日志   journalctl -u ${SERVICE_NAME} -f
  重启服务   sudo systemctl restart ${SERVICE_NAME}
  升级代码   sudo ./update.sh

  注意：本机编译固件需要较大的磁盘与内存（单次构建工作目录约 2 GB）。
        磁盘不足时构建会返回可读错误而非静默失败。
DONE
