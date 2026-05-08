#!/bin/bash
# install_services.sh — 安裝 Pharos Python 套件 + 啟用 systemd 服務
# 在 server 上以 root 身份跑

set -e

PROJECT_DIR="/home/yt2epub/pharos"

echo "=========================================="
echo "Install Pharos deps + systemd services"
echo "=========================================="

# 1. 裝 Python 套件
echo ""
echo "→ pip install -r requirements.txt..."
cd "$PROJECT_DIR"
sudo -u yt2epub pip3 install --break-system-packages -r requirements.txt

# 2. 確保 .env 權限
echo ""
echo "→ 收緊 .env 權限..."
chmod 600 "$PROJECT_DIR/.env"
chown yt2epub:yt2epub "$PROJECT_DIR/.env"

# 3. 複製 systemd unit 檔
echo ""
echo "→ 安裝 systemd unit..."
cp "$PROJECT_DIR/server/pharos-bot.service"    /etc/systemd/system/
cp "$PROJECT_DIR/server/pharos-brief.service"  /etc/systemd/system/
cp "$PROJECT_DIR/server/pharos-brief.timer"    /etc/systemd/system/
systemctl daemon-reload

# 4. 啟用 + 啟動
echo ""
echo "→ 啟用 + 啟動服務..."
systemctl enable --now pharos-bot.service
systemctl enable --now pharos-brief.timer

# 5. 顯示狀態
echo ""
echo "=========================================="
echo "✅ 安裝完成"
echo "=========================================="
echo ""
echo "=== Bot 狀態 ==="
systemctl status pharos-bot.service --no-pager -l | head -15
echo ""
echo "=== Brief Timer 狀態 ==="
systemctl status pharos-brief.timer --no-pager -l | head -10
echo ""
echo "=== 下一次觸發時間 ==="
systemctl list-timers pharos-brief.timer --no-pager
