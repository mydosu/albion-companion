#!/bin/bash
# 启动阿尔比恩的大脑与平台桥（TG/Discord）。幂等：端口在就直接返回。
# 用法：bash /root/albion-pi/start.sh          （默认 8650 + 8660）
#       ALBION_BRAIN_PORT=8651 bash start.sh  （试跑用别的端口）
#       ALBION_KERNEL=agy bash start.sh       （agy 内核：顺带拉起扩展宿主 8661）
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

# agy 内核时顺带拉起扩展宿主（bin/agy_host.ts，127.0.0.1:8661）：
# 模型循环在 agy 里跑，扩展（工具 MCP + 每轮钩子）由宿主按会话喂给 server.py。
# 环境变量和 server.py 用的是同一套（API_SERVER_KEY、ALBION_BRAIN_PORT、state/活动库/日志路径）。
start_agy_host() {
  local port="${ALBION_HOST_PORT:-8661}"
  local key="${API_SERVER_KEY:-}"
  if ss -ltn 2>/dev/null | grep -q ":$port "; then
    echo "agy host already up on $port"; return 0
  fi
  if [ ! -x /usr/local/bin/node ] && ! command -v node >/dev/null 2>&1; then
    echo "agy host skipped: node missing"; return 1
  fi
  local node_bin
  node_bin="$(command -v node)"
  setsid nohup env \
    API_SERVER_KEY="$key" \
    ALBION_BRAIN_PORT="$PORT" \
    ALBION_HOST_PORT="$port" \
    PI_CODING_AGENT_DIR="$PWD/agent" \
    ALBION_HOST_LOG="$PWD/logs/agy-host.log" \
    ALBION_STATE_DIR="${ALBION_STATE_DIR:-$PWD/state}" \
    ALBION_ACTIVITY_DB="${ALBION_ACTIVITY_DB:-$PWD/state/activity.db}" \
    TZ="${TZ:-Asia/Shanghai}" \
    "$node_bin" bin/agy_host.ts >> logs/agy-host.log 2>&1 </dev/null &
  for _ in $(seq 1 30); do
    sleep 0.5
    if curl -sS -m 5 "http://127.0.0.1:$port/health" 2>/dev/null | grep -q '"ok"'; then
      echo "agy-host-up on $port"; return 0
    fi
  done
  echo "agy-host-fail on $port（看 logs/agy-host.log）"; return 1
}

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  if curl -sS -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"brain": "pi"'; then
    echo "brain already up on $PORT"; start_chat_bridge
    if [ "${ALBION_KERNEL:-pi}" = "agy" ]; then start_agy_host; fi
    exit 0
  fi
  echo "port $PORT is held by something else (旧脑 Hermes 网关？): 先停掉它再启动 Pi 脑"
  exit 1
fi
setsid nohup python3 bin/server.py >> logs/stdout.log 2>&1 </dev/null &
for _ in $(seq 1 20); do
  sleep 0.5
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    if curl -sS -m 5 "http://127.0.0.1:$PORT/health" | grep -q '"status"'; then
      echo "brain-up on $PORT"; start_chat_bridge
      if [ "${ALBION_KERNEL:-pi}" = "agy" ]; then start_agy_host; fi
      exit 0
    fi
  fi
done
echo "brain-fail on $PORT"; tail -20 logs/stdout.log; exit 1
