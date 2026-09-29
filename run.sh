#!/usr/bin/env bash
# Kwrt 在线定制站 —— 启动脚本
set -e
cd "$(dirname "$0")"

# 依赖检查
MISS=""
if command -v rpm >/dev/null 2>&1; then
  need_rpm="perl-FindBin perl-IPC-Cmd perl-Digest-SHA perl-Time-Piece zstd make gcc gawk unzip"
  for p in $need_rpm; do rpm -q "$p" >/dev/null 2>&1 || MISS="$MISS $p"; done
  if [ -n "$MISS" ]; then
    echo "[!] 缺少构建依赖:$MISS"
    echo "    请执行: dnf install -y$MISS"
    exit 1
  fi
elif command -v dpkg >/dev/null 2>&1; then
  need_deb="perl squashfs-tools zstd make gcc gawk unzip tar"
  for p in $need_deb; do dpkg -s "$p" >/dev/null 2>&1 || MISS="$MISS $p"; done
  if [ -n "$MISS" ]; then
    echo "[!] 缺少构建依赖:$MISS"
    echo "    请执行: sudo apt-get install -y$MISS"
    exit 1
  fi
else
  echo "[!] 未识别包管理器（需要 rpm 或 dpkg），跳过依赖检查"
fi

# ---------------------------------------------------------------------------
# 选一个「真的能跑」的解释器
#
# ★ 只比版本号是不够的。实测踩到的坑：一台机器上 `python3` 是 3.9，
#   而 site-packages 里装着只支持 3.10+ 的 fastapi/uvicorn —— 于是
#       import uvicorn → TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
#       import fastapi → SyntaxError: invalid syntax (_lazyimport.py)
#   版本号「看起来够」（README 写 3.9+），实际根本起不来。
#   所以这里做的是**能力探测**：真的去 import 一次，成功才算数。
#
# 探测顺序：KWRT_PYTHON 指定 > python3 > 常见高版本解释器。
# 这样系统默认解释器坏了、但旁边装着能用的 3.12/3.13 时，脚本能自己找到活的。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 归类「导入失败」的原因
#
# ★ 这里的判据是**实测**出来的，不是想当然。兼容版本缺失时，真实报错可能是：
#     ImportError: cannot import name 'TypeAlias' from 'typing'
#     TypeError:   unsupported operand type(s) for |: 'type' and 'NoneType'
#     SyntaxError: invalid syntax
#   这些全都是「装上的包要求更新的 Python」，**不是「依赖没装」**。
#   最初我按猜想去匹配，把第一条误判成了「依赖没装」—— 那会把用户引向
#   重装依赖（解决不了），而不是换解释器。实测后改成下面的判据。
#
#   真正的「没装」长这样：No module named 'fastapi'（一级模块，名字里没有点）。
#   而 No module named 'pydantic_core._pydantic_core'（带点）= 包**已装但坏了**
#   （二进制/版本不匹配），同属环境问题。
#
# 输出：version | missing | other
classify_import_error() {
  _err="$1"
  case "$_err" in
    *"unsupported operand type"*|*SyntaxError*|*"from 'typing'"*|*"from 'types'"*)
      printf 'version'; return 0 ;;
  esac
  case "$_err" in
    *"No module named"*)
      _mod="$(printf '%s' "$_err" | sed -n "s/.*No module named '\([^']*\)'.*/\1/p")"
      case "$_mod" in
        *.*) printf 'version' ;;   # 子模块缺失 = 已装但坏
        *)   printf 'missing' ;;   # 一级模块缺失 = 真没装
      esac
      return 0 ;;
  esac
  printf 'other'
}

pick_python() {
  for c in "${KWRT_PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.10; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

FIRST_ERR=""
if ! KWRT_PY="$(pick_python)"; then
  echo "[!] 没找到能用的 Python 环境。" >&2
  echo "" >&2
  echo "    逐个解释器的实测结果：" >&2
  for c in "${KWRT_PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.10; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    ver="$("$c" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)"
    err="$("$c" -c "import fastapi, uvicorn" 2>&1 | tail -1)"
    if [ -z "$FIRST_ERR" ] && [ -n "$err" ]; then
      FIRST_ERR="$err"
    fi
    if [ -z "$err" ]; then
      echo "      $c ($ver)  ✓ 可用" >&2
    else
      echo "      $c ($ver)  ✗ $err" >&2
    fi
  done
  echo "" >&2
  # 按**真实错误**给结论，而不是一律猜「版本太旧」：
  #   · unsupported operand type(s) for | / SyntaxError  → 解释器太旧，装上的包要求 3.10+
  #   · ModuleNotFoundError                              → 依赖根本没装
  # 猜错方向会让用户跑去重装依赖（解决不了），或去升级 Python（本不必）。
  case "$(classify_import_error "$FIRST_ERR")" in
    version)
      echo "    诊断：**依赖版本与解释器不兼容** —— 装上的 fastapi/uvicorn 要求更新的 Python。" >&2
      echo "    修复（推荐虚拟环境，不要装进系统 Python）：" >&2
      echo "      python3.13 -m venv .venv && . .venv/bin/activate" >&2
      echo "      pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple" >&2
      echo "      KWRT_PYTHON=.venv/bin/python ./run.sh" >&2 ;;
    missing)
      echo "    诊断：**依赖没装**（解释器本身没问题）。" >&2
      echo "    修复：" >&2
      echo "      pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple" >&2
      echo "    若不想污染系统环境：" >&2
      echo "      python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt" >&2
      echo "      KWRT_PYTHON=.venv/bin/python ./run.sh" >&2 ;;
    *)
      echo "    诊断：见上方错误原文。可先单独执行下面的命令看完整堆栈：" >&2
      echo "      python3 -c 'import fastapi, uvicorn'" >&2 ;;
  esac
  echo "" >&2
  echo "    ⚠ 不要照抄「pip install fastapi uvicorn」这种不带版本约束的命令：" >&2
  echo "      它会把最新版装进旧解释器，落得和现在一样的报错。" >&2
  exit 1
fi

PORT="${PORT:-8443}"

# ★ --forwarded-allow-ips 必须显式设置，否则 uvicorn 自己会先「替你做决定」：
#   它的 proxy_headers 默认开启、forwarded_allow_ips 默认是 127.0.0.1,::1，
#   于是只要直连对端是本机（本机反代很常见），uvicorn 就会**用 X-Forwarded-For
#   改写 request.client** —— 应用层再也分不清「真实对端」和「伪造的客户端」，
#   app/netcfg.py 里那套可信代理判定就被架空了。
#   实测后果：封禁可被一个请求头绕过（本条已复现并修复）。
#   留空 = 完全不信转发头，交给应用层按「可信代理网段」设置自行判定。
#   若你确实有前置反代/CDN，请把这里设成其回源网段（与后台设置保持一致），
#   例如：FORWARDED_ALLOW_IPS="172.16.0.0/12"
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-}"

echo "==> Kwrt 在线定制站启动于 http://0.0.0.0:$PORT"
if [ -n "$FORWARDED_ALLOW_IPS" ]; then
  echo "    转发头信任来源: $FORWARDED_ALLOW_IPS"
else
  echo "    转发头信任来源: 无（应用层按「可信代理网段」设置自行判定）"
fi
exec "$KWRT_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --log-level info \
  --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
