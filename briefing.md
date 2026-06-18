# 2AI Project - Session Handoff Briefing
Last updated: 2026-06-19 (session 8)

## Current Status - Railway server LIVE in production

Infrastructure:
- Railway: LIVE https://orchestrator-production-61d8.up.railway.app
- GitHub: 2counterculture2-eng/2ai-orchestrator
- Claude API: active | LINE Bot: active (@317fpwfv)
- Latest commit: bb829d6 (feat: add IBKRWorker v1)

## Railway credentials
API token:      1cb1c348-5358-4620-99bb-4a2e17a7984c
Project ID:     a9375e6d-1f7d-47aa-94f3-dd70f2e0b50e
Service ID:     155db9ac-abb9-408b-8bea-00b51b8a02c7
Environment ID: f23ef4f6-5a1f-46b3-98f9-8f5eacf2f45c

## Railway 環境変数（設定済み）
- ANTHROPIC_API_KEY: sk-ant-api03-... ✅
- LINE_CHANNEL_ACCESS_TOKEN: ✅
- LINE_CHANNEL_SECRET: ✅
- LINE_USER_ID: Ud3be14241e193a4a7bf80a1b10a004c0 ✅
- ALPACA_EMAIL: 2counterculture2@gmail.com ✅
- ALPACA_PASSWORD: Ai2System2025! ✅
- ALPACA_MFA_SECRET: OP7QNXFV7JHI... ✅
- ALPACA_PAPER_ACCOUNT_ID: 14ec7d81-39a8-46fc-afe3-a760ae665fc3 ✅
- GITHUB_TOKEN: gho_riGtQh... ✅
- RAILWAY_TOKEN: 1cb1c348-... ✅
- ALPHA_VANTAGE_API_KEY: QB7HJ9U0... ✅
- ALPACA_API_KEY: 未設定（KYC必要）
- ALPACA_SECRET_KEY: 未設定（KYC必要）
- IBKR_GATEWAY_URL: 未設定（口座開設後に設定）
- IBKR_ACCOUNT_ID: 未設定（口座開設後に設定）
- IBKR_PAPER: true（デフォルト）

## === 2026-06-19 セッション8 成果 ===

### IBKR（Interactive Brokers）調査結果（Rule 41 多段階リサーチ完了）

| 項目 | 内容 |
|---|---|
| 判定 | 🟢 GREEN（前セッションで確認済み） |
| 対応市場 | 米国株・ETF・日本株（TSE）・先物・オプション・FX |
| JPY入金 | ✅（国内銀行振込、最低入金額なし） |
| ペーパー | ✅（ただしライブ口座開設・入金が先決条件） |
| API | TWS API / Client Portal Web API（REST）|
| 自動化 | ✅ IB Gateway Docker化で完全自動化可能 |

### IBKRワーカー実装完了（v1）

**実装ファイル:**
- `orchestrator/workers/ibkr_worker.py` (v1) — IBKRClient + IBKRWorker
- `orchestrator/config.py` — IBKR_GATEWAY_URL / IBKR_ACCOUNT_ID / IBKR_PAPER
- `orchestrator/orchestrator_core.py` — IBKR dispatcher + trading loop統合

**動作:**
- IBKR_GATEWAY_URL が未設定 → 完全no-op（既存システムに影響なし）
- IBKR_GATEWAY_URL 設定済み → 30分おきにRSI2シグナル + 注文実行
- データソース: IBKR Historical Bars → Alpha Vantage（日次キャッシュ）フォールバック
- 日本株にも対応（7203.T, 6758.T等をシンボルとして指定可能）
- 注文失敗時は signal_only として success=True を返す（タスク失敗にならない）

**IBKRを有効化するために必要なこと:**
1. IBKR口座開設（KYC）→ ライブ口座に入金
2. Client Portal Gateway（Java）をDockerコンテナ化してRailwayに追加
   - 参考Docker: `gnzsnz/ib-gateway-docker`（TOTP自動認証対応）
