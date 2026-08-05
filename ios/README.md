# Live Transcript for iOS 27

這是一個獨立的 SwiftUI client。它不在 iPhone 上執行 Whisper，而是把 16 kHz、
16-bit、mono PCM 即時傳給既有的 Mac `live-transcript` 服務。因此電腦網頁版、
Python 啟動方式與逐字稿保存格式都不需要改變。

## 目前功能

- 設定並記住 Mac 伺服器 URL
- 健康檢查與 SSE 即時狀態
- 載入／卸載 Mac 上的 Whisper 模型
- 中文、英文與 Auto 語言選擇
- 自訂逐字稿名稱
- iOS 27 ScreenCaptureKit 系統音訊擷取
- 在系統分享選擇器中選用麥克風
- 僅麥克風錄音模式
- 即時逐字稿、完成中狀態、Markdown 與 JSONL 下載
- 錄音停止後由 Mac 完成剩餘辨識並自動釋放模型

## 系統需求

- Xcode 27 或更新版本
- iOS 27 SDK
- iOS 27 真機；系統音訊不能只靠 Simulator 完整驗證
- 正在運行的 Mac `live-transcript` 服務
- 從 iPhone 可連線的 HTTPS/WSS 服務網址，或開發階段可使用的區域網路網址

Apple 目前仍把 iOS ScreenCaptureKit 標示為 beta。每次更新 Xcode 27 beta 後，
都應重新編譯並在真機驗證 picker、背景執行及音訊輸出 API。

## 建置

1. 安裝完整 Xcode 27，並將 Command Line Tools 指向該版本。
2. 開啟 `ios/LiveTranscript.xcodeproj`。
3. 在 `Signing & Capabilities` 選擇自己的 Personal Team 或開發團隊。
4. 必要時把 `dev.livetranscript.app` 改成自己擁有的 bundle identifier。
5. 選擇 iOS 27 真機後執行 `LiveTranscript` scheme。

也可以在安裝 Xcode 27 後使用命令列做不簽章的 Simulator build：

```bash
xcodebuild \
  -project ios/LiveTranscript.xcodeproj \
  -scheme LiveTranscript \
  -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO \
  build
```

共用傳輸協定是獨立 Swift Package，可執行：

```bash
cd ios
swift test
```

## 使用流程

1. 在 Mac 啟動 `uv run live-transcript`。
2. 在 App 輸入可由 iPhone 連線的伺服器 URL，按「連線」。
3. 按「啟用模型」，等待狀態變成已載入。
4. 選擇「系統音訊」或「僅麥克風」。
5. 系統音訊模式會顯示 iOS 分享選擇器；需要記錄自己的聲音時，同時開啟
   選擇器內的麥克風控制。
6. 停止後等待 Mac 完成剩餘辨識，接著下載 Markdown 或 JSONL。

## 傳輸協定

- `GET /health`：連線檢查
- `GET /events`：SSE 模型、服務、session 與逐字稿事件
- `POST /model/load`、`POST /model/unload`：模型生命週期
- `POST /transcript/clear`：清除即時顯示歷史
- `WS /audio?name=...&language=...`：音訊傳輸

每個 WebSocket binary message 的第一個 byte 是音源：

- `0`：系統／會議另一端
- `1`：麥克風／使用者

後續 bytes 是 little-endian signed Int16 PCM，取樣率 16 kHz、mono。

## 真機驗收清單

由於 ScreenCaptureKit 與其他 App 的音訊政策只能在真機確認，第一輪至少測試：

- Safari、Google Meet、Zoom、Microsoft Teams
- iPhone 喇叭、有線耳機、AirPods
- App 切到背景後持續 30 分鐘
- 鎖定畫面、Wi-Fi 短暫切換、耳機連線切換
- 來電或 Siri 中斷後能停止並保存 session
- 停止錄音後剩餘逐字稿完成、下載按鈕出現、模型記憶體釋放

目前後端一次只允許一個 active recording client；電腦網頁和 iPhone App 可以
交替使用，但不能同時各自開始一場錄音。
