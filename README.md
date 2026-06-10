# Anubis Auto Report

本地 Mac Mini 自動報表工具：讀取 Chrome 裡的 Anubis Bridge 管理端頁面，抓取今日總轉入/轉出與各幣種明細，生成「無公式」PNG 表格與 XLSX，並發送到 Telegram Bot。

## 快速使用

需要 Python 3.10 或以上版本。macOS 安裝腳本會優先使用 Homebrew Python，避免使用系統內置的舊版本。

1. 第一次安裝雙擊 `scripts/setup.command`
2. 開 GUI 雙擊 `scripts/run_gui.command`，程式會啟動本地 Web GUI 並打開 `127.0.0.1:8765` 控制台，狀態時間每秒自動更新
3. 立即跑一次雙擊 `scripts/run_once.command`
4. 常駐服務雙擊 `scripts/run_service.command`，會每 60 秒刷新頁面、監聽 Telegram `/daily`、每日 23:30 自動發報表
5. 背景常駐服務雙擊 `scripts/install_service_launch_agent.command`
6. 背景 Web GUI 控制台雙擊 `scripts/install_web_gui_launch_agent.command`，之後可一直打開 `http://127.0.0.1:8765/`
7. 24/7 前台循環雙擊 `scripts/run_daemon.command`
8. 24/7 背景循環雙擊 `scripts/install_launch_agent.command`
9. 舊版每日香港時間 23:30 單次排程，雙擊 `scripts/install_daily_2330_launch_agent.command`

如要停止背景常駐，雙擊 `scripts/uninstall_launch_agent.command`。
如要停止新常駐服務，雙擊 `scripts/uninstall_service_launch_agent.command`。
如要停止背景 Web GUI 控制台，雙擊 `scripts/uninstall_web_gui_launch_agent.command`。
如要停止每日 23:30 排程，雙擊 `scripts/uninstall_daily_2330_launch_agent.command`。

## Telegram 指令

常駐服務運行時，在授權 Telegram 帳號向 Bot 發送：

```text
/daily
```

程式會立即刷新 Chrome 管理端頁面，讀取最新資料，生成 example 樣式的 PNG 和無公式 XLSX，並發送給主接收和抄送 chat_id。

Telegram Bot API 的 `getUpdates` 同一時間只能由一個程序使用。安裝新常駐服務時，腳本會暫停舊的 `com.anubisbridge.callback-handler`，避免 `/daily` 被 409 Conflict 擋住。

## 設定

設定檔是專案根目錄的 `.env`。`.env.example` 是範本，實際 Token 不建議提交到 Git。

重要欄位：

- `ADMIN_URL`: 管理端流動性頁面
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`: 管理端帳密
- `TELEGRAM_BOT_TOKEN`: Telegram Bot API Token
- `TELEGRAM_CHAT_ID`: 主接收 chat_id
- `TELEGRAM_COPY_CHAT_IDS`: 抄本接收者，可以用逗號分隔；建議填數字 chat_id。Telegram Bot API 不能直接向普通個人 username 發私訊，`@username` 通常只適用於公開頻道/群組。
- `REPORT_INTERVAL_SECONDS`: 循環間隔，預設 3600 秒
- `REFRESH_INTERVAL_SECONDS`: 常駐服務刷新 Chrome 頁面的間隔，預設 60 秒
- `DAILY_REPORT_TIME`: 常駐服務每日自動發送時間，預設 `23:30`
- `OTP_TIMEOUT_SECONDS`: 等待 Google 驗證碼秒數

## 數字格式

報表中的代幣數量按以下規則顯示並寫入 Excel 純數值：

- `DAI`、`USDC`、`USDT`: 不保留小數
- `ETH`、`BNB`: 保留 3 位小數
- 其他代幣: 保留 1 位小數

## Chrome 權限

程式用 JXA/AppleScript 控制 Chrome。首次執行時 macOS 可能彈出自動化權限提示，請允許本程式或 Terminal 控制 Google Chrome。

如果 Chrome 不允許 Apple Events 執行 JavaScript，請在 Chrome 選單啟用：

`View > Developer > Allow JavaScript from Apple Events`

## 登入與 Google 驗證碼

程式檢測到登入頁會自動輸入帳號密碼。當頁面彈出 Google Authenticator 驗證碼欄位時，Bot 會發消息給 `TELEGRAM_CHAT_ID`，直接回覆 6 位數驗證碼即可，程式會自動填入並提交。

## 輸出

每次生成的檔案會放在：

- `outputs/YYYYMMDD/*.png`
- `outputs/YYYYMMDD/*.xlsx`

XLSX 只寫入數值與文字，不包含任何公式。

## 測試

```bash
PYTHONPATH=src python -m unittest
```

## 打包

Mac 版：

```bash
./scripts/build_app.command
```

完成後桌面會出現 `Anubis Auto Report.app` 和 `Anubis Auto Report NOW.app`。

Windows 版必須在 Windows 電腦上打包，因為 PyInstaller 不能在 macOS 直接交叉生成真正的 `.exe`：

```powershell
scripts\build_windows.ps1
```

或雙擊：

```text
scripts\build_windows.bat
```

完成後會生成 `dist\Anubis Auto Report.exe`。請把 `.env` 放在 exe 同一個資料夾。
