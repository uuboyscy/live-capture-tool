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
  <meta name="color-scheme" content="light">
  <title>Live Transcript</title>
  <style>
    :root {
      --color-canvas: #ece6dc;
      --color-surface: #f1ece3;
      --color-surface-raised: #f7f2ea;
      --color-surface-inset: #e2dbd0;
      --color-ink: #292724;
      --color-muted: #57534d;
      --color-subtle: #635d56;
      --color-line: #c8beb0;
      --color-line-strong: #aa9d8d;
      --color-primary: #97462f;
      --color-primary-hover: #7f3825;
      --color-primary-soft: #ead1c6;
      --color-success: #3f6a55;
      --color-success-soft: #d5e2d9;
      --color-warning: #8b5b1b;
      --color-danger: #873b31;
      --color-danger-soft: #ead0cc;
      --color-focus: #235e78;
      --space-1: 8px;
      --space-2: 16px;
      --space-3: 24px;
      --space-4: 32px;
      --space-5: 48px;
      --radius-control: 12px;
      --radius-card: 16px;
      --shadow-raised:
        8px 8px 20px rgba(86, 69, 51, .16),
        -8px -8px 20px rgba(255, 255, 255, .76),
        inset 1px 1px 0 rgba(255, 255, 255, .72);
      --shadow-control:
        4px 4px 10px rgba(86, 69, 51, .15),
        -4px -4px 10px rgba(255, 255, 255, .72),
        inset 1px 1px 0 rgba(255, 255, 255, .68);
      --shadow-pressed:
        inset 3px 3px 7px rgba(86, 69, 51, .18),
        inset -3px -3px 7px rgba(255, 255, 255, .64);
      --motion-fast: 160ms;
      --motion-base: 240ms;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      color: var(--color-ink);
      background: var(--color-canvas);
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      min-width: 320px;
      min-height: 100vh;
      margin: 0;
      color: var(--color-ink);
      background: var(--color-canvas);
      font-size: 16px;
      line-height: 1.5;
    }
    button, input, select { font: inherit; }
    button, a, input, select, summary { -webkit-tap-highlight-color: transparent; }
    [hidden] { display: none !important; }
    .skip-link {
      position: fixed;
      top: var(--space-2);
      left: var(--space-2);
      z-index: 100;
      transform: translateY(-160%);
      border-radius: var(--radius-control);
      padding: 12px 16px;
      color: #fff;
      background: var(--color-primary);
      transition: transform var(--motion-fast) ease;
    }
    .skip-link:focus { transform: translateY(0); }
    .app-shell {
      width: min(1200px, calc(100% - 48px));
      margin: 0 auto;
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 30;
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      min-height: 72px;
      border-bottom: 1px solid rgba(170, 157, 141, .58);
      background: rgba(236, 230, 220, .94);
      backdrop-filter: blur(16px);
    }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      width: fit-content;
      color: var(--color-ink);
      font-size: 15px;
      font-weight: 780;
      letter-spacing: -.01em;
      text-decoration: none;
    }
    .brand-mark {
      display: grid;
      place-items: center;
      width: 40px;
      height: 40px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      color: #fff;
      background: var(--color-primary);
      box-shadow: var(--shadow-control);
      font-size: 12px;
      letter-spacing: .06em;
    }
    .brand:hover .brand-mark { background: var(--color-primary-hover); transform: translateY(-1px); }
    .brand:active .brand-mark { transform: translateY(0); box-shadow: var(--shadow-pressed); }
    .nav-links { display: flex; align-items: center; gap: 8px; }
    .nav-links a {
      min-height: 44px;
      padding: 11px 14px;
      border-radius: var(--radius-control);
      color: var(--color-muted);
      font-size: 14px;
      font-weight: 650;
      text-decoration: none;
      transition: color var(--motion-fast), background var(--motion-fast), box-shadow var(--motion-fast);
    }
    .nav-links a:hover { color: var(--color-ink); background: var(--color-surface); }
    .nav-links a:active { box-shadow: var(--shadow-pressed); }
    .privacy-badge {
      justify-self: end;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      border: 1px solid var(--color-line);
      border-radius: 999px;
      padding: 7px 12px;
      color: var(--color-success);
      background: var(--color-surface);
      font-size: 12px;
      font-weight: 720;
      box-shadow: var(--shadow-control);
    }
    .privacy-badge::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--color-success);
      box-shadow: 0 0 0 4px rgba(63, 106, 85, .12);
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr);
      gap: 64px;
      align-items: center;
      padding: 72px 0 64px;
    }
    .eyebrow, .section-label {
      margin: 0 0 8px;
      color: var(--color-primary);
      font-size: 13px;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }
    h1, h2, h3, p { overflow-wrap: anywhere; }
    h1 {
      max-width: 760px;
      margin: 0;
      font-size: clamp(44px, 5.4vw, 72px);
      line-height: 1.04;
      font-weight: 720;
      letter-spacing: -.055em;
    }
    h1 span { color: var(--color-primary); }
    .lede {
      max-width: 660px;
      margin: 24px 0 0;
      color: var(--color-muted);
      font-size: 18px;
      line-height: 1.7;
    }
    .hero-meta {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      max-width: 680px;
      margin-top: 32px;
    }
    .meta-chip {
      min-height: 72px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      padding: 14px 16px;
      background: var(--color-surface);
      box-shadow: var(--shadow-control);
    }
    .meta-chip strong { display: block; font-size: 14px; }
    .meta-chip span { display: block; margin-top: 3px; color: var(--color-subtle); font-size: 12px; }
    .soft-card {
      border: 1px solid var(--color-line);
      border-radius: var(--radius-card);
      background: var(--color-surface);
      box-shadow: var(--shadow-raised);
    }
    .status-console { padding: 24px; }
    .status-console h2 {
      margin: 0;
      font-size: 22px;
      line-height: 1.25;
      letter-spacing: -.025em;
    }
    .status-stack { display: grid; gap: 12px; margin-top: 24px; }
    .status {
      position: relative;
      display: grid;
      grid-template-columns: 10px 1fr auto;
      align-items: center;
      gap: 12px;
      min-height: 56px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      padding: 12px 14px;
      color: var(--color-muted);
      background: var(--color-surface-inset);
      box-shadow: var(--shadow-pressed);
      font-size: 13px;
      font-weight: 620;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--color-warning);
      box-shadow: 0 0 0 4px rgba(139, 91, 27, .12);
    }
    .status.live .dot, .status.model-ready .dot {
      background: var(--color-success);
      box-shadow: 0 0 0 4px rgba(63, 106, 85, .14);
    }
    .status.live .dot { animation: pulse 1.8s ease-in-out infinite; }
    .status.ended .dot { background: var(--color-subtle); box-shadow: none; }
    .status-loader {
      display: none;
      width: 18px;
      height: 18px;
      border: 2px solid var(--color-line-strong);
      border-top-color: var(--color-primary);
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    .status.busy .status-loader { display: block; }
    .processing-notice {
      display: grid;
      grid-template-columns: 28px 1fr;
      align-items: center;
      gap: 14px;
      margin-top: 20px;
      border: 1px solid #d6b37b;
      border-radius: var(--radius-control);
      padding: 14px 16px;
      color: #64420f;
      background: #f3e3c7;
      box-shadow: inset 1px 1px 0 rgba(255, 255, 255, .55), 0 8px 18px rgba(83, 57, 32, .08);
    }
    .processing-notice[hidden] { display: none; }
    .processing-notice strong, .processing-notice span { display: block; }
    .processing-notice strong { font-size: 14px; }
    .processing-notice span { margin-top: 3px; font-size: 12px; line-height: 1.55; }
    .processing-spinner {
      width: 24px;
      height: 24px;
      border: 3px solid rgba(100, 66, 15, .2);
      border-top-color: #8b5b1b;
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    .console-note {
      display: flex;
      gap: 10px;
      margin: 16px 0 0;
      color: var(--color-subtle);
      font-size: 12px;
      line-height: 1.55;
    }
    .console-note::before { content: "●"; color: var(--color-success); font-size: 9px; padding-top: 3px; }
    main { padding-bottom: 96px; }
    .workspace {
      display: grid;
      grid-template-columns: minmax(340px, 400px) minmax(0, 1fr);
      gap: 32px;
      align-items: start;
      scroll-margin-top: 96px;
    }
    .controls[hidden] + .transcript-panel { grid-column: 1 / -1; }
    .controls {
      position: sticky;
      top: 96px;
      max-height: calc(100vh - 120px);
      overflow: auto;
      padding: 24px;
      transition: padding var(--motion-base) ease, box-shadow var(--motion-base) ease;
    }
    .control-bar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }
    .control-bar h2, .panel-header h2 {
      margin: 0;
      font-size: 24px;
      line-height: 1.25;
      letter-spacing: -.03em;
    }
    .control-content { display: grid; gap: 24px; margin-top: 24px; }
    .controls.compact { padding: 16px; }
    .controls.compact .control-content { display: none; }
    .controls.compact .control-bar { align-items: center; }
    .compact-toggle {
      min-width: 64px;
      min-height: 44px;
      padding: 10px 12px;
      color: var(--color-muted);
      background: var(--color-surface);
      font-size: 12px;
    }
    .notice {
      margin: 0;
      border: 1px solid #d6b37b;
      border-radius: var(--radius-control);
      padding: 12px 14px;
      color: #64420f;
      background: #f3e3c7;
      font-size: 13px;
      line-height: 1.55;
    }
    fieldset { min-width: 0; margin: 0; border: 0; padding: 0; }
    legend, .field-label {
      display: block;
      margin-bottom: 8px;
      color: var(--color-muted);
      font-size: 13px;
      font-weight: 740;
    }
    .sources { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .source-option {
      position: relative;
      display: grid;
      grid-template-columns: 24px 1fr;
      align-items: center;
      gap: 12px;
      min-height: 76px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      padding: 12px;
      color: var(--color-ink);
      background: var(--color-surface-raised);
      box-shadow: var(--shadow-control);
      cursor: pointer;
      transition: transform var(--motion-fast), border-color var(--motion-fast), box-shadow var(--motion-fast);
    }
    .source-option:hover { border-color: var(--color-line-strong); transform: translateY(-1px); }
    .source-option:active { transform: translateY(0); box-shadow: var(--shadow-pressed); }
    .source-option:focus-within { outline: 3px solid var(--color-focus); outline-offset: 3px; }
    .source-option:has(input:disabled) { cursor: not-allowed; opacity: .52; transform: none; }
    .source-copy strong { display: block; font-size: 14px; }
    .source-copy small { display: block; margin-top: 2px; color: var(--color-subtle); font-size: 11px; }
    input[type="checkbox"] {
      appearance: none;
      display: grid;
      place-content: center;
      width: 22px;
      height: 22px;
      margin: 0;
      border: 1px solid var(--color-line-strong);
      border-radius: 7px;
      background: var(--color-surface-inset);
      box-shadow: var(--shadow-pressed);
      cursor: pointer;
    }
    input[type="checkbox"]::before {
      content: "";
      width: 10px;
      height: 6px;
      border: solid #fff;
      border-width: 0 0 2px 2px;
      opacity: 0;
      transform: translateY(-1px) rotate(-45deg) scale(.7);
      transition: opacity var(--motion-fast), transform var(--motion-fast);
    }
    input[type="checkbox"]:checked { border-color: var(--color-primary); background: var(--color-primary); box-shadow: inset 2px 2px 4px rgba(74, 27, 14, .25); }
    input[type="checkbox"]:checked::before { opacity: 1; transform: translateY(-1px) rotate(-45deg) scale(1); }
    .form-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
    .field { min-width: 0; }
    .field-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
    .field-row .field-label { margin: 0; }
    .language-hint, .name-state { color: var(--color-subtle); font-size: 11px; }
    input[type="text"], select {
      width: 100%;
      min-height: 48px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      padding: 11px 13px;
      color: var(--color-ink);
      background: var(--color-surface-inset);
      box-shadow: var(--shadow-pressed);
      outline: none;
      transition: border-color var(--motion-fast), box-shadow var(--motion-fast), background var(--motion-fast);
    }
    select { cursor: pointer; }
    input[type="text"]::placeholder { color: var(--color-subtle); }
    input[type="text"]:hover, select:hover { border-color: var(--color-line-strong); }
    input:disabled, select:disabled { cursor: not-allowed; opacity: .52; }
    .name-tools { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
    button, .download {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 44px;
      border: 1px solid #71311f;
      border-radius: var(--radius-control);
      padding: 10px 16px;
      color: #fff;
      background: var(--color-primary);
      box-shadow: var(--shadow-control);
      font-size: 14px;
      font-weight: 760;
      line-height: 1.2;
      text-decoration: none;
      cursor: pointer;
      transition: transform var(--motion-fast), background var(--motion-fast), box-shadow var(--motion-fast), opacity var(--motion-fast);
    }
    button:hover, .download:hover { background: var(--color-primary-hover); transform: translateY(-1px); }
    button:active, .download:active { transform: translateY(0); box-shadow: var(--shadow-pressed); }
    button.secondary, .download {
      border-color: var(--color-line);
      color: var(--color-ink);
      background: var(--color-surface-raised);
    }
    button.secondary:hover, .download:hover { background: #fffaf3; }
    button.danger { border-color: #c7978f; color: var(--color-danger); background: var(--color-danger-soft); }
    button.danger:hover { background: #e2bbb5; }
    button:disabled, .download[aria-disabled="true"] {
      cursor: not-allowed;
      opacity: .42;
      transform: none;
      box-shadow: none;
    }
    button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
      outline: 3px solid var(--color-focus);
      outline-offset: 3px;
    }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .actions #start { grid-column: 1 / -1; min-height: 52px; font-size: 15px; }
    .download-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .helper {
      border-top: 1px solid var(--color-line);
      padding-top: 16px;
      color: var(--color-muted);
      font-size: 12px;
    }
    .helper summary {
      min-height: 44px;
      padding: 12px 0;
      color: var(--color-muted);
      font-weight: 720;
      cursor: pointer;
    }
    .helper summary:hover { color: var(--color-ink); }
    .helper summary:active { color: var(--color-primary); }
    .helper p { margin: 8px 0 0; line-height: 1.7; }
    .transcript-panel {
      min-height: 680px;
      padding: 24px;
      scroll-margin-top: 96px;
    }
    .panel-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 24px;
      border-bottom: 1px solid var(--color-line);
      padding-bottom: 20px;
    }
    .feed-count {
      flex: none;
      border: 1px solid var(--color-line);
      border-radius: 999px;
      padding: 7px 11px;
      color: var(--color-muted);
      background: var(--color-surface-inset);
      box-shadow: var(--shadow-pressed);
      font-size: 11px;
      font-weight: 720;
    }
    .transcript { display: grid; gap: 12px; margin-top: 20px; }
    .empty {
      display: grid;
      place-items: center;
      min-height: 430px;
      margin-top: 20px;
      border: 1px dashed var(--color-line-strong);
      border-radius: var(--radius-card);
      padding: 48px 24px;
      color: var(--color-muted);
      background: rgba(226, 219, 208, .42);
      text-align: center;
    }
    .empty-content { max-width: 360px; }
    .empty-icon {
      display: grid;
      grid-template-columns: repeat(4, 4px);
      align-items: center;
      justify-content: center;
      gap: 4px;
      width: 56px;
      height: 56px;
      margin: 0 auto 16px;
      border: 1px solid var(--color-line);
      border-radius: 50%;
      background: var(--color-surface);
      box-shadow: var(--shadow-control);
    }
    .empty-icon i { width: 4px; border-radius: 4px; background: var(--color-primary); }
    .empty-icon i:nth-child(1), .empty-icon i:nth-child(4) { height: 10px; }
    .empty-icon i:nth-child(2) { height: 24px; }
    .empty-icon i:nth-child(3) { height: 17px; }
    .empty strong { display: block; color: var(--color-ink); font-size: 16px; }
    .empty p { margin: 6px 0 0; color: var(--color-subtle); font-size: 13px; }
    .transcript article {
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 20px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      padding: 18px;
      background: var(--color-surface-raised);
      box-shadow: var(--shadow-control);
      animation: enter var(--motion-base) ease-out both;
    }
    .transcript .meta { color: var(--color-subtle); font-size: 11px; line-height: 1.65; }
    .transcript .speaker { color: var(--color-success); font-weight: 780; }
    .transcript .text { margin: 0; color: var(--color-ink); font-size: 18px; line-height: 1.62; }
    .workflow { padding: 88px 0 0; scroll-margin-top: 96px; }
    .workflow-heading { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
    .workflow-heading h2 { margin: 0; font-size: 32px; letter-spacing: -.035em; }
    .workflow-heading p { max-width: 480px; margin: 0; color: var(--color-muted); font-size: 14px; }
    .workflow-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
    .workflow-card { padding: 24px; }
    .step-number {
      display: grid;
      place-items: center;
      width: 40px;
      height: 40px;
      border: 1px solid var(--color-line);
      border-radius: var(--radius-control);
      color: var(--color-primary);
      background: var(--color-surface-inset);
      box-shadow: var(--shadow-pressed);
      font-size: 12px;
      font-weight: 820;
    }
    .workflow-card h3 { margin: 20px 0 8px; font-size: 18px; }
    .workflow-card p { margin: 0; color: var(--color-muted); font-size: 13px; line-height: 1.65; }
    footer {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      border-top: 1px solid var(--color-line);
      margin-top: 80px;
      padding: 24px 0 40px;
      color: var(--color-subtle);
      font-size: 12px;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 80;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(41, 39, 36, .48);
      backdrop-filter: blur(5px);
      animation: fade-in var(--motion-fast) ease-out;
    }
    .modal {
      width: min(440px, 100%);
      border: 1px solid var(--color-line);
      border-radius: var(--radius-card);
      padding: 24px;
      background: var(--color-surface-raised);
      box-shadow: 16px 18px 42px rgba(43, 34, 26, .28), inset 1px 1px 0 rgba(255,255,255,.8);
      animation: modal-in var(--motion-base) ease-out;
    }
    .modal h2 { margin: 0; font-size: 24px; letter-spacing: -.025em; }
    .modal-message { margin: 12px 0 0; color: var(--color-muted); line-height: 1.65; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 24px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes pulse { 50% { opacity: .35; } }
    @keyframes enter { from { opacity: 0; transform: translateY(8px); } }
    @keyframes fade-in { from { opacity: 0; } }
    @keyframes modal-in { from { opacity: 0; transform: translateY(10px) scale(.98); } }
    @media (max-width: 960px) {
      .app-shell { width: min(100% - 40px, 760px); }
      .topbar { grid-template-columns: 1fr auto; }
      .nav-links { display: none; }
      .hero { grid-template-columns: 1fr; gap: 32px; padding: 56px 0 48px; }
      .status-console { display: grid; grid-template-columns: auto 1fr; gap: 0 24px; }
      .status-console .section-label, .status-console h2, .status-console .console-note { grid-column: 1; }
      .status-stack { grid-column: 2; grid-row: 1 / span 3; margin-top: 0; }
      .workspace { grid-template-columns: 1fr; }
      .controls { position: static; max-height: none; }
      .workflow-grid { grid-template-columns: 1fr 1fr; }
      .workflow-card:last-child { grid-column: 1 / -1; }
    }
    @media (max-width: 600px) {
      .app-shell { width: min(100% - 28px, 440px); }
      .topbar { min-height: 64px; }
      .brand-mark { width: 36px; height: 36px; }
      .brand > span:last-child { display: none; }
      .privacy-badge { min-height: 34px; padding: 6px 10px; }
      .hero { gap: 24px; padding: 40px 0 36px; }
      h1 { font-size: 40px; line-height: 1.08; }
      .lede { margin-top: 16px; font-size: 16px; }
      .hero-meta { grid-template-columns: 1fr; gap: 8px; margin-top: 24px; }
      .meta-chip { min-height: 60px; padding: 11px 14px; }
      .status-console { display: block; padding: 20px; }
      .status-stack { margin-top: 20px; }
      .workspace { gap: 20px; }
      .controls, .transcript-panel { padding: 18px; }
      .control-bar h2, .panel-header h2 { font-size: 21px; }
      .sources, .actions, .download-row, .name-tools { grid-template-columns: 1fr; }
      .actions #start { grid-column: auto; }
      .source-option { min-height: 68px; }
      .panel-header { align-items: center; }
      .transcript-panel { min-height: 520px; }
      .empty { min-height: 330px; padding: 32px 18px; }
      .transcript article { grid-template-columns: 1fr; gap: 8px; padding: 16px; }
      .transcript .meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
      .transcript .text { font-size: 16px; }
      .workflow { padding-top: 64px; }
      .workflow-heading { display: block; }
      .workflow-heading h2 { font-size: 27px; }
      .workflow-heading p { margin-top: 10px; }
      .workflow-grid { grid-template-columns: 1fr; }
      .workflow-card:last-child { grid-column: auto; }
      footer { display: block; margin-top: 56px; }
      footer span { display: block; margin-top: 6px; }
      .modal-backdrop { align-items: end; padding: 12px; }
      .modal { padding: 20px; }
      .modal-actions { display: grid; grid-template-columns: 1fr 1fr; }
    }
    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#workspace">跳到錄音工作台</a>
  <div class="app-shell">
    <nav class="topbar" aria-label="主要導覽">
      <a class="brand" href="#top" aria-label="Live Transcript 首頁">
        <span class="brand-mark" aria-hidden="true">LT</span>
        <span>Live Transcript</span>
      </a>
      <div class="nav-links">
        <a href="#workspace">工作台</a>
        <a href="#transcriptPanel">逐字稿</a>
        <a href="#howItWorks">使用流程</a>
      </div>
      <span class="privacy-badge">On-device AI</span>
    </nav>

    <header id="top" class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Private meeting workspace</p>
        <h1>讓每場會議<br><span>留下清楚脈絡。</span></h1>
        <p class="lede">
          在自己的 Mac 上即時辨識中文與英文，把你和對方的聲音整理成可保存、可下載的逐字稿。
        </p>
        <div class="hero-meta" aria-label="產品特色">
          <div class="meta-chip"><strong>中英雙語</strong><span>中文、英文或自動偵測</span></div>
          <div class="meta-chip"><strong>雙音源</strong><span>麥克風與分頁音訊</span></div>
          <div class="meta-chip"><strong>本機辨識</strong><span>模型只在你的 Mac 運行</span></div>
        </div>
      </div>
      <aside class="status-console soft-card" aria-label="系統即時狀態">
        <div>
          <p class="section-label">System status</p>
          <h2>現在可以開始嗎？</h2>
          <p class="console-note">狀態會隨模型載入與錄音即時更新。</p>
        </div>
        <div class="status-stack">
          <div id="modelStatus" class="status" role="status">
            <span class="dot" aria-hidden="true"></span>
            <span id="modelStatusText">模型狀態：連線中…</span>
            <span class="status-loader" aria-hidden="true"></span>
          </div>
          <div id="status" class="status" role="status">
            <span class="dot" aria-hidden="true"></span>
            <span id="statusText">服務狀態：正在連線…</span>
            <span class="status-loader" aria-hidden="true"></span>
          </div>
        </div>
      </aside>
    </header>

    <main>
      <section id="workspace" class="workspace" aria-label="逐字稿工作區">
        <aside id="controls" class="controls soft-card" aria-label="錄音控制">
          <div class="control-bar">
            <div>
              <p class="section-label">Capture setup</p>
              <h2>錄音工作台</h2>
            </div>
            <button id="compactToggle" class="compact-toggle secondary" type="button" aria-expanded="true">縮小</button>
          </div>

          <div class="control-content">
            <p id="secureNotice" class="notice" hidden>
              瀏覽器只允許 HTTPS 網頁使用麥克風。請改用 HTTPS 網址。
            </p>

            <fieldset>
              <legend>音訊來源</legend>
              <div class="sources">
                <label class="source-option">
                  <input id="microphone" type="checkbox" checked>
                  <span class="source-copy"><strong>麥克風</strong><small>你的聲音</small></span>
                </label>
                <label class="source-option">
                  <input id="systemAudio" type="checkbox">
                  <span class="source-copy"><strong>分頁音訊</strong><small>會議另一端</small></span>
                </label>
              </div>
            </fieldset>

            <div class="form-grid">
              <div class="field">
                <div class="field-row">
                  <label class="field-label" for="language">辨識語言</label>
                  <span class="language-hint">下一次錄音套用</span>
                </div>
                <select id="language">
                  <option value="zh">中文</option>
                  <option value="en">英文</option>
                  <option value="auto">Auto</option>
                </select>
              </div>

              <div class="field name-row">
                <div class="field-row">
                  <label class="field-label" for="sessionName">逐字稿名稱</label>
                  <span id="nameState" class="name-state"></span>
                </div>
                <div class="name-tools">
                  <input id="sessionName" type="text" maxlength="80" placeholder="留空會自動命名" autocomplete="off">
                  <button id="resetSessionName" class="secondary" type="button">重置</button>
                </div>
              </div>
            </div>

            <div class="actions">
              <button id="start" type="button" disabled>開始遠端錄音</button>
              <button id="modelToggle" class="secondary" type="button">啟用模型</button>
              <button id="stop" class="secondary" type="button" disabled>停止錄音</button>
              <button id="clearTranscript" class="danger" type="button" disabled>清除畫面</button>
            </div>

            <div class="download-row">
              <a id="download" class="download" href="#" hidden>下載 Markdown</a>
              <a id="downloadJsonl" class="download" href="#" hidden>下載 JSONL</a>
            </div>

            <details class="helper">
              <summary>錄音前提醒</summary>
              <p>
                先啟用模型，再開始錄音。分享分頁時請勾選「分享分頁音訊」。停止後會完成辨識、保存檔案並釋放模型記憶體。
              </p>
            </details>
          </div>
        </aside>

        <section id="transcriptPanel" class="transcript-panel soft-card" aria-labelledby="transcriptTitle">
          <div class="panel-header">
            <div>
              <p class="section-label">Live feed</p>
              <h2 id="transcriptTitle">即時逐字稿</h2>
            </div>
            <span id="transcriptCount" class="feed-count">等待內容</span>
          </div>
          <div id="processingNotice" class="processing-notice" role="status" aria-live="polite" aria-busy="true" hidden>
            <span class="processing-spinner" aria-hidden="true"></span>
            <div>
              <strong>正在完成剩餘辨識</strong>
              <span>模型仍在處理排隊中的音訊；完成保存前無法卸載模型或下載檔案。</span>
            </div>
          </div>
          <section id="transcript" class="transcript" aria-live="polite"></section>
          <div id="empty" class="empty">
            <div class="empty-content">
              <span class="empty-icon" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
              <strong>等待第一段語音</strong>
              <p>辨識完成的內容會依時間順序出現在這裡。</p>
            </div>
          </div>
        </section>
      </section>

      <section id="howItWorks" class="workflow" aria-labelledby="workflowTitle">
        <div class="workflow-heading">
          <div>
            <p class="section-label">Three simple steps</p>
            <h2 id="workflowTitle">開始一場清楚的會議記錄</h2>
          </div>
          <p>模型按需載入，錄音結束後自動保存並釋放資源，讓常駐服務維持輕量。</p>
        </div>
        <div class="workflow-grid">
          <article class="workflow-card soft-card">
            <span class="step-number">01</span>
            <h3>啟用模型</h3>
            <p>需要錄音時再載入 large-v3-turbo，狀態列會清楚顯示載入進度。</p>
          </article>
          <article class="workflow-card soft-card">
            <span class="step-number">02</span>
            <h3>選擇音源</h3>
            <p>使用麥克風記錄自己，也能加入分頁音訊捕捉會議另一端。</p>
          </article>
          <article class="workflow-card soft-card">
            <span class="step-number">03</span>
            <h3>保存與下載</h3>
            <p>停止錄音後完成剩餘辨識，並提供 Markdown 與 JSONL 檔案。</p>
          </article>
        </div>
      </section>
    </main>

    <footer>
      <strong>Live Transcript</strong>
      <span>Private, bilingual meeting notes on your Mac.</span>
    </footer>
  </div>

  <div id="modalBackdrop" class="modal-backdrop" hidden>
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle" aria-describedby="modalMessage">
      <p class="section-label">Action required</p>
      <h2 id="modalTitle">需要確認</h2>
      <p id="modalMessage" class="modal-message"></p>
      <div class="modal-actions">
        <button id="modalCancel" class="secondary" type="button">取消</button>
        <button id="modalConfirm" type="button">確認</button>
      </div>
    </section>
  </div>
  <script>
    const transcript = document.querySelector("#transcript");
    const empty = document.querySelector("#empty");
    const transcriptCount = document.querySelector("#transcriptCount");
    const processingNotice = document.querySelector("#processingNotice");
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
    const modalBackdrop = document.querySelector("#modalBackdrop");
    const modalDialog = modalBackdrop.querySelector(".modal");
    const modalTitle = document.querySelector("#modalTitle");
    const modalMessage = document.querySelector("#modalMessage");
    const modalCancel = document.querySelector("#modalCancel");
    const modalConfirm = document.querySelector("#modalConfirm");
    let serverState = "starting";
    let modelState = "unloaded";
    let modelName = "";
    let transcriptHasItems = false;
    let transcriptFinishing = false;
    let audioSocket = null;
    let audioContext = null;
    let silentOutput = null;
    let captureStreams = [];
    let audioNodes = [];
    let currentSession = null;
    let renameTimer = null;
    let nameResetForNextRecording = false;
    let modalResolver = null;
    let modalPreviousFocus = null;
    const remoteAudioEnabled = __REMOTE_AUDIO_ENABLED__;
    const modelControlEnabled = __MODEL_CONTROL_ENABLED__;
    language.value = "__DEFAULT_LANGUAGE__";

    function updateTranscriptCount() {
      const count = transcript.childElementCount;
      transcriptCount.textContent = count ? `${count} 段內容` : "等待內容";
    }

    function setEmptyState(title, description) {
      const content = document.createElement("div");
      content.className = "empty-content";
      const heading = document.createElement("strong");
      heading.textContent = title;
      const detail = document.createElement("p");
      detail.textContent = description;
      content.append(heading, detail);
      empty.replaceChildren(content);
      empty.hidden = false;
      updateTranscriptCount();
    }

    function closeModal(result) {
      if (modalBackdrop.hidden) return;
      modalBackdrop.hidden = true;
      document.body.style.overflow = "";
      const resolve = modalResolver;
      modalResolver = null;
      if (modalPreviousFocus instanceof HTMLElement) modalPreviousFocus.focus();
      modalPreviousFocus = null;
      if (resolve) resolve(result);
    }

    function showDialog({ title, message, confirmLabel, showCancel, destructive = false }) {
      if (modalResolver) closeModal(false);
      modalPreviousFocus = document.activeElement;
      modalTitle.textContent = title;
      modalMessage.textContent = message;
      modalConfirm.textContent = confirmLabel;
      modalConfirm.classList.toggle("danger", destructive);
      modalCancel.hidden = !showCancel;
      modalBackdrop.hidden = false;
      document.body.style.overflow = "hidden";
      requestAnimationFrame(() => modalConfirm.focus());
      return new Promise(resolve => { modalResolver = resolve; });
    }

    function showAlert(message, title = "請注意") {
      return showDialog({ title, message, confirmLabel: "知道了", showCancel: false });
    }

    function showConfirm(message, title = "確認操作") {
      return showDialog({
        title,
        message,
        confirmLabel: "確認清除",
        showCancel: true,
        destructive: true,
      });
    }

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
      const busy = data.state === "processing";
      status.setAttribute("aria-busy", String(busy));
      status.classList.toggle("live", data.state === "recording");
      status.classList.toggle("busy", busy);
      status.classList.toggle("ended", data.state === "stopped" || data.state === "error");
      updateControls();
    }

    function setProcessingState(processing) {
      transcriptFinishing = processing;
      processingNotice.hidden = !processing;
      processingNotice.setAttribute("aria-busy", String(processing));
      updateControls();
    }

    function setModelStatus(data) {
      modelState = data.state;
      modelName = data.name || modelName;
      modelStatusText.textContent = `模型：${data.message}`;
      const busy = data.state === "loading" || data.state === "unloading";
      modelStatus.setAttribute("aria-busy", String(busy));
      modelStatus.classList.toggle("model-ready", data.state === "loaded");
      modelStatus.classList.toggle("busy", busy);
      modelStatus.classList.toggle("ended", data.state === "unloaded" || data.state === "error");
      updateControls();
    }

    function updateControls() {
      const capturing = audioSocket !== null;
      const serverReady = serverState === "waiting" || serverState === "recording";
      const serviceBusy = serverState === "processing";
      const modelBusy = modelState === "loading" || modelState === "unloading";
      startButton.disabled =
        capturing || transcriptFinishing || !serverReady ||
        modelState !== "loaded" || !window.isSecureContext;
      stopButton.disabled = !capturing;
      modelToggle.disabled =
        capturing || transcriptFinishing || serviceBusy || modelBusy || !modelControlEnabled;
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
      article.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "nearest",
      });
      updateTranscriptCount();
      updateControls();
    }

    function clearTranscriptDisplay() {
      transcript.replaceChildren();
      transcriptHasItems = false;
      setEmptyState("畫面已清除", "已保存的 Markdown 與 JSONL 檔案不受影響。");
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
        setEmptyState("等待第一段語音", "辨識完成的內容會依時間順序出現在這裡。");
      }
      currentSession = data;
      if (isNew || document.activeElement !== sessionName) {
        sessionName.value = data.name || "";
      }
      const saved = data.state === "saved";
      const finishing = data.state === "finishing";
      if (finishing) setProcessingState(true);
      if (saved || data.state === "error") setProcessingState(false);
      download.hidden = !saved || !data.download_url;
      downloadJsonl.hidden = !saved || !data.jsonl_url;
      if (data.download_url) download.href = data.download_url;
      if (data.jsonl_url) downloadJsonl.href = data.jsonl_url;
      nameState.textContent =
        transcriptFinishing
          ? "完成辨識中…"
          : data.state === "recording"
            ? "錄音中"
            : saved
              ? "已保存"
              : data.state === "error"
                ? "保存失敗"
                : "";
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
        showAlert("錄音進行中，請停止錄音後再重置下一場名稱。");
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
      if (transcriptFinishing) {
        await showAlert("剩餘音訊仍在辨識與保存中，完成前無法卸載模型。");
        return;
      }
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
        await showAlert(error instanceof Error ? error.message : "模型控制失敗");
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
      if (response.status === 409 && result.confirmation_required) {
        if (await showConfirm(result.message)) return requestTranscriptClear(true);
        return { ok: false, cancelled: true };
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
                stopCapture(true, false);
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
        await showAlert("請至少選擇一個音訊來源。");
        return;
      }
      if (modelState !== "loaded") {
        await showAlert("請先啟用模型並等待載入完成。");
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
            await showAlert(`${message}。本次將繼續只錄麥克風。`);
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

    async function stopCapture(closeSocket = true, showProcessing = closeSocket) {
      const socket = audioSocket;
      if (socket && showProcessing) {
        setProcessingState(true);
        setStatus({ state: "processing", message: "正在辨識剩餘音訊並保存逐字稿" });
        nameState.textContent = "完成辨識中…";
      }
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
    modalConfirm.addEventListener("click", () => closeModal(true));
    modalCancel.addEventListener("click", () => closeModal(false));
    modalBackdrop.addEventListener("click", event => {
      if (event.target === modalBackdrop) closeModal(false);
    });
    document.addEventListener("keydown", event => {
      if (modalBackdrop.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeModal(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...modalDialog.querySelectorAll("button:not([hidden]):not(:disabled)")];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    modelToggle.addEventListener("click", toggleModel);
    resetSessionName.addEventListener("pointerdown", () => {
      nameResetForNextRecording = true;
    });
    resetSessionName.addEventListener("click", resetNameForNextRecording);
    startButton.addEventListener("click", startCapture);
    stopButton.addEventListener("click", async () => {
      clearTimeout(renameTimer);
      setProcessingState(true);
      setStatus({ state: "processing", message: "正在辨識剩餘音訊並保存逐字稿" });
      nameState.textContent = "完成辨識中…";
      await renameSession();
      await stopCapture();
    });
    clearTranscriptButton.addEventListener("click", async () => {
      clearTranscriptButton.disabled = true;
      try {
        await requestTranscriptClear();
      } catch (error) {
        await showAlert(error instanceof Error ? error.message : "清除逐字稿失敗");
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
    updateTranscriptCount();
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
