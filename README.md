# Live Capture Tool

把 Apple Silicon Mac 當成即時會議逐字稿伺服器。其他電腦可從瀏覽器擷取：

- 麥克風：遠端電腦使用者的聲音
- 分頁／螢幕音訊：遠端電腦正在播放的會議聲音

瀏覽器把 16 kHz PCM 音訊即時傳到 Mac；Whisper 辨識只在 Mac 本機執行。音訊預設不保存，逐字稿會串流回網頁，並在 Mac 寫入 Markdown 與 JSONL。

一般會在一句話結束後約數秒顯示結果；連續發言最長每 15 秒切成一段送去辨識。

## 系統需求

- Apple Silicon Mac
- `uv`
- 第一次啟用模型需連線下載約 1.61 GB 的模型
- 遠端電腦建議使用最新版 Chrome 或 Edge
- 遠端錄音需使用 HTTPS 網頁；瀏覽器不允許一般 HTTP 網頁使用麥克風

## 安裝與第一次準備

```bash
uv sync
uv run live-transcript prepare
```

`prepare` 會預先下載、驗證並卸載預設的 `whisper-large-v3-turbo` 模型。
不先執行也可以；第一次從網頁按「啟用模型」時會自動下載。

## 開始與停止

```bash
uv run live-transcript
```

伺服器預設只監聽 [http://localhost:8083](http://localhost:8083)。遠端錄音請在具備 HTTPS 的網頁環境開啟服務：

1. 按「啟用模型」，等狀態列顯示 `whisper-large-v3-turbo 已載入記憶體`。
2. 選擇麥克風及／或分頁／螢幕音訊。
3. 按「開始遠端錄音」並允許瀏覽器權限。

選擇「分頁／螢幕音訊」時，請在瀏覽器分享選擇器中勾選「分享分頁音訊」。若所選分頁沒有提供音訊軌，但麥克風可用，服務會繼續錄製麥克風，不會中止整場錄音；若只選分頁音訊，頁面會顯示明確錯誤且不會建立空白錄音 session。

逐字稿會在每句辨識完成後自動出現在頁面。停止錄音後，服務會先完成
排隊中的辨識並保存檔案，接著卸載模型並清除 MLX Metal 快取；常駐服務本身
仍維持低記憶體待機。下次錄音前再按一次「啟用模型」即可。

手動按「卸載模型」時，記憶體釋放會在背景執行，網頁會立即收到 JSON 狀態，避免反向代理等待逾時後回傳 HTML 錯誤頁。

每次按「開始遠端錄音」都會建立獨立的逐字稿 session。名稱留空時會使用 `transcript-日期_時間`；錄音中或停止後都可在頁面改名。按「重置為自動名稱」會讓下一場錄音重新使用新的日期時間檔名，不會改動目前已保存的檔案。按「停止錄音」後會完成剩餘辨識並顯示 Markdown 與 JSONL 下載按鈕。Mac 上也會建立對應目錄：

```text
transcripts/2026-07-17_15-30-00/
├── transcript-2026-07-17_15-30-00.md
└── transcript-2026-07-17_15-30-00.jsonl
```

在 Mac 終端機按 `Ctrl+C` 可停止整個伺服器。

頁面的「清除畫面逐字稿」只清除瀏覽器及伺服器的即時顯示歷史，不會刪除
Markdown 或 JSONL。錄音結束、檔案已保存時可直接清除；錄音或保存尚未完成時，
頁面會先要求確認。

## 語言

預設 `--language auto` 會對每個語音片段自動偵測語言，支援中文、英文，以及同一場會議中中英交替。極短片語或一句內高度混合兩種語言時，語言判斷與標點可能較不穩定。

使用遠端錄音網頁時，也可以在「辨識語言」選單選擇中文、英文或 Auto；選擇會套用到下一次錄音，錄音進行中不能變更。

若整場會議確定只有一種語言，可使用 `--language zh` 或 `--language en` 固定語言。

## 擷取 Mac 本機音訊

若要使用原本的模式，直接擷取 Mac 本身的系統音訊與麥克風：

```bash
uv run live-transcript prepare --input local
uv run live-transcript --input local
```

此模式需要 macOS 15、Xcode Command Line Tools，以及麥克風與螢幕／系統音訊錄製權限。不需要 BlackHole 或 Loopback。若也要保留兩條原始音軌：

```bash
uv run live-transcript --input local --save-audio
```

會額外產生 `system.wav` 與 `microphone.wav`。

## 常用選項

```bash
# 明確指定中文或英文；預設 auto 適合中英混合會議
uv run live-transcript --language zh
uv run live-transcript --language en

# 預設即為 large-v3-turbo；需要時仍可指定其他 MLX Whisper 模型
uv run live-transcript --model mlx-community/whisper-small-mlx-4bit

# 指定輸出目錄
uv run live-transcript --output-dir ~/Documents/meeting-transcripts

# 改用其他連接埠
uv run live-transcript --port 9000

# 僅供觀看時，可監聽指定的 Tailscale IP
uv run live-transcript --host 100.x.y.z
```

## 權限疑難排解

若遠端網頁不能開始錄音，先確認網址是 HTTPS，並在瀏覽器網站權限中允許麥克風。分享會議音訊時，請在瀏覽器的分享選擇器中選擇分頁或畫面，並勾選「分享音訊」。

在 `--input local` 模式若擷取器顯示權限錯誤，開啟「系統設定 → 隱私權與安全性」，確認終端機已取得「麥克風」及「螢幕與系統音訊錄製」權限；變更後請完全關閉終端機再重新開啟。
