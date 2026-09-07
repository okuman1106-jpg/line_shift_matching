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


def _parse_hm_to_minutes(s):
    """'HH:MM' 形式の文字列を、0時からの分数に変換する。"""
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


def expand_to_hour_bands(start_str, end_str):
    """
    出勤・退勤（'HH:MM'形式）を受け取り、0〜47時の「通し時間帯」ごとに
    その時間帯にかかっている分数を返す。

    ダイス表示と同じ考え方：
      ・1時間ごとのマスに分解し、頭と尻には端数（分）が残る
      ・終了時刻が開始時刻以下（日またぎ）の場合は、終了側を+24時間して扱う
        （例：22:00〜翌6:00 → 22:00〜30:00として計算し、22,23,24,25...の
        マスに分配する）
      ・各マスの分数を60で割れば、そのマスにおける頭数（1人＝1.0）になる

    戻り値：{時間帯(0〜47の整数): 分数} の辞書
    """
    sm = _parse_hm_to_minutes(start_str)
    em = _parse_hm_to_minutes(end_str)
    if em <= sm:
        em += 24 * 60  # 日またぎ

    cells = {}
    h = sm // 60
    while h * 60 < em:
        cell_start = max(sm, h * 60)
        cell_end = min(em, (h + 1) * 60)
        minutes = cell_end - cell_start
        if minutes > 0:
            cells[h] = cells.get(h, 0) + minutes
        h += 1
    return cells


def build_hour_breakdown(confirmed_df: pd.DataFrame, wishes_df: pd.DataFrame,
                          site: str, date: str):
    """
    指定した「現場」「日付」について、0〜47時間帯ごとの
      ・確定人数／確定している人の氏名（確定シフトの実績から）
      ・希望人数／希望している人の氏名（未処理の希望から）
    を返す。

    人数は48個の数値配列（頭数、小数）、氏名はそれぞれの時間帯に対応する
    48個のリスト（各時間帯にかかっている人の氏名の一覧）として返す。
    これにより、ダイスの数字マスの中身が「実際は誰なのか」を追跡できる。

    戻り値：(confirmed_heads, pending_heads, confirmed_names, pending_names)
    """
    confirmed_heads = [0.0] * 48
    pending_heads = [0.0] * 48
    confirmed_names = [[] for _ in range(48)]
    pending_names = [[] for _ in range(48)]

    if not confirmed_df.empty:
        rows = confirmed_df[
            (confirmed_df["現場"] == site) & (confirmed_df["日付"] == date)]
        for _, r in rows.iterrows():
            try:
                cells = expand_to_hour_bands(r["開始"], r["終了"])
            except Exception:
                continue
            name = str(r.get("氏名", "")).strip() or "(氏名不明)"
            for h, minutes in cells.items():
                if 0 <= h < 48:
                    confirmed_heads[h] += minutes / 60.0
                    if name not in confirmed_names[h]:
                        confirmed_names[h].append(name)

    if not wishes_df.empty:
        rows = wishes_df[
            (wishes_df["第1希望現場"] == site) & (wishes_df["希望日"] == date)
            & (wishes_df["ステータス"] == "未処理")]
        for _, r in rows.iterrows():
            try:
                cells = expand_to_hour_bands(r["開始"], r["終了"])
            except Exception:
                continue
            name = str(r.get("氏名", "")).strip() or "(氏名不明)"
            for h, minutes in cells.items():
                if 0 <= h < 48:
                    pending_heads[h] += minutes / 60.0
                    if name not in pending_names[h]:
                        pending_names[h].append(name)

    return confirmed_heads, pending_heads, confirmed_names, pending_names


def build_reference_pattern(actual_df: pd.DataFrame, site: str):
    """
    実績データ（1行＝1回の勤務。列：現場・日付・開始・終了）から、
    指定した現場の「お手本ダイス」を作る。

    直近の実績をそのまま使う方式：アップロードされたデータに含まれる
    日数で48時間帯ごとの頭数を合計し、日数で割って「1日あたりの平均的な
    頭数パターン」にする（「週単位でやってみる」の第一弾として、
    曜日は区別せず、期間全体の単純平均で近似する）。

    戻り値：{時間帯(0〜47): 基準人数(float)} の辞書（0の時間帯は含まない）
    """
    rows = actual_df[actual_df["現場"] == site]
    if rows.empty:
        return {}

    n_days = rows["日付"].nunique() or 1
    totals = [0.0] * 48
    for _, r in rows.iterrows():
        try:
            cells = expand_to_hour_bands(r["開始"], r["終了"])
        except Exception:
            continue
        for h, minutes in cells.items():
            if 0 <= h < 48:
                totals[h] += minutes / 60.0

    return {h: totals[h] / n_days for h in range(48) if totals[h] > 0}


