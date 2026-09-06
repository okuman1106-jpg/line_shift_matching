/**
 * ============================================================
 * LINE Webhook 受信スクリプト（Google Apps Script）
 * ============================================================
 * 役割：
 *   スタッフがLINEで送ってきた「希望シフト」のメッセージを受け取り、
 *   スプレッドシートの「希望」シートに1行追記する。
 *   受付できたら、本人にLINEで確認メッセージを返信する。
 *
 * 費用：0円（Google Apps Scriptは個人アカウントで無料利用可）
 *
 * ---- セットアップ手順 ----
 * 1. Googleスプレッドシートを新規作成し、シート名を以下の3つにする
 *      「希望」「現場マスタ」「確定シフト」
 *    （各シートの列構成は sheets_schema.md を参照）
 * 2. スプレッドシートのメニュー「拡張機能」→「Apps Script」を開く
 * 3. このファイルの中身を丸ごと貼り付けて保存
 * 4. 下の LINE_CHANNEL_ACCESS_TOKEN に、LINE Developersコンソールで
 *    発行した「チャネルアクセストークン（長期）」を貼り付ける
 * 5. 画面右上「デプロイ」→「新しいデプロイ」→種類「ウェブアプリ」
 *      - 実行するユーザー：自分
 *      - アクセスできるユーザー：全員
 *    でデプロイし、発行されたURLをコピーする
 * 6. LINE Developersコンソールの「Messaging API設定」→
 *    「Webhook URL」に、5でコピーしたURLを貼り付けて「検証」する
 * 7. 「Webhookの利用」をオンにする
 * ============================================================
 */

// ↓↓↓ ここにLINEのチャネルアクセストークン（長期）を貼り付ける ↓↓↓
const LINE_CHANNEL_ACCESS_TOKEN = CLXk6RCC/LWy5AyMsZ9/P4kh9D4WL0SJlodMhhddKExteUe/KY35RZYSGdamvdLTSO+I+Sm/AMtIx+3Yt2qebgbWhaTfdoKlyiSvABbUbhqKD6sxheCHmc3D3WbIet0UljaHvUoBOgDcFNubEEDImQdB04t89/1O/w1cDnyilFU=

// スプレッドシートのシート名（変更していなければそのままでOK）
const SHEET_NAME_WISH = "希望";

/**
 * スタッフに送ってもらう希望メッセージのフォーマット例：
 *   希望 2026-09-10 09:00-18:00 渋谷
 *   希望 2026-09-10 9:00-18:00 渋谷現場
 *
 * 「希望」に続けて「日付 開始-終了 現場名」の順で送ってもらう。
 * 多少のスペースの空き方や全角半角の違いは吸収する。
 */
function parseWishMessage(text) {
  const normalized = text
    .replace(/　/g, " ")          // 全角スペース→半角
    .replace(/[０-９]/g, function (s) {  // 全角数字→半角
      return String.fromCharCode(s.charCodeAt(0) - 0xFEE0);
    })
    .replace(/[〜～]/g, "-")       // 波ダッシュ→ハイフン
    .trim();

  if (!/^希望/.test(normalized)) {
    return null;
  }

  // 例: "希望 2026-09-10 9:00-18:00 渋谷" を分解する
  const m = normalized.match(
    /希望\s*(\d{4}-\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s+(\S+)/
  );
  if (!m) {
    return null;
  }
  return {
    date: m[1],
    start: m[2],
    end: m[3],
    site: m[4],
  };
}

/**
 * LINEからのWebhookはすべてPOSTで届く。
 */
function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    const events = body.events || [];

    for (const ev of events) {
      if (ev.type !== "message" || ev.message.type !== "text") {
        continue;
      }
      handleTextMessage(ev);
    }
  } catch (err) {
    // Webhook側でエラーを投げるとLINE側の再送が延々続くため、
    // ログにだけ残して200を返す。
    console.error("doPost error: " + err);
  }
  return ContentService.createTextOutput(JSON.stringify({ status: "ok" }))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleTextMessage(ev) {
  const userId = ev.source.userId;
  const text = ev.message.text;
  const replyToken = ev.replyToken;

  const wish = parseWishMessage(text);

  if (!wish) {
    replyLine(
      replyToken,
      "希望の送り方は次の形式でお願いします。\n" +
        "「希望 2026-09-10 9:00-18:00 渋谷」\n" +
        "（日付 開始-終了 現場名の順）"
    );
    return;
  }

  const displayName = getLineDisplayName(userId);
  const wishId = Utilities.getUuid();
  const now = new Date();

  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME_WISH);
  sheet.appendRow([
    wishId,          // A: wish_id
    userId,          // B: line_user_id
    displayName,     // C: 氏名（LINE表示名）
    wish.date,       // D: 希望日
    wish.start,      // E: 開始
    wish.end,        // F: 終了
    wish.site,       // G: 第1希望現場
    "未処理",         // H: ステータス
    "",              // I: マッチ現場（確定時に記入）
    now,             // J: 登録日時
  ]);

  replyLine(
    replyToken,
    `希望を受け付けました。\n` +
      `${wish.date} ${wish.start}〜${wish.end}\n` +
      `第1希望：${wish.site}\n` +
      `マッチングが決まり次第、あらためてご連絡します。`
  );
}

/**
 * LINEのプロフィールAPIで表示名を取得する。
 * 取得に失敗した場合は、LINEのユーザーIDをそのまま氏名欄に入れておき、
 * あとで管理画面から正しい氏名に手動で紐づけられるようにする。
 */
function getLineDisplayName(userId) {
  try {
    const res = UrlFetchApp.fetch("https://api.line.me/v2/bot/profile/" + userId, {
      headers: { Authorization: "Bearer " + LINE_CHANNEL_ACCESS_TOKEN },
      muteHttpExceptions: true,
    });
    const data = JSON.parse(res.getContentText());
    return data.displayName || userId;
  } catch (err) {
    return userId;
  }
}

function replyLine(replyToken, message) {
  UrlFetchApp.fetch("https://api.line.me/v2/bot/message/reply", {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + LINE_CHANNEL_ACCESS_TOKEN },
    payload: JSON.stringify({
      replyToken: replyToken,
      messages: [{ type: "text", text: message }],
    }),
    muteHttpExceptions: true,
  });
}
