#!/bin/bash
# 把本目录（integrations/pi-brain）里"仓库管"的那批文件部署到 WSL 里的脑目录，默认 /root/albion-pi。
# 用法：bash deploy.sh [目标目录]      或  ALBION_HOME=/some/dir bash deploy.sh
#   DRY_RUN=1   只列会动什么，不写
#   SKIP_CHECK=1  不跑 tools/check_pi_brain_sync.py
# 范围 = tools/check_pi_brain_sync.py 的 SCOPE（bin/、agent/extensions/、agent/APPEND_SYSTEM.md、accept.sh）
#        + install.sh、deploy.sh 本身；两个模板 agent/.env.example、agent/SOUL.example.md 仅目标里没有时才放。
# README、start.sh、cli.sh、models.json、settings.json、.env、SOUL.md 等生产自管的文件一律不碰。
# 每个会被覆盖的已有文件，先原样备份到 <目标>/backup/deploy-<时间>/<相对路径>；比较时两边都去掉 CR，
# 落地的文本文件是 LF。文件来源：SRC 在 git 仓库里用 git ls-files（不带未入库的垃圾），否则 find。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-${ALBION_HOME:-/root/albion-pi}}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$DEST/backup/deploy-$STAMP"
DRY="${DRY_RUN:-0}"

in_scope() { case "$1" in bin/*|agent/extensions/*|agent/APPEND_SYSTEM.md|accept.sh|install.sh|deploy.sh) return 0 ;; *) return 1 ;; esac; }
is_template() { case "$1" in agent/.env.example|agent/SOUL.example.md) return 0 ;; *) return 1 ;; esac; }
is_text() { case "$1" in *.sh|*.py|*.ts|*.json|*.md|*.example) return 0 ;; *) return 1 ;; esac; }

find_files() {
  ( cd "$SRC" && find . -type f ! -path '*/node_modules/*' ! -path '*/__pycache__/*' ! -name '*.bak*' ! -name '*.prev-*' -print0 | sed -z 's|^\./||' )
}
list_files() {
  # -c safe.directory=* 只用来判断"是不是 git 仓库"，免得 dubious ownership 让整个判断落空
  local root; root="$(git -c safe.directory='*' -C "$SRC" rev-parse --show-toplevel 2>/dev/null)" || root=""
  if [ -z "$root" ]; then find_files; return; fi
  git -C "$SRC" ls-files -z 2>/dev/null || git -c safe.directory="$root" -C "$SRC" ls-files -z 2>/dev/null && return 0
  if [ "${ALLOW_FIND:-0}" = 1 ]; then echo "[警告] git ls-files 失败，ALLOW_FIND=1：改用 find（会带上未入库文件）" >&2; find_files; return; fi
  echo "git ls-files 失败（仓库 $root）。可先 git config --global --add safe.directory '$root'，或设 ALLOW_FIND=1 改用 find。" >&2
  return 1
}

LIST="$(mktemp)"; trap 'rm -f "$LIST"' EXIT
list_files > "$LIST" || exit 1
mkdir -p "$DEST"
n_new=0; n_upd=0; n_same=0; n_keep=0
while IFS= read -r -d '' rel; do
  f="$SRC/$rel"
  [ -f "$f" ] || continue
  if is_template "$rel"; then
    [ -e "$DEST/$rel" ] && { n_keep=$((n_keep+1)); continue; }
  elif ! in_scope "$rel"; then
    continue
  fi
  tmp="$(mktemp)"
  if is_text "$rel"; then tr -d '' < "$f" > "$tmp"; else cp "$f" "$tmp"; fi
  if [ -e "$DEST/$rel" ]; then
    if tr -d '' < "$DEST/$rel" | cmp -s "$tmp" -; then n_same=$((n_same+1)); rm -f "$tmp"; continue; fi
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
done < "$LIST"

mkdir -p "$DEST/logs" "$DEST/state"
echo "目标 $DEST：新增 $n_new，更新 $n_upd（备份在 $BACKUP），未变 $n_same，已有的模板 $n_keep"
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
