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

# 模型跟着脑走：读 agent/settings.json 的 defaultProvider/defaultModel —— 脑每轮把它实际在用的模型写回那份设置
# （bin/follow_settings.py）。这里必须显式传 --model：只省略的话，pi 会沿用会话文件里记着的旧模型
# （2026-10-30 实测：albion-cli 还停在 grokpool/grok-chat-fast，回出来是乱的英文）。临时换一次：ALBION_CLI_MODEL。
MODEL_ARGS=()
if [ -n "${ALBION_CLI_MODEL:-}" ]; then
  MODEL_ARGS=(--model "$ALBION_CLI_MODEL")
else
  CLI_MODEL="$(python3 -c 'import json
try:
    d = json.load(open("/root/albion-pi/agent/settings.json", encoding="utf-8"))
except Exception:
    raise SystemExit(0)
p, m = d.get("defaultProvider") or "", d.get("defaultModel") or ""
print("%s/%s" % (p, m) if p and m else "")' 2>/dev/null)"
  if [ -n "$CLI_MODEL" ]; then MODEL_ARGS=(--model "$CLI_MODEL"); fi
fi
SESSION="${ALBION_CLI_SESSION:-albion-cli}"

EXT=()
for f in agent/extensions/*.ts; do [ -e "$f" ] && EXT+=(-e "$PWD/$f"); done
[ ${#EXT[@]} -gt 0 ] || { echo "[FAIL] 一件扩展都没有，连上的不是她"; exit 1; }

# --tools "*" 不能省：RPC/print 模式不给这个参数就一件工具都没有
if [ "$#" -gt 0 ]; then
  exec pi -p "$*" --session-id "$SESSION" --tools "*" "${MODEL_ARGS[@]}" "${EXT[@]}"
fi
exec pi --session-id "$SESSION" --tools "*" "${MODEL_ARGS[@]}" "${EXT[@]}"