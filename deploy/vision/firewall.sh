#!/usr/bin/env bash
# 只让局域网和 tailnet 访问视觉模型的端口（默认 8080）。
#
# Docker 每次启动会重建 DOCKER-USER 链，写在里面的规则会消失，所以这里跟生产一样：
# 把脚本装到 /usr/local/bin/docker-user-rules.sh，用 systemd unit（PartOf=docker.service）
# 在 docker 起来时重跑一遍。
#
# 用法：
#   sudo bash firewall.sh status     # 看现在的 DOCKER-USER
#   sudo bash firewall.sh apply      # 按 .env 重写规则（立即生效，不装服务）
#   sudo bash firewall.sh install    # 装到 /usr/local/bin + systemd，开机和 docker 重启时自动重跑
#   sudo bash firewall.sh remove     # 停掉服务、删 unit 和脚本（DOCKER-USER 留空）
#
# 参数在 .env：VLM_PORT、ALLOWED_SOURCES（空格分隔的网段）、VLM_IPV6。
# 注意：apply/install 会清空整条 DOCKER-USER，再按这里的规则重建；这台机器上如果还有
# 别的容器要自定义放行，把它的规则一起写进 ALLOWED_SOURCES 或改这个脚本。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME=docker-user-rules.service
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
TARGET_BIN=/usr/local/bin/docker-user-rules.sh

# install 时脚本被拷到 /usr/local/bin，参数得从原地读：找同目录的 .env，找不到用环境变量。
for candidate in "$HERE/.env" "${DOCKER_USER_RULES_ENV:-}"; do
  if [ -n "$candidate" ] && [ -f "$candidate" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$candidate"
    set +a
    break
  fi
done

PORT="${VLM_PORT:-8080}"
ALLOWED_SOURCES="${ALLOWED_SOURCES:-192.168.0.0/16 10.0.0.0/8 100.64.0.0/10}"
VLM_IPV6="${VLM_IPV6:-1}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "要用 root 跑：sudo bash $0 $*" >&2
    exit 1
  fi
}

apply_family() {
  local cmd="$1" family="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[skip] 没有 $cmd" >&2
    return 0
  fi
  if ! "$cmd" -n -L DOCKER-USER >/dev/null 2>&1; then
    echo "[skip] $family 的 DOCKER-USER 链不存在（docker 没起或没开 $family）" >&2
    return 0
  fi
  "$cmd" -F DOCKER-USER
  "$cmd" -A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  local src
  for src in $ALLOWED_SOURCES; do
    "$cmd" -A DOCKER-USER -s "$src" -p tcp --dport "$PORT" -j RETURN
  done
  "$cmd" -A DOCKER-USER -p tcp --dport "$PORT" -j DROP
  "$cmd" -A DOCKER-USER -j RETURN
  echo "[ok  ] $family DOCKER-USER 已重写：$PORT 只放行 $ALLOWED_SOURCES"
}

apply_all() {
  apply_family iptables IPv4
  if [ "$VLM_IPV6" = "1" ]; then
    apply_family ip6tables IPv6
  fi
}

show_status() {
  for cmd in iptables ip6tables; do
    command -v "$cmd" >/dev/null 2>&1 || continue
    echo "== $cmd -S DOCKER-USER"
    "$cmd" -S DOCKER-USER 2>/dev/null || echo "(没有 DOCKER-USER 链)"
  done
}

do_install() {
  need_root install
  apply_all
  if [ "$(readlink -f "$0")" != "$(readlink -f "$TARGET_BIN" 2>/dev/null || echo)" ]; then
    install -m 0755 "$0" "$TARGET_BIN"
  fi
  cat >"$UNIT_PATH" <<EOF
[Unit]
Description=Rewrite Docker DOCKER-USER rules for the vision model port
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=DOCKER_USER_RULES_ENV=$HERE/.env
ExecStart=$TARGET_BIN apply

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$UNIT_NAME"
  echo "[ok  ] $UNIT_NAME 已启用；docker 重启后会自动重写 DOCKER-USER"
  echo "       改端口或网段：编辑 $HERE/.env，再 systemctl restart $UNIT_NAME"
}

do_remove() {
  need_root remove
  systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_PATH"
  systemctl daemon-reload
  rm -f "$TARGET_BIN"
  for cmd in iptables ip6tables; do
    command -v "$cmd" >/dev/null 2>&1 || continue
    "$cmd" -F DOCKER-USER 2>/dev/null || true
  done
  echo "[ok  ] 已移除 $UNIT_NAME 和 $TARGET_BIN"
}

case "${1:-apply}" in
  status) show_status ;;
  apply) need_root apply; apply_all ;;
  install) do_install ;;
  remove) do_remove ;;
  *)
    echo "用法：sudo bash $0 [status|apply|install|remove]" >&2
    exit 2
    ;;
esac
