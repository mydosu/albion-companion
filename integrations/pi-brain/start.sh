#!/bin/bash
# 启动阿尔比恩的大脑（Pi 版）。幂等：端口在就直接返回。
# 用法：bash /root/albion-pi/start.sh          （默认 8650）
#       ALBION_BRAIN_PORT=8651 bash start.sh  （试跑用别的端口）
cd "$(dirname "$0")" || exit 1
PORT="${ALBION_BRAIN_PORT:-8650}"
mkdir -p logs state
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  if curl -sS -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"brain": "pi"'; then
    echo "brain already up on $PORT"; exit 0
  fi
  echo "port $PORT is held by something else (旧脑 Hermes 网关？): 先停掉它再启动 Pi 脑"
  exit 1
fi
setsid nohup python3 bin/server.py >> logs/stdout.log 2>&1 </dev/null &
for _ in $(seq 1 20); do
  sleep 0.5
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    if curl -sS -m 5 "http://127.0.0.1:$PORT/health" | grep -q '"status"'; then
      echo "brain-up on $PORT"; exit 0
    fi
  fi
done
echo "brain-fail on $PORT"; tail -20 logs/stdout.log; exit 1
