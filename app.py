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
from datetime import datetime

import pandas as pd
import streamlit as st

from data_backend import (
    is_live_mode, load_wishes, load_sites, load_confirmed,
    update_wish_status, append_confirmed, seed_demo_data,
    load_reference, save_reference,
)
from matching import (
    build_suggestions, build_board, build_hour_breakdown,
    build_reference_pattern, reference_df_to_array,
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

    st.markdown("#### 🔍 マスをクリックする感覚で、時間帯ごとの内訳を見る")
    st.caption(
        "上の盤面は1日単位の合計人数ですが、こちらは選んだ現場・日付を"
        "1時間刻み（日またぎの勤務は翌日側まで延長）で分解して表示します。"
        "「お手本」は、下の「実績データの取り込み」で作成した、この現場の"
        "いつもの人数パターンです。お手本と見比べながら、少しずつ希望を"
        "確定に当てはめてください。")
    hc1, hc2 = st.columns(2)
    _drill_site = hc1.selectbox(
        "現場を選択", list(labels.index) if len(labels.index) else [], key="drill_site")
    _drill_date = hc2.selectbox(
        "日付を選択", list(labels.columns) if len(labels.columns) else [], key="drill_date")
    if _drill_site and _drill_date:
        _c_heads, _p_heads, _c_names, _p_names = build_hour_breakdown(
            confirmed_df, wishes_df, _drill_site, _drill_date)
        _ref_heads = reference_df_to_array(reference_df, _drill_site)
        st.markdown(
            render_hourly_html(_c_heads, _p_heads, _c_names, _p_names, _ref_heads),
            unsafe_allow_html=True)

    with st.expander("📥 実績データの取り込み（お手本ダイスの作成）"):
        st.caption(
            "過去（例：直近1週間）の実際の勤怠データ（現場・日付・出勤時刻・"
            "退勤時刻の列を含むCSV）を取り込むと、その現場の「いつもの人数"
            "パターン」を自動で計算し、お手本ダイスとして保存します。"
            "曜日は区別せず、取り込んだ期間全体の1日あたり平均で近似する"
            "簡易版です（第一弾）。同じ現場を取り込み直すと、内容は最新の"
            "ものに置き換わります。")
        actual_file = st.file_uploader(
            "実績CSV", type=["csv"], key="actual_upload")
        if actual_file:
            try:
                actual_raw = pd.read_csv(actual_file, encoding="utf-8-sig", dtype=str)
            except UnicodeDecodeError:
                actual_file.seek(0)
                actual_raw = pd.read_csv(actual_file, encoding="cp932", dtype=str)
            actual_cols = list(actual_raw.columns)
            st.caption(f"{len(actual_raw)} 行 ／ 認識した列：{', '.join(actual_cols[:12])}")

            ac1, ac2, ac3, ac4 = st.columns(4)
            c_site = ac1.selectbox(
                "現場の列", actual_cols,
                index=actual_cols.index(guess_col(actual_cols, ["現場", "事業所", "拠点"]))
                if guess_col(actual_cols, ["現場", "事業所", "拠点"]) in actual_cols else 0,
                key="ac_site")
            c_date = ac2.selectbox(
                "日付の列", actual_cols,
                index=actual_cols.index(guess_col(actual_cols, ["日付", "勤務日"]))
                if guess_col(actual_cols, ["日付", "勤務日"]) in actual_cols else 0,
                key="ac_date")
            c_start = ac3.selectbox(
                "出勤時刻の列", actual_cols,
                index=actual_cols.index(guess_col(actual_cols, ["出勤", "開始"]))
                if guess_col(actual_cols, ["出勤", "開始"]) in actual_cols else 0,
                key="ac_start")
            c_end = ac4.selectbox(
                "退勤時刻の列", actual_cols,
                index=actual_cols.index(guess_col(actual_cols, ["退勤", "終了"]))
                if guess_col(actual_cols, ["退勤", "終了"]) in actual_cols else 0,
                key="ac_end")

            if st.button("📊 この内容でお手本ダイスを作成する", key="build_reference_btn"):
                actual_df = actual_raw.rename(columns={
                    c_site: "現場", c_date: "日付", c_start: "開始", c_end: "終了",
                })[["現場", "日付", "開始", "終了"]].dropna()
                now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                target_sites = sorted(actual_df["現場"].dropna().unique())
                done = 0
                for s in target_sites:
                    pattern = build_reference_pattern(actual_df, s)
                    if pattern and save_reference(s, pattern, now_s):
                        done += 1
                st.success(f"✅ {done} 現場分のお手本ダイスを作成・更新しました。")
                st.rerun()

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

with st.expander("🧑‍🤝‍🧑 現場マスタ（エリア設定）"):
    st.caption("同じエリアの現場同士が、近場スライドの候補になります。")
    st.dataframe(sites_df, hide_index=True, width="stretch")
