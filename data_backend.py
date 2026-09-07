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
                "第1希望現場", "ステータス", "マッチ現場", "登録日時",
                "振替希望現場", "プール通知済み"]
# 「時給」は現場ごとの基本時給（新規応募者向け）。空欄でもよい。
SITE_COLUMNS = ["現場名", "エリア", "時給"]
CONFIRMED_COLUMNS = ["shift_id", "氏名", "line_user_id", "現場", "日付",
                     "開始", "終了", "マッチング方法", "確定日時"]
# 「基準パターン」＝実績データから作る、現場ごとの時間帯別お手本ダイス。
# 時間帯は0〜47の通し番号（日またぎ対応）、基準人数は頭数（小数）。
REFERENCE_COLUMNS = ["現場", "時間帯", "基準人数", "対象期間", "作成日時"]
# 現場名の表記ゆれを吸収するための対応表（例："Kosugi 3rd" → "kosugi3rd Avenue"）
ALIAS_COLUMNS = ["表記ゆれ", "正式名"]
# スタッフごとの「優遇時給」。ここに載っている人だけ、現場の基本時給より
# 優先してこちらの時給が使われる（経験者への時給アップ等）。
# 載っていないスタッフは、現場の基本時給がそのまま適用される。
STAFF_WAGE_COLUMNS = ["氏名", "時給", "備考"]


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


@st.cache_resource(show_spinner=False)
def _get_spreadsheet():
    """
    スプレッドシート自体を開く処理は、Google側のAPIを1回消費する。
    以前は「希望」「現場マスタ」等を読み書きするたびに毎回開き直して
    いたため、画面を1回操作するだけで何度もAPIを呼び出してしまい、
    短時間にアクセスが集中してエラーになることがあった。
    ここでキャッシュし、開く処理自体は最初の1回だけにする。
    """
    client = _get_gspread_client()
    return client.open_by_key(st.secrets["SHEET_ID"])


def _get_sheet(sheet_name):
    sh = _get_spreadsheet()
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


@st.cache_data(ttl=10, show_spinner=False)
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


@st.cache_data(ttl=30, show_spinner=False)
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


@st.cache_data(ttl=10, show_spinner=False)
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
        load_wishes.clear()
        return

    df = _load_demo("希望", WISH_COLUMNS)
    if df.empty:
        return
    idx = df.index[df["wish_id"] == wish_id]
    if len(idx):
        df.loc[idx, "ステータス"] = status
        df.loc[idx, "マッチ現場"] = match_site
        _save_demo("希望", df)
    load_wishes.clear()


def update_wish_field(wish_id, field, value):
    """
    希望シートの該当行、指定した1つの列だけを更新する汎用関数。
    「振替希望現場」（本人がLINEで意思表示した現場）や「プール通知済み」
    （二重通知防止フラグ）の更新に使う。
    """
    if is_live_mode():
        ws = _get_sheet("希望")
        cell = ws.find(wish_id)
        if cell:
            row = cell.row
            header = ws.row_values(1)
            if field not in header:
                return False
            col = header.index(field) + 1
            ws.update_cell(row, col, value)
        load_wishes.clear()
        return True

    df = _load_demo("希望", WISH_COLUMNS)
    if df.empty:
        return False
    idx = df.index[df["wish_id"] == wish_id]
    if len(idx):
        df.loc[idx, field] = value
        _save_demo("希望", df)
    load_wishes.clear()
    return True


def append_confirmed(row: dict):
    """確定シフトシートに1行追加する。rowはCONFIRMED_COLUMNSのキーを持つ辞書。"""
    if is_live_mode():
        ws = _get_sheet("確定シフト")
        ws.append_row([row.get(c, "") for c in CONFIRMED_COLUMNS])
        load_confirmed.clear()
        return

    df = _load_demo("確定シフト", CONFIRMED_COLUMNS)
    new_row = pd.DataFrame([{c: row.get(c, "") for c in CONFIRMED_COLUMNS}])
    df = pd.concat([df, new_row], ignore_index=True)
    _save_demo("確定シフト", df)
    load_confirmed.clear()


@st.cache_data(ttl=10, show_spinner=False)
def load_reference():
    """現場ごとの、時間帯別お手本ダイス（基準パターン）を読み込む。"""
    if is_live_mode():
        try:
            ws = _get_sheet("基準パターン")
        except Exception:
            return pd.DataFrame(columns=REFERENCE_COLUMNS)
        records = ws.get_all_records()
        df = pd.DataFrame(records, dtype=str) if records else pd.DataFrame(columns=REFERENCE_COLUMNS)
        for c in REFERENCE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df[REFERENCE_COLUMNS]
    return _load_demo("基準パターン", REFERENCE_COLUMNS)


