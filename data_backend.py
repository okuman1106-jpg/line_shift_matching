# -*- coding: utf-8 -*-
"""
データの読み書き層。

本番運用ではGoogleスプレッドシート（gspread経由）を使うが、
st.secrets にサービスアカウント情報が無い環境（動作確認・デモ）では、
自動的にローカルCSVファイルにフォールバックする。

呼び出し側（matching.py・app.py）は、このモジュールが本番かデモかを
意識する必要が無いように、同じ関数名・同じ形（pandas.DataFrame）で
やり取りできるようにしてある。
"""
from pathlib import Path

import pandas as pd
import streamlit as st

DEMO_DIR = Path(__file__).parent / "demo_data"
DEMO_DIR.mkdir(exist_ok=True)

WISH_COLUMNS = ["wish_id", "line_user_id", "氏名", "希望日", "開始", "終了",
                "第1希望現場", "ステータス", "マッチ現場", "登録日時"]
SITE_COLUMNS = ["現場名", "エリア"]
CONFIRMED_COLUMNS = ["shift_id", "氏名", "line_user_id", "現場", "日付",
                     "開始", "終了", "マッチング方法", "確定日時"]


def is_live_mode():
    """st.secretsにGoogleスプレッドシートの接続情報があれば本番モード。"""
    try:
        return "gcp_service_account" in st.secrets and "SHEET_ID" in st.secrets
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def _get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes)
    return gspread.authorize(creds)


def _get_sheet(sheet_name):
    client = _get_gspread_client()
    sh = client.open_by_key(st.secrets["SHEET_ID"])
    return sh.worksheet(sheet_name)


def _demo_path(name):
    return DEMO_DIR / f"{name}.csv"


def _load_demo(name, columns):
    path = _demo_path(name)
    if path.exists():
        df = pd.read_csv(path, dtype=str).fillna("")
        for c in columns:
            if c not in df.columns:
                df[c] = ""
        return df[columns]
    return pd.DataFrame(columns=columns)


def _save_demo(name, df):
    df.to_csv(_demo_path(name), index=False)


def load_wishes():
    if is_live_mode():
        ws = _get_sheet("希望")
        records = ws.get_all_records()
        df = pd.DataFrame(records, dtype=str) if records else pd.DataFrame(columns=WISH_COLUMNS)
        for c in WISH_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df[WISH_COLUMNS]
    return _load_demo("希望", WISH_COLUMNS)


def load_sites():
    if is_live_mode():
        ws = _get_sheet("現場マスタ")
        records = ws.get_all_records()
        df = pd.DataFrame(records, dtype=str) if records else pd.DataFrame(columns=SITE_COLUMNS)
        for c in SITE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df[SITE_COLUMNS]
    return _load_demo("現場マスタ", SITE_COLUMNS)


def load_confirmed():
    if is_live_mode():
        ws = _get_sheet("確定シフト")
        records = ws.get_all_records()
        df = pd.DataFrame(records, dtype=str) if records else pd.DataFrame(columns=CONFIRMED_COLUMNS)
        for c in CONFIRMED_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df[CONFIRMED_COLUMNS]
    return _load_demo("確定シフト", CONFIRMED_COLUMNS)


def update_wish_status(wish_id, status, match_site=""):
    """希望シートの該当行のステータス・マッチ現場を更新する。"""
    if is_live_mode():
        ws = _get_sheet("希望")
        cell = ws.find(wish_id)
        if cell:
            row = cell.row
            header = ws.row_values(1)
            status_col = header.index("ステータス") + 1
            match_col = header.index("マッチ現場") + 1
            ws.update_cell(row, status_col, status)
            ws.update_cell(row, match_col, match_site)
        return

    df = _load_demo("希望", WISH_COLUMNS)
    if df.empty:
        return
    idx = df.index[df["wish_id"] == wish_id]
    if len(idx):
        df.loc[idx, "ステータス"] = status
        df.loc[idx, "マッチ現場"] = match_site
        _save_demo("希望", df)


def append_confirmed(row: dict):
    """確定シフトシートに1行追加する。rowはCONFIRMED_COLUMNSのキーを持つ辞書。"""
    if is_live_mode():
        ws = _get_sheet("確定シフト")
        ws.append_row([row.get(c, "") for c in CONFIRMED_COLUMNS])
        return

    df = _load_demo("確定シフト", CONFIRMED_COLUMNS)
    new_row = pd.DataFrame([{c: row.get(c, "") for c in CONFIRMED_COLUMNS}])
    df = pd.concat([df, new_row], ignore_index=True)
    _save_demo("確定シフト", df)


def seed_demo_data():
    """デモモード用の初期データが無ければ作成する（動作確認しやすくするため）。"""
    if not _demo_path("希望").exists():
        _save_demo("希望", pd.DataFrame([
            {"wish_id": "w1", "line_user_id": "U001", "氏名": "佐藤健一",
             "希望日": "2026-09-10", "開始": "09:00", "終了": "18:00",
             "第1希望現場": "渋谷現場", "ステータス": "未処理", "マッチ現場": "",
             "登録日時": "2026-09-05 10:00"},
            {"wish_id": "w2", "line_user_id": "U002", "氏名": "田中花子",
             "希望日": "2026-09-10", "開始": "09:00", "終了": "18:00",
             "第1希望現場": "渋谷現場", "ステータス": "未処理", "マッチ現場": "",
             "登録日時": "2026-09-05 10:05"},
            {"wish_id": "w3", "line_user_id": "U003", "氏名": "鈴木一郎",
             "希望日": "2026-09-10", "開始": "10:00", "終了": "19:00",
             "第1希望現場": "渋谷現場", "ステータス": "未処理", "マッチ現場": "",
             "登録日時": "2026-09-05 10:10"},
        ], dtype=str))
    if not _demo_path("現場マスタ").exists():
        _save_demo("現場マスタ", pd.DataFrame([
            {"現場名": "渋谷現場", "エリア": "城南エリア"},
            {"現場名": "新宿現場", "エリア": "城南エリア"},
            {"現場名": "赤坂現場", "エリア": "城南エリア"},
            {"現場名": "六本木現場", "エリア": "城南エリア"},
            {"現場名": "上野現場", "エリア": "城東エリア"},
        ], dtype=str))
    if not _demo_path("確定シフト").exists():
        _save_demo("確定シフト", pd.DataFrame([
            {"shift_id": "c1", "氏名": "佐藤健一", "line_user_id": "U001",
             "現場": "渋谷現場", "日付": "2026-08-01", "開始": "09:00", "終了": "18:00",
             "マッチング方法": "自動", "確定日時": "2026-08-01 08:00"},
            {"shift_id": "c2", "氏名": "佐藤健一", "line_user_id": "U001",
             "現場": "渋谷現場", "日付": "2026-08-08", "開始": "09:00", "終了": "18:00",
             "マッチング方法": "自動", "確定日時": "2026-08-08 08:00"},
            {"shift_id": "c3", "氏名": "佐藤健一", "line_user_id": "U001",
             "現場": "渋谷現場", "日付": "2026-08-15", "開始": "09:00", "終了": "18:00",
             "マッチング方法": "自動", "確定日時": "2026-08-15 08:00"},
        ], dtype=str))
