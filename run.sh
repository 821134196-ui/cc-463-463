#!/usr/bin/env bash
# 一条本地命令：生成样例并启动热柜鲜食效期与报损系统
set -e
cd "$(dirname "$0")"
exec python3 run.py "$@"
