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
