#!/usr/bin/env bash
# 启动脚本自检：run.sh 的「解释器选择」与「导入失败归类」逻辑。
#
# 为什么要有这个：这两段逻辑专门用来**把环境问题讲清楚**，它们自己出错就会
# 把人引向错误方向（实测踩到过：把「版本不兼容」误判成「依赖没装」，
# 于是用户去重装依赖 —— 解决不了问题）。所以判据必须被测试钉住。
#
# 用法：bash tools/verify_launcher.sh
set -u
cd "$(dirname "$0")/.." || exit 1

PASS=0; FAIL=0
ok()   { printf "  ✓ %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  ✗ %s\n" "$1"; FAIL=$((FAIL+1)); }

echo "======================================================================"
echo " 启动脚本自检"
echo "======================================================================"

# ---- 1. 语法 ---------------------------------------------------------------
for f in run.sh install.sh; do
  if bash -n "$f" 2>/dev/null; then ok "$f 语法正确"; else bad "$f 语法错误"; fi
done

# ---- 2. 两个脚本都存在能力探测（不能只比版本号）-----------------------------
for f in run.sh install.sh; do
  if grep -q "import fastapi, uvicorn" "$f"; then
    ok "$f 含「实际导入」能力探测"
  else
    bad "$f 缺少能力探测（只比版本号会漏掉「版本够但包装错了」）"
  fi
done

# ---- 3. 归类判据：用**实测到的真实报错原文**验证 -----------------------------
sed -n '/^classify_import_error() {/,/^}/p' run.sh > /tmp/_kwrt_cls.sh
if [ ! -s /tmp/_kwrt_cls.sh ]; then
  bad "未能从 run.sh 提取 classify_import_error（函数被改名或删除？）"
else
  # shellcheck disable=SC1091
  . /tmp/_kwrt_cls.sh
  chk() { # want, err, 说明
    got="$(classify_import_error "$2")"
    if [ "$got" = "$1" ]; then ok "$3 → $got"; else bad "$3 → 期望 $1 得到 $got"; fi
  }
  chk version "ImportError: cannot import name 'TypeAlias' from 'typing' (/usr/lib64/python3.9/typing.py)" \
      "3.9 装了要求 3.10+ 的包（实测原文）"
  chk version "TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'" \
      "运行时 | 运算不支持（实测原文）"
  chk version "ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'" \
      "子模块缺失 = 已装但坏"
  chk missing "ModuleNotFoundError: No module named 'fastapi'" "一级依赖真的没装"
  chk other   "RuntimeError: 其他未知错误" "未知错误不硬套结论"
  rm -f /tmp/_kwrt_cls.sh
fi

# ---- 4. run.sh 必须真的用选出来的解释器启动（而不是仍写死 python3）-----------
if grep -qE 'exec "\$KWRT_PY" -m ' run.sh; then
  ok "run.sh 用探测到的解释器启动"
else
  bad "run.sh 仍写死解释器（探测结果没被使用，等于白探）"
fi

# ---- 5. 不再推荐不带版本约束的安装命令 --------------------------------------
if grep -q "pip install fastapi uvicorn python-multipart -i" run.sh; then
  bad "run.sh 仍在推荐裸包名安装（会把新版装进旧解释器）"
else
  ok "run.sh 不再推荐裸包名安装"
fi

echo "======================================================================"
if [ "$FAIL" = "0" ]; then
  echo " 结果: 通过 $PASS 项，失败 0 项   全部通过 ✓"
else
  echo " 结果: 通过 $PASS 项，失败 $FAIL 项   ✗"
fi
echo "======================================================================"
[ "$FAIL" = "0" ] || exit 1