def build_reference_pattern_from_hourly(hourly_df: pd.DataFrame, site: str):
    """
    すでに時間帯ごとに集計済みの実績データ（列：現場名・日付・時間帯・頭数。
    前の特許用アプリの「お手本ダイス用データの書き出し」がこの形式で出す）
    から、指定した現場のお手本ダイスを作る。

    複数日ぶんのデータが含まれている場合は、含まれる日数で割って
    「1日あたり平均」にする（曜日は区別しない簡易版）。
    出退勤時刻からの再計算が不要なため、列名の指定は要らない。

    戻り値：{時間帯(0〜47): 基準人数(float)} の辞書（0の時間帯は含まない）
    """
    rows = hourly_df[hourly_df["現場名"] == site]
    if rows.empty:
        return {}

    n_days = rows["日付"].nunique() or 1
    totals = {}
    for _, r in rows.iterrows():
        try:
            h = int(r["時間帯"])
            v = float(r["頭数"])
        except (ValueError, TypeError):
            continue
        if 0 <= h < 48:
            totals[h] = totals.get(h, 0.0) + v

    return {h: v / n_days for h, v in totals.items() if v > 0}


def reference_df_to_array(reference_df: pd.DataFrame, site: str):
    """
    保存済みの「基準パターン」データ（現場・時間帯・基準人数の表）から、
    指定した現場の48時間帯分の配列を取り出す。
    """
    arr = [0.0] * 48
    if reference_df.empty:
        return arr
    rows = reference_df[reference_df["現場"] == site]
    for _, r in rows.iterrows():
        try:
            h = int(r["時間帯"])
            v = float(r["基準人数"])
        except (ValueError, TypeError):
            continue
        if 0 <= h < 48:
            arr[h] = v
    return arr


def build_staff_dice_rows(confirmed_df: pd.DataFrame, wishes_df: pd.DataFrame,
                           site: str, date: str):
    """
    「現場×日付」を指定し、そこに関わるスタッフを1人1行として、
    48時間帯ぶんの配列（頭数）を持つ行のリストを作る。
    縦にスタッフ名、横に47時間帯（0〜47）が並ぶ、本物のダイス表と
    同じ形にするための元データ。

    確定シフトの行はそのまま「種別＝確定」として、未処理の希望は
    「種別＝希望」として、それぞれ1行ずつ返す。希望の行だけ、
    その場で確定できるように wish_id を保持しておく。

    戻り値：[{"氏名":str, "種別":"確定"/"希望", "wish_id":str|None,
              "開始":str, "終了":str, "hours":[48個のfloat]}, ...]
    """
    result = []

    if not confirmed_df.empty:
        rows = confirmed_df[
            (confirmed_df["現場"] == site) & (confirmed_df["日付"] == date)]
        for _, r in rows.iterrows():
            try:
                cells = expand_to_hour_bands(r["開始"], r["終了"])
            except Exception:
                continue
            hours = [0.0] * 48
            for h, minutes in cells.items():
                if 0 <= h < 48:
                    hours[h] = minutes / 60.0
            result.append({
                "氏名": str(r.get("氏名", "")).strip() or "(氏名不明)",
                "種別": "確定", "wish_id": None,
                "開始": r["開始"], "終了": r["終了"], "hours": hours,
            })

    if not wishes_df.empty:
        rows = wishes_df[
            (wishes_df["第1希望現場"] == site) & (wishes_df["希望日"] == date)
            & (wishes_df["ステータス"] == "未処理")]
        for _, r in rows.iterrows():
            try:
                cells = expand_to_hour_bands(r["開始"], r["終了"])
            except Exception:
                continue
            hours = [0.0] * 48
            for h, minutes in cells.items():
                if 0 <= h < 48:
                    hours[h] = minutes / 60.0
            result.append({
                "氏名": str(r.get("氏名", "")).strip() or "(氏名不明)",
                "種別": "希望", "wish_id": r["wish_id"],
                "開始": r["開始"], "終了": r["終了"], "hours": hours,
            })

    # 確定を上、希望を下に。同じ種別内は開始時刻順にしておくと見やすい。
    result.sort(key=lambda x: (x["種別"] != "確定", x["開始"]))
    return result


