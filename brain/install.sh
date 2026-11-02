#!/bin/bash
# 在 WSL(Ubuntu) 里装脑的运行环境：Node + Pi。幂等，装过且版本对就什么都不做。
# 用法：sudo bash install.sh          （root 或有 sudo 的用户）
# 版本钉的是作者生产机 2026-11-01 的实际版本；要升级改下面两行，别用 latest。
set -euo pipefail

NODE_VERSION="${NODE_VERSION:-26.9.0}"
PI_PACKAGE="@earendil-works/pi-coding-agent"
PI_VERSION="${PI_VERSION:-1.0.4}"
NODE_PREFIX="${NODE_PREFIX:-/usr/local}"

[ "$(id -u)" = 0 ] || { echo "要 root 权限（写 $NODE_PREFIX）：sudo bash $0"; exit 1; }
for c in curl tar xz python3; do
  command -v "$c" >/dev/null 2>&1 || { echo "缺 $c：apt-get install -y curl xz-utils python3"; exit 1; }
done

case "$(uname -m)" in
  x86_64) ARCH=x64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "不支持的架构 $(uname -m)"; exit 1 ;;
esac

export PATH="$NODE_PREFIX/bin:$PATH"
if [ "$(node -v 2>/dev/null || true)" = "v$NODE_VERSION" ]; then
  echo "[skip] node v$NODE_VERSION 已在"
else
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  URL="https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$ARCH.tar.xz"
  echo "[node] 下载 $URL"
  curl -fL --retry 3 -o "$TMP/node.tar.xz" "$URL"
  tar -xJf "$TMP/node.tar.xz" -C "$TMP"
  # 只拷 bin/lib/include/share，不带上游的 LICENSE/README，免得盖掉 $NODE_PREFIX 里别的东西
  for d in bin lib include share; do
    mkdir -p "$NODE_PREFIX/$d"
    cp -a "$TMP/node-v$NODE_VERSION-linux-$ARCH/$d/." "$NODE_PREFIX/$d/"
  done
  echo "[node] 装好：$(node -v)"
fi

HAVE="$(npm ls -g --depth=0 "$PI_PACKAGE" 2>/dev/null | grep -o "$PI_PACKAGE@[^ ]*" | head -1 || true)"
if [ "$HAVE" = "$PI_PACKAGE@$PI_VERSION" ]; then
  echo "[skip] $HAVE 已在"
else
  echo "[pi] npm i -g $PI_PACKAGE@$PI_VERSION"
  npm install -g "$PI_PACKAGE@$PI_VERSION"
fi

command -v pi >/dev/null || { echo "pi 不在 PATH 里：检查 $NODE_PREFIX/bin"; exit 1; }
echo "OK node $(node -v)  pi $(npm ls -g --depth=0 "$PI_PACKAGE" | grep -o "$PI_PACKAGE@[^ ]*" | head -1)"
