# Claude Code, Codex CLI, GitHub Copilot CLI 附檔案方式技術資料整理

## 解析後參數
- topic：Claude Code、OpenAI Codex CLI、GitHub Copilot CLI 如何把本地檔案或圖片加入 prompt/context
- max_sources：10
- min_priority：P0
- date_range：不限制；以 2026-05-14 可查到的官方文件為準
- language：繁體中文整理，英文官方文件來源
- source_types：official_docs
- notebooklm：false
- notebooklm_mode：不適用
- existing_notebook：不適用
- include_conflicts：true
- output_format：Markdown
- 假設與不確定：這裡的 Copilot CLI 指 GitHub 官方 `copilot` CLI，不是舊的 `gh copilot` 擴充；若使用者指的是舊工具，需要另查。

## 結論摘要
- Claude Code CLI：文字/程式檔可用 `@path` 引用，單檔會把完整內容放進對話；目錄引用提供 listing。圖片可拖放、從剪貼簿貼上，或在 prompt 中給圖片路徑。跨目錄用 `/add-dir` 或 `--add-dir`。
- Codex CLI：官方明確的「附件」能力是圖片，使用 `-i` 或 `--image`。文字/程式檔更接近「讓 Codex 在工作目錄讀檔」：在 TUI 可用 `@` 做 workspace fuzzy file search，或在 prompt 指定路徑；跨目錄用 `--add-dir`。
- GitHub Copilot CLI：互動模式可用 `@relative/path` 把單一檔案內容加入 prompt context；跨目錄用 `/add-dir`、`/cwd` 或程式化模式的 `--add-dir`。
- 對 Slack AI Bridge 來說，最穩的共同模式是：把 Slack 附件先落在受控工作目錄，再依 CLI 生成「明確路徑引用」；圖片對 Codex 可直接轉成 `--image/-i`，文字檔對 Claude/Copilot 可用 `@path`，對 Codex 則提示它讀指定路徑。

## 推薦閱讀順序
依「優先級 > 相關度 > 時效性 > 原創性 > 可讀性」排序。

## 來源清單
| 優先級 | 標題 | 作者/發布方 | 日期 | 適用版本 | URL | 100字內重點 | 分級理由 |
|---|---|---|---|---|---|---|---|
| P0 | Common workflows - Claude Code Docs | Anthropic | 未確認；查閱日 2026-05-14 | Claude Code | https://code.claude.com/docs/en/tutorials | 說明圖片加入、`@file`、`@dir`、MCP resource 引用；單檔會加入完整內容。 | 官方文件 |
| P0 | Interactive mode - Claude Code Docs | Anthropic | 未確認；查閱日 2026-05-14 | Claude Code CLI | https://code.claude.com/docs/en/interactive-mode | 說明 CLI 貼上圖片快捷鍵，貼上後以 `[Image #N]` chip 引用。 | 官方文件 |
| P0 | CLI reference - Claude Code Docs | Anthropic | 未確認；查閱日 2026-05-14 | Claude Code CLI | https://code.claude.com/docs/en/cli-reference | 說明 `cat file \| claude -p`、`--add-dir`、system prompt file flags。 | 官方文件 |
| P0 | Commands - Claude Code Docs | Anthropic | 未確認；查閱日 2026-05-14 | Claude Code CLI | https://code.claude.com/docs/en/commands | 說明 `/add-dir <path>` 可把工作目錄加入目前 session 的檔案存取範圍。 | 官方文件 |
| P0 | Codex CLI features | OpenAI | 未確認；查閱日 2026-05-14 | Codex CLI | https://developers.openai.com/codex/cli/features | 說明 TUI 可送 prompt/code snippets/screenshots、圖片 `-i/--image`、`@` fuzzy file search、`--add-dir`。 | 官方文件 |
| P0 | Codex CLI | OpenAI | 未確認；查閱日 2026-05-14 | Codex CLI | https://developers.openai.com/codex/cli | 說明 Codex CLI 可在選定目錄讀檔、改檔、執行 code。 | 官方文件 |
| P0 | Using GitHub Copilot CLI | GitHub | 未確認；查閱日 2026-05-14 | GitHub Copilot CLI | https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/overview | 說明 `@relative/path` 將檔案內容加入 prompt context，並有 `/add-dir`、`/cwd`。 | 官方文件 |
| P0 | GitHub Copilot CLI programmatic reference | GitHub | 未確認；查閱日 2026-05-14 | GitHub Copilot CLI | https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference | 說明 `-p` 非互動 prompt、`--add-dir`、`--allow-all-paths` 等路徑與權限選項。 | 官方文件 |
| P0 | Adding custom instructions for GitHub Copilot CLI | GitHub | 未確認；查閱日 2026-05-14 | GitHub Copilot CLI | https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions | 說明 `.github/copilot-instructions.md`、`.github/instructions`、`AGENTS.md` 會自動成為 context。 | 官方文件 |

## 主題整理