def _find_common_free_seat(occupancy, hours_used, max_seats):
    """
    hours_used のすべての時間帯で共通して空いている、一番若い座席番号を探す。
    見つからなければ None を返す（呼び出し側で「座席あふれ」として扱う）。
    """
    if not hours_used or max_seats <= 0:
        return None
    for seat_no in range(1, max_seats + 1):
        if all((h, seat_no) not in occupancy for h in hours_used):
            return seat_no
    return None


def make_seat_id(site, date, hour, seat_no):
    """20260203_09_1_みやした のような、座席1マスぶんのID文字列を作る。"""
    date_key = str(date).replace("-", "").replace("/", "")
    return f"{date_key}_{hour:02d}_{seat_no}_{site}"


def build_seat_grid(reference_heads, confirmed_df: pd.DataFrame,
                     wishes_df: pd.DataFrame, site: str, date: str):
    """
    「座席番号は1時間ごとに独立」という方針で、お手本ダイスから座席を作り、
    確定シフト・未処理の希望をその座席に割り当てる（試作・1現場ぶん）。

    割り当てルール：
      ・各時間帯の座席数は、その時間帯のお手本人数を四捨五入した数
      ・確定シフトは、またがる時間帯すべてで共通して空いている一番若い
        座席番号に入る（優先的に座席を確保する）
      ・未処理の希望は、確定の後に、同じルールで空いている座席を探して
        「仮当てはめ」する。座席が足りなければ、超過枠として別扱いにする

    戻り値：
      seat_counts … 48時間帯ぶんの座席数（お手本から四捨五入した整数）
      occupancy   … {(時間帯, 座席番号): {"氏名":str, "状態":"確定"/"希望"/"希望(超過)",
                     "wish_id":str|None, "seat_id":str}}
    """
    seat_counts = [max(0, round(v)) for v in reference_heads]
    occupancy = {}

    if not confirmed_df.empty:
        rows = confirmed_df[
            (confirmed_df["現場"] == site) & (confirmed_df["日付"] == date)]
        for _, r in rows.iterrows():
            try:
                cells = expand_to_hour_bands(r["開始"], r["終了"])
            except Exception:
                continue
            hours_used = [h for h in cells if 0 <= h < 48]
            if not hours_used:
                continue
            max_seats = max(seat_counts[h] for h in hours_used)
            seat_no = _find_common_free_seat(occupancy, hours_used, max_seats)
            if seat_no is None:
                seat_no = max_seats + 1  # お手本より多い分は超過枠として追加
            name = str(r.get("氏名", "")).strip() or "(氏名不明)"
            for h in hours_used:
                occupancy[(h, seat_no)] = {
                    "氏名": name, "状態": "確定", "wish_id": None,
                    "seat_id": make_seat_id(site, date, h, seat_no),
                }

    if not wishes_df.empty:
        rows = wishes_df[
            (wishes_df["第1希望現場"] == site) & (wishes_df["希望日"] == date)
            & (wishes_df["ステータス"] == "未処理")]
        for _, r in rows.iterrows():
            try:
                cells = expand_to_hour_bands(r["開始"], r["終了"])
            except Exception:
                continue
            hours_used = [h for h in cells if 0 <= h < 48]
            if not hours_used:
                continue
            max_seats = max(seat_counts[h] for h in hours_used)
            seat_no = _find_common_free_seat(occupancy, hours_used, max_seats)
            status = "希望"
            if seat_no is None:
                seat_no = max_seats + 1
                status = "希望(超過)"
            name = str(r.get("氏名", "")).strip() or "(氏名不明)"
            for h in hours_used:
                occupancy[(h, seat_no)] = {
                    "氏名": name, "状態": status, "wish_id": r["wish_id"],
                    "seat_id": make_seat_id(site, date, h, seat_no),
                }

    return seat_counts, occupancy


