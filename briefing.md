# 2AI Project - Session Handoff Briefing
Last updated: 2026-06-17 (session 5)

## Current Status - Railway server LIVE in production

Infrastructure:
- Railway: LIVE https://orchestrator-production-61d8.up.railway.app
- GitHub: 2counterculture2-eng/2ai-orchestrator
- Claude API: active | LINE Bot: active (@317fpwfv)
- Latest commit: 4862e74 (fix: debug endpoint uses scope=party)

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

## === 2026-06-17 セッション5 成果 ===

### 完全解決: Alpaca authx ES256 JWT認証
**問題:** authx が 400 invalid_request を返し続けていた
**根本原因:** `scope=party` が必須パラメータ（Alpaca JSバンドル解析で判明）
**修正箇所:**
- `orchestrator/alpaca_client.py`: data に `"scope": "party"` 追加
- `orchestrator/main.py`: debug endpoint も同様に修正
- `requirements.txt`: pycognito==2023.5.1→2023.5.0（存在しないバージョン修正）

**確認結果 (2026-06-17):**
- authx_status: 200 ✅
- es256_jwt_length: 556 ✅
- internal_api_status: 200 ✅
- buying_power: $99,900.01 ✅

### 現在の稼働状態
- Trading Worker: 30分間隔・米国市場時間（UTC 13:30-21:00）に自動実行
- RSI2 Mean Reversion + MA50フィルター（v5.2）
- signal_only モード: アカウント取得失敗時のみ（正常時は実際に発注）
- データ: Alpaca data API（APIキー不要・JWTで取得）

## Alpaca認証フロー（完全解明・恒久記録）

**2層認証フロー（必ずこの順で）:**
1. Cognito USER_SRP_AUTH → IdToken（RS256）を取得
   - pycognito==2023.5.0（2023.5.1は存在しない）
   - Cognito Pool: us-east-1_CZEBlNVuv
   - Client ID: 3tgca6mnp6g138dbkcs7lq7j0a
2. POST https://authx.alpaca.markets/v1/oauth2/token → ES256 JWT
   - Body (form-encoded): grant_type=jwt-bearer, assertion=<idtoken>, **scope=party**
   - Headers: Content-Type: x-www-form-urlencoded, Origin: https://app.alpaca.markets

**注意点:**
- scope=party を忘れると常に 400 invalid_request
- 短時間に複数回認証するとExpiredCodeException（TOTP30秒ウィンドウで1回限り）
- JWTは55分間キャッシュされるので通常は問題なし

**内部API (ES256 JWT必須):**
- GET/POST /internal/paper_accounts/{id}/orders → 200
- GET /internal/paper_accounts/{id}/positions → 200
- GET /internal/paper_accounts/{id}/trade_account → 200

**paper_account詳細（確定）:**
- Paper Account ID: 14ec7d81-39a8-46fc-afe3-a760ae665fc3
- Account#: PA3B7D57H71L, 残高: $100k, status: ACTIVE
- Brokerage Account ID: 3c7812e8-5561-417b-a6a5-9f7574d3c474（status: PAPER_ONLY = KYC未完）
- Owner ID: 6a88e553-7972-4075-be01-d014368f5805

## Trading Workers状態

### Alpaca（US株 PAPER）
- RSI2 Mean Reversion + MA50（v5.2）
- **STATUS: ACTIVE** ✅（JWTで内部API認証済み）
- データ取得: get_bars()でAlpaca data API（JWT）
- 銘柄: AAPL, MSFT, NVDA固定

### GMO Coin（暗号資産 LIVE）
- WAITING FOR API KEY - KYC未完了（システム限界例外・スマホ操作必要）

### Bitget（暗号資産 SANDBOX）
- WAITING FOR DEMO API KEY

## 次セッション優先タスク

1. **トレード稼働確認** - /status で tasks_completed > 0 を確認（市場時間後）
2. **GMO Coin APIキー設定** - KYC完了後（卓磨さんのスマホ操作・システム限界例外）
3. **Bitget Sandbox APIキー取得**
4. **LINE経由の動作確認** - LINEから「ポジション確認」などを送信してみる

## 重要な運用ルール（今セッションで確認）

### Railwayデプロイ（必ず commitSha 指定）
```powershell
$sha = git rev-parse HEAD
$q = 'mutation { serviceInstanceDeployV2(serviceId: "155db9ac...", environmentId: "f23ef4f6...", commitSha: "' + $sha + '") }'
Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers @{"Authorization"="Bearer TOKEN";"Content-Type"="application/json"} -Body (@{query=$q}|ConvertTo-Json)
```
- commitShaなし → 古いビルドキャッシュが再利用される
- デプロイ確認: 本番でのデバッグエンドポイント呼び出しで確認

### DevAgentの自律commit問題
- Railway上のDevAgentが常時GitHubにpushしている
- ローカルでpushする前に必ず: git fetch origin; git merge origin/master --no-edit
