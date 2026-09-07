# -*- coding: utf-8 -*-
"""
マッチングロジック（純粋なデータ処理のみ・Streamlit非依存）。

「経験者を優先する」「第1希望が埋まっていそうなら近場現場を提案する」
という2つのルールを中心に、管理画面がそのまま採用・修正できる
「提案」を1件ずつ作る。最終確定は必ず管理者の操作を経る
（自動提案＋管理者がポチポチ確認、の方針）。
"""
import pandas as pd

EXPERIENCE_THRESHOLD = 3  # この現場での通算出勤回数がこれ以上で「経験者」


def compute_experience_counts(confirmed_df: pd.DataFrame) -> pd.DataFrame:
    """
    確定シフトの実績から、「氏名 × 現場」ごとの通算出勤回数を集計する。
    これが「経験者」判定の唯一の材料。手動タグ付けは行わない。
    """
    if confirmed_df.empty:
        return pd.DataFrame(columns=["氏名", "現場", "出勤回数"])
    g = (confirmed_df.groupby(["氏名", "現場"])
         .size().reset_index(name="出勤回数"))
    return g


def get_experience(exp_df: pd.DataFrame, name: str, site: str) -> int:
    row = exp_df[(exp_df["氏名"] == name) & (exp_df["現場"] == site)]
    if row.empty:
        return 0
    return int(row.iloc[0]["出勤回数"])


def is_experienced(count: int) -> bool:
    return count >= EXPERIENCE_THRESHOLD


def nearby_sites(sites_df: pd.DataFrame, site: str) -> list:
    """指定した現場と同じ「エリア」に属する、他の現場名一覧。"""
    row = sites_df[sites_df["現場名"] == site]
    if row.empty:
        return []
    area = row.iloc[0]["エリア"]
    others = sites_df[(sites_df["エリア"] == area) & (sites_df["現場名"] != site)]
    return list(others["現場名"])


def site_headcount_on_date(confirmed_df: pd.DataFrame, site: str, date: str) -> int:
    """その現場・その日に、すでに確定している人数。「混み具合」の目安。"""
    if confirmed_df.empty:
        return 0
    return int(((confirmed_df["現場"] == site) & (confirmed_df["日付"] == date)).sum())


def build_suggestions(wishes_df: pd.DataFrame, sites_df: pd.DataFrame,
                       confirmed_df: pd.DataFrame) -> pd.DataFrame:
    """
    「未処理」の希望それぞれについて、提案内容を1行にまとめる。

    列：
      wish_id, 氏名, 希望日, 開始, 終了, 第1希望現場,
      提案現場       … 基本は第1希望。第1希望の混み具合が上位者に譲られる場合は
                       近場のより空いている現場を提案する
      経験          … 提案現場での経験（経験者／初めて）
      同時希望人数   … 同じ日・同じ第1希望を出している他の人数（埋まり具合の参考）
      候補現場一覧   … 管理画面のプルダウンに出す、選び直せる現場の候補
    """
    pending = wishes_df[wishes_df["ステータス"] == "未処理"].copy()
    if pending.empty:
        return pd.DataFrame(columns=[
            "wish_id", "氏名", "希望日", "開始", "終了", "第1希望現場",
            "提案現場", "経験", "同時希望人数", "候補現場一覧"])

    exp_df = compute_experience_counts(confirmed_df)

    # 同じ日・同じ第1希望現場を出している人数（多い日は競合が起きやすい）
    comp_counts = pending.groupby(["希望日", "第1希望現場"]).size()

    rows = []
    for _, w in pending.iterrows():
        site = w["第1希望現場"]
        date = w["希望日"]
        name = w["氏名"]

        exp_count = get_experience(exp_df, name, site)
        experienced = is_experienced(exp_count)
        same_wish_count = int(comp_counts.get((date, site), 1))
        candidates = [site] + nearby_sites(sites_df, site)

        # 提案ロジック：
        #   競合者がいない、または経験者であれば第1希望をそのまま提案。
        #   競合していて未経験者なら、近場現場のうち一番空いていそうな所を
        #   代替候補としても分かるようにする（採用するかは管理者が選ぶ）。
        suggested_site = site
        if same_wish_count > 1 and not experienced and nearby_sites(sites_df, site):
            alt = sorted(
                nearby_sites(sites_df, site),
                key=lambda s: site_headcount_on_date(confirmed_df, s, date))
            if alt:
                suggested_site = f"{site}（混雑の可能性）→ {alt[0]} を検討"
                candidates = [site] + alt

        rows.append({
            "wish_id": w["wish_id"],
            "氏名": name,
            "希望日": date,
            "開始": w["開始"],
            "終了": w["終了"],
            "第1希望現場": site,
            "提案現場": suggested_site,
            "経験": f"経験者（{exp_count}回）" if experienced else f"初めて／浅い（{exp_count}回）",
            "同時希望人数": same_wish_count,
            "候補現場一覧": candidates,
        })

    result = pd.DataFrame(rows)
    # 経験者を優先して上に出す（管理者が上から確認していけば経験者が先に確定しやすい）
    result["_exp_sort"] = result["氏名"].apply(
        lambda n: -1)  # プレースホルダ（下で実際の並び替えに使う値を入れ直す）
    result["_is_exp"] = result.apply(
        lambda r: is_experienced(get_experience(exp_df, r["氏名"], r["第1希望現場"])), axis=1)
    result = result.sort_values("_is_exp", ascending=False).drop(
        columns=["_exp_sort", "_is_exp"])
    return result.reset_index(drop=True)


def build_board(wishes_df: pd.DataFrame, confirmed_df: pd.DataFrame,
                 sites_df: pd.DataFrame, days_ahead: int = 14):
    """
    「現場 × 日付」のマス目（盤面）データを作る。ダイス盤面のように、
    各マスに「確定n件／希望n件」を表示するための2つの表を返す。

    戻り値：
      labels … マスに表示する文字列（例："確定2 / 希望1"）の表
      counts … マスの色分けに使う、確定人数だけの数値の表
      pendings … マスの色分けに使う、未処理希望件数だけの数値の表
    """
    today = pd.Timestamp.now().normalize()
    dates = [(today + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(days_ahead)]
    sites = list(sites_df["現場名"]) if not sites_df.empty else []

    labels = pd.DataFrame("", index=sites, columns=dates)
    counts = pd.DataFrame(0, index=sites, columns=dates)
    pendings = pd.DataFrame(0, index=sites, columns=dates)

    if not confirmed_df.empty and sites and dates:
        conf = confirmed_df[
            confirmed_df["現場"].isin(sites) & confirmed_df["日付"].isin(dates)]
        conf_counts = conf.groupby(["現場", "日付"]).size()
    else:
        conf_counts = pd.Series(dtype=int)

    if not wishes_df.empty and sites and dates:
        pend = wishes_df[
            (wishes_df["ステータス"] == "未処理")
            & wishes_df["第1希望現場"].isin(sites)
            & wishes_df["希望日"].isin(dates)]
        pend_counts = pend.groupby(["第1希望現場", "希望日"]).size()
    else:
        pend_counts = pd.Series(dtype=int)

    for site in sites:
        for date in dates:
            c = int(conf_counts.get((site, date), 0))
            p = int(pend_counts.get((site, date), 0))
            counts.loc[site, date] = c
            pendings.loc[site, date] = p
            parts = []
            if c:
                parts.append(f"確定{c}")
            if p:
                parts.append(f"希望{p}")
            labels.loc[site, date] = " / ".join(parts) if parts else "―"

    return labels, counts, pendings
