#!/bin/bash
# 启动阿尔比恩的大脑（Pi 版）与平台桥（TG/Discord）。幂等：端口在就直接返回。
# 用法：bash /root/albion-pi/start.sh          （默认 8650 + 8660）
#       ALBION_BRAIN_PORT=8651 bash start.sh  （试跑用别的端口）
cd "$(dirname "$0")" || exit 1
PORT="${ALBION_BRAIN_PORT:-8650}"
mkdir -p logs state

# 平台桥（bin/chat_bridge.py，chat-venv 跑，127.0.0.1:8660）：TG/Discord 收发、send_message 的落点。
# 没配 token 时桥自己写一行日志说明就不干活；chat-venv 不在就跳过（脑照常起来）。
start_chat_bridge() {
  local port="${ALBION_CHAT_PORT:-8660}"
  if ss -ltn 2>/dev/null | grep -q ":$port "; then
    if curl -sS -m 5 "http://127.0.0.1:$port/health" 2>/dev/null | grep -q '"bridge": "chat"'; then
      echo "chat bridge already up on $port"; return 0
    fi
    echo "port $port is held by something else: 平台桥没起"; return 1
  fi
  if [ ! -x chat-venv/bin/python ]; then
    echo "chat bridge skipped: chat-venv missing（需要 discord.py + aiohttp）"; return 1
  fi
  setsid nohup chat-venv/bin/python bin/chat_bridge.py >> logs/chat.log 2>&1 </dev/null &
  for _ in $(seq 1 20); do
    sleep 0.5
    if curl -sS -m 5 "http://127.0.0.1:$port/health" 2>/dev/null | grep -q '"bridge": "chat"'; then
      echo "chat-bridge-up on $port"; return 0
    fi
  done
  echo "chat-bridge-fail on $port（看 logs/chat.log）"; return 1
}

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  if curl -sS -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"brain": "pi"'; then
    echo "brain already up on $PORT"; start_chat_bridge; exit 0
  fi
  echo "port $PORT is held by something else (旧脑 Hermes 网关？): 先停掉它再启动 Pi 脑"
  exit 1
fi
setsid nohup python3 bin/server.py >> logs/stdout.log 2>&1 </dev/null &
for _ in $(seq 1 20); do
  sleep 0.5
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    if curl -sS -m 5 "http://127.0.0.1:$PORT/health" | grep -q '"status"'; then
      echo "brain-up on $PORT"; start_chat_bridge; exit 0
    fi
  fi
done
echo "brain-fail on $PORT"; tail -20 logs/stdout.log; exit 1
