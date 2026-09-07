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

import streamlit as st

from data_backend import (
    is_live_mode, load_wishes, load_sites, load_confirmed,
    update_wish_status, append_confirmed, seed_demo_data,
)
from matching import build_suggestions, build_board, build_hour_breakdown
from line_notify import send_confirmation


def render_hourly_html(confirmed_heads, pending_heads, confirmed_names, pending_names):
    """
    0〜47時間帯の確定・希望人数を、ダイス表示と同じ考え方（1時間ごとの
    マス）で、色付きの2行の表として組み立てる。動きがある時間帯の
    前後だけを表示し、テーブルが無駄に横長にならないようにする。
    数字の下に、その時間帯にかかっている人の氏名を小さく添える
    （「そのマスに実際は誰が入っているか」が一目で分かるようにするため）。
    """
    active = [h for h in range(48)
              if confirmed_heads[h] > 0 or pending_heads[h] > 0]
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

    st.markdown("#### 🔍 マスをクリックする感覚で、時間帯ごとの内訳を見る")
    st.caption(
        "上の盤面は1日単位の合計人数ですが、こちらは選んだ現場・日付を"
        "1時間刻み（日またぎの勤務は翌日側まで延長）で分解して表示します。"
        "どの時間帯に人が足りている／足りていないかが分かります。")
    hc1, hc2 = st.columns(2)
    _drill_site = hc1.selectbox(
        "現場を選択", list(labels.index) if len(labels.index) else [], key="drill_site")
    _drill_date = hc2.selectbox(
        "日付を選択", list(labels.columns) if len(labels.columns) else [], key="drill_date")
    if _drill_site and _drill_date:
        _c_heads, _p_heads, _c_names, _p_names = build_hour_breakdown(
            confirmed_df, wishes_df, _drill_site, _drill_date)
        st.markdown(
            render_hourly_html(_c_heads, _p_heads, _c_names, _p_names),
            unsafe_allow_html=True)

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
