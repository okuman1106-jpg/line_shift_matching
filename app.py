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
    update_wish_status, append_confirmed, seed_demo_data,
    load_reference, save_reference, save_reference_bulk,
)
from matching import (
    build_suggestions, build_board, build_hour_breakdown,
    build_reference_pattern, build_reference_pattern_from_hourly,
    reference_df_to_array,
    build_staff_dice_rows, build_seat_grid, build_daily_dice_from_confirmed,
    generate_seat_list, annotate_seat_list_with_occupancy,
)
from line_notify import send_confirmation


def guess_col(cols, cands):
    """列名の一覧から、候補キーワードを含む最初の列を推測する。"""
    return next((c for c in cols if any(k in c for k in cands)), None)


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


def render_board_html(labels, counts, pendings):
    """
    「現場 × 日付」の盤面を、色付きのHTML表として組み立てる。
      緑の濃さ … 確定人数が多いほど濃くなる
      黄色    … 確定はまだ無いが、未処理の希望がある（要対応の目印）
    """
    def esc(v):
        return html_lib.escape(str(v))

    parts = ['<div style="overflow-x:auto;">'
             '<table style="border-collapse:collapse;font-size:13px;width:100%;">']
    parts.append('<tr>')
    parts.append(
        '<th style="border:1px solid #ddd;padding:8px;background:#f5f5f5;'
        'position:sticky;left:0;z-index:1;text-align:left;">現場＼日付</th>')
    for date in labels.columns:
        short = esc(date[5:]) if len(date) >= 5 else esc(date)
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
        for date in labels.columns:
            c = int(counts.loc[site, date])
            p = int(pendings.loc[site, date])
            label = esc(labels.loc[site, date])
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
        '色の濃い緑ほど確定人数が多い現場・日付です。黄色は、確定はまだ無いが'
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

st.markdown("---")

c1, c2, c3 = st.columns(3)
c1.metric("未処理の希望", int((wishes_df["ステータス"] == "未処理").sum()))
c2.metric("マッチ済み", int((wishes_df["ステータス"] == "マッチ済").sum()))
c3.metric("登録されている現場数", len(sites_df))

st.markdown("---")
st.header("📊 現場×日付の盤面")
st.caption("今日から2週間分の、現場ごとの確定・希望状況を一覧できます。")

_board_days = st.slider("表示する日数", 7, 30, 14, key="board_days")
labels, counts, pendings = build_board(wishes_df, confirmed_df, sites_df, days_ahead=_board_days)

if sites_df.empty:
    st.info("現場マスタに現場が登録されていません。")
else:
    st.markdown(render_board_html(labels, counts, pendings), unsafe_allow_html=True)

    reference_df = load_reference()

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

                if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_btn"):
                    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                    target_sites = sorted(actual_raw["現場名"].dropna().unique())
                    patterns = {}
                    for s in target_sites:
                        pattern = build_reference_pattern_from_hourly(actual_raw, s)
                        if pattern:
                            patterns[s] = pattern
                    done = save_reference_bulk(patterns, now_s)
                    st.success(f"✅ {done} 現場分のお手本ダイスを作成・更新しました。")
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

                if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_matrix_btn"):
                    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                    target_sites = sorted(_melted["現場名"].dropna().unique())
                    patterns = {}
                    for s in target_sites:
                        pattern = build_reference_pattern_from_hourly(_melted, s)
                        if pattern:
                            patterns[s] = pattern
                    done = save_reference_bulk(patterns, now_s)
                    st.success(f"✅ {done} 現場分のお手本ダイスを作成・更新しました。")
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

                if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_raw_btn"):
                    actual_df = actual_raw.rename(columns={
                        c_site: "現場", c_date: "日付", c_start: "開始", c_end: "終了",
                    })[["現場", "日付", "開始", "終了"]].dropna()
                    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                    target_sites = sorted(actual_df["現場"].dropna().unique())
                    patterns = {}
                    for s in target_sites:
                        pattern = build_reference_pattern(actual_df, s)
                        if pattern:
                            patterns[s] = pattern
                    done = save_reference_bulk(patterns, now_s)
                    st.success(f"✅ {done} 現場分のお手本ダイスを作成・更新しました。")
                    st.rerun()

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
                    st.warning(
                        "座席が1件も生成されませんでした。お手本ダイスが"
                        "登録されているか確認してください。")
                else:
                    _seat_list = annotate_seat_list_with_occupancy(
                        _seat_list, _reference_df_all, confirmed_df, wishes_df)
                    _n_filled = int((_seat_list["状態"] != "空席").sum())
                    st.success(
                        f"✅ {len(_target_dates)} 日ぶん・{_seat_list['現場名'].nunique()} "
                        f"現場ぶん、合計 {len(_seat_list)} 席を生成しました"
                        f"（うち {_n_filled} 席に申請者がいます）。")
                    st.caption(
                        "「氏名」「状態」の列で、確定前（希望）でも確定後でも、"
                        "その座席の申請者が分かるようになっています。")
                    st.dataframe(_seat_list, hide_index=True, width="stretch")
                    _seat_list_csv = _seat_list.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 座席リストのCSVをダウンロード", _seat_list_csv,
                        file_name=f"座席リスト_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv", key="seat_list_csv")

st.markdown("---")
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

with st.expander("🧑‍🤝‍🧑 現場マスタ（エリア設定）"):
    st.caption("同じエリアの現場同士が、近場スライドの候補になります。")
    st.dataframe(sites_df, hide_index=True, width="stretch")
