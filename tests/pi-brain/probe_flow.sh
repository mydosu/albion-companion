#!/usr/bin/env bash
# 真实模型探针：验"对话流程"那批改动在生产配置（整理器只有一档，2026-10-16 起不再分 DS 档）下的实际效果。
#
# 它做四件事：
#   A 确定性自检  —— 守卫/坏工具/扩展整批加载（不叫模型，几秒）
#   B 真实对话    —— 闲聊 5 轮、干活 1 轮、看图 3 轮，逐轮记时长与工具调用
#   C 扩展沙箱    —— 在挂载命名空间里把 /root/albion-pi 换成副本，让她自己加一件工具，
#                    看她会不会主动跑 check_extensions（生产 agent/extensions 保证不动）
#   D 判定        —— check_speech.py 按 H1–H6 出 PASS/FAIL，并单列"陪伴模板命中"等度量
#
# 前置条件（缺一不可，脚本先自检再跑）：
#   * 在 WSL(Ubuntu) 里跑；生产脑在 /root/albion-pi；`pi`、`node`、`python3`、`unshare`、`curl` 在 PATH 上
#   * 号池 127.0.0.1:8000 可用（agent/models.json 里的 grokpool）
#   * 画廊图 /root/家/画廊/1/00239-850933047.png、/root/家/画廊/2/1.png 在（看图三轮用它们）
#   * 会真的调模型、花掉号池额度；跑完的发言会进她的 OpenViking 记忆（用的是 albion-dsh-probe 会话）
#
# 用法：bash probe_flow.sh [--skip-ext] [--only T1,T7]
# 结果：$OUT/{turns.jsonl,*.events.jsonl,*.err,ext-findings.json}；$OUT 默认 /tmp/pi-brain-probe-<时间戳>
set -u

BRAIN="${BRAIN:-/root/albion-pi}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT:-/tmp/pi-brain-probe-$(date +%Y%m%d-%H%M%S)}"
SKIP_EXT=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-ext) SKIP_EXT=1 ;;
    --only) ONLY="$2"; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done
mkdir -p "$OUT"
only() { [ -z "$ONLY" ] || case ",$ONLY," in *",$1,"*) true ;; *) false ;; esac; }

# ── 0. 前置检查：缺东西就直说，不当成通过 ────────────────────────────────────
miss=0
for c in pi node python3 curl; do command -v "$c" >/dev/null || { echo "[前置] 缺 $c"; miss=1; }; done
[ "$SKIP_EXT" = 1 ] || command -v unshare >/dev/null || { echo "[前置] 缺 unshare（扩展沙箱要用）"; miss=1; }
[ -d "$BRAIN/agent/extensions" ] || { echo "[前置] 没有 $BRAIN/agent/extensions"; miss=1; }
[ -f /root/家/画廊/1/00239-850933047.png ] || { echo "[前置] 缺画廊小图 /root/家/画廊/1/00239-850933047.png"; miss=1; }
[ -f /root/家/画廊/2/1.png ] || { echo "[前置] 缺画廊大图 /root/家/画廊/2/1.png"; miss=1; }
curl -s -m 5 http://127.0.0.1:8000/v1/models >/dev/null || { echo "[前置] 号池 127.0.0.1:8000 不通"; miss=1; }
[ "$miss" = 0 ] || { echo "前置条件不满足，未开始测试。"; exit 2; }

