# State Lens : Chat (iMessage Relay + Viewer)

Mac mini 上で iMessage を監視して HTTP 転送し、Viewer で可視化・分析するシステムです。

説明書（HTML）: `docs/StateLens_Chat_Manual.html`

## 構成

- Relay 本体: `imessage_relay.py`
- Viewer API/UI: `viewer_server.py` + `viewer/`
- 起動ランチャー: `StateLens_Chat 起動.app`
- 停止ランチャー: `StateLens_Chat 停止.app`
- 起動コマンド実体: `scripts/start_terminal.command`
- 連絡先マップ: `Address_list.md`

## 起動フロー

`StateLens_Chat 起動.app` は次をまとめて実行します。

- `scripts/start_terminal.command` を Terminal で起動
- runtime ディレクトリ（`~/StateLens_Chat_runtime`）へ実行ファイルを同期
- `viewer_server.py` をバックグラウンド起動
- `imessage_relay.py` を監視プロセスとして起動
- ブラウザで `http://localhost:8080/` を開く

停止時は `StateLens_Chat 停止.app` で relay/viewer を停止します。

## Relay 仕様

### 監視・送信

- DB: `~/Library/Messages/chat.db` (read-only)
- 監視間隔: `POLL_INTERVAL = 5.0s`
- 転送先: `ENDPOINT = http://192.168.4.44:5000/ingest`
- 送信: HTTP POST (`application/json`)
- 成功コード: `200/201/202/204`

### 永続化

- 状態: `~/.imessage_relay_state.json`
  - `last_rowid`
  - `processed_guids`（上限 10,000）
- 送信キュー: `~/.imessage_relay_queue.jsonl`
  - サーバー不達時は保持し、次回再送

## Viewer API 仕様

### エンドポイント

- `GET /api/messages`
- `POST /api/reanalyze` (`{"scope":"all"}` / `{"scope":"thread","thread_id":"..."}`)

### /api/messages レスポンス概要

- `messages[]`
  - `analysis`（感情/意図/タスク等）
  - `analysis_status` (`pending` / `complete`)
  - `analysis_source` (`fallback` / `openai:gpt-5-mini` / `openai:gpt-5.2` / `openai:mixed`)
- `thread_summaries[]`
- `meta`
  - `analysis_provider`
  - `openai_enabled`
  - `openai_model_light`
  - `openai_model_heavy`
  - `pending_count`

## 分析仕様

### A/C（コード計算・deterministic）

- A: `timing.minutes_since_previous`, `timing.topic_duration_minutes`
- C:
  - `thread_metrics.messages_per_minute`
  - `thread_metrics.topics_per_minute`
  - `thread_metrics.messages_per_topic`
  - `thread_metrics.chars_per_message`

### B/D/E（LLM + 後処理）

- 感情: `emotion.label/score/nuance`
- 言語特徴: typo/wordplay/表現分類
- 意味: `semantic.content_summary/topic/intent`
- タスク:
  - `semantic.participant_tasks`（participant 単位）
  - 互換: `tasks`, `self_tasks`, `other_tasks`

### intent ラベル

- `request`
- `report`
- `ask`
- `confirm`
- `complain`
- `share`
- `plan`
- `emotional_expression`

## OpenAI 設定

`.env` で環境変数を設定します（推奨）。

1. `.env.example` をコピーして `.env` を作成
2. `OPENAI_API_KEY` などを実値に変更

```bash
cp .env.example .env
```

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_MODEL_LIGHT`
- `OPENAI_MODEL_HEAVY`
- `OPENAI_TIMEOUT`
- `OPENAI_MAX_RETRIES`
- `SELF_DISPLAY_NAME`（任意、既定: `西岡まひろ`）

補足:

- `OPENAI_API_KEY` が未設定/プレースホルダ時は自動で `fallback`
- `.env` は `.gitignore` 済み（GitHubへは上がらない）
- `gpt-5-mini`/`gpt-5.2` で `temperature` 非対応のため、温度パラメータは送信しない
- `chat/completions` で 400 の場合は `responses` API へフォールバック

## participant タスク表示仕様

UI は右パネルで次を表示します。

- `自分のタスク`（常時表示）
- `参加者ごとのタスク`（横スクロールカード）

participant タスク推定ルール（暫定）:

- `OUTGOING` 依頼: 宛先 participant のタスク
- `INCOMING` 依頼/期限: 自分タスク
- 明示名/あだ名が本文にあればその participant を優先
- 明示名なしグループ依頼は全受信者に配布（単独指定語がある場合のみ絞る）

タスク文は自然文に整形し、可能な範囲で `いつ/どこ/どう` を補完します。

## あだ名解決（participant 判定）

`viewer_server.py` 内の `NICKNAME_RULES` で人物別あだ名を定義しています。

- 単純あだ名（例: `千尋`, `まま`, `ピコ`）
- 条件付きあだ名（例: 送信者/受信者条件付きの `おばあちゃん`, `お兄さん`）

このルールは participant タスク割当ての明示対象判定に使われます。

## 一人称置換仕様

分析表示の文中に一人称がある場合は送信者名へ置換します。

- 対象: `私`, `わたし`, `僕`, `俺`
- 併せて `送信者` / `話者` も送信者名に置換
- 送信者が `Me/self` の場合は `SELF_DISPLAY_NAME` を使用

## 既知の制約

- 画像そのものは Viewer にレンダリングせず、必要時は `[画像]` 表示
- LLM 出力品質はメッセージ内容に依存（後処理で生文混入を抑制中）
- participant 推定はルールベース主体（高精度会話セグメンテーションは未実装）

## 運用手順

### 1. 通常起動（作業開始時）

1. `StateLens_Chat 起動.app` を実行
2. Viewer を開く（通常は `http://localhost:8080/`）
3. メッセージ1件送受信して反映確認
4. 必要なら確認:

