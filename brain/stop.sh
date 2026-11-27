#!/bin/bash
# 停掉阿尔比恩的大脑（Pi 版）：先关 8650 的大脑，再关 8660 的平台桥、8798 的转发。
cd "$(dirname "$0")" || exit 1
# 8661 是 agy 内核的扩展宿主（ALBION_KERNEL=agy 时由 start.sh 拉起）；Pi 内核下那儿空着。
for port in "${ALBION_BRAIN_PORT:-8650}" "${ALBION_CHAT_PORT:-8660}" "${ALBION_HOST_PORT:-8661}" 8798; do
  pid=$(ss -ltnp 2>/dev/null | grep ":$port " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  if [ -n "$pid" ]; then kill "$pid" && echo "stopped :$port (pid $pid)"; else echo "nothing on :$port"; fi
done
