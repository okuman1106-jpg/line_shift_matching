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
import streamlit as st

from data_backend import (
    is_live_mode, load_wishes, load_sites, load_confirmed,
    update_wish_status, append_confirmed, seed_demo_data,
)
from matching import build_suggestions
from line_notify import send_confirmation

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
