# セットアップ手順

このツールは4つのファイルで構成されています。

| ファイル | 役割 | 動かす場所 |
|---|---|---|
| `gas_webhook.gs` | LINEからの希望メッセージを受け取る | Google Apps Script |
| `sheets_schema.md` | スプレッドシートの列構成の説明 | （ドキュメント） |
| `data_backend.py` / `matching.py` / `line_notify.py` / `app.py` | マッチング確認・確定・通知の管理画面 | Streamlit Community Cloud |

まずは**デモモード**（ローカルCSVのみ・実際のLINE/スプレッドシートには繋がない）で
動作を確認してから、本番接続に進むことをおすすめします。

## 0. デモモードで動作確認する（手元のパソコンで）

```bash
pip install -r requirements.txt
streamlit run app.py
```

st.secrets が無い状態で起動すると自動的にデモモードになり、サンプルデータで
マッチング画面の動きを確認できます。

## 1. Googleスプレッドシートを準備する

1. 新しいスプレッドシートを作成する
2. `sheets_schema.md` の通りに「希望」「現場マスタ」「確定シフト」の3シートを作り、
   ヘッダー行（項目名）を入力する
3. 「現場マスタ」シートに、実際の現場名とエリアを入力しておく

## 2. LINE公式アカウントを準備する

1. [LINE Developers](https://developers.line.biz/) でアカウントを作成し、
   「Messaging API」のチャネルを新規作成する
2. 「Messaging API設定」タブで「チャネルアクセストークン（長期）」を発行し、
   控えておく（`gas_webhook.gs` と `app.py` の両方で使う）

## 3. Google Apps Script（LINEの受信口）をデプロイする

`gas_webhook.gs` の先頭コメントに書いた手順の通りに進めてください。要点だけ書くと：

1. スプレッドシートの「拡張機能」→「Apps Script」を開く
2. `gas_webhook.gs` の中身を貼り付け、`LINE_CHANNEL_ACCESS_TOKEN` を書き換える
3. 「デプロイ」→「新しいデプロイ」→種類「ウェブアプリ」でデプロイし、URLを発行する
4. LINE Developersの「Webhook URL」にそのURLを設定し、「Webhookの利用」をオンにする

これで、スタッフが以下のようなメッセージをLINEに送ると、自動的にスプレッドシートの
「希望」シートに1行追加されるようになります。

```
希望 2026-09-10 9:00-18:00 渋谷
```

## 4. サービスアカウントを作る（Streamlitからスプレッドシートへの接続用）

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成する
2. 「APIとサービス」→「ライブラリ」で次の2つを有効化する
   - Google Sheets API
   - Google Drive API
3. 「認証情報」→「サービスアカウントを作成」し、キー（JSON形式）をダウンロードする
4. ダウンロードしたJSONの中の `client_email` の値（〇〇@〇〇.iam.gserviceaccount.com
   という形式のメールアドレス）を、スプレッドシートの「共有」から**編集者**として追加する

## 5. Streamlit Community Cloudにデプロイする

1. `app.py` ・`data_backend.py`・`matching.py`・`line_notify.py`・`requirements.txt`
   をGitHubリポジトリに置く
2. [Streamlit Community Cloud](https://streamlit.io/cloud) でそのリポジトリを選び、
   `app.py` をエントリーポイントとしてデプロイする
3. アプリの「Settings」→「Secrets」に、以下の形式で貼り付ける

```toml
SHEET_ID = "スプレッドシートのURLに含まれるID"
LINE_CHANNEL_ACCESS_TOKEN = "LINEのチャネルアクセストークン"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "....iam.gserviceaccount.com"
client_id = "..."
token_uri = "https://oauth2.googleapis.com/token"
```

（ダウンロードしたサービスアカウントのJSONの中身を、そのままTOML形式に書き写す形です）

4. 保存すると自動的に再起動し、画面上部に「🟢 本番モード」と表示されれば接続成功です

## 今回のスコープに含まれていないもの（今後の拡張候補）

- 現場ごとの「必要人数（上限）」に基づく、厳密な空き枠管理
  （今回は「その日その現場に何人確定しているか」を目安として見せるだけです）
- LINEのリッチメニューやボタン形式での希望入力
  （今回はテキストメッセージの決まったフォーマットでの受付です）
- 経験値・ランクを名簿ツール（`labor_roster_tool.py`）側にも反映する連携
