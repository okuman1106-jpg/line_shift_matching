# -*- coding: utf-8 -*-
"""
確定したシフトを、本人のLINEへ直接通知する。

Webhook受信（LINEからの受信）はGoogle Apps Script側が担当するが、
こちらの「確定通知（こちらからの送信）」はPythonから直接LINEの
Push APIを呼べば良いので、Streamlit側だけで完結する
（別サーバーは不要）。
"""
import requests
import streamlit as st

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def is_line_configured():
    try:
        return "LINE_CHANNEL_ACCESS_TOKEN" in st.secrets
    except Exception:
        return False


def send_confirmation(line_user_id: str, message: str):
    """
    確定通知を送る。トークン未設定（デモモード等）の場合は実際には送らず、
    「送信された想定の内容」を返すだけにする。
    """
    if not is_line_configured() or not line_user_id:
        return False, "（デモモードのため実際には送信していません）" + message

    token = st.secrets["LINE_CHANNEL_ACCESS_TOKEN"]
    try:
        res = requests.post(
            LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "to": line_user_id,
                "messages": [{"type": "text", "text": message}],
            },
            timeout=10,
        )
        if res.status_code == 200:
            return True, "送信しました。"
        return False, f"送信に失敗しました（HTTP {res.status_code}）：{res.text[:200]}"
    except Exception as e:
        return False, f"送信エラー：{e}"


def send_pool_alternatives(line_user_id: str, wish_id: str, wish_info: str,
                            alternatives: list):
    """
    第1希望が満席だった人に、近場の空いている現場をボタン（クイック
    リプライ）で提示する。本人がボタンをタップすると「振替希望:{wish_id}:
    {現場名}」という文章がそのままトーク欄に送信され、GAS側のWebhookが
    それを受け取って希望シートの「振替希望現場」に記録する。

    ここではあくまで「本人の意思表示」を集めるだけで、実際の確定は
    今まで通り管理者がアプリの画面から行う。

    alternatives … [{"現場": str, "空き人数": float}, ...]
    """
    if not is_line_configured() or not line_user_id:
        return False, "（デモモードのため実際には送信していません）"

    token = st.secrets["LINE_CHANNEL_ACCESS_TOKEN"]
    text = (
        f"{wish_info}\n\n"
        f"第1希望の現場が満席のため、まだ確定できていません。\n"
        f"近くで空いている現場があります。働いてみたい現場があれば、"
        f"下のボタンから選んでください（選んでもすぐに確定はせず、"
        f"管理者が確認したうえで確定のご連絡をします）。"
    )
    items = []
    for alt in alternatives[:12]:  # クイックリプライは最大13件までのため安全側に
        label = alt["現場"][:20]  # ボタンのラベルは20文字までの制限がある
        items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": label,
                "text": f"振替希望:{wish_id}:{alt['現場']}",
            },
        })

    try:
        res = requests.post(
            LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "to": line_user_id,
                "messages": [{
                    "type": "text",
                    "text": text,
                    "quickReply": {"items": items},
                }],
            },
            timeout=10,
        )
        if res.status_code == 200:
            return True, "送信しました。"
        return False, f"送信に失敗しました（HTTP {res.status_code}）：{res.text[:200]}"
    except Exception as e:
        return False, f"送信エラー：{e}"