def save_reference(site, hour_pattern, now_str, period=""):
    """
    1つの現場について、48時間帯分の基準人数をまとめて保存する。
    複数の現場をまとめて保存したい場合は、save_reference_bulk() を
    使うこと（現場の数だけ通信が発生し、Google側の利用制限に
    かかりやすいため、この関数は「1つだけ直したい」ときの用途に限る）。
    hour_pattern … {時間帯(int): 基準人数(float)} の辞書
    """
    return save_reference_bulk({site: hour_pattern}, now_str, period)


def save_reference_bulk(patterns: dict, now_str: str, period: str = ""):
    """
    複数の現場ぶんの基準パターンを、まとめて1回の読み書きで保存する。
    現場の数だけ通信が発生する save_reference の繰り返し呼び出しでは、
    現場数が多いとGoogle側のアクセス制限（1分あたりの読み込み回数）に
    かかりやすいため、こちらでは既存データの読み込み・書き込みを
    それぞれ1回だけで済ませる。

    period … このデータの対象期間（例："2026-02"）。同じ現場でも、
             対象期間が違えば別のお手本として両方残る（洗い替えの対象は
             「現場・対象期間」が両方一致するものだけ）。これにより、
             2026年2月と2027年2月のダイスを両方保存して見比べられる。

    patterns … {現場名: {時間帯(int): 基準人数(float)}, ...}
    戻り値：更新できた現場数
    """
    new_rows_list = []
    for site, hour_pattern in patterns.items():
        for h, v in hour_pattern.items():
            if v > 0:
                new_rows_list.append({
                    "現場": site, "時間帯": str(h),
                    "基準人数": f"{v:.2f}", "対象期間": period,
                    "作成日時": now_str,
                })
    new_rows = pd.DataFrame(new_rows_list, dtype=str)
    target_sites = set(patterns.keys())

    def _is_target(row_site, row_period):
        return row_site in target_sites and row_period == period

    if is_live_mode():
        try:
            ws = _get_sheet("基準パターン")
        except Exception:
            return 0
        existing = ws.get_all_records()  # 読み込みは1回だけ
        df = pd.DataFrame(existing, dtype=str) if existing else pd.DataFrame(columns=REFERENCE_COLUMNS)
        for c in REFERENCE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        _mask = df.apply(lambda r: _is_target(r["現場"], r.get("対象期間", "")), axis=1) \
            if not df.empty else pd.Series([], dtype=bool)
        df = df[~_mask] if not df.empty else df
        df = pd.concat([df, new_rows], ignore_index=True)
        ws.clear()
        ws.append_row(REFERENCE_COLUMNS)
        if not df.empty:
            ws.append_rows(df[REFERENCE_COLUMNS].values.tolist())
        load_reference.clear()
        return len(target_sites)

    df = _load_demo("基準パターン", REFERENCE_COLUMNS)
    _mask = df.apply(lambda r: _is_target(r["現場"], r.get("対象期間", "")), axis=1) \
        if not df.empty else pd.Series([], dtype=bool)
    df = df[~_mask] if not df.empty else df
    df = pd.concat([df, new_rows], ignore_index=True)
    _save_demo("基準パターン", df)
    load_reference.clear()
    return len(target_sites)


@st.cache_data(ttl=30, show_spinner=False)
def load_aliases():
    """現場名の表記ゆれ対応表（表記ゆれ → 正式名）を読み込む。"""
    if is_live_mode():
        try:
            ws = _get_sheet("現場名エイリアス")
        except Exception:
            return pd.DataFrame(columns=ALIAS_COLUMNS)
        records = ws.get_all_records()
        df = pd.DataFrame(records, dtype=str) if records else pd.DataFrame(columns=ALIAS_COLUMNS)
        for c in ALIAS_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df[ALIAS_COLUMNS]
    return _load_demo("現場名エイリアス", ALIAS_COLUMNS)


def add_alias(variant: str, canonical: str):
    """
    表記ゆれ1件を対応表に追加する（既に同じ表記ゆれが登録されていれば
    上書きする）。
    """
    variant = variant.strip()
    canonical = canonical.strip()
    if not variant or not canonical:
        return False

    if is_live_mode():
        try:
            ws = _get_sheet("現場名エイリアス")
        except Exception:
            return False
        existing = ws.get_all_records()
        df = pd.DataFrame(existing, dtype=str) if existing else pd.DataFrame(columns=ALIAS_COLUMNS)
        for c in ALIAS_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df = df[df["表記ゆれ"] != variant]
        new_row = pd.DataFrame([{"表記ゆれ": variant, "正式名": canonical}], dtype=str)
        df = pd.concat([df, new_row], ignore_index=True)
        ws.clear()
        ws.append_row(ALIAS_COLUMNS)
        if not df.empty:
            ws.append_rows(df[ALIAS_COLUMNS].values.tolist())
        load_aliases.clear()
        return True

    df = _load_demo("現場名エイリアス", ALIAS_COLUMNS)
    df = df[df["表記ゆれ"] != variant]
    new_row = pd.DataFrame([{"表記ゆれ": variant, "正式名": canonical}], dtype=str)
    df = pd.concat([df, new_row], ignore_index=True)
    _save_demo("現場名エイリアス", df)
    load_aliases.clear()
    return True


