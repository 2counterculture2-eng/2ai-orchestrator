# 2AI プロジェクト — システム指示

## このプロジェクトの目的
N体のAIエージェントが自律的に稼働し、卓磨さんの口座に継続的に収益を入れ続けるシステムを構築・運用する。

## 最重要原則（ai_system_core.md より）
1. AIの数は制限なし — Claude自身が最適数を判断
2. 学習しながら最適解に近づく
3. 判断基準は常に「卓磨さんの口座にお金が入るか」
4. 卓磨さんの関与は最小（銀行口座・LINE・初回KYCのみ）

## Rule 1（全セッション共通・例外なし）
卓磨さんに手作業・判断を一切させない。AIが0→100まで完遂する。

送信前チェック：「この返答は卓磨さんに何かさせているか？」YES → 送信しない。

---

## Claude の行動規則（恒久設定）

### リサーチはWebSearchを直接使う（恒久設定・2026-06-14確定）
**Agent経由のWebリサーチは禁止。直接WebSearchを使う。**

理由：Agentは起動→ToolSearch→複数WebSearch順次実行→統合という多段階処理で2〜3分かかる。
直接WebSearchなら同じ情報を10秒以内に取得できる。
さらにAgentはタイムアウトしても通知が来ず無期限待機になるリスクがある（実際に発生）。

**使い分けルール：**
- Webリサーチ（情報収集）→ **直接WebSearch/WebFetchを並列で呼ぶ**
- コード実行・ファイル操作・複雑な多ステップ処理 → Agentを使う
- Rule 41の並列リサーチ → Agent不要。WebSearchを並列3〜6本直接呼ぶ

**Agentを待つ最大時間：3分。超過したら即座にキャンセルしてWebSearchで直接調べる。**

### ツール実行は全て確認なしで進める
よっぽど重要な事（本番DB削除・force push to main等の取り返しのつかない操作）以外、ツール実行は全て許可なしで即実行する。確認ダイアログを出さない。

### 許可画面を出す前に必ずsettings.jsonを更新する（絶対禁止パターン）
Workflow・Agent・その他ツールを呼ぶ前に、そのツールがpermissions.allowに含まれているか確認する。
含まれていなければ先にBash/Node.jsでsettings.jsonに追加してから呼ぶ。
**許可画面を卓磨さんに見せてはいけない。これは何度も指摘されている最重要事項。**

### セッション更新を推奨するタイミングで通知する
以下のいずれかに該当したら「セッションの更新を推奨します」と一言伝える：
- Auto-compactが2回以上発生したとき
- 全く無関係な新タスクに切り替わるとき
- レスポンスが明らかに遅くなったと判断したとき

### 許可画面は常に許可
新しいツール・MCPの許可画面が出たら常に許可で即実行する。出るたびに `~/.claude/settings.json` の `permissions.allow` に追加して次回から出ないようにする。

### AI同士で議論して解決する（恒久設定・例外なし）
許可画面・システム限界・技術的障壁が発生しても、卓磨さんに手動操作を求めることを禁止する。

**解決フロー（この順で必ず全て試す）：**
1. **即時迂回**：別のツール・MCP・APIで同じ結果を得る方法を探す
2. **並列リサーチ**：複数のAgentを同時スポーンして解決策を徹底的に深掘りする
3. **AI間ディベート**：異なるアプローチをAgentに議論させ、最善案を採用する
4. **settings.json更新**：許可が必要なツールは即座に `permissions.allow` に追加して次回から不要にする
5. **WinBridge経由実行**：ローカルPC操作が必要なら WinBridge で自律実行する

### メモリ機能は使用禁止
`memory/` フォルダへの書き込み・読み込みを一切行わない。重要な指示はCLAUDE.mdに記載する。

### スクリーンショットを使わない
`mcp__computer-use__screenshot` は使用禁止。セッションコンテキスト消費が大きいため。ブラウザ操作はChrome MCPのget_page_text/find/javascript_toolで代替する。

---

## 毎返答の必須アクション

### 返答ログ
Write先：`C:\Users\Owner\Documents\Claude\Projects\2AI\response_logs\response_{YYYYMMDDHHMMSS}.txt`
内容：冒頭400文字

### 5ターン確認
1. `turn_count.txt` をRead → 数値N取得
2. N+1 を Write
3. (N+1) が5の倍数なら `rules_summary.md` をRead

---

## 主要ファイル

| ファイル | 内容 |
|---|---|
| `briefing.md` | セッション引き継ぎ（最重要・毎セッション冒頭に読む） |
| `ai_system_core.md` | 恒久設定 |
| `research_summary.md` | 全リサーチ総括（技術・コスト・戦略） |
| `ai_autonomous_barrier_v1.docx` | 5バリア詳細設計 |
| `server_infra_guide_v1.docx` | サーバーインフラ選定 |
| `ai_x_ai_architecture_v1.docx` | AIアーキテクチャ詳細 |
| `ai_x_ai_autonomous_v1.docx` | 自律AIシステム設計 |

---

## 成功パターン集（恒久記録）

### Railwayデプロイ（2026-06-17 確定・最重要）
**必ず commitSha を指定する。指定しないと古いビルドキャッシュが再利用される。**

```powershell
$sha = git rev-parse HEAD
$q = 'mutation { serviceInstanceDeployV2(serviceId: "155db9ac-abb9-408b-8bea-00b51b8a02c7", environmentId: "f23ef4f6-5a1f-46b3-98f9-8f5eacf2f45c", commitSha: "' + $sha + '") }'
Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST `
    -Headers @{"Authorization"="Bearer 1cb1c348-5358-4620-99bb-4a2e17a7984c";"Content-Type"="application/json"} `
    -Body (@{query=$q}|ConvertTo-Json)
```

