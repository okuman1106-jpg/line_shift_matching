# -*- coding: utf-8 -*-
"""
シフト希望マッチング 管理画面
================================
LINEから届いたスタッフの希望シフトを一覧表示し、
「自動提案＋管理者がポチポチ確認」の方式でマッチングを確定する。

確定すると：
  1. 確定シフトシートに記録される
  2. 希望シートのステータスが更新される
  3. 本人にLINEで確定通知が送られる（本番モードのみ）

st.secrets に以下が設定されていれば「本番モード」（Googleスプレッドシート
・LINE通知が実際に動く）、無ければ自動的に「デモモード」（ローカルCSV・
通知は送信シミュレーションのみ）で動作する。

  [gcp_service_account]
  type = "service_account"
  ...（サービスアカウントJSONの中身をそのままTOML形式で）

  SHEET_ID = "スプレッドシートのID"
  LINE_CHANNEL_ACCESS_TOKEN = "LINEのチャネルアクセストークン"
"""
import html as html_lib
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from data_backend import (
    is_live_mode, load_wishes, load_sites, load_confirmed,
    update_wish_status, update_wish_field, append_confirmed, seed_demo_data,
    load_reference, save_reference, save_reference_bulk, save_reference_full,
    save_reference_multi_period,
    load_aliases, add_alias, delete_alias,
    load_staff_wages, save_staff_wage, delete_staff_wage,
)
from matching import (
    build_suggestions, build_board, build_hour_breakdown, build_day_hourly_board,
    build_reference_pattern, build_reference_pattern_from_hourly,
    reference_df_to_array, detect_period_label, list_reference_periods,
    build_staff_dice_rows, build_seat_grid, build_daily_dice_from_confirmed,
    generate_seat_list, annotate_seat_list_with_occupancy,
    normalize_site_name, find_unmatched_site_names,
    compute_labor_cost, compute_labor_cost_precise, get_wage_for,
    find_pool_wishes, suggest_alternative_slots, build_seat_diff,
    renormalize_reference_sites, extract_daily_hourly_matrix,
    build_weekday_hourly_matrix, classify_dice_shape,
)
from matching import _WEEKDAY_ORDER as _WEEKDAY_ORDER_APP
from line_notify import send_confirmation, send_pool_alternatives


def guess_col(cols, cands):
    """列名の一覧から、候補キーワードを含む最初の列を推測する。"""
    return next((c for c in cols if any(k in c for k in cands)), None)


def parse_actual_csv_flexible(file):
    """
    お手本ダイス用の縦長形式（現場名・日付・時間帯・頭数）、または
    マトリクス形式（現場名・日付・0時・1時…）のどちらのCSVでも読み込み、
    縦長形式のDataFrameに揃えて返す。どちらの形式でもなければNoneを返す。
    """
    try:
        raw = pd.read_csv(file, encoding="utf-8-sig", dtype=str)
    except UnicodeDecodeError:
        file.seek(0)
        raw = pd.read_csv(file, encoding="cp932", dtype=str)
    cols = list(raw.columns)
    hcols = [c for c in cols if re.match(r"^\d{1,2}時$", c)]
    if {"現場名", "日付", "時間帯", "頭数"}.issubset(set(cols)):
        return raw
    if {"現場名", "日付"}.issubset(set(cols)) and hcols:
        melted = raw.melt(
            id_vars=["現場名", "日付"], value_vars=hcols,
            var_name="時間帯", value_name="頭数")
        melted["時間帯"] = melted["時間帯"].str.replace("時", "", regex=False)
        melted["頭数"] = pd.to_numeric(melted["頭数"], errors="coerce").fillna(0)
        return melted[melted["頭数"] > 0]
    return None


def render_hourly_html(confirmed_heads, pending_heads, confirmed_names, pending_names,
                        reference_heads=None):
    """
    0〜47時間帯の確定・希望人数を、ダイス表示と同じ考え方（1時間ごとの
    マス）で、色付きの表として組み立てる。動きがある時間帯の前後だけを
    表示し、テーブルが無駄に横長にならないようにする。
    数字の下に、その時間帯にかかっている人の氏名を小さく添える
    （「そのマスに実際は誰が入っているか」が一目で分かるようにするため）。

    reference_heads … 実績データから作った「お手本ダイス」（48要素）。
                       指定があれば、確定・希望と並べて一番上に表示し、
                       見比べながらマッチングを進められるようにする。
    """
    reference_heads = reference_heads or [0.0] * 48
    active = [h for h in range(48)
              if confirmed_heads[h] > 0 or pending_heads[h] > 0 or reference_heads[h] > 0]
    if active:
        lo = max(0, min(active) - 2)
        hi = min(47, max(active) + 2)
    else:
        lo, hi = 6, 23
    hours = list(range(lo, hi + 1))

    def hour_label(h):
        return f"{h % 24}時" + ("+1" if h >= 24 else "")

    def fmt(v):
        return "" if v == 0 else (str(int(v)) if v == int(v) else f"{v:.1f}")

    def esc(v):
        return html_lib.escape(str(v))

    def name_html(names):
        if not names:
            return ""
        return "<br>".join(
            f'<span style="font-size:10px;color:#555;">{esc(n)}</span>' for n in names)

    parts = ['<div style="overflow-x:auto;">'
             '<table style="border-collapse:collapse;font-size:12px;">']
    parts.append('<tr><th style="border:1px solid #ddd;padding:4px 6px;'
                 'background:#f5f5f5;text-align:left;">時間帯</th>')
    for h in hours:
        parts.append(f'<th style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:#f5f5f5;white-space:nowrap;">{hour_label(h)}</th>')
    parts.append('</tr>')

    if any(v > 0 for v in reference_heads):
        parts.append('<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                     'background:#eef2fb;font-weight:bold;white-space:nowrap;'
                     'vertical-align:top;">お手本</td>')
        for h in hours:
            v = reference_heads[h]
            parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                         f'text-align:center;background:#eef2fb;color:#3355aa;'
                         f'vertical-align:top;min-width:56px;">{fmt(v)}</td>')
        parts.append('</tr>')

    parts.append('<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                 'background:#fafafa;font-weight:bold;white-space:nowrap;'
                 'vertical-align:top;">確定</td>')
    for h in hours:
        v = confirmed_heads[h]
        bg = "#8fd19e" if v >= 2 else ("#d9f0df" if v > 0 else "#ffffff")
        parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                     f'text-align:center;background:{bg};vertical-align:top;'
                     f'min-width:56px;">{fmt(v)}<br>{name_html(confirmed_names[h])}</td>')
    parts.append('</tr>')

    parts.append('<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                 'background:#fafafa;font-weight:bold;white-space:nowrap;'
                 'vertical-align:top;">希望</td>')
    for h in hours:
        v = pending_heads[h]
        bg = "#ffe9a8" if v > 0 else "#ffffff"
        parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                     f'text-align:center;background:{bg};vertical-align:top;'
                     f'min-width:56px;">{fmt(v)}<br>{name_html(pending_names[h])}</td>')
    parts.append('</tr>')

    parts.append('</table></div>')
    return "".join(parts)


def render_board_html(labels, counts, pendings, row_header="現場＼日付", col_label_fn=None):
    """
    「現場 × 列（日付 or 時間帯）」の盤面を、色付きのHTML表として組み立てる。
      緑の濃さ … 確定人数が多いほど濃くなる
      黄色    … 確定はまだ無いが、未処理の希望がある（要対応の目印）

    col_label_fn … 列見出しの表示文字列を作る関数。指定が無ければ、
                   日付の月日部分（例："09-10"）をそのまま使う（従来通り）。
                   時間帯の盤面では、例えば "9時" のようなラベルを返す
                   関数を渡す。
    """
    def esc(v):
        return html_lib.escape(str(v))

    if col_label_fn is None:
        col_label_fn = lambda c: c[5:] if len(str(c)) >= 5 else c

    parts = ['<div style="overflow-x:auto;">'
             '<table style="border-collapse:collapse;font-size:13px;width:100%;">']
    parts.append('<tr>')
    parts.append(
        f'<th style="border:1px solid #ddd;padding:8px;background:#f5f5f5;'
        f'position:sticky;left:0;z-index:1;text-align:left;">{esc(row_header)}</th>')
    for col in labels.columns:
        short = esc(col_label_fn(col))
        parts.append(
            f'<th style="border:1px solid #ddd;padding:8px;background:#f5f5f5;'
            f'white-space:nowrap;">{short}</th>')
    parts.append('</tr>')

    for site in labels.index:
        parts.append('<tr>')
        parts.append(
            f'<td style="border:1px solid #ddd;padding:8px;background:#fafafa;'
            f'font-weight:bold;white-space:nowrap;position:sticky;left:0;">'
            f'{esc(site)}</td>')
        for col in labels.columns:
            c = float(counts.loc[site, col])
            p = float(pendings.loc[site, col])
            label = esc(labels.loc[site, col])
            if c >= 3:
                bg = "#8fd19e"
            elif c >= 1:
                bg = "#d9f0df"
            elif p >= 1:
                bg = "#ffe9a8"
            else:
                bg = "#ffffff"
            parts.append(
                f'<td style="border:1px solid #ddd;padding:8px;text-align:center;'
                f'background:{bg};white-space:nowrap;">{label}</td>')
        parts.append('</tr>')

    parts.append('</table></div>')
    parts.append(
        '<p style="font-size:12px;color:#666;margin-top:8px;">'
        '色の濃い緑ほど確定人数が多いマスです。黄色は、確定はまだ無いが'
        '未処理の希望が来ているマス（対応が必要）です。</p>')
    return "".join(parts)


