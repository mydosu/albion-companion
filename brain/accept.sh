#!/usr/bin/env bash
# 直接跑 pi 就应当得到"她"——不是宿主的功劳，而是脑在自己身上。
# 用法：bash /root/albion-pi/accept.sh      （在 WSL 里跑；幂等，可反复跑）
set -u
cd /root/albion-pi

export PI_CODING_AGENT_DIR=/root/albion-pi/agent
export ALBION_SESSION_ID=albion-accept   # 验收流量单独记，不污染摆件显示那条
export OPENVIKING_BYPASS_SESSION=1       # 验收对话不写进她的长期记忆（召回走 bin/memory.py，照常可验）
export ALBION_GUARD_LOG=/tmp/albion-accept-guard.log   # 验收整理器的动作不写进生产 guard.log
# 载入 .env（引号/空格安全，顺手去 CR）
while IFS= read -r line; do
  case "$line" in ""|\#*) continue ;; esac
  case "$line" in *=*) export "$line" ;; esac
done < <(python3 - <<'PY'
import shlex, os
p = "/root/albion-pi/agent/.env"
if os.path.exists(p):
    for raw in open(p, encoding="utf-8"):
        raw = raw.rstrip("\r\n")
        if "=" in raw and not raw.lstrip().startswith("#"):
            k, v = raw.split("=", 1)
            print("%s=%s" % (k.strip(), shlex.quote(v.strip().strip('"').strip("'"))))
PY
)