3. Railway環境変数: IBKR_GATEWAY_URL / IBKR_ACCOUNT_ID を設定

### 証券会社選定確定（セッション7-8 調査完了）

| 取引所 | 判定 | 理由 |
|---|---|---|
| **IBKR** | 🟢 採用決定 | FSA認可・JPY入金・日米株API完備・ペーパー有 |
| **Alpaca** | ⚠️ 暫定維持 | 日本人KYCなし→リアルトレード不可。ペーパーのみ継続 |
| **マネックス証券** | ❌ 不採用 | 個人向けAPI非公開・自動売買不可 |
| GMO Coin | ⚠️ 待機 | KYC未完了（スマホ操作・システム限界例外） |

**Alpaca戦略（確定）:**
- 今: ペーパー継続（signal_only、注文は401/403でスキップ）
- IBKR稼働後: Alpacaを停止

### Alpacaワーカー現状（v5.3）

- データ取得: Alpha Vantage（日次キャッシュ） → Alpaca JWT フォールバック ✅
- 注文: 401/403 → signal_only（success=True）として記録（失敗にならない）✅
- 根本原因: 標準APIキー（KYC完了後）なしでの内部API注文は認証が変更された

## 次セッション優先タスク

1. **IBKR口座開設** — KYC（パスポート＋住所証明＋セルフィー）はシステム限界例外。卓磨さんのスマホ/PCで完了後に通知。
2. **IBKR入金後** — IB GatewayをDockerでRailwayに追加 → 環境変数設定 → ペーパートレード開始
3. **GMO Coin APIキー** — KYC完了後、APIキーを取得してRAILWAY環境変数に設定

## Alpaca認証フロー（完全解明・恒久記録）

**2層認証フロー（必ずこの順で）:**
1. Cognito USER_SRP_AUTH → IdToken（RS256）を取得
   - pycognito==2023.5.0（2023.5.1は存在しない）
   - Cognito Pool: us-east-1_CZEBlNVuv / Client ID: 3tgca6mnp6g138dbkcs7lq7j0a
2. POST https://authx.alpaca.markets/v1/oauth2/token → ES256 JWT
   - Body: grant_type=jwt-bearer, assertion=<idtoken>, **scope=party**（必須）
   - Headers: Origin: https://app.alpaca.markets

**paper_account詳細（確定）:**
- Paper Account ID: 14ec7d81-39a8-46fc-afe3-a760ae665fc3
- Account#: PA3B7D57H71L, 残高: $100k, status: ACTIVE
- Brokerage Account ID: 3c7812e8-5561-417b-a6a5-9f7574d3c474（status: PAPER_ONLY = KYC未完）

## 重要な運用ルール

### Railwayデプロイ（必ず commitSha 指定）
```powershell
$sha = git rev-parse HEAD
$q = 'mutation { serviceInstanceDeployV2(serviceId: "155db9ac-abb9-408b-8bea-00b51b8a02c7", environmentId: "f23ef4f6-5a1f-46b3-98f9-8f5eacf2f45c", commitSha: "' + $sha + '") }'
Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers @{"Authorization"="Bearer 1cb1c348-5358-4620-99bb-4a2e17a7984c";"Content-Type"="application/json"} -Body (@{query=$q}|ConvertTo-Json)
```
- commitShaなし → 古いビルドキャッシュが再利用される

### DevAgentの自律commit問題
- Railway上のDevAgentが常時GitHubにpushしている
- ローカルでpushする前に必ず: git fetch origin; git merge origin/master --no-edit

### Python実行（Windows）
- `py` コマンドを使う（`python3` はWindows環境でexit 49）
- 日本語JSON送信: `json.dumps(data, ensure_ascii=False).encode('utf-8')`

### LINEからPCセッション履歴を参照
- 毎返答後に `py` コマンドで POST /api/pc-turn を送信（CLAUDE.md記載）
- LINEで「直近3ターン見せて」→ DevAgentが get_pc_turns ツールで表示
- 永続化: GitHub `data/pc_turns.json`
