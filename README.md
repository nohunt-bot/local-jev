# local-jev

用本機或機房的開放權重模型，跑 Jev 那種「封閉式問題 → 機率分佈」的判斷，讓 agent 用的 Jev 工具不必把內容送到雲端。

*Local, open-weight backend for Jev-style typed judgments: a FastMCP port of jev-mcp's eleven tools, and a System One-compatible endpoint for the unmodified jev-mcp. Docs in Traditional Chinese.*

Jev 是 TypeSafe 的判斷模型：把封閉式問題（是／否、單選、評分）直接回答成機率分佈。
[`@jkudish/jev-mcp`](https://www.npmjs.com/package/@jkudish/jev-mcp) 是個人開發者發布的 MIT 開源套件（套件沒有說明它和
TypeSafe 的關係），把 Jev 包成 11 個 MCP（Model Context Protocol）工具給 agent 使用，預設呼叫 TypeSafe 的雲端 API。

這個 repo 提供兩種地端用法，都向 OpenAI 相容的推論引擎（llama.cpp、vLLM、Ollama、MLX）要「下一個 token 的機率」
（logprobs），換算成 Jev 格式的答案：

1. **FastMCP 版**（`jev_fastmcp.py`，建議）：一個 Python MCP server，提供跟 jev-mcp 0.8.0 相同的 11 個工具
   （同樣的名稱、參數、題目和判定邏輯），在程式裡直接讀模型的機率。不需要 Node.js、jev-mcp 或另外的端點。
2. **jev-mcp + 相容端點**（`systemone_local.py`）：jev-mcp 不修改，用它的 `compatible` 模式改問這個本機端點
   （System One 相容端點：接受同樣 `{state, questions}` 請求、回傳同樣答案格式的 HTTP 服務）。

兩種用法共用同一套讀取程式。`parity_test.py` 把同一批輸入送進兩條路，要求送給引擎的請求和回傳的結果完全一樣。

> **狀態：研究用參考實作，不是正式服務。**
> 整合路徑已用模擬引擎驗證：11 個工具都回傳有效判定；模型沒照格式回答時（選項字母合計低於 0.5），11 個工具都安全失敗。
> FastMCP 版和 jev-mcp 0.8.0 在一致性測試裡（模擬引擎、兩種模式、43 種輸入加 15 種錯誤輸入）完全一致，回傳的文字逐字相同。
> **還沒接過真的推論引擎**，判斷品質、校準和速度都還沒測。

## 快速開始（Mac mini 等 Apple Silicon 的 Mac，FastMCP 版）

用 llama.cpp 跑 Qwen3-4B-Instruct-2507 的最短路徑；細節、其他引擎和疑難排解都在後面。
`brew` 和 `llama-server` 這兩段還沒在 Mac 上實測，其他指令都實際跑過。

**終端機 A**：安裝，然後啟動推論引擎（第一次會下載約 2.5 GB 的模型；之後這個終端機保持開著）

```bash
git clone https://github.com/nohunt-bot/local-jev.git && cd local-jev
brew install llama.cpp python
llama-server -hf bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M --port 8080 -c 8192 -ngl 99
```

`-c 8192` 限制 context 長度，避免預留太多記憶體；`-ngl 99` 把整個模型放到 GPU。

**終端機 B**：安裝 Python 套件、檢查引擎，再註冊到 Claude Code（只要做一次）

```bash
cd local-jev               # 到 repo 資料夾
python3 -m venv .venv && .venv/bin/pip install -r requirements-fastmcp.txt
export ENGINE_URL=http://127.0.0.1:8080/v1/chat/completions ENGINE_MODEL=local
.venv/bin/python check_engine.py    # 第 [1] 部分必須 PASS；第 [2] 部分怎麼看，見「接上推論引擎」
claude mcp add -s user jev-local \
  -e ENGINE_URL="$ENGINE_URL" \
  -e ENGINE_MODEL="$ENGINE_MODEL" \
  -- "$PWD/.venv/bin/python" "$PWD/jev_fastmcp.py"
claude mcp list            # jev-local 要顯示 Connected
```

之後在 Claude Code 裡就能用 `jev-local` 的 11 個工具（`jev_verify`、`jev_screen`、`jev_decide`……）；
引擎（終端機 A）要一直開著。記憶體 32 GB 以上可以換更大的模型，見「模型」。

## 檔案

| 檔案 | 用途 |
|---|---|
| `jev_fastmcp.py` | FastMCP 版：11 個工具的 MCP server（移植自 jev-mcp 0.8.0） |
| `jev_lib.py` | FastMCP 版的判定、門檻和答案檢查（移植自 jev-mcp 0.8.0） |
| `systemone_local.py` | 讀取模型機率的程式；單獨執行時是相容端點，提供 `POST /v1/systemone`。只用 Python 標準函式庫 |
| `check_engine.py` | 接真引擎前的檢查：候選 token 數，以及答不出來時字母拿到多少機率 |
| `fake_engine.py` | 模擬引擎：固定輸出，只用來測管線，不是模型 |
| `contract_test.py` | 相容端點的測試：用 `npx` 啟動真的 jev-mcp 0.8.0，逐一呼叫 11 個工具 |
| `parity_test.py` | 一致性測試：同樣的輸入送進 FastMCP 版和 jev-mcp + 端點，結果必須相同 |
| `example-request.json` | 相容端點的 API 範例請求 |
| `requirements-fastmcp.txt` | FastMCP 版的 Python 套件（fastmcp 4.0.10、mini-racer 0.14.1） |
| `LICENSES/` | jev-mcp 和 burnigtm/jev-mcp 的 MIT 授權全文（移植的程式碼要附上） |

## 需求

- Linux 或 macOS。Windows 請用 WSL：`contract_test.py` 用到 POSIX 的程序群組，下面的指令也是 bash 寫法。
- Python 3.10 以上（3.10、3.13 都跑過相容測試）。FastMCP 版要另外裝 `requirements-fastmcp.txt`，其中 mini-racer
  內建 V8，讓 `jev_extract` 用 JavaScript 跑正規表示式（有 macOS、glibc 2.27 以上的 Linux 和 Windows 的預編版本）；
  相容端點只用標準函式庫（「接上推論引擎」裡的 tokenizer 檢查需要 `transformers`）。
- 接真模型時：一個支援 `logprobs` 和 `top_logprobs` 的 OpenAI 相容推論引擎。
- 註冊用 Claude Code 的 `claude` 指令（其他 MCP 用戶端也可以）；相容端點的試打用 `curl`。
- Node.js 22 以上與 `npx`：只有 jev-mcp + 端點的做法，以及 `contract_test.py`、`parity_test.py` 需要
  （jev-mcp 0.8.0 要求 Node ≥ 22；第一次執行會從 npm 下載套件）。

所有指令都在 repo 資料夾裡執行。公司電腦如果設了 `http_proxy`／`HTTP_PROXY`，**每個**要用的終端機都先執行下面這行，
否則連本機引擎、`curl` 連端點時，都會被導去 proxy 而失敗：

```bash
export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}" NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
```

FastMCP 版是 Claude Code 啟動的，只看得到 Claude Code 自己的環境變數和註冊時的 `-e`；公司電腦有 proxy 時，
註冊時也加上 `-e no_proxy=127.0.0.1,localhost -e NO_PROXY=127.0.0.1,localhost`。

公司用內部 npm 鏡像時，請用 `~/.npmrc` 或小寫的 `npm_config_registry` 設定；相容測試不會把大寫的
`NPM_CONFIG_*` 傳給 `npx`。

## 接上推論引擎（兩種用法共用）

照下表 `export` 環境變數。**同一組值**要用在引擎檢查、測試，以及 MCP 註冊（FastMCP 版用 `-e` 傳進去）或啟動端點，
所以請在同一個終端機繼續做。`ENGINE_MODEL` 會當成 `model` 欄位送給引擎：vLLM 和 Ollama 會檢查這個名稱，
填錯會回 404；mlx_lm.server 會照這個名稱去載入模型。

| 引擎 | 啟動方式（例） | `ENGINE_URL` | `ENGINE_MODEL` | 其他 |
|---|---|---|---|---|
| llama.cpp | `llama-server -m <模型>.gguf --port 8080` | `http://127.0.0.1:8080/v1/chat/completions` | 任意（llama-server 不看） | `TOP_LOGPROBS` 可調到 26 |
| vLLM | `vllm serve <模型名稱> --port 8000` | `http://127.0.0.1:8000/v1/chat/completions` | 服務中的模型名稱 | `TOP_LOGPROBS` ≤ `max_logprobs`（預設 20） |
| Ollama | `ollama serve`，模型先 `ollama pull <標籤>` | `http://127.0.0.1:11434/v1/chat/completions` | 模型標籤 | `TOP_LOGPROBS` ≤ 20。官方文件標示 `/v1` 不支援 logprobs，但程式碼有傳遞，要先實測；拿不到就改用 llama.cpp |
| mlx_lm.server | `mlx_lm.server --model <模型>` | `http://127.0.0.1:8080/v1/chat/completions` | `default_model`（代表 `--model` 指定的模型），或跟 `--model` 完全相同的值 | **必須設 `TOP_LOGPROBS=11`**（上限 11，預設 20 會失敗）；名稱寫錯會回 404，或觸發下載、換模型 |

例如：

```bash
# llama.cpp
export ENGINE_URL=http://127.0.0.1:8080/v1/chat/completions ENGINE_MODEL=local

# mlx_lm.server
export ENGINE_URL=http://127.0.0.1:8080/v1/chat/completions ENGINE_MODEL=default_model TOP_LOGPROBS=11
```

推理型模型要關掉推理，讓模型直接作答。Qwen3-2507 的 Instruct 版本身不做推理；原版 Qwen3 在 vLLM 上要再
`export ENGINE_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`；其他引擎關掉推理的方式
請查該引擎的文件。

### 接上之後先檢查（相容測試抓不到這些問題）

```bash
python3 check_engine.py      # 已建 .venv 的話，用 .venv/bin/python 也可以
```

1. **第 [1] 部分必須 PASS**：引擎要回傳多個候選 token 的機率。如果只回被選中的那一個，每個答案都會是
   p=1 的假確定。回傳筆數比 `TOP_LOGPROBS` 少，代表引擎有自己的上限，`TOP_LOGPROBS` 要調到那個上限以下。
2. **看第 [2] 部分的數字**。它用 10 題答不出來的題目（內容裡沒有答案，每題 10 個選項 A–J）：
   - 「I 不是選項」那一行，是模型想用 “I …” 開頭說話（例如 “I cannot…”）的機率。選項 9 個以上時，
     這些機率會被讀成選 I。
   - A 的平均機率如果明顯高於其他字母，代表模型習慣猜第一個選項（位置偏誤），或想用 “A …” 開頭。

   答不出來的題目也被作答是常見的（prompt 要求只回一個字母），要看的是機率有沒有集中在 A 或 I。
   提高 `MIN_LABEL_MASS` 擋不掉這兩種情況：A、I 本身就是選項字母，它們的機率會算進字母合計。
   - I 偏高：選項 9 個以上的題目，答案不可靠。目前沒有設定能單獨修正（標籤 A–Z 寫死在程式裡）；可以把
     `TOP_LOGPROBS` 降到 8，讓 9 個以上選項的題目直接不答（代價是前幾名的名額更少），或改程式換一套標籤。
   - A 偏高：位置偏誤或 “A …” 開頭，也沒有設定能修，要靠校準或選項輪替（都還沒實作）。
3. **每個選項字母都是單一 token**，包含前面帶空白的寫法。這要用模型自己的 tokenizer 檢查，例如 Hugging Face
   `transformers`（先 `pip install transformers`；離線時把名稱換成本機存放模型的資料夾路徑）：

   ```python
   from transformers import AutoTokenizer
   tok = AutoTokenizer.from_pretrained("<模型在 Hugging Face 上的名稱>")
   for s in ["A", " A", "B", " B", "I", " I"]:
       print(repr(s), tok.encode(s, add_special_tokens=False))  # 每一行都應該只有一個 id
   ```

## 用法一：FastMCP 版

### 安裝與註冊

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-fastmcp.txt
claude mcp add -s user jev-local \
  -e ENGINE_URL="$ENGINE_URL" \
  -e ENGINE_MODEL="$ENGINE_MODEL" \
  -- "$PWD/.venv/bin/python" "$PWD/jev_fastmcp.py"
```

- 照「接上推論引擎」的表，把該引擎需要的變數都用 `-e` 傳進去，例如 mlx_lm.server 要再加 `-e TOP_LOGPROBS=11`。
  MCP server 是 Claude Code 啟動的，只看得到 Claude Code 自己的環境變數和註冊時的 `-e`；在終端機 `export` 的值要用 `-e` 傳進去。
- `-s user` 讓你所有的專案都能用 `jev-local`。拿掉的話，只會註冊在執行指令的那個資料夾。
- 已經用 jev-mcp 的做法註冊過 `jev-local`，先 `claude mcp remove jev-local -s user` 再註冊。
- `claude mcp list` 顯示 Connected 只代表 server 啟動成功；要真的拿到判斷，引擎必須一直開著。

其他 MCP 用戶端用同樣的設定（路徑換成 repo 的絕對路徑）：

```json
{
  "mcpServers": {
    "jev-local": {
      "command": "/絕對路徑/local-jev/.venv/bin/python",
      "args": ["/絕對路徑/local-jev/jev_fastmcp.py"],
      "env": {
        "ENGINE_URL": "http://127.0.0.1:8080/v1/chat/completions",
        "ENGINE_MODEL": "local"
      }
    }
  }
}
```

### 設定

引擎相關的環境變數跟相容端點相同（`ENGINE_URL`、`ENGINE_MODEL`、`TOP_LOGPROBS`、`MIN_LABEL_MASS`、
`ENGINE_EXTRA_BODY`、`ENGINE_TIMEOUT_S`，見「設定（環境變數）」），另外有兩個跟 jev-mcp 同名的設定：

| 變數 | 預設 | 說明 |
|---|---|---|
| `JEV_MCP_REQUEST_TIMEOUT_MS` | `60000` | 一次工具呼叫的期限（毫秒）。每次呼叫引擎前檢查，已經送出的那次呼叫不會被中斷（最多再花 `ENGINE_TIMEOUT_S`）；候選很多的 `jev_rerank` 在慢的引擎上可能要調高 |
| `JEV_MCP_MAX_ATTEMPTS` | `3` | 單次引擎呼叫遇到暫時性錯誤（連不上、HTTP 408／409／429／5xx）時的嘗試次數，1–6 |

設定不合法時（例如 `MIN_LABEL_MASS` 超出範圍），server 會拒絕啟動，`claude mcp list` 只會顯示連線失敗
（`Failed to connect`）。要看原因，直接執行註冊的指令，拒絕啟動的訊息會印在終端機，例如：

```bash
MIN_LABEL_MASS=0 .venv/bin/python jev_fastmcp.py
```

### 跟 jev-mcp 0.8.0 的差異

工具名稱、參數、送給模型的題目、判定邏輯和回傳的文字都相同。刻意不同的地方：

- 結果裡的 `provider` 是 `"local"`（`jev_extract` 完全沒呼叫模型時，跟 jev-mcp 一樣是 `"none"`），`model` 是 `ENGINE_MODEL`。
- 引擎呼叫失敗時只重試那一次呼叫；一直失敗就回工具錯誤，並寫出原因：`engine unreachable (…)`、
  `engine returned HTTP 404` 或 `engine reply had no usable logprobs (…)`。
- `JEV_MCP_REQUEST_TIMEOUT_MS` 在每次呼叫引擎前檢查，已經送出的那次呼叫不會被中斷；用戶端取消也不會中斷
  正在跑的引擎呼叫。
- `jev_extract` 的正規表示式跟 jev-mcp 一樣在 V8 裡執行（jev-mcp 用 Node 裡的 V8，這裡透過 mini-racer），
  比對結果和錯誤訊息跟 JavaScript 相同。mini-racer 的 V8 比 Node 22 新，少數較新的語法（例如 `(?i:…)`）
  這裡能用、Node 22 會報錯；逾時的時間點也可能略有不同。
- 字串長度的上限和截斷以 Unicode 字元計算，JavaScript 以 UTF-16 單位計算；只有表情符號這類字元會算得不同。
- 參數檢查：選填參數（包括項目裡選填的 `id`）也接受 `null`（視同沒填）；整數參數不接受 `5.0`，物件不接受
  `__proto__` 這個鍵，jev-mcp 兩者都接受（只有非 JavaScript 的用戶端會送出這些）。
- `jev_classify` 的 `by_class`：類別 id 剛好叫 `constructor`、`toString` 這類名稱時照常計數；jev-mcp 0.8.0 會算錯。
- 工具說明把「TypeSafe Jev」改成本機模型，拿掉 TypeSafe 的基準測試和「已校準」的說法，寫出這個 server
  一題最多幾個選項（超過會回 `invalid_response`），`jev_rerank` 另外註明每個候選是一次引擎呼叫。server 會送出
  MCP instructions，並把所有工具標成唯讀（`readOnlyHint`）、不連外（`openWorldHint` 為 false）。
- 參數裡有落單的 UTF-16 代理字元（lone surrogate）時，這個 server 不會回應（MCP Python SDK 會丟掉這種請求）；
  jev-mcp 會回應。

### 一致性測試

```bash
.venv/bin/python parity_test.py
```

需要 Node.js 22 以上（會用 `npx` 跑 jev-mcp 0.8.0 當對照）。它會：

- 用 Node 驗證移植的 JavaScript 行為：`toFixed`、數字轉字串、`Number()`、`JSON.stringify`、`trim`，以及
  119 個正規表示式樣式的比對結果和錯誤訊息；
- 比對兩邊的工具清單和參數；
- 把 58 種輸入（43 種正常、15 種錯誤）在兩種引擎模式下送進兩條路，比對送給引擎的請求、回傳的結果
  （文字要逐字相同，只遮掉 `provider`、`model` 的值）和錯誤；
- 確認 FastMCP 版的 stdout 只有 MCP 訊息，以及引擎掛掉時回的是寫出原因的工具錯誤。

最後一行是 `=== overall: PASS (0 failing) ===`、exit code 是 0。

## 用法二：jev-mcp + 相容端點

### 1. 先跑相容測試（不需要模型）

```bash
python3 contract_test.py
```

它會自己啟動模擬引擎，跑兩輪（正常作答、亂答），再做一次「引擎掛掉時不能被當成通過」的自我檢查。
最後一行是 `=== overall: PASS ===`、exit code 是 0，就代表整合路徑沒問題。套件下載過之後大約 5 秒。

### 2. 用真的引擎跑一次相容測試

先照「接上推論引擎」設好環境變數、做完檢查。`contract_test.py` 會沿用環境裡的 `TOP_LOGPROBS`、`MIN_LABEL_MASS`、
`ENGINE_EXTRA_BODY` 和 `ENGINE_TIMEOUT_S`，網址和模型名稱則用參數傳（`:?` 讓沒 export 時直接報錯，
不會改跑模擬引擎）：

```bash
python3 contract_test.py --engine-url "${ENGINE_URL:?}" --engine-model "${ENGINE_MODEL:?}"
```

這個模式只檢查 11 個工具都回傳有效判定，不評判斷品質。

### 3. 啟動端點

在設好環境變數的終端機執行（端點會沿用那些環境變數，預設只聽 `127.0.0.1:8787`，並佔住這個終端機）：

```bash
SYSTEMONE_TOKEN=change-me python3 systemone_local.py
```

**沒有模型也能走完整個流程**：先在另一個終端機執行 `python3 fake_engine.py`（模擬引擎，
聽 `127.0.0.1:8080`，也就是 `ENGINE_URL` 的預設值），再啟動端點。

在另一個終端機、同一個資料夾，用範例請求試打：

```bash
curl -s http://127.0.0.1:8787/v1/systemone \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d @example-request.json | python3 -m json.tool --no-ensure-ascii
```

`json.tool --no-ensure-ascii` 只是讓回應好讀；原始回應裡的中文是 `\uXXXX` 跳脫字元。

已經在跑的端點（包括別人做的相容端點）也可以直接測：
`python3 contract_test.py --endpoint http://127.0.0.1:8787/v1/systemone --token change-me`。

### 4. 在 Claude Code 註冊成 MCP 工具

```bash
claude mcp add -s user jev-local \
  -e JEV_PROVIDER=compatible \
  -e JEV_API_BASE_URL=http://127.0.0.1:8787/v1/systemone \
  -e JEV_API_KEY=change-me \
  -- npx -y @jkudish/jev-mcp@0.8.0
```

- `-s user` 讓你所有的專案都能用 `jev-local`。拿掉的話，只會註冊在執行指令的那個資料夾（預設的 local scope）。
- `JEV_API_KEY` 要跟 `SYSTEMONE_TOKEN` 相同。沒設 `SYSTEMONE_TOKEN` 時也要填一個非空的值（jev-mcp 要求）。
- `JEV_API_BASE_URL` 是完整網址，jev-mcp 會原樣 POST 到這裡。
- jev-mcp 每個請求的總期限預設 60 秒，包含重試（`JEV_MCP_REQUEST_TIMEOUT_MS`）；遇到 408、409、429 或 5xx
  會重試，總共最多 3 次（`JEV_MCP_MAX_ATTEMPTS`）。
- 用 `claude mcp list` 確認 `jev-local` 顯示 Connected。Connected 只代表 jev-mcp 啟動成功；要真的拿到判斷，
  第 3 步的端點和引擎必須一直開著。端到端的檢查用第 3 步的 `contract_test.py --endpoint …`。

其他 MCP 用戶端用同樣的設定：

```json
{
  "mcpServers": {
    "jev-local": {
      "command": "npx",
      "args": ["-y", "@jkudish/jev-mcp@0.8.0"],
      "env": {
        "JEV_PROVIDER": "compatible",
        "JEV_API_BASE_URL": "http://127.0.0.1:8787/v1/systemone",
        "JEV_API_KEY": "change-me"
      }
    }
  }
}
```

`change-me` 請換成自己產生的隨機字串，不要把 token 寫進會 commit 的檔案。

## 設定（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `SYSTEMONE_HOST` | `127.0.0.1` | 端點綁定的位址（只有相容端點用） |
| `SYSTEMONE_PORT` | `8787` | 端點綁定的 port（只有相容端點用） |
| `SYSTEMONE_TOKEN` | 未設 | 設了之後，請求要帶 `Authorization: Bearer <token>`，否則回 401（只有相容端點用） |
| `ENGINE_URL` | `http://127.0.0.1:8080/v1/chat/completions` | 推論引擎的 chat completions 網址 |
| `ENGINE_MODEL` | `local` | 送給引擎的模型名稱 |
| `TOP_LOGPROBS` | `20` | 向引擎要幾個候選 token，也是一題最多幾個選項（最多 26）；不可超過引擎本身的上限 |
| `MIN_LABEL_MASS` | `0.5` | 選項字母合計機率低於這個值就不作答；必須在 (0, 1] 之間 |
| `ENGINE_EXTRA_BODY` | 未設 | 合併進每個引擎請求的 JSON 物件，例如上面關掉推理的設定 |
| `ENGINE_TIMEOUT_S` | `20` | 每次引擎呼叫的逾時秒數（限制連線和每次讀取，不是整次呼叫的總時間） |
| `SYSTEMONE_LOG_TIMING` | 未設 | 設成 `1` 時，端點每個請求在 stderr 印一行耗時、引擎呼叫次數、答了幾題、放棄幾題 |

設定不合法時（`TOP_LOGPROBS` 小於 2、`MIN_LABEL_MASS` 超出範圍、`ENGINE_EXTRA_BODY` 不是 JSON 物件、
`ENGINE_TIMEOUT_S` 不是正數），端點和 FastMCP 版都會拒絕啟動。`check_engine.py` 用的是同一組變數和同一套檢查。
FastMCP 版另外的兩個設定見「用法一」。

## 運作方式

每一題都是一次引擎呼叫：

1. 把選項標成 A、B、C…，和內容（STATE）、題目組成 prompt，要求模型只回一個字母。
2. 用 `max_tokens=1`、`temperature=0` 呼叫引擎，讀第一個 token 的 `top_logprobs`。
3. 把同一個字母的不同寫法（`"A"`、`" A"`、`"ĠA"`、`"▁A"`）合併，只在選項字母之間重新正規化，得到機率分佈。
4. 選項字母合計低於 `MIN_LABEL_MASS` 時不回這一題；工具會回報 `invalid_response`，不會編出答案。

一共三種題型：`noul`（是／否，回傳「是」的機率）、`choice`（單選，回傳各選項機率、最可能的選項和信心）、
`score`（評分，回傳各級機率和期望分數）。11 個工具的判定、門檻和錯誤處理建在這三種題型上：
FastMCP 版在 `jev_fastmcp.py`／`jev_lib.py` 裡，另一種做法在 jev-mcp 裡；讀機率的部分兩者共用。

### 相容端點的 API 範例

請求就是 `example-request.json`：`state` 是要判斷的內容，`questions` 裡每一題有 `type`、`instructions`
和 `criteria`。下面是**模擬引擎**的回應，數字不代表真的判斷（已排版，並四捨五入到小數兩位）：

```json
{
  "model": "local",
  "answers": {
    "shipped":  {"type": "noul", "noul": 0.9},
    "category": {"type": "choice", "choice": "shipping", "confidence": 0.64,
                 "probabilities": {"shipping": 0.9, "refund": 0.05, "other": 0.05}},
    "risk":     {"type": "score", "score": 0.15, "confidence": 0.64,
                 "legend": {"0": "低", "1": "中", "2": "高"},
                 "probabilities": {"0": 0.9, "1": 0.05, "2": 0.05}}
  },
  "usage": {"input_tokens": 283, "output_tokens": 3}
}
```

`confidence` 是 1 − H(p)/ln K（均勻分佈為 0，完全確定為 1）；`score` 是 Σ i·pᵢ。
請求格式錯誤回 400，token 不對回 401；引擎連不上、回傳錯誤或回應缺少 logprobs 時回 502（jev-mcp 會重試）。
少數格式不對的引擎回應會讓連線直接中斷。不管哪一種，jev-mcp 都拿不到答案，不會編出判定。

## 疑難排解

| 症狀 | 常見原因 |
|---|---|
| FastMCP 版：`Local engine error: engine unreachable (…)`；端點：502 `engine unreachable` | 引擎沒啟動，或 `ENGINE_URL` 的主機、port 錯；`TOP_LOGPROBS` 超過引擎上限（mlx_lm.server 會直接斷線）；proxy 設定指向連不上的主機 |
| `engine returned HTTP 404` | `ENGINE_URL` 少了 `/v1/chat/completions`，或 `ENGINE_MODEL` 跟引擎服務中的模型名稱不符（vLLM、Ollama、mlx_lm.server） |
| `engine returned HTTP 400` | 引擎拒絕這個請求，常見原因是 `TOP_LOGPROBS` 超過上限 |
| `engine returned HTTP 403`／`407`／`502`／`503`／`504` | 常見是請求被公司 proxy 攔下或轉走：照「需求」設 `no_proxy`（FastMCP 版要在註冊時用 `-e` 傳） |
| FastMCP 版：`engine reply had no usable logprobs`；端點：502 `engine unreachable` | 引擎沒有回傳 logprobs（例如 Ollama 的 `/v1` 丟掉了這些欄位） |
| `Jev request exceeded the 60000ms deadline.` | 慢引擎加上很多題（例如候選多的 `jev_rerank`）；調高 `JEV_MCP_REQUEST_TIMEOUT_MS` |
| 401（端點） | 請求沒帶 token，或 `JEV_API_KEY` 跟 `SYSTEMONE_TOKEN` 不一樣 |
| 工具回 `invalid_response` | 模型沒照格式回答（字母合計低於 `MIN_LABEL_MASS`），或選項數超過上限 |
| `claude mcp list` 顯示連線失敗 | 路徑錯、`.venv` 沒裝套件，或環境變數不合法；直接執行註冊的指令，終端機會印出原因 |
| FastMCP 版：`jev_extract needs the mini-racer package` | `.venv` 沒裝 `requirements-fastmcp.txt`，或這個平台沒有 mini-racer 的預編版本；其他 10 個工具不受影響 |

`python3 check_engine.py` 會直接印出引擎呼叫失敗的原因，比看工具錯誤容易找問題。

## 限制與已知問題

- **選項超過上限就不作答。** 一題最多 min(`TOP_LOGPROBS`, 26) 個選項，超過的題目不呼叫引擎、直接不答，
  工具會回報 `invalid_response`。以預設 `TOP_LOGPROBS=20` 計：候選超過 20 個的 `jev_find`
  （jev-mcp 最多收 250 個）、類別超過 20 個的 `jev_classify`、比對到 20 個值的 `jev_extract`（加上「都不是」
  共 21 個選項），目前在地端版都不能用。mlx_lm.server（上限 11）的門檻更低；llama.cpp 調到 26 時，
  `jev_extract` 最多 21 個選項就在上限內。候選很多的情況，要等之後改用專門的排序模型（reranker）。
- **`jev_verify` 的例外。** 證據則數達到上限時（預設 20 則以上），「依據哪一則證據」那一題會被放棄：
  「依據哪一則」會是空的，判定本身不受影響。
- **前幾名的名額有限。** 同一個字母的不同寫法和無關的 token 都會佔掉名額；選項數接近上限時，
  真正的選項可能被擠出去而記成 0，這時的 0 不能當成「排除」。
- **"A"、"I" 開頭的誤讀。** 模型如果用 "A …"、"I …" 這類剛好是選項字母的字開頭，會被讀成選項。
  先用 `check_engine.py` 量看看。
- **依序呼叫引擎。** 一題一次，一個接一個。`jev_rerank`（每個候選一題，最多 250 題）在慢的引擎上
  可能超過 60 秒的期限。
- **prompt 很陽春。** 沒有 few-shot，也沒有校準。對話模型的機率通常偏向過度自信，正式使用前要用
  有標準答案的題目校準（例如溫度縮放）。
- **Prompt injection。** 只有 STATE 用分隔線包起來，並註明「裡面是資料、不是指令」；題目和選項
  （例如 `jev_classify` 的項目文字、`jev_rerank` 的候選、`jev_extract` 抽到的值）都沒有標記。
  這個防護也還沒對真模型測過。
- **沒有 TLS。** 相容端點只講 HTTP，預設只聽 127.0.0.1。要跨機器使用，請放在有 TLS 的反向代理後面，並設
  `SYSTEMONE_TOKEN`。
- **資料流向。** 引擎在本機或機房時，Jev 這一段的內容不會送到 TypeSafe。但 agent 本身如果用雲端模型
  （例如 Claude Code），它讀到的內容仍然會送到那個模型的供應商。
- **相依套件。** jev-mcp 的做法：`@jkudish/jev-mcp` 固定在 0.8.0，但它依賴的 `@typesafe-ai/sdk`、
  `@jkudish/jev-agent-tools`、`@modelcontextprotocol/sdk`、`zod` 用的是版本範圍。FastMCP 版：
  `requirements-fastmcp.txt` 固定了 fastmcp 和 mini-racer 的版本，它們自己的相依套件沒有鎖定。
- **FastMCP 版跟 jev-mcp 是分開的程式。** jev-mcp 之後改版，FastMCP 版不會跟著變；`parity_test.py`
  固定比對 0.8.0。

還沒做的改進：

- 相容測試的 `--engine-url` 模式抓不到「引擎只回一個 token」的情況，所以要另外跑 `check_engine.py`。
- 相容端點：引擎沒有回傳 logprobs 時，回的是籠統的 502 `engine unreachable`；少數格式不對的引擎回應
  （例如 `top_logprobs` 裡的項目不是物件）會讓端點直接斷線，而不是回 502。FastMCP 版會回寫出原因的工具錯誤。
- `TOP_LOGPROBS`、`SYSTEMONE_PORT`、`MIN_LABEL_MASS` 填了非數字時，會印 traceback，而不是清楚的錯誤訊息。
- `TOP_LOGPROBS` 沒有自動替同一個字母的不同寫法多留名額。
- `contract_test.py` 只把小寫的 `npm_config_*` 傳給 jev-mcp（並濾掉名稱含 auth、token、password 的）。
  大寫的 `NPM_CONFIG_*`（例如 `NPM_CONFIG_REGISTRY`）完全不會傳，所以用大寫環境變數設定的公司內部 npm 鏡像，
  相容測試裡的 `npx` 看不到，請改用 `~/.npmrc` 或小寫的 `npm_config_registry`。`npm_config_key` 則會被傳過去。

## 模型

用不會先「推理」再回答的 Instruct 模型（這套做法只讀第一個字）。依機器的記憶體選
（Mac 可以用 `system_profiler SPHardwareDataType | grep Memory` 查）：

| 記憶體 | 建議模型 | 模型檔大小（推估） |
|---|---|---|
| 8 GB | Qwen3-4B-Instruct-2507，Q4_K_M | 約 2.2 GB；放得下，但很緊，先關掉其他大程式 |
| 16 GB／24 GB | Qwen3-4B-Instruct-2507，Q4_K_M 或 Q8_0 | 約 2.2–4 GB |
| 32 GB 以上 | Qwen3-30B-A3B-Instruct-2507，Q4_K_M | 約 17–19 GB |

- 兩個模型都是 Apache-2.0。GGUF 檔是第三方轉檔，例如
  [bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF)；
  也可以手動下載 `.gguf`，改用 `llama-server -m <檔案>.gguf --port 8080`。
- 避開檔名 `UD-` 開頭的量化檔：有 llama.cpp 版本載入時會 crash 的回報
  （[ggml-org/llama.cpp#18287](https://github.com/ggml-org/llama.cpp/issues/18287)）。
- 16 GB 也可以改用原版 Qwen3-8B／14B（Q4），但要關掉推理（方式依引擎而定，見「接上推論引擎」）。
- 這些都還沒實測：正式使用前，先用幾十題有標準答案的題目量準確率和校準，再決定要不要用。

## 授權

- `jev_fastmcp.py` 和 `jev_lib.py` 移植自 `@jkudish/jev-mcp` 0.8.0，適用它的 MIT 授權（Copyright (c) 2026 Joey Kudish）；
  `jev_review`／`jev_gate` 的題目設計在上游改編自 burnigtm/jev-mcp（MIT，Copyright (c) 2026 jev-mcp contributors）。
  授權全文在 `LICENSES/jev-mcp.txt` 和 `LICENSES/burnigtm-jev-mcp.txt`，複製或散布這兩個檔案時要一併附上。
- repo 其他部分目前沒有授權檔。沒有授權時預設保留所有權利：別人可以瀏覽程式碼，但沒有取得複製、修改
  或再散布的權利。要讓別人使用，請先加上授權。
- 執行時依賴的 `@jkudish/jev-mcp`（MIT）由 `npx` 從 npm 下載，fastmcp（Apache-2.0）和 mini-racer（ISC，內含 V8，
  BSD-3-Clause）由 pip 安裝，都不包含在這個 repo 裡。

## 版本

- 2026-09-26：新增 FastMCP 版（fastmcp 4.0.10、mini-racer 0.14.1），與 jev-mcp 0.8.0 的一致性測試通過。
- 2026-09-25：相容端點與檢查工具，對應 `@jkudish/jev-mcp@0.8.0`。
