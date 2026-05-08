#!/bin/bash
# setup.sh — Ubuntu 24.04 伺服器初始化
# 在 server 上 ssh 進去後直接跑這個腳本

set -e

echo "=========================================="
echo "Pharos Server Setup"
echo "=========================================="

# 1. 更新系統
echo ""
echo "→ 更新 apt..."
apt-get update -qq
apt-get upgrade -y -qq

# 2. 裝必要套件
echo ""
echo "→ 安裝 Python 3 + 字型 + 工具..."
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git \
    fonts-noto-cjk fonts-noto-cjk-extra \
    ca-certificates curl

# 3. 建專屬使用者（避免用 root 跑服務）
if ! id -u yt2epub > /dev/null 2>&1; then
    echo ""
    echo "→ 建立 yt2epub 使用者..."
    useradd -m -s /bin/bash yt2epub
    # 把 root 的 SSH key 也複製給 yt2epub 使用者，方便 SSH 進去
    mkdir -p /home/yt2epub/.ssh
    cp /root/.ssh/authorized_keys /home/yt2epub/.ssh/ 2>/dev/null || true
    chown -R yt2epub:yt2epub /home/yt2epub/.ssh
    chmod 700 /home/yt2epub/.ssh
    chmod 600 /home/yt2epub/.ssh/authorized_keys 2>/dev/null || true
fi

# 4. 建專案目錄
echo ""
echo "→ 建立 ~yt2epub/pharos/ 目錄..."
sudo -u yt2epub mkdir -p /home/yt2epub/pharos

# 5. 設定基本防火牆（只允許 SSH）
echo ""
echo "→ 設定防火牆（ufw allow ssh）..."
ufw allow 22/tcp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

# 6. 確認 systemd 可用（Ubuntu 內建）
echo ""
echo "→ systemd 版本：$(systemctl --version | head -1)"

echo ""
echo "=========================================="
echo "✅ 系統準備好了"
echo "=========================================="
echo ""
echo "下一步：從你 Mac 把程式碼 rsync 上來，然後跑 install_services.sh"