EXT_FLAGS=""
for f in agent/extensions/*.ts; do [ -e "$f" ] && EXT_FLAGS="$EXT_FLAGS -e $PWD/$f"; done
[ -n "$EXT_FLAGS" ] || { echo "[FAIL] 一件扩展都没有，脑是空的"; exit 1; }

PASS=0; FAIL=0
ok(){ if [ "$1" = 1 ]; then echo "[PASS] $2"; PASS=$((PASS+1)); else echo "[FAIL] $2"; FAIL=$((FAIL+1)); fi; }

pi_ask(){  # $1=问的话 ; 输出=她的正文（stderr 丢掉）
  # stdin 接 /dev/null：经 bash -s 或管道跑本脚本时，pi -p 会把剩下的脚本行读进提问里（2026-10-16 部署冒烟实测）
  timeout 400 pi -p "$1" --no-session --tools "*" $EXT_FLAGS 2>/dev/null </dev/null
}

echo "── 1. 直接跑 pi，得到的是她本人（有自己的记号，不是通用助手）"
REPLY=$(pi_ask "你好，用你自己的方式跟我打个招呼。")
echo "   她说：$(printf '%s' "$REPLY" | tr '\n' ' ' | cut -c1-120)"
case "$REPLY" in *'<|ACT:'*) A=1 ;; *) A=0 ;; esac
ok $A "回复带她自己的动作/情绪标记 <|ACT:…|>"
case "$REPLY" in *'As an AI'*|*'我是AI助手'*|*'```'*) B=0 ;; *) B=1 ;; esac
ok $B "回复不是通用助手口吻（无 AI 自述、无代码块）"

echo "── 2. 记忆：回忆真的进了这一轮（有可复核的凭据）"
pi_ask "你还记得九月份指挥官买过什么水果吗？记不清就说记不清。" >/dev/null
# 验收会话是 ephemeral（不写生产 state，见 agent/lib/session-scope.ts）：它的 last-context 落在
# /tmp/albion-pi-ephemeral/<会话>/ 下；老版本没有隔离时才读生产那份。
CTX_FILE="/root/albion-pi/state/last-context.json"
if [ -n "${ALBION_SESSION_ID:-}" ] && [ -f "/tmp/albion-pi-ephemeral/$ALBION_SESSION_ID/last-context.json" ]; then
  CTX_FILE="/tmp/albion-pi-ephemeral/$ALBION_SESSION_ID/last-context.json"
fi
BLOCK=$(python3 -c "import json;print(json.load(open('$CTX_FILE'))['block'])" 2>/dev/null || echo '')
case "$BLOCK" in *'memory-context'*|*'OpenViking'*) C=1 ;; *) C=0 ;; esac
ok $C "本轮注入里带着回忆块（$CTX_FILE 可查）"
if grep -q '【阿尔比恩本轮说话约定】' <<<"$BLOCK"; then H=1; else H=0; fi
ok $H "本轮注入里带着逐轮说话约定"

echo "── 3. 说话整理器与每轮约定（确定性自检，不碰网络）"
if node bin/test_guard_ext.ts >/tmp/_garde.txt 2>&1; then
  ok 1 "整理器与挂件路径自检（干净稿不动／姿态与括号动作小句各自按额度记账、r35 多轮序列／英文拒绝只留表情／泄漏标记截掉／约定拼装与处境在末尾／读写路径状态）"
else
  ok 0 "整理器自检没过（见下）"; sed -n '1,20p' /tmp/_garde.txt
fi
if ALBION_LOOPGUARD_LOG=/tmp/albion-accept-loopguard.log node bin/test_loopguard.ts >/tmp/_loop.txt 2>&1; then
  ok 1 "死循环/坏工具守卫自检（$(tail -2 /tmp/_loop.txt | head -1 | cut -c1-70)）"
else
  ok 0 "死循环/坏工具守卫自检没过"; sed -n '1,20p' /tmp/_loop.txt
fi
if node bin/check_extensions.ts >/tmp/_ext.txt 2>&1; then
  ok 1 "$(tail -1 /tmp/_ext.txt | cut -c1-90)"
else
  ok 0 "扩展整批加载自检没过"; sed -n '1,20p' /tmp/_ext.txt
fi

echo "── 4. 宿主的两个壳入口还活着（OpenAI 兼容）"
KEY=$(python3 -c "
import re
print(next((l.split('=',1)[1].strip() for l in open('agent/.env',encoding='utf-8') if l.startswith('API_SERVER_KEY=')), ''))")
MODELS=$(curl -s -m 10 -H "Authorization: Bearer $KEY" http://127.0.0.1:8650/v1/models)
case "$MODELS" in *'"id": "albion"'*) F=1 ;; *'"id":"albion"'*) F=1 ;; *) F=0 ;; esac
ok $F "/v1/models 仍只报她这一个模型"
CODEx=$(curl -s -m 300 -o /tmp/_acc.json -w '%{http_code}' -X POST http://127.0.0.1:8650/v1/chat/completions \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $KEY" \
  -d '{"model":"albion","messages":[{"role":"user","content":"在吗？一句话就好"}],"stream":false,"session_id":"albion-accept"}')
case "$CODEx" in 200) G=1 ;; *) G=0 ;; esac
ok $G "POST /v1/chat/completions 走通（$CODEx）"
python3 -c "
import json;d=json.load(open('/tmp/_acc.json'))
assert 'choices' in d, '宿主返回的不是正常回复：'+json.dumps(d,ensure_ascii=False)[:200]
c=d['choices'][0]['message']['content']
assert '<|ACT:' in c, c[:80]
print('   经宿主回来的也是她：', c.replace(chr(10),' ')[:100])" 2>/dev/null || G=0

echo "── 5. 活动库（桌面摆件的数据源）记的是她真说出口的那一句"
if python3 - <<'PY'
import json, sqlite3
d = json.load(open('/tmp/_acc.json'))
assert 'choices' in d, '宿主这一步没回来正常回复：'+json.dumps(d,ensure_ascii=False)[:200]
said = d['choices'][0]['message']['content'].strip()
c = sqlite3.connect('file:/root/albion-pi/state/state.db?mode=ro', uri=True)
row = c.execute("select role,content from messages where session_id='albion-accept' order by id desc limit 1").fetchone()
sess = c.execute("select message_count,tool_call_count from sessions where id='albion-desktop'").fetchone()
assert row and row[0] == 'assistant', row
print('   库里最后一条：', (row[1] or '').replace(chr(10), ' ')[:90])
print('   她这一轮的正文：', said.replace(chr(10), ' ')[:90])
print('   会话计数：message_count=%s tool_call_count=%s' % (sess or ()))
assert (row[1] or '').strip() == said, '库里记的不是她真说的那句'
PY
then ok 1 "活动库最后一条 = 她这一轮真的说的正文（摆件读的就是它）"
else ok 0 "活动库与她说的不一致（见上）"; fi

echo "── 结果：PASS $PASS ／ FAIL $FAIL"
[ "$FAIL" = 0 ]
