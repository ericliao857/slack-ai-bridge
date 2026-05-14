# Slack AI Bridge CLI 附件支援計畫

## 目標

讓 Slack AI Bridge 能把 Slack 訊息中的檔案安全地提供給本機 Claude Code、Codex CLI、GitHub Copilot CLI 作為上下文，同時維持目前 read-only、project allowlist、non-interactive CLI 的安全邊界。

## 依據

- 技術研究報告：`.agents/reports/technical-docs/20260514_claude_codex_copilot_cli_file_attachments.md`
- 目前 bridge 行為：
  - `bridge.py` 以 `subprocess.run(..., shell=False)` 呼叫 CLI。
  - Codex 使用 `--json --output-last-message`，Claude 使用 `--print`，Copilot 使用 `--prompt`。
  - 目前已有限制寫入意圖、外部路徑讀取、project allowlist、`project=all` 寬讀取模式。

## 關鍵判斷

| 工具 | 文字/程式檔 | 圖片 | 計畫中的處理方式 |
|---|---|---|---|
| Claude Code | `@path` 官方確認會 include file content | prompt 可提供圖片 path | 文字檔用 `@relative/path`；圖片用明確 path 放入 prompt |
| Codex CLI | 官方未確認一般文字附件 flag | `-i/--image` 官方確認 | 文字檔用 prompt 指定受控路徑；圖片用 `--image` |
| Copilot CLI | 互動模式 `@relative/path` 官方確認 | CLI 圖片附件未確認 | 文字檔先用 `@relative/path`；圖片 MVP 暫不支援 |

## MVP 範圍

- 只支援 Slack bot mention / thread message 中已存在的 file payload。
- 只下載 Slack 檔案到 project 內的受控暫存目錄，例如 `.slack-ai-bridge/attachments/<command-id>/`。
- 只把受控暫存目錄中的檔案暴露給 CLI。
- 支援文字類檔案與 Codex/Claude 圖片檔。
- 不把附件內容寫進 audit log 或 command log。
- 不支援 Slack slash command 直接附檔，除非後續確認 Slack payload 能穩定帶 file metadata。

## 非目標

- 不支援讓 CLI 任意讀取 Slack 原始下載路徑、使用者 home、Downloads 或 repo 外任意位置。
- 不支援 Copilot CLI 圖片附件，除非另行用官方 CLI help 或實測確認。
- 不支援 PDF、Office 文件、壓縮檔的內容解析。
- 不支援讓 AI CLI 修改或產出附件檔案。
- 不新增跨 workspace 的全域附件 cache。

## 設計計畫

### 1. Attachment model

新增最小資料模型，保存下載後的附件資訊：

- Slack file id
- 原始檔名
- sanitize 後檔名
- MIME type
- byte size
- local path
- attachment kind：`text`、`image`、`unsupported`

成功條件：
- 相同檔名不覆蓋，必要時加短 hash。
- local path 一定在受控 attachment root 內。

### 2. Slack file download

在 Socket Mode event path 中讀取 `event.files`，使用 bot token 下載檔案。

必要限制：
- 只接受 Slack 官方 file URL。
- 使用 `Authorization: Bearer <bot-token>`。
- 單檔大小上限先設小，例如 1 MB 文字、5 MB 圖片。
- MIME allowlist：文字、Markdown、JSON、YAML、常見程式碼、PNG、JPEG。
- 下載失敗時回覆使用者「附件無法讀取」，不呼叫 CLI。

成功條件：
- 無 file payload 時維持現有行為。
- file payload 存在但不支援時，給明確拒絕原因。

### 3. Prompt composition

在原始 Slack prompt 後追加一段內部附件說明，不改使用者原文：

```text
Attached files available in this task:
- original.txt: @.slack-ai-bridge/attachments/<id>/original.txt
- screenshot.png: .slack-ai-bridge/attachments/<id>/screenshot.png
```

依工具產生不同 prompt/args：

- Claude：
  - 文字：加入 `@relative/path`
  - 圖片：加入 `Analyze image at relative/path`