def render_dice_matrix_html(reference_heads, rows):
    """
    「現場×日付」の実際のダイス表：縦にスタッフ名（1人1行）、横に
    47時間帯（0〜47時、日またぎ対応）を並べたマトリクス。
    一番上に「お手本」の行を置き、見比べながら確認できるようにする。

    rows … build_staff_dice_rows() が返す、1人1行のデータのリスト
    """
    active = set(h for h in range(48) if reference_heads[h] > 0)
    for row in rows:
        active |= set(h for h in range(48) if row["hours"][h] > 0)
    if active:
        lo = max(0, min(active) - 2)
        hi = min(47, max(active) + 2)
    else:
        lo, hi = 6, 23
    hours = list(range(lo, hi + 1))

    def hour_label(h):
        return f"{h % 24}時" + ("+1" if h >= 24 else "")

    def fmt(v):
        return "" if v == 0 else (str(int(v)) if v == int(v) else f"{v:.1f}")

    def esc(v):
        return html_lib.escape(str(v))

    parts = ['<div style="overflow-x:auto;">'
             '<table style="border-collapse:collapse;font-size:12px;">']
    parts.append('<tr><th style="border:1px solid #ddd;padding:4px 6px;'
                 'background:#f5f5f5;text-align:left;position:sticky;left:0;'
                 'z-index:1;">氏名</th>')
    for h in hours:
        parts.append(f'<th style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:#f5f5f5;white-space:nowrap;">{hour_label(h)}</th>')
    parts.append('</tr>')

    if any(v > 0 for v in reference_heads):
        parts.append('<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                     'background:#eef2fb;font-weight:bold;white-space:nowrap;'
                     'position:sticky;left:0;">お手本</td>')
        for h in hours:
            v = reference_heads[h]
            parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                         f'text-align:center;background:#eef2fb;color:#3355aa;">'
                         f'{fmt(v)}</td>')
        parts.append('</tr>')

    for row in rows:
        is_confirmed = row["種別"] == "確定"
        row_bg = "#e9f7ee" if is_confirmed else "#fff8e6"
        cell_bg = "#8fd19e" if is_confirmed else "#ffe9a8"
        badge = "確定" if is_confirmed else "希望"
        label = (f'{esc(row["氏名"])}'
                 f'<br><span style="font-size:10px;color:#666;">{badge}</span>')
        parts.append(f'<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:{row_bg};font-weight:bold;white-space:nowrap;'
                     f'position:sticky;left:0;">{label}</td>')
        for h in hours:
            v = row["hours"][h]
            bg = cell_bg if v > 0 else "#ffffff"
            parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                         f'text-align:center;background:{bg};">{fmt(v)}</td>')
        parts.append('</tr>')

    parts.append('</table></div>')
    return "".join(parts)


def render_seat_grid_html(seat_counts, occupancy, hours):
    """
    「座席番号（縦）× 時間帯（横）」の試作マス目を表示する。
    座席1つ1つのマスに、氏名と状態（確定／希望／希望(超過)）を表示する。
    """
    def esc(v):
        return html_lib.escape(str(v))

    max_seats = max([seat_counts[h] for h in hours] + [0])
    # 超過枠も含めた最大座席番号を求める
    for (h, seat_no) in occupancy:
        if h in hours:
            max_seats = max(max_seats, seat_no)

    def hour_label(h):
        return f"{h % 24}時" + ("+1" if h >= 24 else "")

    color_map = {"確定": "#8fd19e", "希望": "#ffe9a8", "希望(超過)": "#f3c6c6"}

    parts = ['<div style="overflow-x:auto;">'
             '<table style="border-collapse:collapse;font-size:12px;">']
    parts.append('<tr><th style="border:1px solid #ddd;padding:4px 6px;'
                 'background:#f5f5f5;text-align:left;position:sticky;left:0;'
                 'z-index:1;">座席</th>')
    for h in hours:
        parts.append(f'<th style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:#f5f5f5;white-space:nowrap;">{hour_label(h)}</th>')
    parts.append('</tr>')

    for seat_no in range(1, max_seats + 1):
        parts.append(f'<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:#fafafa;font-weight:bold;white-space:nowrap;'
                     f'position:sticky;left:0;">座席{seat_no}</td>')
        for h in hours:
            info = occupancy.get((h, seat_no))
            is_slot = seat_no <= seat_counts[h]
            if info:
                bg = color_map.get(info["状態"], "#ffffff")
                text = f'{esc(info["氏名"])}<br><span style="font-size:9px;color:#666;">{esc(info["状態"])}</span>'
            elif is_slot:
                bg = "#ffffff"
                text = '<span style="color:#bbb;">空席</span>'
            else:
                bg = "#f0f0f0"
                text = ""
            parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                         f'text-align:center;background:{bg};min-width:56px;'
                         f'vertical-align:top;">{text}</td>')
        parts.append('</tr>')

    parts.append('</table></div>')
    return "".join(parts)


def render_seat_diff_html(diff_list, hours):
    """
    「お手本の座席数」と「実際の確定人数」の差分を、1時間ごとに
    色分けして表示する。
      赤（濃いほど差が大きい）… お手本より増員
      青（濃いほど差が大きい）… お手本より欠員
      白                     … お手本どおり
    """
    def esc(v):
        return html_lib.escape(str(v))

    def fmt(v):
        if v == 0:
            return "±0"
        sign = "+" if v > 0 else ""
        return f"{sign}{v:g}"

    def diff_bg(v):
        if v > 0:
            shade = min(1.0, v / 3.0)
            return f"rgba(220,60,60,{0.15 + shade * 0.55:.2f})"
        elif v < 0:
            shade = min(1.0, abs(v) / 3.0)
            return f"rgba(60,100,220,{0.15 + shade * 0.55:.2f})"
        return "#ffffff"

    def hour_label(h):
        return f"{h % 24}時" + ("+1" if h >= 24 else "")

    parts = ['<div style="overflow-x:auto;">'
             '<table style="border-collapse:collapse;font-size:12px;">']
    parts.append('<tr><th style="border:1px solid #ddd;padding:4px 6px;'
                 'background:#f5f5f5;text-align:left;">時間帯</th>')
    for h in hours:
        parts.append(f'<th style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:#f5f5f5;white-space:nowrap;">{esc(hour_label(h))}</th>')
    parts.append('</tr>')

    for row_key in ["お手本", "確定", "増減"]:
        parts.append(f'<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                     f'background:#fafafa;font-weight:bold;white-space:nowrap;">'
                     f'{esc(row_key)}</td>')
        for h in hours:
            if row_key == "増減":
                v = diff_list[h]["差分"]
                bg = diff_bg(v)
                text = fmt(v)
            else:
                v = diff_list[h][row_key]
                bg = "#ffffff"
                text = "" if not v else (str(int(v)) if v == int(v) else f"{v:g}")
            parts.append(f'<td style="border:1px solid #ddd;padding:4px 6px;'
                         f'text-align:center;background:{bg};">{text}</td>')
        parts.append('</tr>')

    parts.append('</table></div>')
    parts.append(
        '<p style="font-size:12px;color:#666;margin-top:8px;">'
        '<span style="color:#c0392b;">赤</span>＝お手本より増員、'
        '<span style="color:#2255cc;">青</span>＝お手本より欠員。'
        '色が濃いほど差が大きいことを示します。</p>')
    return "".join(parts)


def render_alias_helper(raw_names, sites_df, aliases_df, key_prefix):
    """
    アップロードされたデータの現場名のうち、現場マスタに無いもの
    （表記ゆれの可能性があるもの）を検出し、その場でエイリアス登録
    できるミニUIを表示する。登録すると、正規化した結果が次の画面
    更新から反映される。
    """
    unmatched = find_unmatched_site_names(raw_names, sites_df, aliases_df)
    if not unmatched:
        return
    st.warning(
        f"⚠️ 現場マスタに見つからない現場名が {len(unmatched)} 件あります。"
        "表記ゆれの可能性があります。下で対応する正式な現場名を選んで"
        "登録すると、次回から自動的に読み替えられます。")
    site_options = list(sites_df["現場名"]) if not sites_df.empty else []
    for i, name in enumerate(unmatched):
        ac1, ac2, ac3 = st.columns([2, 2, 1])
        ac1.write(f"`{name}`")
        if site_options:
            selected = ac2.selectbox(
                "対応する正式な現場名", site_options,
                key=f"{key_prefix}_alias_target_{i}", label_visibility="collapsed")
            if ac3.button("登録", key=f"{key_prefix}_alias_btn_{i}"):
                if add_alias(name, selected):
                    st.success(f"「{name}」→「{selected}」として登録しました。")
                    st.rerun()
        else:
            ac2.caption("現場マスタが空です。先に現場を登録してください。")


st.set_page_config(page_title="シフト希望マッチング", layout="wide")
st.title("🧩 シフト希望マッチング")

if is_live_mode():
    st.success("🟢 本番モード（Googleスプレッドシート・LINE通知に接続しています）")
else:
    st.warning(
        "🟡 デモモード（st.secretsに接続情報が無いため、ローカルのサンプル"
        "データで動作しています。実際のスプレッドシート・LINE通知には"
        "つながっていません。SETUP_GUIDE.mdの手順で接続すると本番モードに"
        "切り替わります。）")
    seed_demo_data()

st.caption(
    "スタッフがLINEで送った希望を、経験・混雑状況をもとに自動で提案します。"
    "最終的な確定は、この画面でボタンを押した時点で行われます"
    "（提案されたものを自動で確定するわけではありません）。")

wishes_df = load_wishes()
sites_df = load_sites()
confirmed_df = load_confirmed()
aliases_df = load_aliases()
staff_wages_df = load_staff_wages()

with st.sidebar:
    st.markdown("### 👤 あなたの立場")
    _role = st.radio(
        "立場を選んでください", ["現場担当者（ユニット長）", "承認者（社長）"],
        key="user_role")
    is_approver = False
    if _role == "承認者（社長）":
        try:
            _approver_password = st.secrets.get("APPROVER_PASSWORD", "")
        except Exception:
            _approver_password = ""
        if not _approver_password:
            st.warning(
                "承認者用のパスワードが設定されていません（st.secretsに"
                "APPROVER_PASSWORDが未設定）。デモとしてそのまま進めます。")
            is_approver = True
        else:
            _input_pw = st.text_input("パスワード", type="password", key="approver_pw")
            if _input_pw == _approver_password:
                is_approver = True
                st.success("承認者として認証されました。")
            elif _input_pw:
                st.error("パスワードが違います。")

    st.markdown("---")
    st.header("🖥️ 表示するセクション")
    st.caption(
        "チェックを外したセクションは表示されません。今使わない機能は"
        "隠しておくと、画面が短くなって操作しやすくなります。")
    _SECTIONS = [
        ("board_detail", "📋 現場×日付の内訳（詳細表）", True),
        ("day_hourly_board", "📆 1日×全現場×47時間帯", True),
        ("dice_matrix", "🎲 現場×日付の実際のダイス表・座席・増減表", True),
        ("reference_import", "📥 実績データの取り込み（お手本ダイス作成）", True),
        ("reference_compare", "📈 期間ごとのお手本を見比べる", True),
        ("daily_compare", "📅 日付×時間帯で実績を比較する（日別）", True),
        ("muscle_day", "🏋️ 筋肉質な日を選んでお手本にする", True),
        ("reference_export", "📤 保存済みのお手本ダイスを書き出す", True),
        ("seat_generation", "🪑 座席番号の一括生成", True),
        ("pool_list", "🏊 プール要員一覧", True),
        ("matching_candidates", "📋 マッチング候補の確認", True),
        ("confirmed_list", "✅ 確定済みシフト一覧", True),
        ("site_master", "🧑‍🤝‍🧑 現場マスタ", True),
        ("alias_management", "🔤 現場名エイリアスの管理", True),
    ]
    SHOW = {k: st.checkbox(lbl, value=dv, key=f"show_{k}")
            for k, lbl, dv in _SECTIONS}