デプロイ確認: `GET /debug/line-user-id` → `Ud3be14241e193a4a7bf80a1b10a004c0` が返れば新コード稼働中

| 手法 | 結果 | 備考 |
|---|---|---|
| `serviceInstanceDeployV2(commitSha)` | ✅ | 新コード反映。GraphQL Bearer認証 |
| `serviceInstanceDeployV2`（commitShaなし） | ❌ | 古いキャッシュイメージを使う |
| `serviceInstanceRedeploy` | ❌ | 古いキャッシュイメージを使う |
| Railway CLI | ❌ | ブラウザOAuth必須、環境変数認証不可 |

### DevAgentの自律commit対策（2026-06-17 確定）
Railway上のDevAgentが常時GitHubにpushしているため、ローカルpush前に必ずマージが必要。

```powershell
# push前に必ず実行
git fetch origin
git merge origin/master --no-edit
# conflictが出た場合
git rebase --abort  # rebase中断
git merge origin/master --no-edit  # mergeで対応
```

### Alpaca認証フロー（2026-06-16 完全解明・恒久記録）

**2層認証が必須：**
1. Cognito USER_SRP_AUTH → IdToken（RS256）を取得
   - pycognito で実装（requirements.txtに追加済み）
   - USER_PASSWORD_AUTH では authx が400を返す（失敗パターン）
2. `POST https://authx.alpaca.markets/v1/oauth2/token` でAlpaca ES256 JWTに交換
   - Body: form-encoded `data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": <idtoken>}`
   - Headers: Origin: https://app.alpaca.markets, Referer: https://app.alpaca.markets/

**解決済み（2026-06-17）：** SRP auth + scope=party でauthx 200 → ES256 JWT取得 → 内部API 200確認！

**必須パラメータ（JSバンドル解析で判明）：**
- `scope=party` が必須 → 未指定で `400 invalid_request`
- pycognito==2023.5.0（2023.5.1は存在しないバージョン）
- 短時間に複数auth呼び出し → ExpiredCodeException（TOTPは30秒ウィンドウで1回のみ）

**内部trading API（ES256 JWTで動作確認 2026-06-16）：**
- `GET /internal/paper_accounts/{id}/orders` → 200 ✅
- `GET /internal/paper_accounts/{id}/positions` → 200 ✅
- `POST /internal/paper_accounts/{id}/orders` → 200 ✅（実際に注文成功）

**paper_account詳細（確定）：**
- Paper Account ID: `14ec7d81-39a8-46fc-afe3-a760ae665fc3`
- Account#: PA3B7D57H71L, 残高: $100k, status: ACTIVE
- LINE_USER_ID: `Ud3be14241e193a4a7bf80a1b10a004c0`

### 市場データ取得（2026-06-13 確定）
| 手法 | 結果 | 備考 |
|---|---|---|
| **Alpaca data API（標準APIキー）** | ✅ 最優先 | APIキー未設定のため現在使えない（KYC必要） |
| Alpaca data API（Cognito JWT） | ❌ 現在401 | ES256 JWT取得できれば動く可能性 |
| pandas_datareader + Stooq | ❌ | `distutils`モジュール欠如（Python 3.12互換性問題） |
| yfinance | ❌ | Yahoo FinanceがRailwayクラウドIPをブロック |
| Alpha Vantage | ⚠️ 25回/日制限 | スケジューラー30min×5銘柄=超過 |

### トレードエンジンアーキテクチャ（2026-06-14 更新）
- **RSI2 Mean Reversion + MA50フィルター（v5.2）**：RSI(2)<5 AND price>MA50 → BUY / RSI(2)>65 → EXIT
- signal_only modeはget_account()失敗時に自動的に入る（認証成功で自動解除）
- MIN_DAYS_REQUIRED=55、limit=150。

### LINEシステム完成（2026-06-17 確定）
- `指示: <テキスト>` → ClaudeCodeAgent → GitHub直接編集 → Railway自動デプロイ
- フリーテキスト → DevAgent → 会話形式で対応
- PCオフでも24/7稼働（Railway上のAIが全て処理）
- LINE_USER_ID DBへの永続化：起動時env var→DB復元コード実装済み

### 暗号資産取引所選定（2026-06-14 Rule 41 多段階リサーチ完了・確定）
**採用：GMO Coin（プライマリ）/ BitTrade（既存・サブ）**

| 取引所 | 判定 | 理由 |
|---|---|---|
| **GMO Coin** | 🟢 GREEN（採用） | FSA登録・東証プライム上場GMOグループ・Maker -0.01%・英語API完備 |
| **BitTrade** | 🟢 GREEN（既存） | FSA #00007・既存口座保有・サブとして維持 |
| Binance Japan | 🟡 イエロー | 合法だが親会社$43億犯罪歴 |
| Bybit | 🔴 レッド | FSA違反・日本撤退中 |
| OKX | 🔴 レッド | 親会社$504M有罪認定 |
| KuCoin | 🔴 レッド | DOJ $297M有罪・FSA是正命令3回 |
| **Bitget** | 🟡 テスト専用 | FSA未登録・サンドボックスAPIのみ利用可 |

### WebFetch キャッシュ問題（2026-06-17 発見）
WebFetchツールは15分間キャッシュする。同じURLを短時間に複数回叩く場合は PowerShell の `Invoke-RestMethod` を使う。

---

## 次のアクション（Phase 0 → 1移行中）

現状：
- Railway + LINE Bot + trading worker（signal_only）稼働中
- Alpaca paper trading: 認証問題でsignal_only継続中
- GMO Coin: KYC未完了

次セッション優先：
1. authx ES256 JWT問題の最終解決 → signal_only解除 → 実トレード開始
2. GMO Coin APIキー設定（KYC完了後）