- Codex：
  - 文字：加入「Read the file at relative/path」
  - 圖片：加入 `--image relative/path`
- Copilot：
  - 文字：加入 `@relative/path`
  - 圖片：MVP 回覆不支援

成功條件：
- prompt 中只出現受控相對路徑。
- Codex 圖片參數不經 shell 字串拼接，直接加入 argv list。

### 4. CLI runner integration

調整 `run_tool` / `run_codex` / `run_claude` / `run_copilot` 的介面，讓 caller 可傳入 attachment metadata。

建議最小改法：

- 新增 `attachments: Tuple[AttachmentContext, ...] = ()`
- `cli_prompt(project, prompt, attachments, tool_name)` 負責產生工具特定 prompt
- `run_codex` 額外把圖片附件轉成 `--image`

成功條件：
- 沒有 attachments 時，現有測試與 CLI args 不變。
- 有 attachments 時，只增加必要 prompt 或 args。

### 5. Safety and cleanup

- 下載前檢查檔名 sanitize。
- 下載後再次確認 resolved path 在 attachment root 內。
- AI CLI 回覆完成後刪除該 command 的 attachment 目錄。
- timeout 或 exception 也要清理。
- command log 若啟用，只記錄附件數量與類型，不記錄內容。

成功條件：
- 清理不影響其他 command 的附件。
- 即使 CLI timeout，也不留下附件檔。

## 測試計畫

### Unit tests

- `sanitize_attachment_filename` 阻擋 path traversal、Windows drive path、空檔名。
- `classify_attachment` 正確分類文字、圖片、不支援 MIME。
- 下載目的路徑必須在受控 root 內。
- Claude 文字附件 prompt 產生 `@relative/path`。
- Codex 圖片附件 argv 產生 `--image relative/path`。
- Codex 文字附件不產生 `--image`，只在 prompt 指定 path。
- Copilot 文字附件 prompt 產生 `@relative/path`。
- Copilot 圖片附件被拒絕。
- 無附件時既有 CLI args 完全不變。
- CLI timeout 後附件目錄被清理。

### Integration-style tests

- 模擬 Socket Mode event 含 `files`，確認會下載、呼叫對應 CLI、回覆 Slack。
- 模擬 Slack file download 失敗，確認不呼叫 CLI。
- 模擬超過大小限制，確認回覆拒絕。
- 模擬 prompt 要求讀取外部路徑，仍由既有 preflight 阻擋。

## 實作順序

1. 新增 attachment 資料模型、檔名 sanitize、MIME/大小分類。
2. 補 unit tests，先不接 Slack download。
3. 實作 Slack file download helper，並加 mock 測試。
4. 接到 Socket Mode event flow，但沒有附件時維持現行流程。
5. 調整工具 prompt/args 組裝，先支援 Claude/Codex/Copilot 文字檔。
6. 加上 Codex 圖片 `--image` 與 Claude 圖片 prompt path。
7. 補清理流程與 timeout 測試。
8. 更新 README / README.zh-TW，明確列出各工具支援矩陣。

## 需要先確認的問題

- Slack slash command 是否要支援附件？目前假設不支援，MVP 只做 bot mention / thread files。
- 附件暫存目錄要放在 project root 下，還是 bridge repo 自己的 `logs/tmp` 下？建議放 project root 下，方便 CLI 以 project-relative path 讀取。
- 文字檔大小上限要多大？建議 MVP 先 1 MB，避免一次把大型 log 丟進 context。
- Copilot `--prompt` 非互動模式是否穩定展開 `@relative/path`？建議實作前用本機 CLI 做一次最小實測。

## 驗收條件

- 使用者在 Slack thread 上傳一個文字檔並 mention bot，Claude / Copilot 可根據該檔回答。
- 使用者上傳一張 PNG/JPEG 並使用 Codex，Codex 以 `--image` 收到該圖片。
- 不支援的檔案類型有明確拒絕訊息，且不呼叫 AI CLI。
- 附件永遠不會突破 project allowlist 或 `project=all` 的既有安全語意。
- 全部新增與相關既有 tests 通過：`python -m unittest discover -s tests`。
