#!/bin/bash
# 连她的 CLI。交互：bash cli.sh        一句话：bash cli.sh "在吗"
# 与宿主同源：同一个 agent dir、同一套扩展、同一具人格、同一个模型。
# 会话名默认 albion-cli —— 摆件只读 albion-desktop，所以 CLI 里的对话不会显示在她桌面上。
set -u
cd /root/albion-pi || exit 1
export PI_CODING_AGENT_DIR=/root/albion-pi/agent
export ALBION_SESSION_ID="${ALBION_SESSION_ID:-albion-cli}"

# agent/.env（去 CR、引号安全）：pi 自己不读这个文件，宿主也是手工注入的
eval "$(python3 - <<'PY'
import shlex
try:
    fh = open("/root/albion-pi/agent/.env", encoding="utf-8")
except FileNotFoundError:
    raise SystemExit(0)
for raw in fh:
    raw = raw.rstrip("\r\n")
    if "=" in raw and not raw.lstrip().startswith("#"):
        k, v = raw.split("=", 1)
        print("export %s=%s" % (k.strip(), shlex.quote(v.strip().strip('"').strip("'"))))
PY
)"

MODEL="${ALBION_CLI_MODEL:-commandcode/deepseek/deepseek-v4.1-flash}"   # 只有脑本体用 v4-flash，其余用 v4.1-flash；换就 export ALBION_CLI_MODEL=provider/model（不叫 ALBION_MODEL：宿主那个只填模型名）
SESSION="${ALBION_CLI_SESSION:-albion-cli}"

EXT=()
for f in agent/extensions/*.ts; do [ -e "$f" ] && EXT+=(-e "$PWD/$f"); done
[ ${#EXT[@]} -gt 0 ] || { echo "[FAIL] 一件扩展都没有，连上的不是她"; exit 1; }

# --tools "*" 不能省：RPC/print 模式不给这个参数就一件工具都没有
if [ "$#" -gt 0 ]; then
  exec pi -p "$*" --session-id "$SESSION" --tools "*" --model "$MODEL" "${EXT[@]}"
fi
exec pi --session-id "$SESSION" --tools "*" --model "$MODEL" "${EXT[@]}"