st.markdown("---")

c1, c2, c3 = st.columns(3)
c1.metric("未処理の希望", int((wishes_df["ステータス"] == "未処理").sum()))
c2.metric("マッチ済み", int((wishes_df["ステータス"] == "マッチ済").sum()))
c3.metric("登録されている現場数", len(sites_df))

if is_approver:
    st.markdown("---")
    st.header("🏢 承認者専用：予算ビュー")
    st.caption(
        "確定済みシフトから、現場ごとの実働時間・人件費を集計します。"
        "優遇時給が登録されているスタッフはその時給、それ以外は現場の"
        "基本時給を使って計算します（どちらも未設定の場合は0円になります）。")

    _cost_mode = st.radio(
        "計算方式", ["精密（深夜・残業割増込み）", "簡易（時給×時間のみ）"],
        key="cost_mode", horizontal=True,
        help="「精密」は、前の特許用アプリ（人件費ダイス分析システム）と"
             "同じ考え方で、深夜割増・残業割増を計算に含めます。")

    if _cost_mode == "精密（深夜・残業割増込み）":
        _wcol1, _wcol2 = st.columns(2)
        _night_rate = _wcol1.slider(
            "深夜割増（22時〜翌5時）", 0, 100, 25, step=5, key="night_rate") / 100
        _ot_rate = _wcol2.slider(
            "残業割増（1日8時間超）", 0, 100, 25, step=5, key="ot_rate") / 100
        _cost_df = compute_labor_cost_precise(
            confirmed_df, sites_df, staff_wages_df,
            night_rate=_night_rate, ot_rate=_ot_rate)
    else:
        _cost_df = compute_labor_cost(confirmed_df, sites_df, staff_wages_df)

    if _cost_df.empty:
        st.info("まだ確定したシフトがありません。")
    else:
        _budget_summary = (
            _cost_df.groupby("現場")
            .agg(実働時間合計=("実働時間", "sum"), 人件費合計=("人件費", "sum"))
            .reset_index().sort_values("現場"))
        _total_cost = int(_budget_summary["人件費合計"].sum())

        _bcol1, _bcol2 = st.columns([2, 1])
        _budget_cap = _bcol1.number_input(
            "今期の予算上限（円・任意）", min_value=0, value=0, step=10000,
            key="budget_cap")
        _bcol2.metric("確定シフト人件費 合計", f"¥{_total_cost:,}")
        if _budget_cap > 0:
            _over = _total_cost - _budget_cap
            if _over > 0:
                st.error(f"⚠️ 予算を ¥{_over:,} 超過しています。")
            else:
                st.success(f"✅ 予算内です（残り ¥{-_over:,}）。")

        st.dataframe(_budget_summary, hide_index=True, width="stretch")

        _n_no_wage = int((_cost_df["適用時給"] == 0).sum())
        if _n_no_wage:
            st.warning(
                f"⚠️ 時給が未設定のまま計算されているシフトが {_n_no_wage} 件"
                "あります（人件費が0円のまま含まれています）。下の"
                "「現場マスタ」または「スタッフ優遇時給」で時給を"
                "設定してください。")

    with st.expander("💰 スタッフ優遇時給の管理"):
        st.caption(
            "経験者など、現場の基本時給より高い時給を個別に設定したい"
            "スタッフだけをここに登録します。登録が無い人は、自動的に"
            "現場の基本時給（現場マスタで設定）が使われます。")
        if staff_wages_df.empty:
            st.info("まだ優遇時給の登録はありません。")
        else:
            st.dataframe(staff_wages_df, hide_index=True, width="stretch")
            for i, row in staff_wages_df.reset_index(drop=True).iterrows():
                if st.button(f"🗑️ {row['氏名']}の登録を削除", key=f"wage_delete_{i}"):
                    if delete_staff_wage(row["氏名"]):
                        st.success(f"{row['氏名']}の優遇時給を削除しました。")
                        st.rerun()

        wc1, wc2, wc3 = st.columns([2, 1, 2])
        _wage_name = wc1.text_input("氏名", key="new_wage_name")
        _wage_amount = wc2.number_input("時給（円）", min_value=0, step=50, key="new_wage_amount")
        _wage_note = wc3.text_input("備考（任意）", key="new_wage_note")
        if st.button("➕ この内容で優遇時給を登録する", key="add_wage_btn"):
            if save_staff_wage(_wage_name, _wage_amount, _wage_note):
                st.success(f"{_wage_name}さんの優遇時給（¥{_wage_amount}）を登録しました。")
                st.rerun()

st.markdown("---")
st.header("📊 現場×日付の盤面")

_board_days = st.slider("表示する日数", 7, 30, 14, key="board_days")
labels, counts, pendings = build_board(wishes_df, confirmed_df, sites_df, days_ahead=_board_days)

if sites_df.empty:
    st.info("現場マスタに現場が登録されていません。")
