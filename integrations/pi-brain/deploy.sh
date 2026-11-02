#!/bin/bash
# 把本目录（integrations/pi-brain）部署到 WSL 里的脑目录，默认 /root/albion-pi。
# 用法：bash deploy.sh [目标目录]      或  ALBION_HOME=/some/dir bash deploy.sh
#   DRY_RUN=1   只列会动什么，不写
#   SKIP_CHECK=1  不跑 tools/check_pi_brain_sync.py
# 规矩：
#   · 每个会被覆盖的已有文件，先原样备份到 <目标>/backup/deploy-<时间>/<相对路径>
#   · 私密/运行时文件只在目标里不存在时才放（绝不覆盖）：agent/.env agent/SOUL.md agent/models.json
#     agent/settings.json agent/real-sessions.json agent/auth.json；SYSTEM.md 是生成物，不部署
#   · 文本文件落地时去掉 CR（仓库在 Windows 上可能是 CRLF），可执行位保留
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-${ALBION_HOME:-/root/albion-pi}}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$DEST/backup/deploy-$STAMP"
DRY="${DRY_RUN:-0}"

KEEP_IF_EXISTS="agent/.env agent/SOUL.md agent/models.json agent/settings.json agent/real-sessions.json agent/auth.json"
keep() { local k; for k in $KEEP_IF_EXISTS; do [ "$1" = "$k" ] && return 0; done; return 1; }
is_text() { case "$1" in *.sh|*.py|*.ts|*.json|*.md|*.example) return 0 ;; *) return 1 ;; esac; }

mkdir -p "$DEST"
n_new=0; n_upd=0; n_same=0; n_keep=0
while IFS= read -r -d '' f; do
  rel="${f#"$SRC"/}"
  keep "$rel" && [ -e "$DEST/$rel" ] && { n_keep=$((n_keep+1)); continue; }
  tmp="$(mktemp)"
  if is_text "$rel"; then tr -d '\r' < "$f" > "$tmp"; else cp "$f" "$tmp"; fi
  if [ -e "$DEST/$rel" ]; then
    if cmp -s "$tmp" "$DEST/$rel"; then n_same=$((n_same+1)); rm -f "$tmp"; continue; fi
    echo "[更新] $rel"; n_upd=$((n_upd+1))
    [ "$DRY" = 1 ] || { mkdir -p "$BACKUP/$(dirname "$rel")"; cp -a "$DEST/$rel" "$BACKUP/$rel"; }
  else
    echo "[新增] $rel"; n_new=$((n_new+1))
  fi
  if [ "$DRY" != 1 ]; then
    mkdir -p "$DEST/$(dirname "$rel")"
    cp "$tmp" "$DEST/$rel"; chmod --reference="$f" "$DEST/$rel" 2>/dev/null || true
  fi
  rm -f "$tmp"
done < <(find "$SRC" -type f \
  ! -path '*/node_modules/*' ! -path '*/__pycache__/*' ! -name '*.bak*' ! -name '*.prev-*' \
  ! -path "$SRC/agent/SYSTEM.md" -print0 | sort -z)

mkdir -p "$DEST/logs" "$DEST/state"
echo "目标 $DEST：新增 $n_new，更新 $n_upd（备份在 $BACKUP），未变 $n_same，保留私密/运行时文件 $n_keep"
[ "$DRY" = 1 ] && { echo "DRY_RUN：没写任何东西"; exit 0; }

[ -e "$DEST/agent/.env" ] || echo "[待办] cp $DEST/agent/.env.example $DEST/agent/.env 并填写（README「没有 Magpie 怎么配」）"
[ -e "$DEST/agent/SOUL.md" ] || echo "[待办] cp $DEST/agent/SOUL.example.md $DEST/agent/SOUL.md 并写你的角色设定"

# 核对：仓库与目标一致。检查器是 Windows 侧的 Python，里面会调 `wsl`，所以只有 WSL 能调回 Windows（interop 开着）时才在这里跑；
# 否则到 Windows 上手动跑：python tools/check_pi_brain_sync.py --prod <目标>
CHECK="$SRC/../../tools/check_pi_brain_sync.py"
if [ "${SKIP_CHECK:-0}" = 1 ]; then
  echo "[跳过] SKIP_CHECK=1"
elif [ ! -f "$CHECK" ]; then
  echo "[提示] 没找到 tools/check_pi_brain_sync.py（不在完整仓库里部署），跳过核对"
elif ! command -v wsl >/dev/null 2>&1 && ! command -v wsl.exe >/dev/null 2>&1; then
  echo "[提示] WSL 里调不到 wsl.exe，核对请在 Windows 上跑：python tools/check_pi_brain_sync.py --prod $DEST"
elif python3 "$CHECK" --prod "$DEST"; then
  echo "[核对] 一致"
else
  echo "[核对] 检查器报差异或取不到数据（上面是明细）"; exit 1
fi