def build_daily_dice_from_confirmed(confirmed_df: pd.DataFrame):
    """
    このアプリで確定したシフト（確定シフトシート）から、現場別・1日単位の
    47時間帯ダイス（縦長形式：現場名・日付・時間帯・頭数）を組み立てる。

    前の特許用アプリの「お手本ダイス用データの書き出し」と同じ形式にして
    あるので、そちらに読み込ませて実績として使うことも、このアプリの
    「実績データの取り込み」に読み込ませてお手本を更新することもできる。
    """
    if confirmed_df.empty:
        return pd.DataFrame(columns=["現場名", "日付", "時間帯", "頭数"])

    rows = []
    for _, r in confirmed_df.iterrows():
        try:
            cells = expand_to_hour_bands(r["開始"], r["終了"])
        except Exception:
            continue
        site = r.get("現場", "")
        date = r.get("日付", "")
        for h, minutes in cells.items():
            if 0 <= h < 48 and minutes > 0:
                rows.append({"現場名": site, "日付": date, "時間帯": h,
                            "頭数": minutes / 60.0})

    if not rows:
        return pd.DataFrame(columns=["現場名", "日付", "時間帯", "頭数"])

    out = (pd.DataFrame(rows)
           .groupby(["現場名", "日付", "時間帯"])["頭数"].sum()
           .reset_index())
    return out[out["頭数"] > 0].sort_values(["現場名", "日付", "時間帯"])


def generate_seat_list(reference_df: pd.DataFrame, dates: list):
    """
    保存済みの「お手本パターン」から、指定した日付（複数可）ぶんの
    座席番号を、登録されている全現場について一気に生成する。

    座席番号は「1時間ごとに独立」という方針で、その時間帯のお手本人数を
    四捨五入した数だけ座席を作る。1件＝1つの座席（現場・日付・時間帯・
    座席番号の組み合わせ）として、まだ誰も割り当てられていない
    「空席リスト」を返す（実際の割り当ては、希望が来てから
    build_seat_grid 等で行う）。

    戻り値：DataFrame（列：現場名・日付・時間帯・座席番号・座席ID）
    """
    if reference_df.empty or not dates:
        return pd.DataFrame(columns=["現場名", "日付", "時間帯", "座席番号", "座席ID"])

    rows = []
    for site in sorted(reference_df["現場"].dropna().unique()):
        arr = reference_df_to_array(reference_df, site)
        for date in dates:
            for h in range(48):
                seat_count = max(0, round(arr[h]))
                for seat_no in range(1, seat_count + 1):
                    rows.append({
                        "現場名": site, "日付": date, "時間帯": h,
                        "座席番号": seat_no,
                        "座席ID": make_seat_id(site, date, h, seat_no),
                    })

    if not rows:
        return pd.DataFrame(columns=["現場名", "日付", "時間帯", "座席番号", "座席ID"])
    return pd.DataFrame(rows).sort_values(["現場名", "日付", "時間帯", "座席番号"])


def annotate_seat_list_with_occupancy(seat_list: pd.DataFrame,
                                       reference_df: pd.DataFrame,
                                       confirmed_df: pd.DataFrame,
                                       wishes_df: pd.DataFrame):
    """
    座席リスト（空席テンプレート）に、実際の申請者の氏名と状態を
    紐づける。予約（希望）が来た時点で「希望・氏名」が見え、確定すると
    「確定・氏名」に切り替わる、というのを一覧で確認できるようにする。

    状態：空席／希望／希望(超過)／確定
    """
    if seat_list.empty:
        return seat_list.assign(氏名="", 状態="空席")

    out = seat_list.copy()
    out["氏名"] = ""
    out["状態"] = "空席"

    for (site, date), _ in seat_list.groupby(["現場名", "日付"]):
        ref_arr = reference_df_to_array(reference_df, site)
        _, occupancy = build_seat_grid(ref_arr, confirmed_df, wishes_df, site, date)
        mask = (out["現場名"] == site) & (out["日付"] == date)
        idxs = out[mask].index
        for i in idxs:
            h = int(out.at[i, "時間帯"])
            seat_no = int(out.at[i, "座席番号"])
            info = occupancy.get((h, seat_no))
            if info:
                out.at[i, "氏名"] = info["氏名"]
                out.at[i, "状態"] = info["状態"]

    return out