```bash
curl -s http://192.168.4.33:8080/api/messages | python3 -c 'import sys,json;p=json.load(sys.stdin);m=p.get("meta",{});print("provider=",m.get("analysis_provider"),"openai=",m.get("openai_enabled"),"pending=",m.get("pending_count"))'
```

### 2. 通常停止（作業終了時）

1. `StateLens_Chat 停止.app` を実行
2. 必要に応じてログ確認

### 3. 日次チェック（毎日）

推奨タイミング: 朝の起動直後、または夜の終了前

1. relay/viewer が動いているか確認（Viewer画面更新）
2. OpenAI分析が fallback 固定になっていないか確認（`analysis_source`）
3. エラーログ確認:

```bash
tail -n 80 ~/Library/Logs/imessage_relay_launcher.log
tail -n 80 ~/Library/Logs/imessage_relay.log
```

4. 異常がある場合は「トラブル時手順」を実施

### 4. コード変更後の反映手順（README更新時含む）

1. `StateLens_Chat 停止.app`
2. `StateLens_Chat 起動.app`
3. 必要なら全件再分析:

```bash
curl -X POST http://192.168.4.33:8080/api/reanalyze -H "Content-Type: application/json" -d '{"scope":"all"}'
```

4. 10〜60秒待って確認:

```bash
curl -s http://192.168.4.33:8080/api/messages | python3 -c 'import sys,json;p=json.load(sys.stdin);msgs=p.get("messages",[]);print(msgs[0].get("analysis_source"), msgs[0].get("analysis_status")) if msgs else print("no messages")'
```

### 5. トラブル時手順

#### A. `fallback` のまま変わらない

1. まず `analysis_source` が `pending` か `complete` か確認
2. launcherログを確認:

```bash
tail -n 120 ~/Library/Logs/imessage_relay_launcher.log
```

3. よくある原因
- `HTTP 401`: APIキー不正/失効
- `HTTP 400`: モデル/パラメータ不整合
- `timed out`: OpenAIタイムアウト（`OPENAI_TIMEOUT` 調整）

4. 必要なら再分析を再実行

#### B. 新着が取れない（relay停止や権限問題）

1. 診断スクリプト:

```bash
cd "/Users/nishiokamahiro/Desktop/Antigravity/iMessage soujushin"
./scripts/diagnose_relay_access.sh
```

2. `authorization denied` の場合
- 実行実体（Python.app）へフルディスクアクセス付与を確認
- 停止→起動を実行

#### C. UIが古い表示のまま

1. `StateLens_Chat 停止.app`
2. `StateLens_Chat 起動.app`
3. ブラウザ再読み込み（必要ならハードリロード）

### 6. 週次メンテナンス（推奨）

1. ログ肥大化チェック:

```bash
ls -lh ~/Library/Logs/imessage_relay*.log
```

2. 必要ならログ退避/削除（運用ポリシーに合わせる）
3. 主要仕様変更があれば README の「変更点」に追記

---

## 2026/03/24 変更点

1. 起動統合
- Relay + Viewer 起動を `StateLens_Chat 起動.app` に統合
- 停止を `StateLens_Chat 停止.app` に統一

2. OpenAI 分析導入
- 環境変数読み込み追加（`OPENAI_*`）
- `gpt-5-mini`（通常）+ `gpt-5.2`（難ケース）切替
- `analysis_source` を `fallback/openai:*` で表示
- `POST /api/reanalyze` を追加

3. 分析データ拡張
- `analysis` に timing/thread_metrics/language_features/semantic を統合
- `intent` ラベル正規化
- summary 生文混入抑制

4. タスク仕様変更
- 単一 `やるべきタスク` から participant 単位 (`participant_tasks`) へ拡張
- `自分のタスク` 固定表示 + 他参加者カード横スクロール
- 依頼文の participant 割当ロジック改善
- タスク文を自然文へ整形（5W1H 要素推定）

5. 宛先解決改善
- 人物別あだ名辞書を追加
- 送信者/受信者条件付きあだ名を反映

6. 表示品質改善
- 一人称・`送信者/話者` を送信者人物名に置換
- 改行復元、本文ノイズ除去、連絡先名解決の安定化
