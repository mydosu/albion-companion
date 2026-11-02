#!/usr/bin/env bash
# 下载视觉模型（Qwen3-VL 4B GGUF + mmproj）到 MODEL_DIR。
# 只下不装：下完自己核对文件大小，再用 docker compose up -d。
# 用法：bash download_models.sh [--force]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

HF_REPO="${HF_REPO:-Qwen/Qwen3-VL-4B-Instruct-GGUF}"
HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
HF_ENDPOINT="${HF_ENDPOINT%/}"
MODEL_DIR="${MODEL_DIR:-./models}"
MODEL_FILE="${MODEL_FILE:-Qwen3VL-4B-Instruct-Q4_K_M.gguf}"
MMPROJ_FILE="${MMPROJ_FILE:-mmproj-Qwen3VL-4B-Instruct-F16.gguf}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

mkdir -p "$MODEL_DIR"

fetch() {
  local file="$1" dest="$2" url
  url="$HF_ENDPOINT/$HF_REPO/resolve/main/$file?download=true"
  if [ -s "$dest" ] && [ "$FORCE" -eq 0 ]; then
    echo "[skip] $dest 已存在（--force 可重下）"
    return 0
  fi
  echo "[get ] $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 5 -C - -o "$dest" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -c -O "$dest" "$url"
  else
    echo "没有 curl 也没有 wget，先装一个：sudo apt-get install -y curl" >&2
    return 1
  fi
}

# 主模型和 mmproj 都必须是真 GGUF：下歪了（HTML 错误页、半截文件）这里就报出来，
# 不要留给 llama-server 在启动时报一句看不懂的错。
check_gguf() {
  local dest="$1"
  if [ ! -s "$dest" ]; then
    echo "[FAIL] $dest 是空的" >&2
    return 1
  fi
  if [ "$(head -c 4 "$dest")" != "GGUF" ]; then
    echo "[FAIL] $dest 不是 GGUF（文件头不对，多半下到了错误页）" >&2
    return 1
  fi
  ls -la "$dest"
}

fetch "$MODEL_FILE" "$MODEL_DIR/$MODEL_FILE"
fetch "$MMPROJ_FILE" "$MODEL_DIR/$MMPROJ_FILE"
check_gguf "$MODEL_DIR/$MODEL_FILE"
check_gguf "$MODEL_DIR/$MMPROJ_FILE"

cat <<EOF

下完了。下一步：
  docker compose up -d
  curl -s http://127.0.0.1:\${VLM_PORT:-8080}/health
EOF