else:
    if SHOW.get("board_detail", True):
        st.markdown("###### 📈 日別の予約総数（確定＋希望の合計）")
        st.caption("全現場を合計した、その日1日ぶんの人数です。多い日ほど、パンクしやすい日と言えます。")
        _daily_totals = (counts + pendings).sum(axis=0)
        _daily_summary = pd.DataFrame({
            "日付": _daily_totals.index,
            "確定": counts.sum(axis=0).values,
            "希望": pendings.sum(axis=0).values,
            "合計": _daily_totals.values,
        })
        st.dataframe(_daily_summary, hide_index=True, width="stretch")

        with st.expander("📋 現場ごとの内訳（全現場×全日付）", expanded=False):
            st.caption("今日から指定した日数ぶんの、現場ごとの確定・希望状況を一覧できます。")
            st.markdown(render_board_html(labels, counts, pendings), unsafe_allow_html=True)

    if SHOW.get("day_hourly_board", True):
        st.markdown("---")
        st.markdown("#### 📆 1日を選んで、全現場×47時間帯で見る")
        st.caption(
            "予約が増えてくると、上の「現場×日付」の盤面（1日1マスの合計）だけでは"
            "時間帯までは分からず把握しづらくなります。こちらは日付を1つ選び、"
            "その日の全現場を47時間帯（0〜47時、日またぎ対応）まで細かく"
            "並べた盤面です。動きのある現場だけを自動的に表示します。")
        _day_board_date = st.selectbox(
            "日付を選択", list(labels.columns) if len(labels.columns) else [],
            key="day_board_date")
        if _day_board_date:
            _dh_labels, _dh_counts, _dh_pendings, _dh_active_sites = build_day_hourly_board(
                wishes_df, confirmed_df, sites_df, _day_board_date)
            if not _dh_active_sites:
                st.info("この日は、まだどの現場にも確定・希望の動きがありません。")
            else:
                _active_hours = [
                    h for h in range(48)
                    if _dh_counts[h].sum() > 0 or _dh_pendings[h].sum() > 0]
                _lo = max(0, min(_active_hours) - 2)
                _hi = min(47, max(_active_hours) + 2)
                _hour_range = list(range(_lo, _hi + 1))

                def _hour_col_label(h):
                    return f"{h % 24}時" + ("+1" if h >= 24 else "")

                st.markdown(
                    render_board_html(
                        _dh_labels.loc[_dh_active_sites, _hour_range],
                        _dh_counts.loc[_dh_active_sites, _hour_range],
                        _dh_pendings.loc[_dh_active_sites, _hour_range],
                        row_header="現場＼時間帯", col_label_fn=_hour_col_label),
                    unsafe_allow_html=True)

    reference_df = load_reference()

    if SHOW.get("dice_matrix", True):
        st.markdown("#### 🎲 現場×日付の実際のダイス表（1人1行）")
        st.caption(
            "縦にスタッフ名、横に47時間帯（0〜47時、日またぎ対応）を並べた"
            "本物のダイス表です。「お手本」は、下の「実績データの取り込み」で"
            "作成した、この現場のいつもの人数パターンです。希望の行を見て、"
            "その場でボタンを押せば確定できます。")
        hc1, hc2 = st.columns(2)
        _drill_site = hc1.selectbox(
            "現場を選択", list(labels.index) if len(labels.index) else [], key="drill_site")
        _drill_date = hc2.selectbox(
            "日付を選択", list(labels.columns) if len(labels.columns) else [], key="drill_date")

        if _drill_site and _drill_date:
            _ref_heads = reference_df_to_array(reference_df, _drill_site)
            _dice_rows = build_staff_dice_rows(
                confirmed_df, wishes_df, _drill_site, _drill_date)
            st.markdown(
                render_dice_matrix_html(_ref_heads, _dice_rows),
                unsafe_allow_html=True)

            with st.expander("🪑 座席番号でのマッチング（試作・1現場ぶん）", expanded=True):
                st.caption(
                    "お手本ダイスの人数から、1時間ごとに独立した座席番号を作り、"
                    "確定シフト・未処理の希望をその座席に当てはめた試作版です。"
                    "座席が足りない場合は「希望(超過)」として別枠に表示します。"
                    "座席1つ1つには `日付_時間帯_座席番号_現場名` というIDが"
                    "内部的に付いています。")
                _seat_counts, _occupancy = build_seat_grid(
                    _ref_heads, confirmed_df, wishes_df, _drill_site, _drill_date)
                _active_hours = [h for h in range(48)
                                 if _seat_counts[h] > 0
                                 or any(hh == h for (hh, _) in _occupancy)]
                if _active_hours:
                    _lo = max(0, min(_active_hours) - 2)
                    _hi = min(47, max(_active_hours) + 2)
                    _seat_hours = list(range(_lo, _hi + 1))
                else:
                    _seat_hours = list(range(6, 24))
                st.markdown(
                    render_seat_grid_html(_seat_counts, _occupancy, _seat_hours),
                    unsafe_allow_html=True)

            with st.expander("🔴🔵 お手本との増減表", expanded=False):
                st.caption(
                    "お手本ダイスの人数と、実際に確定した人数を1時間ごとに"
                    "比べます。お手本より人が増えたマスは赤、減った"
                    "（欠員のままの）マスは青で表示します。")
                _c_heads_for_diff, _, _, _ = build_hour_breakdown(
                    confirmed_df, wishes_df, _drill_site, _drill_date)
                _diff_list = build_seat_diff(_ref_heads, _c_heads_for_diff)
                _diff_active_hours = [
                    h for h in range(48)
                    if _diff_list[h]["お手本"] > 0 or _diff_list[h]["確定"] > 0]
                if _diff_active_hours:
                    _dlo = max(0, min(_diff_active_hours) - 2)
                    _dhi = min(47, max(_diff_active_hours) + 2)
                    _diff_hours = list(range(_dlo, _dhi + 1))
                else:
                    _diff_hours = list(range(6, 24))
                st.markdown(
                    render_seat_diff_html(_diff_list, _diff_hours),
                    unsafe_allow_html=True)

            _wish_rows = [r for r in _dice_rows if r["種別"] == "希望"]
            if _wish_rows:
                st.markdown("###### この現場・日付の希望を確定する")

                def _wish_seat_status(wish_id):
                    """この希望が、座席の試作結果でどう扱われたか（希望／希望(超過)）を調べる。"""
                    for info in _occupancy.values():
                        if info.get("wish_id") == wish_id:
                            return info.get("状態", "希望")
                    return "希望"

                for r in _wish_rows:
                    _status = _wish_seat_status(r["wish_id"])
                    _is_over = "超過" in _status
                    wcol1, wcol2 = st.columns([4, 1])
                    if _is_over:
                        wcol1.write(
                            f"⚠️ **{r['氏名']}**　{_drill_date} {r['開始']}〜{r['終了']}")
                        wcol1.caption(
                            "お手本の座席数を超えている希望です。増員として確定するか、"
                            "見送るか判断してください。")
                    else:
                        wcol1.write(
                            f"**{r['氏名']}**　{_drill_date} {r['開始']}〜{r['終了']}")
                    _btn_label = "⚠️ 超過のまま確定する" if _is_over else "✅ 確定する"
                    if wcol2.button(_btn_label, key=f"dice_confirm_{r['wish_id']}"):
                        append_confirmed({
                            "shift_id": r["wish_id"],
                            "氏名": r["氏名"],
                            "line_user_id": wishes_df.loc[
                                wishes_df["wish_id"] == r["wish_id"], "line_user_id"
                            ].iloc[0] if (wishes_df["wish_id"] == r["wish_id"]).any() else "",
                            "現場": _drill_site,
                            "日付": _drill_date,
                            "開始": r["開始"],
                            "終了": r["終了"],
                            "マッチング方法": "増員（お手本超過）" if _is_over else "自動",
                            "確定日時": "",
                        })
                        update_wish_status(r["wish_id"], "マッチ済", _drill_site)
                        _line_uid = wishes_df.loc[
                            wishes_df["wish_id"] == r["wish_id"], "line_user_id"
                        ].iloc[0] if (wishes_df["wish_id"] == r["wish_id"]).any() else ""
                        _ok, _msg = send_confirmation(
                            _line_uid,
                            f"シフトが確定しました。\n{_drill_date} {r['開始']}〜{r['終了']}\n"
                            f"現場：{_drill_site}")
                        if _is_over:
                            st.warning(
                                f"⚠️ {r['氏名']}さんを、お手本の座席数を超えた"
                                f"「増員」として確定しました。{_msg}")
                        else:
                            st.success(f"{r['氏名']}さんを確定しました。{_msg}")
                        st.rerun()

    if SHOW.get("reference_import", True):
        with st.expander("📥 実績データの取り込み（お手本ダイスの作成）"):
            st.caption(
                "前の特許用アプリの「🎲 時間帯別ダイス」内にある"
                "「📤 お手本ダイス用データの書き出し」で作ったCSV（縦長形式）を"
                "そのまま使うこともできますし、JOBLOOK2・タイミー・"
                "スキマクエスト・マイチームなど、各アプリの勤怠CSVを"
                "直接アップロードすることもできます。列名は自動で推測し、"
                "違っていればプルダウンで選び直せます。")
            actual_file = st.file_uploader(
                "実績CSV（お手本ダイス用の4列形式、または各アプリの生データ）",
                type=["csv"], key="actual_upload")
            if actual_file:
                try:
                    actual_raw = pd.read_csv(actual_file, encoding="utf-8-sig", dtype=str)
                except UnicodeDecodeError:
                    actual_file.seek(0)
                    actual_raw = pd.read_csv(actual_file, encoding="cp932", dtype=str)
                actual_cols = list(actual_raw.columns)

                required_cols = {"現場名", "日付", "時間帯", "頭数"}
                _hour_col_pattern = re.compile(r"^\d{1,2}時$")
                _hour_cols = [c for c in actual_cols if _hour_col_pattern.match(c)]

                if required_cols.issubset(set(actual_cols)):
                    # ケース1：前の特許用アプリの書き出し機能と同じ、縦長の4列形式
                    st.caption(
                        f"{len(actual_raw)} 行／"
                        f"{actual_raw['現場名'].nunique()} 現場ぶんのデータを読み込みました"
                        "（縦長形式として認識しました）。")
                    st.dataframe(actual_raw.head(10), hide_index=True, width="stretch")
                    render_alias_helper(
                        actual_raw["現場名"], sites_df, aliases_df, "long")

                    _detected_period = detect_period_label(actual_raw["日付"])
                    _period_input = st.text_input(
                        "この実績の対象期間（例：2026-02）", value=_detected_period,
                        key="period_input_long",
                        help="自動でデータから読み取った期間です。違っていれば書き換えて"
                             "ください。同じ現場でも対象期間が違えば、両方のお手本が"
                             "残るので、あとで年ごとに見比べられます。")

                    if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_btn"):
                        now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                        _normalized = actual_raw.copy()
                        _normalized["現場名"] = _normalized["現場名"].apply(
                            lambda n: normalize_site_name(n, aliases_df))
                        target_sites = sorted(_normalized["現場名"].dropna().unique())
                        patterns = {}
                        for s in target_sites:
                            pattern = build_reference_pattern_from_hourly(_normalized, s)
                            if pattern:
                                patterns[s] = pattern
                        done = save_reference_bulk(patterns, now_s, period=_period_input.strip())
                        st.success(
                            f"✅ {done} 現場分のお手本ダイス"
                            f"（対象期間：{_period_input.strip() or '未設定'}）を作成・更新しました。")
                        st.rerun()

                elif {"現場名", "日付"}.issubset(set(actual_cols)) and _hour_cols:
                    # ケース2：前の特許用アプリの「マトリクス形式」（現場名・日付・
                    # 9時・10時…と横に時間帯が並ぶ形）。縦長形式に変換してから
                    # 中身は縦長形式とまったく同じように扱う。
                    _melted = actual_raw.melt(
                        id_vars=["現場名", "日付"], value_vars=_hour_cols,
                        var_name="時間帯", value_name="頭数")
                    _melted["時間帯"] = _melted["時間帯"].str.replace("時", "", regex=False)
                    _melted["頭数"] = pd.to_numeric(_melted["頭数"], errors="coerce").fillna(0)
                    _melted = _melted[_melted["頭数"] > 0]

                    st.caption(
                        f"{len(actual_raw)} 行／"
                        f"{actual_raw['現場名'].nunique()} 現場ぶんのデータを読み込みました"
                        "（マトリクス形式として認識し、自動的に変換しました）。")
                    st.dataframe(actual_raw.head(10), hide_index=True, width="stretch")
                    render_alias_helper(
                        actual_raw["現場名"], sites_df, aliases_df, "matrix")

                    _detected_period = detect_period_label(actual_raw["日付"])
                    _period_input = st.text_input(
                        "この実績の対象期間（例：2026-02）", value=_detected_period,
                        key="period_input_matrix",
                        help="自動でデータから読み取った期間です。違っていれば書き換えて"
                             "ください。同じ現場でも対象期間が違えば、両方のお手本が"
                             "残るので、あとで年ごとに見比べられます。")

                    if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_matrix_btn"):
                        now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                        _normalized = _melted.copy()
                        _normalized["現場名"] = _normalized["現場名"].apply(
                            lambda n: normalize_site_name(n, aliases_df))
                        target_sites = sorted(_normalized["現場名"].dropna().unique())
                        patterns = {}
                        for s in target_sites:
                            pattern = build_reference_pattern_from_hourly(_normalized, s)
                            if pattern:
                                patterns[s] = pattern
                        done = save_reference_bulk(patterns, now_s, period=_period_input.strip())
                        st.success(
                            f"✅ {done} 現場分のお手本ダイス"
                            f"（対象期間：{_period_input.strip() or '未設定'}）を作成・更新しました。")
                        st.rerun()
                else:
                    # ケース3：各アプリ（JOBLOOK2・タイミー・スキマクエスト等）の生データ。
                    # 列名は自動推測しつつ、プルダウンで選び直せるようにする。
                    st.caption(
                        f"{len(actual_raw)} 行 ／ 認識した列：{', '.join(actual_cols[:12])}"
                        + ("…" if len(actual_cols) > 12 else "")
                        + "（各アプリの生データとして認識しました。列を確認・"
                          "選び直してください）")

                    ac1, ac2, ac3, ac4 = st.columns(4)
                    c_site = ac1.selectbox(
                        "現場の列", actual_cols,
                        index=actual_cols.index(guess_col(
                            actual_cols, ["現場", "事業所", "拠点", "管理用ラベル"]))
                        if guess_col(actual_cols, ["現場", "事業所", "拠点", "管理用ラベル"])
                        in actual_cols else 0,
                        key="ac_site")
                    c_date = ac2.selectbox(
                        "日付の列", actual_cols,
                        index=actual_cols.index(guess_col(
                            actual_cols, ["日付", "勤務日", "求人日"]))
                        if guess_col(actual_cols, ["日付", "勤務日", "求人日"]) in actual_cols
                        else 0,
                        key="ac_date")
                    c_start = ac3.selectbox(
                        "出勤時刻の列", actual_cols,
                        index=actual_cols.index(guess_col(
                            actual_cols, ["出勤", "開始", "チェックイン"]))
                        if guess_col(actual_cols, ["出勤", "開始", "チェックイン"]) in actual_cols
                        else 0,
                        key="ac_start")
                    c_end = ac4.selectbox(
                        "退勤時刻の列", actual_cols,
                        index=actual_cols.index(guess_col(
                            actual_cols, ["退勤", "終了", "チェックアウト"]))
                        if guess_col(actual_cols, ["退勤", "終了", "チェックアウト"]) in actual_cols
                        else 0,
                        key="ac_end")

                    _preview = actual_raw[[c_site, c_date, c_start, c_end]].head(5).rename(
                        columns={c_site: "現場", c_date: "日付", c_start: "開始", c_end: "終了"})
                    st.caption("取り込み前のプレビュー（先頭5件）：氏名・現場名等が正しく"
                               "入っているか確認してください。")
                    st.dataframe(_preview, hide_index=True, width="stretch")
                    render_alias_helper(
                        actual_raw[c_site], sites_df, aliases_df, "raw")

                    _detected_period = detect_period_label(actual_raw[c_date])
                    _period_input = st.text_input(
                        "この実績の対象期間（例：2026-02）", value=_detected_period,
                        key="period_input_raw",
                        help="自動でデータから読み取った期間です。違っていれば書き換えて"
                             "ください。同じ現場でも対象期間が違えば、両方のお手本が"
                             "残るので、あとで年ごとに見比べられます。")

                    if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_raw_btn"):
                        actual_df = actual_raw.rename(columns={
                            c_site: "現場", c_date: "日付", c_start: "開始", c_end: "終了",
                        })[["現場", "日付", "開始", "終了"]].dropna()
                        actual_df["現場"] = actual_df["現場"].apply(
                            lambda n: normalize_site_name(n, aliases_df))
                        now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                        target_sites = sorted(actual_df["現場"].dropna().unique())
                        patterns = {}
                        for s in target_sites:
                            pattern = build_reference_pattern(actual_df, s)
                            if pattern:
                                patterns[s] = pattern
                        done = save_reference_bulk(patterns, now_s, period=_period_input.strip())
                        st.success(
                            f"✅ {done} 現場分のお手本ダイス"
                            f"（対象期間：{_period_input.strip() or '未設定'}）を作成・更新しました。")
                        st.rerun()

    if SHOW.get("reference_compare", True):
        with st.expander("📈 期間ごとのお手本を見比べる"):
            st.caption(
                "同じ現場について、複数の対象期間（例：2026年2月・2027年2月）の"
                "お手本を取り込んでいれば、ここで並べて見比べられます。")
            _compare_reference_df = load_reference()
            _compare_sites = sorted(_compare_reference_df["現場"].dropna().unique()) \
                if not _compare_reference_df.empty else []
            _compare_site = st.selectbox(
                "現場を選択", _compare_sites, key="compare_ref_site")
            if _compare_site:
                _periods = list_reference_periods(_compare_reference_df, _compare_site)
                if len(_periods) < 2:
                    st.info(
                        f"この現場には、まだ比較できるほど対象期間の異なる"
                        f"お手本がありません（現在：{len(_periods)} 件）。"
                        "別の期間のデータも取り込むと、ここで見比べられます。")
                else:
                    _selected_periods = st.multiselect(
                        "見比べる対象期間を選ぶ（2つ以上）", _periods,
                        default=_periods[:2], key="compare_ref_periods")
                    if len(_selected_periods) >= 2:
                        _compare_table = pd.DataFrame(
                            {p: reference_df_to_array(_compare_reference_df, _compare_site, period=p)
                             for p in _selected_periods},
                            index=[f"{h}時" for h in range(48)]).T
                        _active_cols = [c for c in _compare_table.columns
                                        if _compare_table[c].sum() > 0]
                        if _active_cols:
                            st.dataframe(
                                _compare_table[_active_cols].round(2),
                                width="stretch")
                        else:
                            st.info("選んだ期間には、まだ数値が入っていません。")

    if SHOW.get("daily_compare", True):
        with st.expander("📅 日付×時間帯で実績を比較する（日別の細かい表）"):
            st.caption(
                "上の「お手本を見比べる」は月平均に均した数字ですが、こちらは"
                "均さず、日付ごとの実際の人数をそのまま比較できます。前の"
                "特許用アプリの「📤 お手本ダイス用データの書き出し」で作った"
                "CSV（縦長形式・マトリクス形式どちらでも可）を、比較したい"
                "期間ぶん2つアップロードしてください。")

            _cmp_c1, _cmp_c2 = st.columns(2)
            _cmp_file_a = _cmp_c1.file_uploader(
                "期間A用CSV（例：2025年2月）", type=["csv"], key="daily_cmp_file_a")
            _cmp_file_b = _cmp_c2.file_uploader(
                "期間B用CSV（例：2026年2月）", type=["csv"], key="daily_cmp_file_b")

            if _cmp_file_a and _cmp_file_b:
                _long_a = parse_actual_csv_flexible(_cmp_file_a)
                _long_b = parse_actual_csv_flexible(_cmp_file_b)
                if _long_a is None or _long_b is None:
                    st.error(
                        "CSVの形式を認識できませんでした。前の特許用アプリの"
                        "書き出し機能で作ったCSVをそのまま使ってください。")
                else:
                    _cmp_sites = sorted(
                        set(_long_a["現場名"].dropna().unique())
                        | set(_long_b["現場名"].dropna().unique()))
                    _cmp_site = st.selectbox(
                        "比較する現場を選択", _cmp_sites, key="daily_cmp_site")
                    _cmp_mode = st.radio(
                        "比較のそろえ方",
                        ["曜日でそろえる（同じ曜日どうしの平均を比較・推奨）",
                         "日付そのままで比較する"],
                        key="daily_cmp_mode",
                        help="例えば「2025-02-01」と「2026-02-01」は曜日が"
                             "違うことが多く、単純に並べると人数の増減が"
                             "曜日の違いによるものなのか、本当の需要の変化"
                             "なのか区別しにくくなります。「曜日でそろえる」"
                             "を選ぶと、期間内の同じ曜日どうしを平均して"
                             "比較するので、この影響を受けにくくなります。")

                    if _cmp_site:
                        if _cmp_mode.startswith("曜日"):
                            _table_a = build_weekday_hourly_matrix(_long_a, _cmp_site)
                            _table_b = build_weekday_hourly_matrix(_long_b, _cmp_site)
                            _row_label = "曜日"
                        else:
                            _table_a = extract_daily_hourly_matrix(_long_a, _cmp_site)
                            _table_b = extract_daily_hourly_matrix(_long_b, _cmp_site)
                            _row_label = "日付"

                        if _table_a.empty and _table_b.empty:
                            st.info("この現場のデータが、どちらのCSVにもありません。")
                        else:
                            st.markdown("###### 📘 期間A")
                            if _table_a.empty:
                                st.caption("この現場のデータがありません。")
                            else:
                                _cols_a = [c for c in _table_a.columns if _table_a[c].sum() > 0]
                                st.dataframe(
                                    _table_a[_cols_a].rename(
                                        columns={c: f"{c}時" for c in _cols_a}),
                                    width="stretch")
                            st.markdown("###### 📗 期間B")
                            if _table_b.empty:
                                st.caption("この現場のデータがありません。")
                            else:
                                _cols_b = [c for c in _table_b.columns if _table_b[c].sum() > 0]
                                st.dataframe(
                                    _table_b[_cols_b].rename(
                                        columns={c: f"{c}時" for c in _cols_b}),
                                    width="stretch")

                            if _cmp_mode.startswith("曜日") and (not _table_a.empty or not _table_b.empty):
                                st.markdown("###### 🔺 曜日ごとの形（山型・ロート型・二峰型・変則型）")
                                st.caption(
                                    "前の特許用アプリで人手で分類していた4タイプを、"
                                    "山（極大点）の数と幅から自動判定したものです。"
                                    "目安としてお使いください。")
                                _shape_rows = []
                                for _wd in _WEEKDAY_ORDER_APP:
                                    _row_data = {"曜日": _wd}
                                    if not _table_a.empty and _wd in _table_a.index:
                                        _shape_a, _ = classify_dice_shape(
                                            list(_table_a.loc[_wd].values))
                                        _row_data["期間Aの形"] = _shape_a
                                    else:
                                        _row_data["期間Aの形"] = "－"
                                    if not _table_b.empty and _wd in _table_b.index:
                                        _shape_b, _ = classify_dice_shape(
                                            list(_table_b.loc[_wd].values))
                                        _row_data["期間Bの形"] = _shape_b
                                    else:
                                        _row_data["期間Bの形"] = "－"
                                    if _row_data["期間Aの形"] != "－" or _row_data["期間Bの形"] != "－":
                                        _shape_rows.append(_row_data)
                                if _shape_rows:
                                    st.dataframe(
                                        pd.DataFrame(_shape_rows), hide_index=True, width="stretch")

                            if _cmp_mode.startswith("曜日") and not _table_a.empty and not _table_b.empty:
                                st.markdown("###### 🔴🔵 椅子（人数）の増減：期間B − 期間A")
                                st.caption(
                                    "同じ曜日・同じ時間帯どうしで、期間Bが期間Aより"
                                    "何人分増減しているかを示します。"
                                    "赤＝増加、青＝減少、色が濃いほど差が大きいことを示します。")
                                _all_weekdays = [w for w in _WEEKDAY_ORDER_APP
                                                 if w in _table_a.index or w in _table_b.index]
                                _all_hours_diff = sorted(
                                    set(_table_a.columns) | set(_table_b.columns))
                                _diff_rows_html = []
                                for _wd in _all_weekdays:
                                    _va = _table_a.loc[_wd] if _wd in _table_a.index else None
                                    _vb = _table_b.loc[_wd] if _wd in _table_b.index else None
                                    _row = {}
                                    for _h in _all_hours_diff:
                                        _av = float(_va[_h]) if _va is not None and _h in _va.index else 0.0
                                        _bv = float(_vb[_h]) if _vb is not None and _h in _vb.index else 0.0
                                        _row[_h] = round(_bv - _av, 2)
                                    _diff_rows_html.append((_wd, _row))

                                _diff_active_hours = [
                                    h for h in _all_hours_diff
                                    if any(abs(row[h]) > 0 for _, row in _diff_rows_html)]
                                if not _diff_active_hours:
                                    st.info("差分がある時間帯がありませんでした。")
                                else:
                                    _diff_html_parts = [
                                        '<div style="overflow-x:auto;">'
                                        '<table style="border-collapse:collapse;font-size:12px;">'
                                        '<tr><th style="border:1px solid #ddd;padding:4px 6px;'
                                        'background:#f5f5f5;text-align:left;">曜日</th>']
                                    for _h in _diff_active_hours:
                                        _diff_html_parts.append(
                                            f'<th style="border:1px solid #ddd;padding:4px 6px;'
                                            f'background:#f5f5f5;white-space:nowrap;">{_h}時</th>')
                                    _diff_html_parts.append('</tr>')

                                    def _diff_bg_app(v):
                                        if v > 0:
                                            shade = min(1.0, v / 3.0)
                                            return f"rgba(220,60,60,{0.15 + shade * 0.55:.2f})"
                                        elif v < 0:
                                            shade = min(1.0, abs(v) / 3.0)
                                            return f"rgba(60,100,220,{0.15 + shade * 0.55:.2f})"
                                        return "#ffffff"

                                    for _wd, _row in _diff_rows_html:
                                        _diff_html_parts.append(
                                            f'<tr><td style="border:1px solid #ddd;padding:4px 6px;'
                                            f'background:#fafafa;font-weight:bold;">{_wd}</td>')
                                        for _h in _diff_active_hours:
                                            _v = _row[_h]
                                            _bg = _diff_bg_app(_v)
                                            _text = "±0" if _v == 0 else f"{'+' if _v > 0 else ''}{_v:g}"
                                            _diff_html_parts.append(
                                                f'<td style="border:1px solid #ddd;padding:4px 6px;'
                                                f'text-align:center;background:{_bg};">{_text}</td>')
                                        _diff_html_parts.append('</tr>')
                                    _diff_html_parts.append('</table></div>')
                                    st.markdown("".join(_diff_html_parts), unsafe_allow_html=True)

    if SHOW.get("muscle_day", True):
        with st.expander("🏋️ 一番「筋肉質」な日を選んで、お手本にする"):
            st.caption(
                "平均で均すと、無駄の多い日と少ない日が混ざって薄まって"
                "しまいます。ここでは、実際にあった日々のダイスをそのまま"
                "一覧にし、合計人数が少ない順（＝スタッフの力量で効率よく"
                "回せていた可能性がある順）に並べます。「この日が理想的な"
                "配置だった」という日を選べば、その日の時間帯別の人数を"
                "そのままお手本として保存できます（平均をとりません）。")

            _muscle_file = st.file_uploader(
                "実績CSV（縦長形式・マトリクス形式どちらでも可）",
                type=["csv"], key="muscle_upload")
            if _muscle_file:
                _muscle_long = parse_actual_csv_flexible(_muscle_file)
                if _muscle_long is None:
                    st.error(
                        "CSVの形式を認識できませんでした。前の特許用アプリの"
                        "書き出し機能で作ったCSVをそのまま使ってください。")
                else:
                    _muscle_sites = sorted(_muscle_long["現場名"].dropna().unique())
                    _muscle_site = st.selectbox(
                        "現場を選択", _muscle_sites, key="muscle_site")
                    if _muscle_site:
                        _muscle_table = extract_daily_hourly_matrix(_muscle_long, _muscle_site)
                        if _muscle_table.empty:
                            st.info("この現場のデータがありません。")
                        else:
                            _daily_ranking = _muscle_table.sum(axis=1).sort_values()
                            _ranking_df = pd.DataFrame({
                                "日付": _daily_ranking.index,
                                "その日の延べ人数": _daily_ranking.values,
                            })
                            st.caption("延べ人数が少ない日ほど上に来ます（筋肉質な日の候補）。")
                            st.dataframe(_ranking_df, hide_index=True, width="stretch")

                            _picked_date = st.selectbox(
                                "お手本にしたい日を選ぶ", list(_daily_ranking.index),
                                key="muscle_picked_date")
                            if _picked_date:
                                _picked_row = _muscle_table.loc[_picked_date]
                                _picked_cols = [c for c in _picked_row.index if _picked_row[c] > 0]
                                st.markdown(f"###### {_picked_date} の時間帯別の人数")
                                st.dataframe(
                                    pd.DataFrame(
                                        [_picked_row[_picked_cols].values],
                                        columns=[f"{c}時" for c in _picked_cols],
                                        index=[_muscle_site]),
                                    width="stretch")

                                _muscle_period_label = st.text_input(
                                    "この内容を保存するときの対象期間ラベル",
                                    value=str(_picked_date), key="muscle_period_label",
                                    help="例えばそのまま日付にしておけば「この日を選んだ"
                                         "お手本」として区別できます。他の期間ラベルと"
                                         "同じ名前にすると、上書きされます。")
                                if st.button(
                                        f"🏋️ {_picked_date} のパターンをお手本として保存する",
                                        key="muscle_save_btn"):
                                    _picked_pattern = {
                                        int(c): float(_picked_row[c]) for c in _picked_cols}
                                    _now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                                    save_reference_bulk(
                                        {_muscle_site: _picked_pattern}, _now_s,
                                        period=_muscle_period_label.strip())
                                    st.success(
                                        f"✅ {_picked_date} のパターンを、対象期間"
                                        f"「{_muscle_period_label.strip()}」として保存しました。")
                                    st.rerun()

                            st.markdown("---")
                            st.caption(
                                "経験のあるユニット長でないと、変則的な需要に合わせた"
                                "ダイスは組めません。今、人手不足で「穴埋めだけ」に"
                                "なってしまっている現場も多いと思います。1日だけ"
                                "選んで保存するのではなく、**この現場のすべての日**を、"
                                "日付ごとに個別のお手本として一括保存しておけば、"
                                "経験の浅い人でも「過去の実際の配置例」を後から"
                                "参照できるようになります（平均はとりません。"
                                "日ごとの実際の値がそのまま残ります）。")
                            if st.button(
                                    f"💾 {_muscle_site} の全日程（{len(_muscle_table)}日ぶん）を、"
                                    "日付ごとに個別保存する",
                                    key="muscle_save_all_btn"):
                                _entries = []
                                for _d in _muscle_table.index:
                                    _row = _muscle_table.loc[_d]
                                    _cols_d = [c for c in _row.index if _row[c] > 0]
                                    if not _cols_d:
                                        continue
                                    _pattern_d = {int(c): float(_row[c]) for c in _cols_d}
                                    _entries.append((_muscle_site, str(_d), _pattern_d))
                                _now_s2 = datetime.now().strftime("%Y-%m-%d %H:%M")
                                _n_saved = save_reference_multi_period(_entries, _now_s2)
                                st.success(
                                    f"✅ {_muscle_site} の {_n_saved} 日ぶんを、"
                                    "それぞれ個別の対象期間として保存しました。"
                                    "「📈 期間ごとのお手本を見比べる」で、実際の"
                                    "日付を選んで参照できます。")
                                st.rerun()

    if SHOW.get("reference_export", True):
        with st.expander("📤 保存済みのお手本ダイス（全期間・全現場）を書き出す"):
            st.caption(
                "毎月お手本を取り込んでいけば、通年12ヶ月ぶん・全現場ぶんの"
                "お手本がここに蓄積されていきます。ここでは、今保存されている"
                "すべての現場・すべての対象期間（例：2025-01〜2025-12）を"
                "まとめて1つのファイルとして書き出せます。記録の保存や、"
                "他のシステムでの活用にお使いください。")
            _export_reference_df = load_reference()
            if _export_reference_df.empty:
                st.info("まだお手本が1件も保存されていません。")
            else:
                _n_sites = _export_reference_df["現場"].nunique()
                _n_periods = _export_reference_df["対象期間"].nunique()
                _periods_list = sorted(_export_reference_df["対象期間"].dropna().unique())
                st.write(
                    f"現在の保存状況：**{_n_sites} 現場 × {_n_periods} 期間** "
                    f"（{', '.join(_periods_list) if _periods_list else '期間未設定のものを含む'}）")

                st.markdown("###### 📊 現場×期間ごとの合計人数（総計）")
                st.caption(
                    "1日ぶんの延べ人数（全時間帯の合計）です。まずはここで"
                    "期間ごとの増減をざっと見て、気になる現場・期間があれば"
                    "下の詳細表やマトリクスで細かく確認するのが早いです。")
                _totals_src = _export_reference_df.copy()
                _totals_src["基準人数"] = pd.to_numeric(
                    _totals_src["基準人数"], errors="coerce").fillna(0.0)
                _totals_df = (
                    _totals_src.groupby(["現場", "対象期間"])["基準人数"]
                    .sum().round(2).reset_index()
                    .rename(columns={"基準人数": "合計人数"})
                    .sort_values(["現場", "対象期間"]))
                st.dataframe(_totals_df, hide_index=True, width="stretch")

                _export_tab1, _export_tab2 = st.tabs(
                    ["📋 縦長形式（全件）", "🎲 マトリクス形式（現場×期間を1行・47時間帯）"])

                with _export_tab1:
                    st.dataframe(_export_reference_df, hide_index=True, width="stretch")
                    _export_csv_long = _export_reference_df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 縦長形式でダウンロード（全期間・全現場）", _export_csv_long,
                        file_name=f"お手本ダイス_通年_縦長_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="export_all_ref_long")

                with _export_tab2:
                    _export_src = _export_reference_df.copy()
                    _export_src["時間帯"] = _export_src["時間帯"].astype(int)
                    _export_src["基準人数"] = pd.to_numeric(
                        _export_src["基準人数"], errors="coerce").fillna(0.0)
                    _export_matrix = _export_src.pivot_table(
                        index=["現場", "対象期間"], columns="時間帯", values="基準人数",
                        fill_value=0.0, aggfunc="first").reset_index()
                    _export_matrix.columns = [
                        str(c) if isinstance(c, str) else f"{c}時"
                        for c in _export_matrix.columns]
                    st.dataframe(_export_matrix, hide_index=True, width="stretch")
                    _export_csv_matrix = _export_matrix.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 マトリクス形式でダウンロード（現場×期間を1行）", _export_csv_matrix,
                        file_name=f"お手本ダイス_通年_マトリクス_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="export_all_ref_matrix")

    if SHOW.get("seat_generation", True):
        with st.expander("🪑 座席番号の一括生成"):
            st.caption(
                "登録済みの「お手本ダイス」から、指定した期間ぶんの座席番号"
                "（現場・日付・時間帯・座席番号・座席IDの一覧）を、"
                "登録されている全現場について一気に作ります。座席は"
                "1時間ごとに独立していて、その時間帯のお手本人数（四捨五入）ぶん"
                "だけ作られます。まだ誰も割り当てられていない「空席リスト」です。")
            _seat_gen_mode = st.radio(
                "対象日付の指定方法", ["期間で指定（例：3月分をまとめて）", "個別に指定"],
                key="seat_gen_mode", horizontal=True)
            if _seat_gen_mode == "期間で指定（例：3月分をまとめて）":
                _range_c1, _range_c2 = st.columns(2)
                _range_start = _range_c1.date_input("開始日", key="seat_gen_start")
                _range_end = _range_c2.date_input("終了日", key="seat_gen_end")
            else:
                _seat_gen_dates = st.text_area(
                    "対象日付（1行に1つ、YYYY-MM-DD形式）",
                    value=datetime.now().strftime("%Y-%m-%d"), height=80,
                    help="複数の日付をまとめて生成したい場合は、改行で区切って"
                         "何行でも入力してください。")

            if st.button("🪑 この期間ぶんの座席を一括生成する", key="gen_seat_list_btn"):
                if _seat_gen_mode == "期間で指定（例：3月分をまとめて）":
                    if _range_start > _range_end:
                        st.error("開始日は終了日より前の日付にしてください。")
                        _target_dates = []
                    else:
                        _n_days = (_range_end - _range_start).days + 1
                        _target_dates = [
                            (_range_start + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
                            for i in range(_n_days)]
                else:
                    _target_dates = [d.strip() for d in _seat_gen_dates.splitlines() if d.strip()]

                if _target_dates:
                    _reference_df_all = load_reference()
                    _seat_list = generate_seat_list(_reference_df_all, _target_dates)
                    if _seat_list.empty:
                        st.session_state["generated_seat_list"] = None
                        st.warning(
                            "座席が1件も生成されませんでした。お手本ダイスが"
                            "登録されているか確認してください。")
                    else:
                        _seat_list = annotate_seat_list_with_occupancy(
                            _seat_list, _reference_df_all, confirmed_df, wishes_df)
                        # ボタンは押した瞬間の1回しか反応しないため、結果は
                        # session_stateに保存しておく。こうしないと、下の
                        # 「表示する現場」プルダウンを操作しただけで画面が
                        # 再読み込みされ、せっかく生成した結果が消えてしまう。
                        st.session_state["generated_seat_list"] = _seat_list
                        st.session_state["generated_seat_dates"] = _target_dates

            if st.session_state.get("generated_seat_list") is not None:
                _seat_list = st.session_state["generated_seat_list"]
                _target_dates = st.session_state.get("generated_seat_dates", [])
                _n_filled = int((_seat_list["状態"] != "空席").sum())
                st.success(
                    f"✅ {len(_target_dates)} 日ぶん・{_seat_list['現場名'].nunique()} "
                    f"現場ぶん、合計 {len(_seat_list)} 席を生成しました"
                    f"（うち {_n_filled} 席に申請者がいます）。")
                st.caption(
                    "「氏名」「状態」の列で、確定前（希望）でも確定後でも、"
                    "その座席の申請者が分かるようになっています。")

                _seat_tab1, _seat_tab2 = st.tabs(
                    ["📋 縦長形式", "🎲 マトリクス形式（1座席1行・47時間帯）"])

                with _seat_tab1:
                    st.markdown("###### 現場ごとの集計")
                    _seat_summary = (
                        _seat_list.assign(
                            is_空席=lambda d: d["状態"] == "空席",
                            is_希望=lambda d: d["状態"].isin(["希望", "希望(超過)"]),
                            is_確定=lambda d: d["状態"] == "確定")
                        .groupby("現場名")
                        .agg(総座席数=("座席ID", "count"),
                             空席数=("is_空席", "sum"),
                             希望数=("is_希望", "sum"),
                             確定数=("is_確定", "sum"))
                        .reset_index()
                        .sort_values("現場名"))
                    st.dataframe(_seat_summary, hide_index=True, width="stretch")

                    st.markdown("###### 座席1件ずつの詳細")
                    st.dataframe(_seat_list, hide_index=True, width="stretch")
                    _seat_list_csv = _seat_list.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 座席リストのCSVをダウンロード（縦長形式）", _seat_list_csv,
                        file_name=f"座席リスト_縦長_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="seat_list_csv")

                with _seat_tab2:
                    st.caption(
                        "現場・日付・座席番号を1行として、横に時間帯を並べた"
                        "マトリクスです。マスの中身は、その時間帯にその座席へ"
                        "入っている人の氏名です（空席は空欄）。複数の現場を"
                        "一度に表示すると、営業時間帯が違う現場同士で"
                        "無関係な列が空欄だらけになって見づらいため、"
                        "現場を選んで表示します。")
                    _matrix_sites = sorted(_seat_list["現場名"].unique())
                    _matrix_site_filter = st.multiselect(
                        "表示する現場（未選択なら全現場）", _matrix_sites,
                        key="seat_matrix_site_filter")
                    _matrix_source = _seat_list[
                        _seat_list["現場名"].isin(_matrix_site_filter)] \
                        if _matrix_site_filter else _seat_list

                    # 「氏名」だけをピボットすると、実際にはその現場・座席に
                    # 存在しない時間帯（他の現場の時間帯が列に混ざって
                    # くることで生まれる列）まで、あとで「空席」と表示して
                    # しまう。そこで、「そのマスに座席が本当に存在するか」を
                    # 別途ピボットして、行ごとに正確に判定する。
                    _exists_matrix = _matrix_source.assign(_exists=1).pivot_table(
                        index=["現場名", "日付", "座席番号"], columns="時間帯",
                        values="_exists", aggfunc="max", fill_value=0)
                    _seat_matrix = _matrix_source.pivot_table(
                        index=["現場名", "日付", "座席番号"], columns="時間帯",
                        values="氏名", aggfunc="first", fill_value="")
                    # 両方とも同じ index（現場名・日付・座席番号）を持つので、
                    # 位置ではなくindexで正しく突き合わせる。
                    _exists_matrix = _exists_matrix.reindex(
                        columns=_seat_matrix.columns, fill_value=0)

                    for _c in _seat_matrix.columns:
                        _seat_matrix[_c] = [
                            (name if str(name).strip()
                             else ("空席" if exists else ""))
                            for name, exists in zip(
                                _seat_matrix[_c], _exists_matrix[_c])
                        ]

                    _seat_matrix = _seat_matrix.reset_index()
                    # どの現場にとっても座席が1つも無い列（表示している
                    # 現場すべてで空欄のままの列）だけを、最後に取り除く。
                    _hour_cols_in_matrix = [
                        c for c in _seat_matrix.columns
                        if c not in ("現場名", "日付", "座席番号")]
                    _all_empty_cols = [
                        c for c in _hour_cols_in_matrix
                        if (_seat_matrix[c] == "").all()]
                    _seat_matrix = _seat_matrix.drop(columns=_all_empty_cols)
                    _seat_matrix.columns = [
                        str(c) if isinstance(c, str) else f"{c}時"
                        for c in _seat_matrix.columns]
                    st.caption(
                        f"表示中：{len(_seat_matrix)} 座席ぶん。"
                        "「空席」は座席が存在するが未割当、氏名が入っていれば"
                        "その人が確定または希望中であることを示します。"
                        "座席がそもそも存在しない時間帯は空欄のままです。")
                    st.dataframe(_seat_matrix, hide_index=True, width="stretch")
                    _seat_matrix_csv = _seat_matrix.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 座席リストのCSVをダウンロード（マトリクス形式）", _seat_matrix_csv,
                        file_name=f"座席リスト_マトリクス_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="seat_matrix_csv")

st.markdown("---")
if SHOW.get("pool_list", True):
    st.header("🏊 プール要員一覧（席が埋まって確定できていない希望者）")
    st.caption(
        "第1希望の現場・時間帯が、他の人の確定によって既に席が埋まって"
        "しまっている「未処理」の希望者を一覧にします。プール判定された"
        "人には、自動でLINEに近場の空き現場を案内し、本人が気になる現場を"
        "ボタンで選べます（確定は、この画面で管理者が行います）。")

    _reference_df_pool = load_reference()
    _pool_df = find_pool_wishes(wishes_df, confirmed_df, _reference_df_pool)

    if _pool_df.empty:
        st.info("現在、席が埋まって確定できない希望者はいません。")
    else:
        st.write(f"プール要員：{len(_pool_df)} 名")

        # まだ通知していない人にだけ、自動でLINEに振替候補を案内する
        # （毎回の画面表示のたびに再送しないよう「プール通知済み」で防ぐ）。
        for _, prow in _pool_df.iterrows():
            if str(prow.get("プール通知済み", "")).strip():
                continue
            _alts_for_notify = suggest_alternative_slots(
                prow, sites_df, _reference_df_pool, confirmed_df, wishes_df)
            if _alts_for_notify:
                _wish_info = (
                    f"{prow['希望日']} {prow['開始']}〜{prow['終了']}\n"
                    f"第1希望：{prow['第1希望現場']}")
                send_pool_alternatives(
                    prow.get("line_user_id", ""), prow["wish_id"],
                    _wish_info, _alts_for_notify)
            update_wish_field(prow["wish_id"], "プール通知済み", "済")

        # 本人がボタンで振替希望を意思表示している人を、優先して上に表示する
        _pool_df = _pool_df.assign(
            _has_transfer=lambda d: d["振替希望現場"].astype(str).str.strip() != ""
        ).sort_values("_has_transfer", ascending=False)

        for _, prow in _pool_df.iterrows():
            with st.container(border=True):
                _transfer_wish = str(prow.get("振替希望現場", "")).strip()
                if _transfer_wish:
                    st.write(
                        f"⭐ **{prow['氏名']}**　第1希望：{prow['第1希望現場']}　"
                        f"{prow['希望日']} {prow['開始']}〜{prow['終了']}")
                    st.caption(f"本人が「{_transfer_wish}」を希望しています。")
                else:
                    st.write(
                        f"**{prow['氏名']}**　第1希望：{prow['第1希望現場']}　"
                        f"{prow['希望日']} {prow['開始']}〜{prow['終了']}")
                _alts = suggest_alternative_slots(
                    prow, sites_df, _reference_df_pool, confirmed_df, wishes_df)
                if not _alts:
                    st.caption("同じエリアに、空いている現場が見つかりませんでした。")
                else:
                    _alt_labels = [f"{a['現場']}（空き{a['空き人数']:g}人）" for a in _alts]
                    # 本人が意思表示している現場があれば、それを初期選択にする
                    _default_idx = 0
                    for _i, a in enumerate(_alts):
                        if a["現場"] == _transfer_wish:
                            _default_idx = _i
                            break
                    _alt_choice = st.selectbox(
                        "振替先の候補", _alt_labels, index=_default_idx,
                        key=f"pool_alt_{prow['wish_id']}")
                    _chosen_site = _alts[_alt_labels.index(_alt_choice)]["現場"]
                    if st.button(
                            f"↔️ {_chosen_site}へ振り替えて確定する",
                            key=f"pool_confirm_{prow['wish_id']}"):
                        append_confirmed({
                            "shift_id": prow["wish_id"],
                            "氏名": prow["氏名"],
                            "line_user_id": prow.get("line_user_id", ""),
                            "現場": _chosen_site,
                            "日付": prow["希望日"],
                            "開始": prow["開始"],
                            "終了": prow["終了"],
                            "マッチング方法": "振替あっせん",
                            "確定日時": "",
                        })
                        update_wish_status(prow["wish_id"], "マッチ済", _chosen_site)
                        _ok, _msg = send_confirmation(
                            prow.get("line_user_id", ""),
                            f"シフトが確定しました（振替）。\n"
                            f"{prow['希望日']} {prow['開始']}〜{prow['終了']}\n"
                            f"現場：{_chosen_site}\n"
                            f"（第1希望の{prow['第1希望現場']}は満席のため、"
                            f"近くの現場にご案内しました）")
                        st.success(f"{prow['氏名']}さんを{_chosen_site}へ振り替えました。{_msg}")
                        st.rerun()

    st.markdown("---")
if SHOW.get("matching_candidates", True):
    st.header("📋 マッチング候補の確認")

    suggestions = build_suggestions(wishes_df, sites_df, confirmed_df)

    if suggestions.empty:
        st.info("未処理の希望はありません。")
    else:
        for _, row in suggestions.iterrows():
            with st.container(border=True):
                cols = st.columns([2, 2, 2, 3, 2])
                cols[0].write(f"**{row['氏名']}**")
                cols[1].write(f"{row['希望日']}\n{row['開始']}〜{row['終了']}")
                cols[2].write(row["経験"])
                cols[3].write(f"提案：{row['提案現場']}")
                if row["同時希望人数"] > 1:
                    cols[3].caption(f"同じ第1希望に {row['同時希望人数']} 名が希望中")

                candidates = row["候補現場一覧"]
                final_site = cols[4].selectbox(
                    "確定する現場", candidates, key=f"site_{row['wish_id']}")

                if cols[4].button("✅ この内容で確定する", key=f"confirm_{row['wish_id']}"):
                    method = "自動" if final_site == row["第1希望現場"] else "スライド"

                    append_confirmed({
                        "shift_id": row["wish_id"],
                        "氏名": row["氏名"],
                        "line_user_id": wishes_df.loc[
                            wishes_df["wish_id"] == row["wish_id"], "line_user_id"
                        ].iloc[0] if (wishes_df["wish_id"] == row["wish_id"]).any() else "",
                        "現場": final_site,
                        "日付": row["希望日"],
                        "開始": row["開始"],
                        "終了": row["終了"],
                        "マッチング方法": method,
                        "確定日時": "",
                    })
                    update_wish_status(row["wish_id"], "マッチ済", final_site)

                    line_user_id = wishes_df.loc[
                        wishes_df["wish_id"] == row["wish_id"], "line_user_id"
                    ].iloc[0] if (wishes_df["wish_id"] == row["wish_id"]).any() else ""
                    ok, msg = send_confirmation(
                        line_user_id,
                        f"シフトが確定しました。\n{row['希望日']} {row['開始']}〜{row['終了']}\n"
                        f"現場：{final_site}")
                    st.success(f"確定しました（{method}）。{msg}")
                    st.rerun()

    st.markdown("---")
if SHOW.get("confirmed_list", True):
    st.header("✅ 確定済みシフト一覧")
    if confirmed_df.empty:
        st.info("まだ確定したシフトはありません。")
    else:
        st.dataframe(confirmed_df, hide_index=True, width="stretch")

        with st.expander("📤 確定シフトを47時間帯ダイス表として書き出す"):
            st.caption(
                "確定したシフトから、現場ごと・1日単位の47時間帯ダイス表を"
                "作ります。前の特許用アプリの書き出しと同じ形式なので、"
                "運用時に読み込んで実績として使ったり、このアプリの"
                "「実績データの取り込み」に読み込ませてお手本を更新し直したり"
                "できます。")
            _daily_long = build_daily_dice_from_confirmed(confirmed_df)
            if _daily_long.empty:
                st.info("まだダイス表にできる確定シフトがありません。")
            else:
                _dtab1, _dtab2 = st.tabs(
                    ["📋 縦長形式（取り込み用）", "🎲 マトリクス形式（1日1行・47時間帯）"])
                with _dtab1:
                    st.dataframe(_daily_long.round(2), hide_index=True, width="stretch")
                    _daily_long_csv = _daily_long.round(2).to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 縦長形式のCSVをダウンロード", _daily_long_csv,
                        file_name=f"確定シフトダイス_縦長_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="daily_dice_long")
                with _dtab2:
                    _daily_matrix = _daily_long.pivot_table(
                        index=["現場名", "日付"], columns="時間帯", values="頭数",
                        fill_value=0).reset_index()
                    _daily_matrix.columns = [
                        str(c) if isinstance(c, str) else f"{c}時"
                        for c in _daily_matrix.columns]
                    st.dataframe(_daily_matrix.round(2), hide_index=True, width="stretch")
                    _daily_matrix_csv = _daily_matrix.round(2).to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 マトリクス形式のCSVをダウンロード", _daily_matrix_csv,
                        file_name=f"確定シフトダイス_マトリクス_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="daily_dice_matrix")

if SHOW.get("site_master", True):
    with st.expander("🧑‍🤝‍🧑 現場マスタ（エリア設定）"):
        st.caption("同じエリアの現場同士が、近場スライドの候補になります。")
        st.dataframe(sites_df, hide_index=True, width="stretch")

if SHOW.get("alias_management", True):
    with st.expander("🔤 現場名エイリアスの管理"):
        st.caption(
            "登録済みの表記ゆれ対応（例：「Kosugi 3rd」→「kosugi3rd Avenue」）の"
            "一覧です。間違えて登録してしまった場合は、ここから削除・修正できます。")
        _alias_df_view = load_aliases()
        if _alias_df_view.empty:
            st.info("まだ登録されているエイリアスはありません。")
        else:
            for i, row in _alias_df_view.reset_index(drop=True).iterrows():
                ac1, ac2, ac3, ac4 = st.columns([2, 1, 2, 1])
                ac1.write(f"`{row['表記ゆれ']}`")
                ac2.write("→")
                _site_options = list(sites_df["現場名"]) if not sites_df.empty else []
                _current = row["正式名"]
                _idx = _site_options.index(_current) if _current in _site_options else 0
                _new_target = ac3.selectbox(
                    "正式名", _site_options if _site_options else [_current],
                    index=_idx, key=f"alias_edit_{i}", label_visibility="collapsed")
                if ac4.button("🗑️ 削除", key=f"alias_delete_{i}"):
                    if delete_alias(row["表記ゆれ"]):
                        st.success(f"「{row['表記ゆれ']}」のエイリアスを削除しました。")
                        st.rerun()
                if _new_target != _current:
                    if st.button(
                            f"「{row['表記ゆれ']}」の正式名を「{_new_target}」に直す",
                            key=f"alias_update_{i}"):
                        if add_alias(row["表記ゆれ"], _new_target):
                            st.success(
                                f"「{row['表記ゆれ']}」→「{_new_target}」に修正しました。")
                            st.rerun()

        st.markdown("---")
        st.markdown("###### ➕ 新しく表記ゆれを登録する")
        st.caption(
            "実績データの取り込み時に警告から登録する以外に、ここから直接、"
            "手動でも登録できます。")
        _new_alias_c1, _new_alias_c2, _new_alias_c3 = st.columns([2, 2, 1])
        _new_alias_variant = _new_alias_c1.text_input(
            "表記ゆれ（実際に来る方の名前）", key="new_alias_variant")
        _new_alias_site_options = list(sites_df["現場名"]) if not sites_df.empty else []
        _new_alias_target = _new_alias_c2.selectbox(
            "正式名（現場マスタの名前）", _new_alias_site_options,
            key="new_alias_target") if _new_alias_site_options else None
        if _new_alias_c3.button("➕ 登録する", key="new_alias_add_btn"):
            if not _new_alias_variant.strip():
                st.warning("表記ゆれの欄が空欄です。")
            elif not _new_alias_target:
                st.warning("現場マスタに現場が登録されていません。")
            else:
                if add_alias(_new_alias_variant, _new_alias_target):
                    st.success(
                        f"「{_new_alias_variant.strip()}」→「{_new_alias_target}」を登録しました。")
                    st.rerun()

        st.markdown("---")
        st.caption(
            "エイリアスを後から登録した場合、それより前に保存された「基準"
            "パターン」は、古い（表記ゆれのままの）現場名で残ってしまって"
            "います。下のボタンで、今のエイリアス対応表を使って、既存の"
            "お手本データの現場名をまとめて正規化し直せます。")
        if st.button("🧹 既存の基準パターンの現場名を、今のエイリアスで正規化し直す",
                      key="renormalize_reference_btn"):
            _reference_df_all_fix = load_reference()
            _fixed_df, _n_changed = renormalize_reference_sites(
                _reference_df_all_fix, _alias_df_view)
            if _n_changed == 0:
                st.info("正規化が必要なデータはありませんでした。")
            else:
                save_reference_full(_fixed_df)
                st.success(
                    f"✅ {_n_changed} 行の現場名を正規化しました"
                    f"（正規化後の合計：{len(_fixed_df)} 行）。")
                st.rerun()
