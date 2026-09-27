#!/usr/bin/env bash
# Kwrt PHP 版启动脚本（内置服务器，适合试跑与内网）。
# 生产请用 Nginx/Apache + PHP-FPM，见 php/README.md。
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

command -v php >/dev/null 2>&1 || { echo "[x] 未找到 php，请先安装 PHP 8.0+" >&2; exit 1; }

# 必需扩展检查（缺哪个直接说清楚，别让运行时才报错）
miss=""
for ext in pdo_sqlite sqlite3 curl openssl mbstring json; do
  php -m | grep -qi "^${ext}$" || miss="$miss $ext"
done
if [ -n "$miss" ]; then
  echo "[x] 缺少 PHP 扩展:$miss" >&2
  echo "    Debian/Ubuntu: sudo apt install php-cli php-sqlite3 php-curl php-mbstring" >&2
  echo "    RHEL/CentOS:   sudo dnf install php-cli php-pdo php-curl php-mbstring" >&2
  exit 1
fi

# 根目录可写（要建 users.db / store / work / cache）
[ -w "$(pwd)" ] || { echo "[x] 目录不可写：$(pwd)" >&2; exit 1; }

mkdir -p store work cache

echo "==> PHP $(php -r 'echo PHP_VERSION;')  $HOST:$PORT"
echo "    站点      http://127.0.0.1:${PORT}/"
echo "    管理后台  http://127.0.0.1:${PORT}/admin/"
echo "    健康检查  curl -s http://127.0.0.1:${PORT}/healthz"
echo "    （首次使用请先跑：php php/scripts/init_admin.php）"
echo

# ★ 必须带 router.php —— 否则内置服务器会把没有实体文件的路径直接 404
exec php -S "${HOST}:${PORT}" -t php/public php/public/router.php