def delete_alias(variant: str):
    """表記ゆれの登録を1件削除する（間違えて登録した場合の取り消し用）。"""
    variant = variant.strip()
    if not variant:
        return False

    if is_live_mode():
        try:
            ws = _get_sheet("現場名エイリアス")
        except Exception:
            return False
        existing = ws.get_all_records()
        df = pd.DataFrame(existing, dtype=str) if existing else pd.DataFrame(columns=ALIAS_COLUMNS)
        for c in ALIAS_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df = df[df["表記ゆれ"] != variant]
        ws.clear()
        ws.append_row(ALIAS_COLUMNS)
        if not df.empty:
            ws.append_rows(df[ALIAS_COLUMNS].values.tolist())
        load_aliases.clear()
        return True

    df = _load_demo("現場名エイリアス", ALIAS_COLUMNS)
    df = df[df["表記ゆれ"] != variant]
    _save_demo("現場名エイリアス", df)
    load_aliases.clear()
    return True


@st.cache_data(ttl=30, show_spinner=False)
def load_staff_wages():
    """スタッフごとの優遇時給の一覧を読み込む（載っていない人は現場の基本時給を使う）。"""
    if is_live_mode():
        try:
            ws = _get_sheet("スタッフ時給")
        except Exception:
            return pd.DataFrame(columns=STAFF_WAGE_COLUMNS)
        records = ws.get_all_records()
        df = pd.DataFrame(records, dtype=str) if records else pd.DataFrame(columns=STAFF_WAGE_COLUMNS)
        for c in STAFF_WAGE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df[STAFF_WAGE_COLUMNS]
    return _load_demo("スタッフ時給", STAFF_WAGE_COLUMNS)


def save_staff_wage(name: str, wage: str, note: str = ""):
    """1人ぶんの優遇時給を登録・更新する（既存があれば上書き）。"""
    name = name.strip()
    if not name:
        return False

    if is_live_mode():
        try:
            ws = _get_sheet("スタッフ時給")
        except Exception:
            return False
        existing = ws.get_all_records()
        df = pd.DataFrame(existing, dtype=str) if existing else pd.DataFrame(columns=STAFF_WAGE_COLUMNS)
        for c in STAFF_WAGE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df = df[df["氏名"] != name]
        new_row = pd.DataFrame([{"氏名": name, "時給": str(wage), "備考": note}], dtype=str)
        df = pd.concat([df, new_row], ignore_index=True)
        ws.clear()
        ws.append_row(STAFF_WAGE_COLUMNS)
        if not df.empty:
            ws.append_rows(df[STAFF_WAGE_COLUMNS].values.tolist())
        load_staff_wages.clear()
        return True

    df = _load_demo("スタッフ時給", STAFF_WAGE_COLUMNS)
    df = df[df["氏名"] != name]
    new_row = pd.DataFrame([{"氏名": name, "時給": str(wage), "備考": note}], dtype=str)
    df = pd.concat([df, new_row], ignore_index=True)
    _save_demo("スタッフ時給", df)
    load_staff_wages.clear()
    return True


def delete_staff_wage(name: str):
    """スタッフの優遇時給の登録を削除する（削除後は現場の基本時給に戻る）。"""
    name = name.strip()
    if not name:
        return False

    if is_live_mode():
        try:
            ws = _get_sheet("スタッフ時給")
        except Exception:
            return False
        existing = ws.get_all_records()
        df = pd.DataFrame(existing, dtype=str) if existing else pd.DataFrame(columns=STAFF_WAGE_COLUMNS)
        for c in STAFF_WAGE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df = df[df["氏名"] != name]
        ws.clear()
        ws.append_row(STAFF_WAGE_COLUMNS)
        if not df.empty:
            ws.append_rows(df[STAFF_WAGE_COLUMNS].values.tolist())
        load_staff_wages.clear()
        return True

    df = _load_demo("スタッフ時給", STAFF_WAGE_COLUMNS)
    df = df[df["氏名"] != name]
    _save_demo("スタッフ時給", df)
    load_staff_wages.clear()
    return True


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
