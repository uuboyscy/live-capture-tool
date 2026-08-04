from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import socket
import struct
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, quote, urlsplit

from .audio import AudioSource
from .transcription import TranscriptEvent, format_timestamp


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WEBSOCKET_PAYLOAD = 256 * 1024


class AudioReceiver(Protocol):
    def open(
        self,
        client_id: str,
        requested_name: str | None = None,
        requested_language: str | None = None,
    ) -> tuple[bool, str, dict[str, str | None] | None]: ...

    def feed(self, client_id: str, source: AudioSource, pcm: bytes) -> None: ...

    def close(self, client_id: str) -> None: ...

    def rename_session(
        self,
        session_id: str,
        requested_name: str,
    ) -> dict[str, str | None]: ...

    def download(
        self,
        session_id: str,
        file_format: str,
    ) -> tuple[Any, str] | None: ...


class ModelController(Protocol):
    model: str

    @property
    def state(self) -> str: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def start_unload(self) -> bool: ...


PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Live Transcript</title>
  <style>
    :root {
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      color: #f4f1e8;
      background: #11120f;
    }
    * { box-sizing: border-box; }
    body {
      min-height: 100vh;
      margin: 0;
      background:
        radial-gradient(circle at 18% 0%, rgba(103, 141, 96, .18), transparent 35rem),
        #11120f;
    }
    main {
      width: min(900px, calc(100% - 32px));
      margin: 0 auto;
      padding: 48px 0 96px;
    }
    header {
      padding: 16px 0 24px;
    }
    .eyebrow {
      margin: 0 0 8px;
      color: #aab5a5;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .14em;
      text-transform: uppercase;
    }
    .heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    .status-stack {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }
    h1 {
      margin: 0;
      font-family: ui-serif, Georgia, serif;
      font-size: clamp(32px, 6vw, 58px);
      font-weight: 500;
      letter-spacing: -.04em;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex: none;
      border: 1px solid #34372f;
      border-radius: 999px;
      padding: 8px 12px;
      color: #c8cec3;
      background: rgba(26, 28, 24, .86);
      font-size: 13px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #d1a34f;
      box-shadow: 0 0 0 4px rgba(209, 163, 79, .12);
    }
    .status.live .dot {
      background: #8dcc81;
      box-shadow: 0 0 0 4px rgba(141, 204, 129, .12);
      animation: pulse 2s infinite;
    }
    .status.model-ready .dot {
      background: #79b9e7;
      box-shadow: 0 0 0 4px rgba(121, 185, 231, .12);
    }
    .status.busy .dot { animation: pulse 1.2s infinite; }
    .status.ended .dot { background: #8b8e86; box-shadow: none; }
    @keyframes pulse { 50% { opacity: .45; } }
    .controls {
      position: sticky;
      top: 12px;
      z-index: 10;
      display: grid;
      gap: 18px;
      margin-top: 18px;
      border: 1px solid #34372f;
      border-radius: 20px;
      padding: 20px;
      max-height: calc(100vh - 24px);
      overflow: auto;
      background: rgba(26, 28, 24, .96);
      box-shadow: 0 18px 54px rgba(0, 0, 0, .28);
      backdrop-filter: blur(18px);
      transition:
        width .2s ease,
        padding .2s ease,
        border-radius .2s ease,
        box-shadow .2s ease;
    }
    .control-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .control-title {
      margin: 0;
      color: #aab5a5;
      font-size: 12px;
      font-weight: 750;
      letter-spacing: .12em;
      text-transform: uppercase;
    }
    .compact-toggle {
      border-color: #3e423a;
      padding: 7px 12px;
      color: #c8cec3;
      background: #22251f;
      font-size: 12px;
      font-weight: 650;
    }
    .controls.compact {
      width: fit-content;
      max-width: 100%;
      margin-left: auto;
      padding: 10px 12px;
      border-radius: 999px;
      overflow: visible;
    }
    .controls.compact .control-title,
    .controls.compact .sources,
    .controls.compact .name-row,
    .controls.compact .hint,
    .controls.compact .notice,
    .controls.compact .download {
      display: none;
    }
    .controls.compact .control-bar {
      position: absolute;
      top: 50%;
      right: 10px;
      transform: translateY(-50%);
      z-index: 1;
    }
    .controls.compact .actions {
      padding-right: 58px;
    }
    .controls.compact button:not(.compact-toggle) {
      padding: 8px 14px;
    }
    .sources {
      display: flex;
      flex-wrap: wrap;
      gap: 12px 24px;
    }
    label {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      color: #d9ddd4;
      font-size: 14px;
      cursor: pointer;
    }
    input[type="checkbox"] {
      width: 17px;
      height: 17px;
      accent-color: #8dcc81;
    }
    .language-row {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .language-row label { color: #aab0a6; font-size: 13px; }
    select {
      border: 1px solid #454940;
      border-radius: 10px;
      padding: 9px 32px 9px 11px;
      color: #f4f1e8;
      background: #161814;
      font: inherit;
      font-size: 14px;
      outline: none;
    }
    select:focus {
      border-color: #8dcc81;
      box-shadow: 0 0 0 3px rgba(141, 204, 129, .1);
    }
    select:disabled { opacity: .55; }
    .language-hint { color: #898f85; font-size: 12px; }
    .name-row {
      display: grid;
      grid-template-columns: auto minmax(180px, 1fr) auto auto;
      align-items: center;
      gap: 12px;
    }
    .name-row label { color: #aab0a6; font-size: 13px; }
    input[type="text"] {
      width: 100%;
      border: 1px solid #454940;
      border-radius: 10px;
      padding: 9px 11px;
      color: #f4f1e8;
      background: #161814;
      font: inherit;
      font-size: 14px;
      outline: none;
    }
    input[type="text"]:focus {
      border-color: #8dcc81;
      box-shadow: 0 0 0 3px rgba(141, 204, 129, .1);
    }
    .name-state { min-width: 52px; color: #898f85; font-size: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; }
    button {
      border: 1px solid #4b5047;
      border-radius: 999px;
      padding: 10px 18px;
      color: #11120f;
      background: #b7d7b0;
      font: inherit;
      font-size: 14px;
      font-weight: 750;
      cursor: pointer;
    }
    button.secondary { color: #d9ddd4; background: #252822; }
    button.danger {
      border-color: #69453f;
      color: #efc2b9;
      background: #30201d;
    }
    button:disabled { cursor: not-allowed; opacity: .42; }
    .download {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid #4b5047;
      border-radius: 999px;
      padding: 10px 16px;
      color: #d9ddd4;
      background: #252822;
      font-size: 14px;
      text-decoration: none;
    }
    .notice {
      margin: 0;
      border-left: 3px solid #d1a34f;
      padding: 2px 0 2px 12px;
      color: #d8c49f;
      font-size: 13px;
      line-height: 1.55;
    }
    .hint { margin: 0; color: #898f85; font-size: 12px; line-height: 1.55; }
    .transcript {
      display: grid;
      gap: 12px;
      margin-top: 20px;
    }
    .empty {
      margin-top: 48px;
      border: 1px dashed #34372f;
      border-radius: 18px;
      padding: 48px 24px;
      color: #878c83;
      text-align: center;
    }
    article {
      display: grid;
      grid-template-columns: 104px 1fr;
      gap: 24px;
      border: 1px solid #2a2c27;
      border-radius: 18px;
      padding: 20px 22px;
      background: rgba(29, 31, 27, .82);
      box-shadow: 0 16px 44px rgba(0, 0, 0, .12);
      animation: enter .3s ease-out both;
    }
    @keyframes enter {
      from { opacity: 0; transform: translateY(8px); }
    }
    .meta { color: #8f958a; font-size: 12px; line-height: 1.6; }
    .speaker { color: #afc8a9; font-weight: 700; }
    .text {
      margin: 0;
      font-size: clamp(18px, 2.5vw, 24px);
      line-height: 1.55;
      overflow-wrap: anywhere;
    }
    @media (max-width: 620px) {
      main { width: min(100% - 24px, 900px); padding-top: 24px; }
      .heading { align-items: flex-start; flex-direction: column; }
      .status-stack { justify-content: flex-start; }
      .controls { top: 8px; max-height: calc(100vh - 16px); }
      article { grid-template-columns: 1fr; gap: 8px; padding: 18px; }
      .meta { display: flex; gap: 10px; }
      .name-row { grid-template-columns: 1fr; gap: 7px; }
      .language-row { align-items: flex-start; flex-direction: column; gap: 7px; }
      .language-row select { width: 100%; }
      .name-state { min-height: 1em; }
      .actions { flex-direction: column; }
      button, .download { width: 100%; }
      .controls.compact { width: 100%; border-radius: 18px; }
      .controls.compact .actions {
        flex-direction: row;
        padding-right: 58px;
      }
      .controls.compact button:not(.compact-toggle) { width: auto; flex: 1; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <p class="eyebrow">Local meeting capture</p>
      <div class="heading">
        <h1>即時逐字稿</h1>
        <div class="status-stack">
          <div id="modelStatus" class="status" role="status">
            <span class="dot"></span><span id="modelStatusText">模型狀態：連線中…</span>
          </div>
          <div id="status" class="status" role="status">
            <span class="dot"></span><span id="statusText">服務狀態：正在連線…</span>
          </div>
        </div>
      </div>
    </header>
    <section id="controls" class="controls" aria-label="錄音控制">
      <div class="control-bar">
        <p class="control-title">Recording controls</p>
        <button
          id="compactToggle"
          class="compact-toggle"
          type="button"
          aria-expanded="true"
        >縮小</button>
      </div>
      <p id="secureNotice" class="notice" hidden>
        瀏覽器只允許 HTTPS 網頁使用麥克風。請改用 HTTPS 網址。
      </p>
      <div class="sources">
        <label>
          <input id="microphone" type="checkbox" checked>
          麥克風
        </label>
        <label>
          <input id="systemAudio" type="checkbox">
          分頁／螢幕音訊
        </label>
      </div>
      <div class="language-row">
        <label for="language">辨識語言</label>
        <select id="language">
          <option value="zh">中文</option>
          <option value="en">英文</option>
          <option value="auto">Auto</option>
        </select>
        <span class="language-hint">套用到下一次錄音</span>
      </div>
      <div class="name-row">
        <label for="sessionName">逐字稿名稱</label>
        <input
          id="sessionName"
          type="text"
          maxlength="80"
          placeholder="留空會自動命名"
          autocomplete="off"
        >
        <span id="nameState" class="name-state"></span>
        <button id="resetSessionName" class="secondary" type="button">
          重置為自動名稱
        </button>
      </div>
      <div class="actions">
        <button id="modelToggle" class="secondary" type="button">啟用模型</button>
        <button id="start" type="button" disabled>開始遠端錄音</button>
        <button id="stop" class="secondary" type="button" disabled>停止錄音</button>
        <button id="clearTranscript" class="danger" type="button" disabled>
          清除畫面逐字稿
        </button>
        <a id="download" class="download" href="#" hidden>下載 Markdown</a>
        <a id="downloadJsonl" class="download" href="#" hidden>下載 JSONL</a>
      </div>
      <p class="hint">
        先啟用模型，再開始錄音。停止後會完成辨識、保存檔案並自動釋放模型記憶體。
        分享分頁時請在瀏覽器選擇器中勾選「分享分頁音訊」。
        「清除畫面逐字稿」不會刪除已保存檔案。
        「重置為自動名稱」會讓下一場錄音使用新的日期時間檔名，不會改動目前已保存的檔案。
      </p>
    </section>
    <section id="transcript" class="transcript" aria-live="polite"></section>
    <div id="empty" class="empty">逐字稿會在偵測到完整語句後出現在這裡。</div>
  </main>
  <script>
    const transcript = document.querySelector("#transcript");
    const empty = document.querySelector("#empty");
    const controls = document.querySelector("#controls");
    const compactToggle = document.querySelector("#compactToggle");
    const status = document.querySelector("#status");
    const statusText = document.querySelector("#statusText");
    const modelStatus = document.querySelector("#modelStatus");
    const modelStatusText = document.querySelector("#modelStatusText");
    const secureNotice = document.querySelector("#secureNotice");
    const microphone = document.querySelector("#microphone");
    const systemAudio = document.querySelector("#systemAudio");
    const language = document.querySelector("#language");
    const sessionName = document.querySelector("#sessionName");
    const nameState = document.querySelector("#nameState");
    const resetSessionName = document.querySelector("#resetSessionName");
    const modelToggle = document.querySelector("#modelToggle");
    const startButton = document.querySelector("#start");
    const stopButton = document.querySelector("#stop");
    const clearTranscriptButton = document.querySelector("#clearTranscript");
    const download = document.querySelector("#download");
    const downloadJsonl = document.querySelector("#downloadJsonl");
    let serverState = "starting";
    let modelState = "unloaded";
    let modelName = "";
    let transcriptHasItems = false;
    let audioSocket = null;
    let audioContext = null;
    let silentOutput = null;
    let captureStreams = [];
    let audioNodes = [];
    let currentSession = null;
    let renameTimer = null;
    let nameResetForNextRecording = false;
    const remoteAudioEnabled = __REMOTE_AUDIO_ENABLED__;
    const modelControlEnabled = __MODEL_CONTROL_ENABLED__;
    language.value = "__DEFAULT_LANGUAGE__";

    function setControlsCompact(compact, persist = true) {
      controls.classList.toggle("compact", compact);
      compactToggle.textContent = compact ? "展開" : "縮小";
      compactToggle.setAttribute("aria-expanded", String(!compact));
      if (persist) {
        try {
          localStorage.setItem("liveTranscriptControlsCompact", String(compact));
        } catch (_) {
          // The compact preference is optional when browser storage is unavailable.
        }
      }
    }

    function setStatus(data) {
      serverState = data.state;
      statusText.textContent = `服務：${data.message}`;
      status.classList.toggle("live", data.state === "recording");
      status.classList.toggle("ended", data.state === "stopped" || data.state === "error");
      updateControls();
    }

    function setModelStatus(data) {
      modelState = data.state;
      modelName = data.name || modelName;
      modelStatusText.textContent = `模型：${data.message}`;
      modelStatus.classList.toggle("model-ready", data.state === "loaded");
      modelStatus.classList.toggle(
        "busy",
        data.state === "loading" || data.state === "unloading",
      );
      modelStatus.classList.toggle("ended", data.state === "unloaded" || data.state === "error");
      updateControls();
    }

    function updateControls() {
      const capturing = audioSocket !== null;
      const serverReady = serverState === "waiting" || serverState === "recording";
      const modelBusy = modelState === "loading" || modelState === "unloading";
      startButton.disabled =
        capturing || !serverReady || modelState !== "loaded" || !window.isSecureContext;
      stopButton.disabled = !capturing;
      modelToggle.disabled = capturing || modelBusy || !modelControlEnabled;
      modelToggle.textContent =
        modelState === "loaded"
          ? "卸載模型"
          : modelState === "loading"
            ? "模型載入中…"
            : modelState === "unloading"
              ? "模型釋放中…"
              : modelState === "error"
                ? "重新載入模型"
                : "啟用模型";
      clearTranscriptButton.disabled = !transcriptHasItems;
      resetSessionName.disabled = capturing;
      language.disabled = capturing;
      microphone.disabled = capturing;
      systemAudio.disabled = capturing;
    }

    function addTranscript(data) {
      transcriptHasItems = true;
      empty.hidden = true;
      const article = document.createElement("article");
      const meta = document.createElement("div");
      meta.className = "meta";
      const speaker = document.createElement("div");
      speaker.className = "speaker";
      speaker.textContent = data.speaker;
      const timestamp = document.createElement("time");
      timestamp.textContent = data.timestamp;
      meta.append(speaker, timestamp);
      const text = document.createElement("p");
      text.className = "text";
      text.textContent = data.text;
      article.append(meta, text);
      transcript.append(article);
      article.scrollIntoView({ behavior: "smooth", block: "nearest" });
      updateControls();
    }

    function clearTranscriptDisplay() {
      transcript.replaceChildren();
      transcriptHasItems = false;
      empty.textContent = "逐字稿畫面已清除；已保存檔案不受影響。";
      empty.hidden = false;
      updateControls();
    }

    function websocketUrl() {
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      return `${protocol}//${location.host}/audio`;
    }

    function handleSession(data) {
      if (!data) return;
      const isNew = !currentSession || currentSession.id !== data.id;
      if (isNew) {
        transcript.replaceChildren();
        transcriptHasItems = false;
        empty.textContent = "逐字稿會在偵測到完整語句後出現在這裡。";
        empty.hidden = false;
      }
      currentSession = data;
      if (isNew || document.activeElement !== sessionName) {
        sessionName.value = data.name || "";
      }
      const saved = data.state === "saved";
      download.hidden = !saved || !data.download_url;
      downloadJsonl.hidden = !saved || !data.jsonl_url;
      if (data.download_url) download.href = data.download_url;
      if (data.jsonl_url) downloadJsonl.href = data.jsonl_url;
      nameState.textContent =
        data.state === "recording" ? "錄音中" : saved ? "已保存" : "";
    }

    async function renameSession() {
      if (!currentSession) return;
      nameState.textContent = "儲存中…";
      try {
        const response = await fetch(`/sessions/${currentSession.id}/name`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: sessionName.value }),
        });
        const result = await readJsonBody(response, "重新命名服務暫時無法回應");
        if (!response.ok) throw new Error(result.message || "無法重新命名逐字稿");
        handleSession(result.session);
      } catch (error) {
        nameState.textContent =
          error instanceof Error ? error.message : "重新命名失敗";
      }
    }

    function queueRename() {
      if (!currentSession) return;
      nameResetForNextRecording = false;
      clearTimeout(renameTimer);
      nameState.textContent = "尚未保存";
      renameTimer = setTimeout(renameSession, 500);
    }

    function resetNameForNextRecording() {
      clearTimeout(renameTimer);
      if (audioSocket) {
        window.alert("錄音進行中，請停止錄音後再重置下一場名稱");
        return;
      }
      nameResetForNextRecording = true;
      sessionName.value = "";
      nameState.textContent = "下次錄音將自動命名";
    }

    async function readJsonBody(response, fallbackMessage) {
      const body = await response.text();
      try {
        return JSON.parse(body);
      } catch (_) {
        if (response.redirected) {
          try {
            if (new URL(response.url).origin !== location.origin) {
              throw new Error("登入狀態已過期，請重新整理頁面並重新登入");
            }
          } catch (error) {
            if (error instanceof Error && error.message.includes("登入狀態")) throw error;
          }
        }
        throw new Error(`${fallbackMessage}（HTTP ${response.status}）`);
      }
    }

    async function toggleModel() {
      const action = modelState === "loaded" ? "unload" : "load";
      modelToggle.disabled = true;
      try {
        const response = await fetch(`/model/${action}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const result = await readJsonBody(response, "模型服務暫時無法回應");
        if (!response.ok) throw new Error(result.message || "無法變更模型狀態");
      } catch (error) {
        window.alert(error instanceof Error ? error.message : "模型控制失敗");
      } finally {
        updateControls();
      }
    }

    async function requestTranscriptClear(confirmed = false) {
      const response = await fetch("/transcript/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed }),
      });
      const result = await readJsonBody(response, "清除逐字稿服務暫時無法回應");
      if (
        response.status === 409 &&
        result.confirmation_required &&
        window.confirm(result.message)
      ) {
        return requestTranscriptClear(true);
      }
      if (!response.ok) throw new Error(result.message || "無法清除逐字稿");
      return result;
    }

    function openAudioSocket() {
      return new Promise((resolve, reject) => {
        const requestedName = encodeURIComponent(sessionName.value.trim());
        const requestedLanguage = encodeURIComponent(language.value);
        const socket = new WebSocket(
          `${websocketUrl()}?name=${requestedName}&language=${requestedLanguage}`,
        );
        let settled = false;
        socket.binaryType = "arraybuffer";
        socket.onerror = () => {
          if (!settled) reject(new Error("無法連線到 Mac mini 的音訊接收器"));
        };
        socket.onmessage = event => {
          try {
            const message = JSON.parse(event.data);
            if (message.type === "ready" && !settled) {
              settled = true;
              handleSession(message.session);
              resolve(socket);
              return;
            }
            if (message.type === "error") {
              if (!settled) {
                settled = true;
                reject(new Error(message.message));
              } else {
                setStatus({ state: "error", message: message.message });
                stopCapture();
              }
            }
          } catch (_) {
            // Binary audio only travels from the browser to the server.
          }
        };
        socket.onclose = () => {
          if (!settled) {
            settled = true;
            reject(new Error("Mac mini 關閉了音訊連線"));
          }
          if (audioSocket === socket) stopCapture(false);
        };
      });
    }

    function downsampleToInt16(input, inputRate) {
      const outputRate = 16000;
      const ratio = inputRate / outputRate;
      const outputLength = Math.max(1, Math.floor(input.length / ratio));
      const output = new Int16Array(outputLength);
      for (let index = 0; index < outputLength; index += 1) {
        const start = Math.floor(index * ratio);
        const end = Math.min(input.length, Math.floor((index + 1) * ratio));
        let total = 0;
        for (let cursor = start; cursor < end; cursor += 1) total += input[cursor];
        const sample = Math.max(-1, Math.min(1, total / Math.max(1, end - start)));
        output[index] = sample < 0 ? sample * 32768 : sample * 32767;
      }
      return output;
    }

    function attachAudioStream(stream, sourceId) {
      const source = audioContext.createMediaStreamSource(stream);
      const processor = audioContext.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = event => {
        if (!audioSocket || audioSocket.readyState !== WebSocket.OPEN) return;
        if (audioSocket.bufferedAmount > 1024 * 1024) return;
        const samples = downsampleToInt16(
          event.inputBuffer.getChannelData(0),
          audioContext.sampleRate,
        );
        const packet = new Uint8Array(1 + samples.byteLength);
        packet[0] = sourceId;
        packet.set(new Uint8Array(samples.buffer), 1);
        audioSocket.send(packet);
      };
      source.connect(processor);
      processor.connect(silentOutput);
      audioNodes.push(source, processor);
    }

    async function startCapture() {
      if (!window.isSecureContext) {
        secureNotice.hidden = false;
        return;
      }
      if (!microphone.checked && !systemAudio.checked) {
        window.alert("請至少選擇一個音訊來源");
        return;
      }
      if (modelState !== "loaded") {
        window.alert("請先啟用模型並等待載入完成");
        return;
      }

      startButton.disabled = true;
      download.hidden = true;
      downloadJsonl.hidden = true;
      try {
        audioContext = new AudioContext();
        silentOutput = audioContext.createGain();
        silentOutput.gain.value = 0;
        silentOutput.connect(audioContext.destination);
        const selectedStreams = [];

        // getDisplayMedia needs a fresh user gesture, so request it before any
        // other permission prompt or network operation.
        if (systemAudio.checked) {
          const displayStream = await navigator.mediaDevices.getDisplayMedia({
            video: true,
            audio: true,
          });
          const displayAudioTrack = displayStream.getAudioTracks()[0];
          if (!displayAudioTrack) {
            displayStream.getTracks().forEach(track => track.stop());
            const message = "所選分頁沒有音訊；請在分享選擇器中勾選分享分頁音訊";
            if (!microphone.checked) throw new Error(message);
            systemAudio.checked = false;
            window.alert(`${message}。本次將繼續只錄麥克風。`);
          } else {
            displayAudioTrack.addEventListener("ended", () => {
              if (!audioSocket) return;
              systemAudio.checked = false;
              if (microphone.checked) {
                setStatus({ state: "recording", message: "分頁音訊已停止；麥克風仍在錄音" });
              } else {
                stopCapture();
              }
            }, { once: true });
            captureStreams.push(displayStream);
            selectedStreams.push([displayStream, 0]);
          }
        }

        if (microphone.checked) {
          const micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
              channelCount: 1,
              echoCancellation: true,
              noiseSuppression: true,
              autoGainControl: true,
            },
          });
          captureStreams.push(micStream);
          selectedStreams.push([micStream, 1]);
        }

        if (selectedStreams.length === 0) throw new Error("沒有可使用的音訊來源");
        audioSocket = await openAudioSocket();
        selectedStreams.forEach(([stream, sourceId]) => attachAudioStream(stream, sourceId));
        await audioContext.resume();
        updateControls();
      } catch (error) {
        await stopCapture();
        setStatus({
          state: "waiting",
          message: error instanceof Error ? error.message : "無法開始錄音",
        });
      }
    }

    async function stopCapture(closeSocket = true) {
      const socket = audioSocket;
      if (socket) nameState.textContent = "保存中…";
      audioSocket = null;
      audioNodes.forEach(node => {
        node.disconnect();
        if ("onaudioprocess" in node) node.onaudioprocess = null;
      });
      audioNodes = [];
      captureStreams.forEach(stream => stream.getTracks().forEach(track => track.stop()));
      captureStreams = [];
      if (audioContext) await audioContext.close();
      audioContext = null;
      silentOutput = null;
      if (closeSocket && socket && socket.readyState < WebSocket.CLOSING) socket.close();
      updateControls();
    }

    const events = new EventSource("/events");
    events.addEventListener("status", event => setStatus(JSON.parse(event.data)));
    events.addEventListener("model", event => setModelStatus(JSON.parse(event.data)));
    events.addEventListener("transcript", event => addTranscript(JSON.parse(event.data)));
    events.addEventListener("transcript-clear", clearTranscriptDisplay);
    events.addEventListener("session", event => handleSession(JSON.parse(event.data)));
    events.onerror = () => {
      if (events.readyState === EventSource.CONNECTING) {
        setStatus({ state: "connecting", message: "重新連線中…" });
      }
    };
    modelToggle.addEventListener("click", toggleModel);
    resetSessionName.addEventListener("pointerdown", () => {
      nameResetForNextRecording = true;
    });
    resetSessionName.addEventListener("click", resetNameForNextRecording);
    startButton.addEventListener("click", startCapture);
    stopButton.addEventListener("click", async () => {
      clearTimeout(renameTimer);
      await renameSession();
      await stopCapture();
    });
    clearTranscriptButton.addEventListener("click", async () => {
      clearTranscriptButton.disabled = true;
      try {
        await requestTranscriptClear();
      } catch (error) {
        window.alert(error instanceof Error ? error.message : "清除逐字稿失敗");
      } finally {
        updateControls();
      }
    });
    sessionName.addEventListener("input", queueRename);
    sessionName.addEventListener("blur", () => {
      clearTimeout(renameTimer);
      if (nameResetForNextRecording || document.activeElement === resetSessionName) return;
      renameSession();
    });
    compactToggle.addEventListener("click", () => {
      setControlsCompact(!controls.classList.contains("compact"));
    });
    controls.hidden = !remoteAudioEnabled;
    modelToggle.hidden = !modelControlEnabled;
    try {
      setControlsCompact(
        localStorage.getItem("liveTranscriptControlsCompact") === "true",
        false,
      );
    } catch (_) {
      setControlsCompact(false, false);
    }
    secureNotice.hidden = window.isSecureContext;
    updateControls();
  </script>
</body>
</html>
"""


class EventHub:
    def __init__(self, model_name: str = "") -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._transcripts: list[dict[str, Any]] = []
        self._session: dict[str, Any] | None = None
        self._status: dict[str, Any] = {
            "type": "status",
            "state": "starting",
            "message": "正在啟動…",
        }
        self._model_status: dict[str, Any] = {
            "type": "model",
            "state": "unloaded",
            "name": model_name,
            "message": "模型未載入（低記憶體待機）",
        }

    def subscribe(self) -> tuple[queue.Queue[dict[str, Any]], list[dict[str, Any]]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.add(subscriber)
            initial = [self._status.copy(), self._model_status.copy()]
            if self._session is not None:
                initial.append(self._session.copy())
            initial.extend(item.copy() for item in self._transcripts)
        return subscriber, initial

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def set_status(self, state: str, message: str) -> None:
        event = {"type": "status", "state": state, "message": message}
        with self._lock:
            self._status = event
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def set_model_status(self, state: str, name: str, message: str) -> None:
        event = {
            "type": "model",
            "state": state,
            "name": name,
            "message": message,
        }
        with self._lock:
            self._model_status = event
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def publish_transcript(self, event: TranscriptEvent) -> None:
        payload = {
            "type": "transcript",
            "source": event.source.name.lower(),
            "speaker": event.source.display_name,
            "timestamp": format_timestamp(event.start_seconds),
            "start_seconds": round(event.start_seconds, 3),
            "end_seconds": round(event.end_seconds, 3),
            "text": event.text,
            "detected_language": event.detected_language,
        }
        with self._lock:
            self._transcripts.append(payload)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(payload)

    def publish_session(self, payload: dict[str, Any]) -> None:
        event = {"type": "session", **payload}
        with self._lock:
            previous_id = self._session.get("id") if self._session is not None else None
            if event.get("state") == "recording" and event.get("id") != previous_id:
                self._transcripts = []
            self._session = event
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def clear_transcripts(self, *, confirmed: bool) -> tuple[bool, str]:
        with self._lock:
            session_state = (
                self._session.get("state") if self._session is not None else None
            )
            needs_confirmation = (
                bool(self._transcripts)
                and session_state not in {None, "saved"}
            )
            if needs_confirmation and not confirmed:
                return False, (
                    "逐字稿尚未完成保存。清除只會移除目前畫面，"
                    "錄音仍會繼續，稍後保存的檔案不受影響。確定要清除嗎？"
                )
            self._transcripts = []
            event = {"type": "transcript-clear"}
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)
        return True, "逐字稿畫面已清除；已保存檔案不受影響"


def _encode_event(event: dict[str, Any]) -> bytes:
    event_type = str(event["type"])
    payload = {key: value for key, value in event.items() if key != "type"}
    return (
        f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode()


def _read_exact(stream: Any, size: int) -> bytes | None:
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            return None
        result.extend(chunk)
    return bytes(result)


def _read_websocket_frame(stream: Any) -> tuple[int, bytes] | None:
    header = _read_exact(stream, 2)
    if header is None:
        return None
    first, second = header
    if first & 0x70:
        raise ValueError("不支援 WebSocket 擴充 frame")
    if not first & 0x80:
        raise ValueError("不支援分段 WebSocket frame")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    if not masked:
        raise ValueError("瀏覽器 WebSocket frame 必須使用 masking")
    payload_length = second & 0x7F
    if payload_length == 126:
        extended = _read_exact(stream, 2)
        if extended is None:
            return None
        payload_length = struct.unpack("!H", extended)[0]
    elif payload_length == 127:
        extended = _read_exact(stream, 8)
        if extended is None:
            return None
        payload_length = struct.unpack("!Q", extended)[0]
    if payload_length > MAX_WEBSOCKET_PAYLOAD:
        raise ValueError("WebSocket 音訊 frame 過大")
    mask = _read_exact(stream, 4)
    payload = _read_exact(stream, payload_length)
    if mask is None or payload is None:
        return None
    unmasked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, unmasked


def _encode_websocket_frame(opcode: int, payload: bytes = b"") -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.extend((126,))
        header.extend(struct.pack("!H", length))
    else:
        header.extend((127,))
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def make_handler(
    hub: EventHub,
    audio_receiver: AudioReceiver | None,
    model_controller: ModelController | None,
    stopping: threading.Event,
    register_audio_connection: Callable[[str, socket.socket], None],
    unregister_audio_connection: Callable[[str], None],
) -> type[BaseHTTPRequestHandler]:
    class TranscriptHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/":
                page = PAGE.replace(
                    "__REMOTE_AUDIO_ENABLED__",
                    "true" if audio_receiver is not None else "false",
                )
                page = page.replace(
                    "__MODEL_CONTROL_ENABLED__",
                    "true" if model_controller is not None else "false",
                )
                default_language = getattr(model_controller, "language", None)
                if default_language not in {"zh", "en"}:
                    default_language = "auto"
                page = page.replace("__DEFAULT_LANGUAGE__", default_language)
                body = page.encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                body = b'{"ok":true}\n'
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/events":
                self._serve_events()
                return
            if path == "/audio" and audio_receiver is not None:
                self._serve_audio()
                return
            parts = path.strip("/").split("/")
            if (
                audio_receiver is not None
                and len(parts) == 3
                and parts[0] == "sessions"
                and parts[2] == "download"
            ):
                file_format = parse_qs(parsed.query).get("format", ["markdown"])[0]
                self._serve_download(parts[1], file_format)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/model/load" and model_controller is not None:
                if not self._origin_matches_host():
                    self.send_error(HTTPStatus.FORBIDDEN, "Origin does not match Host")
                    return
                if model_controller.state not in {"loading", "loaded"}:
                    threading.Thread(
                        target=self._load_model,
                        name="model-loader",
                        daemon=True,
                    ).start()
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "state": model_controller.state},
                )
                return
            if path == "/model/unload" and model_controller is not None:
                if not self._origin_matches_host():
                    self.send_error(HTTPStatus.FORBIDDEN, "Origin does not match Host")
                    return
                try:
                    started = model_controller.start_unload()
                except RuntimeError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"ok": False, "message": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.ACCEPTED if started else HTTPStatus.OK,
                    {
                        "ok": True,
                        "state": model_controller.state,
                        "message": (
                            "模型正在背景釋放記憶體"
                            if model_controller.state == "unloading"
                            else "模型已卸載"
                        ),
                    },
                )
                return
            if path == "/transcript/clear":
                if not self._origin_matches_host():
                    self.send_error(HTTPStatus.FORBIDDEN, "Origin does not match Host")
                    return
                try:
                    body = self._read_json_body(allow_empty=True)
                    confirmed = bool(body.get("confirmed", False))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "message": str(exc)},
                    )
                    return
                cleared, message = hub.clear_transcripts(confirmed=confirmed)
                self._send_json(
                    HTTPStatus.OK if cleared else HTTPStatus.CONFLICT,
                    {
                        "ok": cleared,
                        "confirmation_required": not cleared,
                        "message": message,
                    },
                )
                return

            parts = path.strip("/").split("/")
            if (
                audio_receiver is None
                or len(parts) != 3
                or parts[0] != "sessions"
                or parts[2] != "name"
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not self._origin_matches_host():
                self.send_error(HTTPStatus.FORBIDDEN, "Origin does not match Host")
                return
            try:
                body = self._read_json_body()
                name = str(body["name"])
                session = audio_receiver.rename_session(parts[1], name)
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            except FileExistsError as exc:
                self.send_error(HTTPStatus.CONFLICT, str(exc))
                return
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            response = json.dumps({"session": session}, ensure_ascii=False).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def _load_model(self) -> None:
            assert model_controller is not None
            try:
                model_controller.load()
            except BaseException as exc:
                print(f"模型載入失敗：{exc}", file=sys.stderr, flush=True)

        def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any]:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if content_length == 0 and allow_empty:
                return {}
            if content_length <= 0 or content_length > 4096:
                raise ValueError("Invalid request body")
            body = json.loads(self.rfile.read(content_length))
            if not isinstance(body, dict):
                raise TypeError("JSON body must be an object")
            return body

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            response = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def _serve_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            subscriber, initial = hub.subscribe()
            try:
                for event in initial:
                    self.wfile.write(_encode_event(event))
                self.wfile.flush()
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                        self.wfile.write(_encode_event(event))
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unsubscribe(subscriber)

        def _serve_audio(self) -> None:
            assert audio_receiver is not None
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(HTTPStatus.UPGRADE_REQUIRED, "WebSocket required")
                return
            if not self._origin_matches_host():
                self.send_error(HTTPStatus.FORBIDDEN, "Origin does not match Host")
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or self.headers.get("Sec-WebSocket-Version") != "13":
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid WebSocket handshake")
                return

            accept = base64.b64encode(
                hashlib.sha1((key + WEBSOCKET_GUID).encode()).digest()
            ).decode()
            self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True

            client_id = secrets.token_urlsafe(12)
            query = parse_qs(urlsplit(self.path).query)
            requested_name = query.get("name", [None])[0]
            requested_language = query.get("language", [None])[0]
            accepted, message, session = audio_receiver.open(
                client_id, requested_name, requested_language
            )
            if not accepted:
                error = json.dumps(
                    {"type": "error", "message": message}, ensure_ascii=False
                ).encode()
                self.wfile.write(_encode_websocket_frame(0x1, error))
                self.wfile.write(_encode_websocket_frame(0x8))
                self.wfile.flush()
                return
            ready = json.dumps(
                {"type": "ready", "session": session}, ensure_ascii=False
            ).encode()
            register_audio_connection(client_id, self.connection)

            try:
                self.wfile.write(_encode_websocket_frame(0x1, ready))
                self.wfile.flush()
                while not stopping.is_set():
                    frame = _read_websocket_frame(self.rfile)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        self.wfile.write(_encode_websocket_frame(0xA, payload))
                        self.wfile.flush()
                        continue
                    if opcode != 0x2:
                        continue
                    if len(payload) < 3 or len(payload[1:]) % 2:
                        raise ValueError("收到無效的 PCM 音訊 frame")
                    try:
                        source = AudioSource(payload[0])
                    except ValueError as exc:
                        raise ValueError("收到未知的遠端音訊來源") from exc
                    audio_receiver.feed(client_id, source, payload[1:])
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as exc:
                error = json.dumps(
                    {"type": "error", "message": str(exc)}, ensure_ascii=False
                ).encode()
                try:
                    self.wfile.write(_encode_websocket_frame(0x1, error))
                    self.wfile.flush()
                except OSError:
                    pass
            finally:
                unregister_audio_connection(client_id)
                audio_receiver.close(client_id)
                try:
                    self.wfile.write(_encode_websocket_frame(0x8))
                    self.wfile.flush()
                except OSError:
                    pass

        def _serve_download(self, session_id: str, file_format: str) -> None:
            assert audio_receiver is not None
            result = audio_receiver.download(session_id, file_format)
            if result is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Transcript is not ready")
                return
            path, filename = result
            try:
                body = path.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND, "Transcript file is missing")
                return
            content_type = (
                "application/x-ndjson; charset=utf-8"
                if file_format == "jsonl"
                else "text/markdown; charset=utf-8"
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(filename)}",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _origin_matches_host(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlsplit(origin).netloc == self.headers.get("Host", "")

        def log_message(self, _format: str, *args: object) -> None:
            return

    return TranscriptHandler


class TranscriptHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        _exc_type, exc, _traceback = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class LiveTranscriptServer:
    def __init__(
        self,
        host: str,
        port: int,
        audio_receiver: AudioReceiver | None = None,
        hub: EventHub | None = None,
        model_controller: ModelController | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.hub = hub or EventHub()
        self._stopping = threading.Event()
        self._audio_connections_lock = threading.Lock()
        self._audio_connections: dict[str, socket.socket] = {}
        self._server = TranscriptHTTPServer(
            (host, port),
            make_handler(
                self.hub,
                audio_receiver,
                model_controller,
                self._stopping,
                self._register_audio_connection,
                self._unregister_audio_connection,
            ),
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="transcript-web-server",
            daemon=True,
        )

    @property
    def url(self) -> str:
        display_host = "localhost" if self.host in {"127.0.0.1", "0.0.0.0", "::"} else self.host
        return f"http://{display_host}:{self._server.server_port}"

    def start(self) -> None:
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stopping.set()
        with self._audio_connections_lock:
            client_ids = tuple(self._audio_connections)
        for client_id in client_ids:
            self.disconnect_audio(client_id)
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join()

    def disconnect_audio(self, client_id: str) -> None:
        with self._audio_connections_lock:
            connection = self._audio_connections.pop(client_id, None)
        if connection is None:
            return
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _register_audio_connection(
        self,
        client_id: str,
        connection: socket.socket,
    ) -> None:
        with self._audio_connections_lock:
            self._audio_connections[client_id] = connection

    def _unregister_audio_connection(self, client_id: str) -> None:
        with self._audio_connections_lock:
            self._audio_connections.pop(client_id, None)