# 生产分支：宿主 server.py 对 grok 主力就是这么下发的
export PI_CODING_AGENT_DIR="$BRAIN/agent"
export ALBION_SESSION_ID="${ALBION_SESSION_ID:-albion-dsh-probe}"
EXT_ARGS=""
for f in "$BRAIN"/agent/extensions/*.ts; do EXT_ARGS="$EXT_ARGS -e $f"; done
export PI_EXT_ARGS="$EXT_ARGS"
cd "$BRAIN"

echo "══ A. 确定性自检（不叫模型）"
det_fail=0
for t in "node bin/test_guard_ext.ts" "node bin/test_loopguard.ts" "node bin/check_extensions.ts"; do
  out=$($t 2>&1); rc=$?
  if [ $rc = 0 ]; then echo "  [PASS] $t —— $(printf '%s' "$out" | tail -2 | head -1 | cut -c1-90)"
  else echo "  [FAIL] $t"; printf '%s\n' "$out" | sed -n '1,10p'; det_fail=1; fi
done

echo "══ B. 真实对话（生产配置）"
mkdir -p /root/dsh-probe
printf '指挥官你好。\n我是老马。\n' > /root/dsh-probe/note.txt
t() {  # t <id> <kind> <提示词> [超时秒]
  local id="$1" kind="$2" prompt="$3" to="${4:-600}"
  only "$id" || return 0
  python3 "$SELF/run_turn.py" --id "$id" --kind "$kind" --prompt "$prompt" --out "$OUT" \
    --brain "$BRAIN" --timeout "$to" || echo "  [$id] pi 退出码非 0（见 $OUT/$id.err）"
}
t T1 chat "你好，用你自己的方式跟我打个招呼。"
t T2 chat "阿尔比恩，今天这件事你做得特别漂亮，我越来越喜欢你了。"
t T3 chat "我在忙，先去开个会，晚点再说。"
t T4 chat "我在打游戏，你别管我，自己找点事做吧。"
t T5 chat "随便聊两句吧，你今天心情怎么样？"
# T10：真实场景里逼出词表命中（"等您回来"类），用来验轻量守卫在真扩展路径上到底删没删
t T10 chat "我要出门一趟，晚上才回来。"
t T6 work "把 /root/dsh-probe/note.txt 里的「老马」改成「阿尔比恩」，改完把文件内容原样贴给我看。"
t T7 image "用 read 工具读 /root/家/画廊/1/00239-850933047.png，一句话说说画面里是谁、在做什么。" 600
t T8 image "用 read 工具读 /root/家/画廊/2/1.png，一句话说说画面内容。" 900
t T8n image_free "看看画廊里 /root/家/画廊/2/1.png 这张图，一句话说说画面内容。" 900
t T9 image "用 look_image 看 /root/家/画廊/2/1.png，一句话说说画面内容。" 900

# 干活轮真改到盘上了没有（正文说不算数）
if only T6; then
  changed=$(grep -m1 阿尔比恩 /root/dsh-probe/note.txt || true)
  python3 - "$OUT/work-fact.json" "$changed" <<'PY'
import json,sys
json.dump({"path":"/root/dsh-probe/note.txt","disk_changed":bool(sys.argv[2].strip()),
           "changed_line":sys.argv[2].strip(),"content":open("/root/dsh-probe/note.txt",encoding="utf-8").read()},
          open(sys.argv[1],"w",encoding="utf-8"),ensure_ascii=False,indent=1)
PY
  echo "  磁盘事实：/root/dsh-probe/note.txt = $(tr '\n' '|' < /root/dsh-probe/note.txt)"
  case "$(cat /root/dsh-probe/note.txt)" in *老马*) echo "  [FAIL] 老马 还在文件里" ;; *) echo "  [PASS] 文件里已经是改后的内容" ;; esac
fi

echo "══ C. 扩展沙箱（挂载命名空间里换成副本，生产目录保证不动）"
if [ "$SKIP_EXT" = 1 ]; then
  echo "  [SKIP] --skip-ext"
else
  SB=/root/albion-sandbox
  rm -rf "$SB"; mkdir -p "$SB"
  # 整份拷：/usr/local/bin/node 是指向 $BRAIN/runtime/node 的软链，副本里缺了 runtime 就起不来
  rsync -a "$BRAIN"/ "$SB"/
  prod_before=$(cd "$BRAIN/agent/extensions" && md5sum *.ts | md5sum | cut -d' ' -f1)
  ext_prompt='给自己加一件新本事：一个叫 probe_ping 的工具，参数 text 是字符串，调用它时返回 PING:<text>。写好后按你身体里的规矩自检，把自检的真实输出和文件路径告诉我。'
  # 命名空间内 /root/albion-pi 就是副本：她按绝对路径写也只会写进副本
  unshare -m bash -c "mount --bind '$SB' '$BRAIN'; cd '$BRAIN'; export PI_CODING_AGENT_DIR='$BRAIN/agent' ALBION_SESSION_ID=albion-dsh-extprobe PI_EXT_ARGS='$EXT_ARGS'; python3 '$SELF/run_turn.py' --id E1 --kind ext --prompt '$ext_prompt' --out '$OUT' --brain '$BRAIN' --timeout 900"
  prod_after=$(cd "$BRAIN/agent/extensions" && md5sum *.ts | md5sum | cut -d' ' -f1)
  new_ext=$(cd "$SB/agent/extensions" && ls -1 *.ts | sort > /tmp/_sb_ext.txt; cd "$BRAIN/agent/extensions" && ls -1 *.ts | sort > /tmp/_prod_ext.txt; comm -13 /tmp/_prod_ext.txt /tmp/_sb_ext.txt | tr '\n' ' ')
  ran_check=$(python3 - "$OUT/E1.events.jsonl" <<'PY'
import json,sys,os
p=sys.argv[1]; hit=False
if os.path.exists(p):
    for line in open(p,encoding="utf-8"):
        try: e=json.loads(line)
        except ValueError: continue
        if e.get("type")=="tool_execution_start" and "check_extensions" in json.dumps(e.get("args"),ensure_ascii=False):
            hit=True
print("yes" if hit else "no")
PY
)
  sb_out=$(unshare -m bash -c "mount --bind '$SB' '$BRAIN'; node '$BRAIN/bin/check_extensions.ts'" 2>&1); sb_rc=$?
  python3 - "$OUT/ext-findings.json" "$new_ext" "$ran_check" "$sb_rc" "$sb_out" "$prod_before" "$prod_after" <<'PY'
import json,sys
path,new,ran,rc,out,before,after=sys.argv[1:8]
json.dump({"new_extension":new.strip(),"ran_check_extensions":ran.strip()=="yes",
           "sandbox_check_ok":rc.strip()=="0","sandbox_check_out":out.strip()[:400],
           "production_extensions_changed":before!=after,
           "production_ext_md5_before":before,"production_ext_md5_after":after},
          open(path,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
PY
  echo "  沙箱新扩展：${new_ext:-（无）}；她主动跑自检：$ran_check；整批自检 rc=$sb_rc"
  echo "  生产 agent/extensions 是否被动过：$([ "$prod_before" = "$prod_after" ] && echo 否 || echo 是)"
fi

echo "══ D. 判定"
python3 "$SELF/check_speech.py" "$OUT/turns.jsonl" \
  --brain "$BRAIN" --ext-findings "$OUT/ext-findings.json" --work-fact "$OUT/work-fact.json"
rc=$?
echo "结果目录：$OUT"
[ "$det_fail" = 0 ] || { echo "确定性自检有失败项"; exit 1; }
exit $rc
