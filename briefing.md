# 2AI Project - Session Handoff Briefing
Last updated: 2026-06-19 (session 6)

## Current Status - Railway server LIVE in production

Infrastructure:
- Railway: LIVE https://orchestrator-production-61d8.up.railway.app
- GitHub: 2counterculture2-eng/2ai-orchestrator
- Claude API: active | LINE Bot: active (@317fpwfv)
- Latest commit: cf48175 (fix: pc-turns uses GitHub file storage)

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

## === 2026-06-19 セッション6 成果 ===

### 新機能: LINEからPCセッション履歴を参照（完全実装・恒久設定）

**概要:** PCがオフでも、LINEから「直近3ターン見せて」と送ると直近の会話が表示される

**実装内容:**
- `orchestrator/main.py`: `POST /api/pc-turn` + `GET /api/pc-turns` エンドポイント追加
- `orchestrator/learning.py`: `save_pc_turn()` / `get_pc_turns()` メソッド追加（SQLiteも実装したがGitHubに変更）
- `orchestrator/dev_agent.py`: `get_pc_turns` ツール + `_get_pc_turns()` ハンドラ追加
- `CLAUDE.md`: 毎ターン必須アクションに `py` コマンドでpc-turn POST追加

**表示フォーマット（LINEモバイル最適化）:**
```
📋 直近3ターン（PCセッション）

【1】06/17 08:56 JST
👤 ユーザーメッセージ
🤖 AI返答

【2】...
```

**永続化方式:** GitHub `data/pc_turns.json`（SQLiteはredeploy毎にリセットされるため変更）
**JST表示:** UTC+9に変換して `06/17 08:56 JST` 形式で表示
**日本語送信:** `py` コマンド（Windows Python launcher）でUTF-8エンコード

**LINEコマンド:**
- 「直近3ターン見せて」「最近のやりとり教えて」「PCで何してた？」→ DevAgent が get_pc_turns ツールを使って返答

**毎ターンの必須アクション（CLAUDE.md記載）:**
```bash
py -c "import urllib.request, json; data={'user_msg':'<MSG>', 'ai_response':'<RESP>'}; req=urllib.request.Request('https://orchestrator-production-61d8.up.railway.app/api/pc-turn', json.dumps(data, ensure_ascii=False).encode('utf-8'), {'Content-Type':'application/json; charset=utf-8'}); urllib.request.urlopen(req, timeout=5)" &
```

## === セッション5 成果（参照用）===

### 完全解決: Alpaca authx ES256 JWT認証
- `scope=party` が必須パラメータ（JSバンドル解析で判明）
- pycognito==2023.5.0（2023.5.1は存在しない）
- authx_status: 200 ✅ / es256_jwt_length: 556 ✅ / buying_power: $99,900.01 ✅

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
- EXCLUDED（FSA未登録・日本App Store削除済み）→ 採用見送り

## 次セッション優先タスク

1. **トレード稼働確認** - /status で tasks_completed > 0 を確認（市場時間後）
2. **GMO Coin APIキー設定** - KYC完了後（卓磨さんのスマホ操作・システム限界例外）
3. **証券会社検討** - マネックス証券はAPI非対応。kabu.com（日本株）またはIBKR（日米株）を調査

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