### 快速矩陣
| CLI | 單一文字/程式檔 | 目錄 | 圖片 | 非互動/腳本模式 | 跨工作目錄 | 注意 |
|---|---|---|---|---|---|---|
| Claude Code CLI | `@src/file.ts`；官方說會包含完整內容 | `@src/components/` 提供 listing | 拖放、剪貼簿貼上、或在 prompt 給圖片路徑 | `cat file \| claude -p "..."` | `/add-dir`、`--add-dir` | Desktop 的 attachment button 支援更多檔案，但那是 GUI surface |
| Codex CLI | TUI 中 `@` 可 fuzzy 搜尋並插入 workspace path；也可請 Codex 讀指定路徑 | 以工作目錄讀取/搜尋；`--add-dir` 擴充 roots | `codex -i screenshot.png "..."` 或 `codex --image img1.png,img2.jpg "..."` | `codex "prompt"`、`codex exec "prompt"` | `--cd`、`--add-dir` | 官方文件未明確說有「一般文字檔 attachment flag」 |
| GitHub Copilot CLI | `@config/file.yml`；官方說會把檔案內容加入 prompt context | 可讓 Copilot 在 trusted cwd 內讀寫；沒有查到 `@dir` 會展開內容的官方說法 | reviewed docs 未確認 CLI 圖片附件 | `copilot -p "..."`；可配 `--add-dir` | `/add-dir`、`/cwd`、`--add-dir` | `@` 範例使用相對路徑；trusted directory 是安全邊界 |

### Claude Code CLI
- 文字/程式檔：使用 `@` 引用檔案，例如 `Explain @src/utils/auth.js`。官方文件明確說這會把該檔完整內容加入對話。
- 目錄：使用 `@src/components/` 可取得目錄 listing 與 file information，不是遞迴加入所有檔案內容。
- 多檔：同一訊息可引用多個檔案，例如 `@file1.js and @file2.js`。
- 圖片：可拖放圖片、貼上剪貼簿圖片，或在 prompt 中給圖片路徑，例如 `Analyze this image: /path/to/image.png`。
- 腳本/非互動：可把檔案內容 pipe 進去，例如 `cat logs.txt | claude -p "explain"`。
- 跨目錄：用 `/add-dir <path>` 或 `claude --add-dir ../apps ../lib` 擴充可讀寫目錄。

### OpenAI Codex CLI
- 圖片是最明確的官方附件路徑：`codex -i screenshot.png "Explain this error"`；多圖可用 `codex --image img1.png,img2.jpg "Summarize these diagrams"`。
- 互動 TUI 可送 screenshots、code snippets，也可用 `@` 開啟 workspace root 的 fuzzy file search，選到後把 path 放進 message。
- 官方文件說 Codex 可讀、改、執行 selected directory 內的 code；因此文字檔最穩做法是把附件存在 workspace 內，再在 prompt 裡明確指定路徑。
- 跨目錄用 `--cd` 設工作根目錄，或用 `--add-dir` 暴露額外 writable roots。
- 未確認：官方文件沒有看到像 `--file notes.md` 這種「一般文字/文件附件」flag；不要把 image attachment 能力推論成所有檔案類型都支援。

### GitHub Copilot CLI
- 互動模式中用 `@` 加相對路徑，例如 `Explain @config/ci/ci-required-checks.yml` 或 `Fix the bug in @src/app.js`；官方明確說這會把檔案內容加入 prompt context。
- 輸入路徑時會有 matching paths，下拉選取後按 `Tab` 完成。
- 跨目錄：互動模式可 `/add-dir /path/to/directory`，或用 `/cwd`/`/cd` 切換目前工作目錄。
- 程式化模式：`copilot -p "..."` 可直接執行 prompt；`--add-dir=DIRECTORY` 可加入允許路徑。
- 自動 context：`.github/copilot-instructions.md`、`.github/instructions/**/*.instructions.md`、`AGENTS.md` 等 instruction files 會自動加入請求 context；這適合穩定規範，不適合作為一次性附件傳遞。

### Slack AI Bridge 實作含意
- 建議把 Slack 附件下載到 task-scoped、可清理的受控目錄，例如 workspace 下的暫存 attachments 目錄，再用相對路徑或 `--add-dir` 暴露給 CLI。
- 文字/程式附件：
  - Claude：`@relative/path` 或 `cat file | claude -p "..."`。
  - Codex：把檔案放在 `--cd` root 或 `--add-dir` 內，在 prompt 明確要求讀取該 path；TUI 可用 `@` path。
  - Copilot：`@relative/path`；非互動時搭配 `-p` 與必要的 `--add-dir`。
- 圖片附件：
  - Claude：可提供圖片 path；互動 CLI 也支援貼上/拖放。
  - Codex：優先用 `-i/--image`。
  - Copilot：本次官方 CLI 文件未確認圖片附件機制，建議先不要宣稱支援；若要支援需另查 GitHub Copilot SDK 或最新 CLI help。
- 安全：不要把 Slack 原始檔任意放到使用者 home 或 repo root；要做 path allowlist、檔名 sanitize、大小限制、mime/type 判斷，並把附件來源視為可能含 prompt injection 的不可信內容。

## 衝突與不確定
- 「附檔案」在三個工具的語意不同：Claude/Copilot 的 `@file` 明確是 context inclusion；Codex 的官方文字檔模式偏向 workspace file access，圖片才是明確 attachment。
- Claude Desktop 支援 attachment button 與拖放「images, PDFs, and other files」，但 CLI 文件確認的主要是 `@path`、圖片貼上/路徑、stdin。不要把 Desktop GUI 能力直接視為 CLI flag。
- GitHub Copilot CLI 官方文件沒有在本次來源中確認圖片附件；只確認文字檔 `@relative/path`。
- 來源多數沒有清楚顯示文件發布/更新日期；本報告以 2026-05-14 查閱到的官方內容為準。

## NotebookLM
未執行；使用者未指定 `notebooklm=true`。
