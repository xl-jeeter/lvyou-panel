#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/lvyou-panel"
DATA_DIR="/var/lib/lvyou-panel"
SERVICE_NAME="lvyou-panel"
DEFAULT_PORT=34567

usage() {
  cat <<EOF
LvYou Panel - 绿微设备群控管理系统

Usage: sudo ./install.sh <command>

Commands:
  install            Install (Docker, recommended)
  install-native     Install native (Python venv + systemd)
  status             Show service status
  restart            Restart service
  logs [n]           Show service logs (default 50 lines)
  backup             Backup data and config
  restore [file]     Restore from backup
  set-port [port]    Change service port ($DEFAULT_PORT)
  uninstall          Uninstall
  help               Show this help

EOF
}

need_root() { [ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }; }

install_docker() {
  need_root
  echo "=== LvYou Panel Docker Install ==="

  if ! command -v docker &>/dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  fi

  mkdir -p "$DATA_DIR"
  cp "$ROOT_DIR/docker-compose.yml" "$APP_DIR/docker-compose.yml" 2>/dev/null || true
  cp "$ROOT_DIR/Dockerfile" "$APP_DIR/Dockerfile" 2>/dev/null || true
  cp -r "$ROOT_DIR/app" "$APP_DIR/" 2>/dev/null || true

  cd "$APP_DIR"
  docker compose up -d --build 2>/dev/null || docker-compose up -d --build

  echo ""
  echo "✅ LvYou Panel installed!"
  echo "   http://$(hostname -I | awk '{print $1}'):$DEFAULT_PORT"
}

install_native() {
  need_root
  echo "=== LvYou Panel Native Install ==="

  apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv 2>/dev/null || true

  mkdir -p "$APP_DIR" "$DATA_DIR"
  cp -r "$ROOT_DIR/app" "$APP_DIR/"
  cp "$ROOT_DIR/requirements.txt" "$APP_DIR/"

  python3 -m venv "$APP_DIR/venv"
  "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

  cat > /etc/systemd/system/$SERVICE_NAME.service << SERVICE
[Unit]
Description=LvYou Panel - Device Management
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=DATA_DIR=$DATA_DIR
ExecStart=$APP_DIR/venv/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port $DEFAULT_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

  systemctl daemon-reload
  systemctl enable --now $SERVICE_NAME

  echo ""
  echo "✅ LvYou Panel installed!"
  echo "   http://$(hostname -I | awk '{print $1}'):$DEFAULT_PORT"
  echo ""
  echo "   systemctl status $SERVICE_NAME"
}

cmd_status() {
  if systemctl is-active --quiet $SERVICE_NAME 2>/dev/null; then
    echo "✅ $SERVICE_NAME: running"
    systemctl status $SERVICE_NAME --no-pager -l | head -15
  elif docker ps 2>/dev/null | grep -q lvyou-panel; then
    echo "✅ Docker: running"
    docker ps | grep lvyou-panel
  else
    echo "❌ Service not running"
  fi
}

cmd_restart() {
  systemctl restart $SERVICE_NAME 2>/dev/null || docker compose -f "$APP_DIR/docker-compose.yml" restart 2>/dev/null || true
  echo "✅ Restarted"
}

cmd_logs() {
  local n="${1:-50}"
  journalctl -u $SERVICE_NAME -n "$n" --no-pager 2>/dev/null || docker logs --tail "$n" lvyou-panel 2>/dev/null
}

cmd_backup() {
  local f="/tmp/lvyou-panel-backup-$(date +%Y%m%d_%H%M%S).tar.gz"
  tar czf "$f" -C / "$DATA_DIR" "$APP_DIR/app" 2>/dev/null
  echo "✅ Backup: $f"
}

cmd_restore() {
  local f="$1"
  [ -f "$f" ] || { echo "File not found: $f"; exit 1; }
  tar xzf "$f" -C /
  systemctl restart $SERVICE_NAME 2>/dev/null || true
  echo "✅ Restored from $f"
}

cmd_uninstall() {
  need_root
  systemctl stop $SERVICE_NAME 2>/dev/null || true
  systemctl disable $SERVICE_NAME 2>/dev/null || true
  rm -f /etc/systemd/system/$SERVICE_NAME.service
  docker compose -f "$APP_DIR/docker-compose.yml" down 2>/dev/null || true
  echo "✅ Uninstalled. Data preserved at $DATA_DIR"
}

CMD="${1:-help}"; shift || true
case "$CMD" in
  install) install_docker "$@" ;;
  install-native) install_native "$@" ;;
  status) cmd_status ;;
  restart) cmd_restart ;;
  logs) cmd_logs "$@" ;;
  backup) cmd_backup "$@" ;;
  restore) cmd_restore "$@" ;;
  uninstall) cmd_uninstall ;;
  help|*) usage ;;
esac
