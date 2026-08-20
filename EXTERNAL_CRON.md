# 外部 cron 準時觸發設定

GitHub Actions 免費方案的 `schedule` 會延遲 20~40 分鐘。要準時（誤差 1~2 分鐘），
改由外部 cron 服務打 GitHub API 觸發 workflow。

GitHub 自身的 schedule **保留當備援**：外部 cron 若準時發過，
備援那班會靠 `state/report_state.json` 去重、自動略過，不會重複發 LINE。

## 1. 建立 GitHub PAT（你自己做，token 不要貼進任何對話）

GitHub → Settings → Developer settings → Personal access tokens →
**Fine-grained tokens** → Generate new token

| 欄位 | 設定 |
|---|---|
| Repository access | Only select repositories → `Yingchii/bitget-funding` |
| Permissions → Actions | **Read and write** |
| Expiration | **No expiration**（本專案採用，見下方取捨） |

只給這一個 repo、只給 Actions 權限，外洩時的損失就侷限在「別人能觸發或取消這個
workflow、刪執行紀錄」——改不了程式碼（沒給 Contents 權限）、偷不到 LINE token
（Secrets 加密且 log 遮罩）、更碰不到交易所金鑰（金鑰在本機，從不上雲）。

**為什麼選永久**：權限已限縮到最小，最壞情況是被惡意觸發多收幾則 LINE，屬騷擾
等級；相對地設到期日就得每隔幾個月重設，忘記會默默失效——那個麻煩比風險更實際。
代價是要顧好 cron-job.org 帳號（token 存在那裡，是唯一入口），token 字串別外流，
不用時記得回 GitHub 按 Delete。

## 2. 在 cron-job.org 建立 job

註冊免費帳號後 Create cronjob：

| 欄位 | 值 |
|---|---|
| URL | `https://api.github.com/repos/Yingchii/bitget-funding/actions/workflows/donchian-report.yml/dispatches` |
| Method | `POST` |
| Schedule | 每天 09:15，時區選 `Asia/Taipei` |

Headers：

```
Authorization: Bearer <你的 PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Request body：

```json
{"ref":"main","inputs":{"only_action":"true"}}
```

`only_action` 必須是**字串** `"true"`（GitHub API 的 inputs 只吃字串）。
漏掉它會變成每天發完整日報，就不是觸發式了。

成功的話 API 回 **204 No Content**（沒有回應內容是正常的）。
建議在 cron-job.org 開啟失敗通知，這樣它掛掉時你會知道。

## 3. 驗證

cron-job.org 存檔後按 "Run now"，然後看 GitHub Actions 有沒有出現一次
`workflow_dispatch` 觸發的執行；log 裡應該要有 `--only-action --state-file`。
訊號沒翻轉時它會印「（未發送）」，這是對的——平常就該安靜。

## 盤中預警要不要也改？

`donchian-intraday.yml` 同樣可以這樣觸發（URL 換成 `donchian-intraday.yml`、
body 不用 inputs）。它本身有 12 小時冷卻，重複觸發不會重複發報。
但免費 cron 服務多半限制最短間隔，且盤中預警本來就只是收盤訊號的提前提醒，
維持現狀（約 30 分鐘一輪）通常就夠。
