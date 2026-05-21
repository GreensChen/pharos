# Pharos

> Φάρος — lighthouse. The beacon that ferries YouTube content into your Kobo for slow reading.

把 YouTube 影片變成繁體中文 epub，自動推送到 Kobo。

姊妹專案：[Anamnesis](https://github.com/GreensChen/anamnesis) — 把 Kobo 重點 / 截圖 / 文章
/ 影片摘要彙整到 Obsidian，並提供 vault-aware Q&A。

Pharos 跟 Anamnesis 部署在同一台 server 時，**user 按「🎧 轉成 epub」轉檔的訊號會自動回寫到 Anamnesis 的興趣模型**，讓 Anamnesis 的 Weekly Paper 系統知道你最近真的在看哪些主題的影片，進而調整推薦給你的 paper。

跨 repo 整合是 opt-in 的——Anamnesis 沒部署也不影響 Pharos 運作。

## 用途

如果你有以下習慣：
- 訂閱很多 podcast 頻道（Acquired、Lex Fridman、Invest Like the Best...）
- 想看深度訪談但**寧願用閱讀的方式**而不是聽 1-2 小時
- 用 Kobo 等電子書閱讀器當主要閱讀工具

這個系統會幫你：
1. 每天自動偵測訂閱頻道有沒有新影片
2. 把英文逐字稿翻譯成自然的**繁體中文**
3. 整理成有章節的 **epub** 檔
4. 自動同步到 Kobo（透過 Dropbox）
5. **手機 Telegram 點按鈕就能轉檔**，不用開電腦

## Demo

```
📡 Daily Brief — 2026-04-26
今天有 3 部新影片

📺 Acquired
📰 NVIDIA CEO Jensen Huang
⭐⭐⭐ Acquired 三巨頭訪 NVIDIA CEO，深度討論 GPU + AI 浪潮
#NVIDIA #AI #半導體

Acquired 的 Ben & David 訪 Jensen 三小時，從 NVIDIA 早期作為
遊戲卡公司、押注 CUDA 被華爾街罵 5 年、AI 浪潮意外讓 NVIDIA
變 3 兆市值龍頭。Jensen 詳述他的「光速管理」哲學、為什麼公司
從不做正式預算規劃，以及他對 AI 推論需求即將爆發的判斷。

[ 🎧 轉成 epub → Kobo ]
```

點「🎧 轉成 epub」按鈕後 1-3 分鐘，Kobo 上就有翻譯好的繁中電子書可看。

## 架構

```
                    YouTube RSS
                         │
                每天 cron（15:00）
                         │
                         ▼
                    daily_brief.py
                         │
            ┌────────────┴────────────┐
            │                         │
         Gemini /                Telegram bot
         Claude API           （推送摘要 + 按鈕）
            │                         │
            ▼                         ▼
       narrative_blocks         手機點按鈕觸發
                                      │
                                      ▼
                                 yt2epub.py
                                  （翻譯）
                                      │
                                      ▼
                              Dropbox API 上傳
                                      │
                          ┌───────────┴───────────┐
                          │                       │
                          ▼                       ▼
                    ☁️ Dropbox 雲端       Anamnesis 興趣模型
                          │              （record_pharos_signal）
                          ▼                       │
                     Kobo 同步下載                 ▼
                                        Weekly Paper 推薦加權
```

## 功能

- ✅ **YouTube RSS 訂閱**（不用 YouTube API key）
- ✅ **段落式摘要 + relevance 評分**（high/medium/low/off-topic）
- ✅ **自動過濾**：沒字幕、低相關性的影片不推
- ✅ **Gemini / Claude 雙引擎**（環境變數切換）
- ✅ **Telegram 全功能管理**：訂閱清單、即時摘要、轉檔、查紀錄
- ✅ **Anamnesis 興趣訊號整合**：轉檔行為自動回饋到 Weekly Paper 推薦系統
- ✅ **直接傳 YouTube URL 給 bot 立刻摘要**
- ✅ **epub 自動章節分段 + 對談人辨識**
- ✅ **Pillow 自製文字書封**（無 YouTube 縮圖版權問題）
- ✅ **YouTube 自動字幕雜訊清理**（`[音樂]` `[笑聲]` `>>` 等）
- ✅ **Mac / Linux 雙環境**：Mac 上用 Dropbox 桌面版資料夾，伺服器上用 Dropbox API

## 成本

每月約 **NT$200-300**（依使用量）：

| 項目 | 用量 | 費用 |
|------|------|------|
| Hetzner CX22（VPS）| 24/7 運作 | €4.5（~$5）|
| Gemini 3 Flash（摘要 + 翻譯）| 每月 ~150 部影片 | $3-5 |
| Dropbox API | 每月 ~200 次上傳 | 免費 |
| Telegram Bot | 不限 | 免費 |

## Quick Start

### 0. 你需要

- Python 3.10+
- Dropbox 帳號（免費版即可）
- Telegram 帳號
- Gemini API key（[免費取得](https://aistudio.google.com/app/apikey)）或 Anthropic API key
- Kobo 閱讀器（綁定 Dropbox 同步）

### 1. clone + 安裝套件

```bash
git clone https://github.com/GreensChen/yt2epub.git
cd yt2epub
pip install -r requirements.txt
```

### 2. 設定 `.env`

```bash
cp .env.example .env
# 編輯 .env 填入各 API key
```

最少要設定的 keys：
- `GEMINI_API_KEY` 或 `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- 用伺服器版才需要 `DROPBOX_APP_KEY` + `DROPBOX_APP_SECRET` + `DROPBOX_REFRESH_TOKEN`

### 3. 設定訂閱清單

三種方式擇一：

**a. 從範例檔開始**
```bash
cp channels.example.json channels.json
# 編輯 channels.json 加入你想追的頻道
```

**b. CLI 工具**
```bash
python3 subscribe.py add "https://youtube.com/@AcquiredFM"
python3 subscribe.py add "https://youtube.com/playlist?list=..."
python3 subscribe.py list
```

**c. 部署完成後直接用 Telegram bot**
```
/sub https://youtube.com/@AcquiredFM
/channels
/unsub Acquired
```

### 4. 第一次跑（標記既有影片為已看，避免一口氣處理 100 部）

```bash
python3 daily_brief.py --init
```

### 5. 啟動 bot

**Mac**（用 launchd，範例 plist 在 `examples/`）：

```bash
cp examples/com.user.yt2epub-bot.plist ~/Library/LaunchAgents/
# 編輯路徑指向你的安裝位置
launchctl load ~/Library/LaunchAgents/com.user.yt2epub-bot.plist
```

**Linux 伺服器**（用 systemd，見下節）。

## 伺服器部署（Hetzner / Vultr / DigitalOcean / 任何 Ubuntu VPS）

### 1. 開機器

任何提供 Ubuntu 24.04 的 VPS 都行。**Hetzner CX22 €4.5/月** CP 值最高。

### 2. SSH 進去跑 setup

```bash
# 從你本地：
ssh root@your-server-ip 'bash -s' < server/setup.sh
```

這會：裝 Python + Noto CJK 字型 + 建 `yt2epub` 使用者 + 設防火牆。

### 3. 部署程式碼

```bash
export SERVER_IP=your-server-ip
bash server/deploy.sh
```

### 4. 上傳 .env

```bash
scp .env root@your-server-ip:/home/yt2epub/yt2epub/.env
```

### 5. 啟動服務

```bash
ssh root@your-server-ip 'bash /home/yt2epub/yt2epub/server/install_services.sh'
```

完成後會自動啟動 `yt2epub-bot.service` 24/7 + `yt2epub-brief.timer` 每天 07:00 UTC（= 15:00 Asia/Taipei）。

## Dropbox API 設定

### 1. 建 App

1. 去 [Dropbox App Console](https://www.dropbox.com/developers/apps)
2. **Create app** → **Scoped access** → **Full Dropbox**
3. Name 隨意（如 `yt2epub-username`）

### 2. 開權限

Permissions 分頁，勾：
- ✅ `files.metadata.write`
- ✅ `files.content.write`
- ✅ `files.content.read`

按 Submit。

### 3. 拿 Refresh Token

把 App key + App secret 寫入 `.env`：

```
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
```

跑：

```bash
python3 dropbox_auth.py
```

跟著腳本指示授權一次，refresh token 會自動寫入 `.env`。

## Telegram bot 指令

部署完成後，跟你的 bot 對話可用：

| 指令 | 說明 |
|------|------|
| 直接傳 YouTube URL | 立即生成摘要 + 🎧 轉 epub 按鈕 |
| `/start` | 顯示說明 |
| `/run` | 立刻跑一次 daily_brief（不等 15:00）|
| `/list` | 列出最近處理過的影片 |
| `/channels` | 列出目前訂閱的頻道 / playlist |
| `/sub <URL>` | 新增訂閱（@頻道 或 playlist） |
| `/unsub <名稱或 id>` | 移除訂閱 |

新增訂閱會自動把該頻道**現有的影片標記為已看**，下次 daily_brief 才不會一次處理 100 部歷史影片。

## Telegram Bot 設定

1. 找 [@BotFather](https://t.me/BotFather) → `/newbot` → 取得 token
2. 跟你的 bot 隨便傳一句話（例如 `hi`）
3. 用以下 Python 拿你的 chat_id：

```python
import urllib.request, json
TOKEN = "你的 bot token"
data = urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/getUpdates").read()
print(json.loads(data)["result"][-1]["message"]["chat"]["id"])
```

## LLM 切換

預設用 Gemini 3 Flash。要切 Claude，改 `.env`：

```
LLM_PROVIDER=claude
```

或反過來。**`yt2epub.py` 跟 `daily_brief.py` 都會吃這個變數。**

模型成本（每集 90 分鐘 podcast 為例）：

| 模型 | 摘要成本 | 翻譯成本 | 速度 |
|------|---------|---------|------|
| Gemini 3 Flash | ~$0.005 | ~$0.05 | 快（~2 分鐘）|
| Claude Sonnet 4.6 | ~$0.03 | ~$0.20 | 慢（~3 分鐘）|
| Claude Haiku 4.5 | ~$0.01 | ~$0.05 | 中 |

## 檔案結構

```
yt2epub/
├── yt2epub.py            # 單一影片 → epub 主程式
├── daily_brief.py        # 每日摘要推送
├── bot_service.py        # Telegram bot 常駐服務
├── subscribe.py          # 訂閱管理 CLI
├── dropbox_auth.py       # Dropbox OAuth 一次性授權
├── dropbox_uploader.py   # Dropbox API 上傳模組
├── channels.json         # （你的）訂閱清單，gitignored
├── seen.json             # 已看過影片 ID，gitignored
├── summaries/            # 每部影片摘要快取，gitignored
├── server/               # 伺服器部署腳本
│   ├── setup.sh
│   ├── install_services.sh
│   ├── deploy.sh
│   └── *.service / *.timer
└── .env                  # API keys，gitignored
```

## 路線圖

歡迎 PR：

- [ ] `channels.json` schema 加入頻道分類（科技/財經/...），讓 daily_brief 對不同分類用不同 prompt
- [ ] Web UI 取代 Telegram（用 Gradio 或類似）
- [ ] 支援 Spotify / Apple Podcast 來源（不只 YouTube）
- [ ] 摘要的 narrative quote timestamp 在 epub 內變成可點擊跳到該時間戳的 YouTube 連結
- [ ] 整合 Reader（Readwise）取代 Kobo

## License

MIT — 拿去隨便用、改、商用都可以。詳見 [LICENSE](LICENSE)。
