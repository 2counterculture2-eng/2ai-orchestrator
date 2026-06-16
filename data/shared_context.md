# Shared Session Context
Last updated by LINE bot (exchange #2)

## Recent LINE Exchange
[User] テスト 今はpc起動しているけど pcオフ状態でも ラインからpcのcodeに指示できる？
[Bot] はい、**できます！** ✅  ## PCオフ状態でもLINEから指示可能です  仕組みはこうです：  ``` Takuma (LINE)      ↓ LINE Bot (@317fpwfv)     ↓ Railway AI (24/7稼働中) ← ここが重要     ↓ GitHub コード編集     ↓ Railway 自動デプロイ ```  **Railwayの上でこのAIが24時間稼働しているので、TakumaのPCは一切関係ありません。**  ## 使い方  | メッセージ形式 | 動作 | |---|---| | 普通のテキスト | 会話・質問に回答 | | `instruction: ...` | コード編集→GitHub→デプロイ自動実行 |  ## 例えば  - PCオフの深夜2時に「`instruction: トレード戦略をRSI3に変更して`」と送信 - → こ

## System State
- Paper trading: Alpaca 30min
- Translation channels: pending API keys
- LINE bot: @317fpwfv
- Railway: https://orchestrator-production-61d8.up.railway.app