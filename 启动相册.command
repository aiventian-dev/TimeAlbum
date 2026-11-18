#!/bin/bash
# 双击启动「时光相册（多库版）」，浏览器会自动打开 http://127.0.0.1:8905
cd "$(dirname "$0")" || exit 1

PY=""
for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$c" ]; then PY="$c"; break; fi
  done
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi
if [ -z "$PY" ]; then
  echo "没有找到 python3，请先安装命令行工具：xcode-select --install"
  read -r -t 15 _
  exit 1
fi

exec "$PY" album_server.py "$@"
