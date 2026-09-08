# -*- coding: utf-8 -*-
"""
マッチングロジック（純粋なデータ処理のみ・Streamlit非依存）。

「経験者を優先する」「第1希望が埋まっていそうなら近場現場を提案する」
という2つのルールを中心に、管理画面がそのまま採用・修正できる
「提案」を1件ずつ作る。最終確定は必ず管理者の操作を経る
（自動提案＋管理者がポチポチ確認、の方針）。
"""
import pandas as pd


def detect_period_label(dates):
    """
    日付の一覧（文字列のリストやSeries）から、「このデータはいつの実績か」
    を表すラベルを作る。1つの月に収まっていれば「2026-02」のように、
    複数の月にまたがっていれば「2026-02〜2026-04」のように表示する。
    日付が読み取れない場合は空文字を返す。
    """
    parsed = pd.to_datetime(pd.Series(list(dates)), errors="coerce").dropna()
    if parsed.empty:
        return ""
    lo = parsed.min().strftime("%Y-%m")
    hi = parsed.max().strftime("%Y-%m")
    return lo if lo == hi else f"{lo}〜{hi}"

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


def build_day_hourly_board(wishes_df: pd.DataFrame, confirmed_df: pd.DataFrame,
                            sites_df: pd.DataFrame, date: str):
    """
    「1日を選んで、全現場 × 47時間帯」の盤面データを作る。
    「現場 × 日付」の盤面（build_board）は日が増えるほどマスが粗くなり、
    予約が増えると管理しづらくなるため、1日に絞った代わりに横軸を
    時間帯まで細かくした版。

    戻り値：
      labels … マスに表示する文字列（例："確定2 / 希望1"）の表
      counts … マスの色分けに使う、確定人数（頭数）だけの数値の表
      pendings … マスの色分けに使う、未処理希望人数（頭数）だけの数値の表
      active_sites … その日に動きがある現場だけを抜き出した一覧
                     （全現場を表示すると縦に長くなりすぎるため）
    """
    sites = list(sites_df["現場名"]) if not sites_df.empty else []
    hours = list(range(48))

    labels = pd.DataFrame("", index=sites, columns=hours)
    counts = pd.DataFrame(0.0, index=sites, columns=hours)
    pendings = pd.DataFrame(0.0, index=sites, columns=hours)

    for site in sites:
        c_heads, p_heads, _, _ = build_hour_breakdown(
            confirmed_df, wishes_df, site, date)
        for h in hours:
            counts.loc[site, h] = c_heads[h]
            pendings.loc[site, h] = p_heads[h]
            parts = []
            if c_heads[h] > 0:
                parts.append(f"確定{c_heads[h]:g}")
            if p_heads[h] > 0:
                parts.append(f"希望{p_heads[h]:g}")
            labels.loc[site, h] = " / ".join(parts) if parts else "―"

    active_sites = [s for s in sites
                    if counts.loc[s].sum() > 0 or pendings.loc[s].sum() > 0]
    return labels, counts, pendings, active_sites


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


def reference_df_to_array(reference_df: pd.DataFrame, site: str, period: str = None):
    """
    保存済みの「基準パターン」データ（現場・時間帯・基準人数・対象期間の表）
    から、指定した現場の48時間帯分の配列を取り出す。

    period を指定すれば、その対象期間（例："2026-02"）のものだけを使う。
    指定しなければ、その現場の中で最後に作成されたもの（作成日時が
    一番新しいもの）を自動で選ぶ（従来通りの動き）。
    """
    arr = [0.0] * 48
    if reference_df.empty:
        return arr
    rows = reference_df[reference_df["現場"] == site]
    if rows.empty:
        return arr

    if period is not None:
        rows = rows[rows.get("対象期間", "") == period]
    elif "作成日時" in rows.columns and rows["作成日時"].notna().any():
        latest_period = (
            rows.sort_values("作成日時", ascending=False)["対象期間"].iloc[0]
            if "対象期間" in rows.columns else None)
        if latest_period is not None:
            rows = rows[rows["対象期間"] == latest_period]

    for _, r in rows.iterrows():
        try:
            h = int(r["時間帯"])
            v = float(r["基準人数"])
        except (ValueError, TypeError):
            continue
        if 0 <= h < 48:
            arr[h] = v
    return arr


def list_reference_periods(reference_df: pd.DataFrame, site: str):
    """指定した現場について、保存されている対象期間の一覧（新しい順）を返す。"""
    if reference_df.empty:
        return []
    rows = reference_df[reference_df["現場"] == site]
    if rows.empty or "対象期間" not in rows.columns:
        return []
    periods = (rows[["対象期間", "作成日時"]].drop_duplicates()
              .sort_values("作成日時", ascending=False)["対象期間"].tolist())
    return [p for p in periods if p]


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


def normalize_site_name(name: str, aliases_df: pd.DataFrame) -> str:
    """
    現場名の表記ゆれを、対応表を使って正式名に変換する。
    対応表に登録が無ければ、入力された名前をそのまま返す
    （エイリアス未登録＝現場マスタと完全一致している、という前提）。
    """
    name = str(name).strip()
    if aliases_df is None or aliases_df.empty:
        return name
    hit = aliases_df[aliases_df["表記ゆれ"] == name]
    if not hit.empty:
        return hit.iloc[0]["正式名"]
    return name


def find_unmatched_site_names(names, sites_df: pd.DataFrame, aliases_df: pd.DataFrame):
    """
    アップロードされたデータに含まれる現場名のうち、正規化しても
    現場マスタに存在しない（＝表記ゆれの可能性がある）ものを一覧で返す。
    """
    known = set(sites_df["現場名"]) if sites_df is not None and not sites_df.empty else set()
    unmatched = []
    seen = set()
    for n in names:
        n = str(n).strip()
        if not n or n in seen:
            continue
        seen.add(n)
        normalized = normalize_site_name(n, aliases_df)
        if normalized not in known:
            unmatched.append(n)
    return unmatched


def get_wage_for(name: str, site: str, sites_df: pd.DataFrame, staff_wages_df: pd.DataFrame):
    """
    「スタッフ優遇時給」に登録があればそちらを優先し、無ければ現場の
    基本時給を返す。どちらにも時給の情報が無ければ 0 を返す。
    """
    if staff_wages_df is not None and not staff_wages_df.empty:
        hit = staff_wages_df[staff_wages_df["氏名"] == name]
        if not hit.empty:
            try:
                w = float(hit.iloc[0]["時給"])
                if w > 0:
                    return w
            except (ValueError, TypeError):
                pass

    if sites_df is not None and not sites_df.empty:
        hit = sites_df[sites_df["現場名"] == site]
        if not hit.empty:
            try:
                w = float(hit.iloc[0].get("時給", "") or 0)
                if w > 0:
                    return w
            except (ValueError, TypeError):
                pass

    return 0.0


def compute_labor_cost(confirmed_df: pd.DataFrame, sites_df: pd.DataFrame,
                        staff_wages_df: pd.DataFrame):
    """
    確定シフトの一覧に、1件ごとの「実働時間」「適用時給」「人件費」を
    付け加えて返す（簡易版：深夜・残業の割増は考慮しない、時給×時間の
    単純計算）。優遇時給が登録されている人はそちらを、登録が無い人は
    現場の基本時給を使う（どちらも無ければ0円として計算される＝
    未設定であることが分かるようにしている）。

    深夜・残業割増込みの精密な計算をしたい場合は
    compute_labor_cost_precise() を使うこと。
    """
    if confirmed_df.empty:
        return confirmed_df.assign(実働時間=[], 適用時給=[], 人件費=[])

    out = confirmed_df.copy()
    hours_list, wage_list, cost_list = [], [], []
    for _, r in out.iterrows():
        try:
            sm = _parse_hm_to_minutes(r["開始"])
            em = _parse_hm_to_minutes(r["終了"])
            if em <= sm:
                em += 24 * 60
            hours = (em - sm) / 60.0
        except Exception:
            hours = 0.0
        wage = get_wage_for(r.get("氏名", ""), r.get("現場", ""), sites_df, staff_wages_df)
        hours_list.append(round(hours, 2))
        wage_list.append(wage)
        cost_list.append(round(hours * wage))

    out["実働時間"] = hours_list
    out["適用時給"] = wage_list
    out["人件費"] = cost_list
    return out


# 深夜（22:00〜翌5:00）にあたる、0〜47時間帯の通し番号の集合。
# 22,23時（当日）と、0,1,2,3,4時（＝24,25,26,27,28時として翌日側に
# 現れる）が対象。
_NIGHT_HOURS_MOD24 = {22, 23, 0, 1, 2, 3, 4}


def compute_shift_wage_precise(start_str, end_str, hourly_wage,
                                night_rate=0.25, ot_rate=0.25,
                                daily_ot_threshold_hours=8.0):
    """
    前の特許用アプリ（人件費ダイス分析システム）と同じ考え方で、
    1回の勤務（出勤〜退勤）の賃金を、深夜割増・残業割増込みで計算する。

    賃金 = 時給 ×
      [ 全時間 ×（1 ＋ 深夜時間の割合 × 深夜割増率）
        ＋ 残業時間 × 残業割増率 ]

    残業時間は、シフトを時系列に並べたとき、1日の所定時間
    （既定8時間）を超えた「後ろ側」の時間とする（前のアプリの
    「残業枠×残業割合」の考え方を、1シフト単位に簡略化したもの）。
    深夜・残業の両方に該当する時間は、両方の割増が上乗せされる。

    戻り値：(実働時間, 深夜時間, 残業時間, 賃金)
    """
    cells = expand_to_hour_bands(start_str, end_str)  # {時間帯: 分}
    if not cells:
        return 0.0, 0.0, 0.0, 0.0

    total_minutes = sum(cells.values())
    total_hours = total_minutes / 60.0

    night_minutes = sum(
        m for h, m in cells.items() if (h % 24) in _NIGHT_HOURS_MOD24)
    night_hours = night_minutes / 60.0

    # 時系列順（＝時間帯の通し番号順）に積み上げて、所定時間を超えた
    # 分だけを残業として扱う。
    threshold_minutes = daily_ot_threshold_hours * 60
    cumulative = 0
    ot_minutes = 0
    for h in sorted(cells.keys()):
        m = cells[h]
        before = cumulative
        cumulative += m
        if cumulative > threshold_minutes:
            ot_minutes += min(m, cumulative - max(before, threshold_minutes))
    ot_hours = ot_minutes / 60.0

    night_share = (night_hours / total_hours) if total_hours > 0 else 0.0
    wage = hourly_wage * (
        total_hours * (1.0 + night_share * night_rate)
        + ot_hours * ot_rate
    )
    return round(total_hours, 2), round(night_hours, 2), round(ot_hours, 2), round(wage)


def compute_labor_cost_precise(confirmed_df: pd.DataFrame, sites_df: pd.DataFrame,
                                staff_wages_df: pd.DataFrame,
                                night_rate=0.25, ot_rate=0.25,
                                daily_ot_threshold_hours=8.0):
    """
    確定シフトの一覧に、深夜割増・残業割増込みの精密な人件費を付けて返す。
    列構成は compute_labor_cost() と互換（実働時間・適用時給・人件費）に
    加えて、深夜時間・残業時間も付与する。
    """
    if confirmed_df.empty:
        return confirmed_df.assign(
            実働時間=[], 深夜時間=[], 残業時間=[], 適用時給=[], 人件費=[])

    out = confirmed_df.copy()
    hours_list, night_list, ot_list, wage_list, cost_list = [], [], [], [], []
    for _, r in out.iterrows():
        wage = get_wage_for(r.get("氏名", ""), r.get("現場", ""), sites_df, staff_wages_df)
        try:
            hours, night_h, ot_h, cost = compute_shift_wage_precise(
                r["開始"], r["終了"], wage,
                night_rate=night_rate, ot_rate=ot_rate,
                daily_ot_threshold_hours=daily_ot_threshold_hours)
        except Exception:
            hours, night_h, ot_h, cost = 0.0, 0.0, 0.0, 0
        hours_list.append(hours)
        night_list.append(night_h)
        ot_list.append(ot_h)
        wage_list.append(wage)
        cost_list.append(cost)

    out["実働時間"] = hours_list
    out["深夜時間"] = night_list
    out["残業時間"] = ot_list
    out["適用時給"] = wage_list
    out["人件費"] = cost_list
    return out


def find_pool_wishes(wishes_df: pd.DataFrame, confirmed_df: pd.DataFrame,
                      reference_df: pd.DataFrame):
    """
    「未処理」の希望のうち、第1希望の現場・日付・時間帯が、**確定済みの
    人だけで**お手本の座席数を使い切ってしまっている（＝他の人が先に
    確定してしまい、後から本人が確定される見込みが薄い）ものを
    「プール要員」として抜き出す。

    ここでは、まだ埋まっていない「未処理」同士の競合は考慮しない
    （それはこれから確定できる余地があるため）。あくまで「確定済みの
    人によって、物理的に席が埋まっている」かどうかだけで判定する。

    戻り値：プール対象の希望だけを含むDataFrame（wishes_dfと同じ列構成）
    """
    if wishes_df.empty:
        return wishes_df.iloc[0:0]

    pending = wishes_df[wishes_df["ステータス"] == "未処理"]
    if pending.empty:
        return pending

    pool_idx = []
    for idx, r in pending.iterrows():
        site = r["第1希望現場"]
        date = r["希望日"]
        try:
            cells = expand_to_hour_bands(r["開始"], r["終了"])
        except Exception:
            continue
        hours_used = [h for h in cells if 0 <= h < 48]
        if not hours_used:
            continue

        ref_arr = reference_df_to_array(reference_df, site)
        c_heads, _, _, _ = build_hour_breakdown(confirmed_df, wishes_df, site, date)

        # 希望している時間帯のどこか1つでも、確定済みだけで座席数を
        # 使い切っていれば「席が無い」と判定する。
        is_full = any(
            round(ref_arr[h]) > 0 and c_heads[h] >= round(ref_arr[h])
            for h in hours_used)
        if is_full:
            pool_idx.append(idx)

    return pending.loc[pool_idx]


def suggest_alternative_slots(wish_row, sites_df: pd.DataFrame,
                               reference_df: pd.DataFrame, confirmed_df: pd.DataFrame,
                               wishes_df: pd.DataFrame):
    """
    プール要員1人ぶんについて、同じエリアの近場現場の中から、希望している
    日付・時間帯に空き（お手本の座席数 − 確定済み人数 > 0）がある候補を
    探す。空きが多い候補ほど上位に来るよう並べ替える。

    戻り値：[{"現場": str, "空き人数": float}, ...]（空きが多い順）
    """
    site = wish_row["第1希望現場"]
    date = wish_row["希望日"]
    try:
        cells = expand_to_hour_bands(wish_row["開始"], wish_row["終了"])
    except Exception:
        return []
    hours_used = [h for h in cells if 0 <= h < 48]
    if not hours_used:
        return []

    candidates = []
    for alt_site in nearby_sites(sites_df, site):
        ref_arr = reference_df_to_array(reference_df, alt_site)
        c_heads, _, _, _ = build_hour_breakdown(confirmed_df, wishes_df, alt_site, date)
        # 希望している時間帯すべてで、一番厳しい（空きが少ない）所を基準にする
        min_capacity = min(
            (round(ref_arr[h]) - c_heads[h]) for h in hours_used)
        if min_capacity > 0:
            candidates.append({"現場": alt_site, "空き人数": min_capacity})

    candidates.sort(key=lambda x: -x["空き人数"])
    return candidates


def build_seat_diff(reference_heads, confirmed_heads):
    """
    「お手本の座席数」と「実際に確定した人数」を、48時間帯ぶん
    比べて差分を出す。

    差分がプラス（お手本より増えた＝増員）なら赤、
    マイナス（お手本より減った＝欠員）なら青、で色分けする想定。

    戻り値：48要素のリスト。各要素は
      {"お手本": int, "確定": float, "差分": float}
    """
    result = []
    for h in range(48):
        ref = round(reference_heads[h])
        conf = confirmed_heads[h]
        result.append({
            "お手本": ref,
            "確定": conf,
            "差分": round(conf - ref, 2),
        })
    return result


def renormalize_reference_sites(reference_df: pd.DataFrame, aliases_df: pd.DataFrame):
    """
    すでに保存済みの「基準パターン」の現場名を、今のエイリアス対応表で
    正規化し直す。エイリアス登録が後から行われた場合、それより前に
    保存されたデータは古い（表記ゆれのままの）現場名で残ってしまって
    いるため、これをまとめて直す。

    同じ「正規化後の現場・時間帯・対象期間」が複数存在する場合は、
    作成日時が一番新しいものだけを残す（重複統合）。

    戻り値：(正規化後のDataFrame, 実際に名前が変わった件数)
    """
    if reference_df.empty:
        return reference_df, 0

    df = reference_df.copy()
    original_sites = df["現場"].copy()
    df["現場"] = df["現場"].apply(lambda n: normalize_site_name(n, aliases_df))
    changed = int((df["現場"] != original_sites).sum())

    if changed == 0:
        return reference_df, 0

    # 正規化後に重複する行（同じ現場・時間帯・対象期間）は、作成日時が
    # 新しい方だけを残す。
    df = df.sort_values("作成日時", ascending=False)
    df = df.drop_duplicates(subset=["現場", "時間帯", "対象期間"], keep="first")
    df = df.sort_values(["現場", "対象期間", "時間帯"])
    return df, changed


def extract_daily_hourly_matrix(long_df: pd.DataFrame, site: str):
    """
    縦長形式（現場名・日付・時間帯・頭数の4列）のデータから、指定した
    現場の「日付 × 時間帯」の表を作る。「お手本」のように月平均に
    均さず、日ごとの実際の値をそのまま残す。

    戻り値：pandas.DataFrame（index=日付、columns=0〜47の時間帯の整数、
    値=頭数）。該当データが無ければ空のDataFrameを返す。
    """
    if long_df.empty:
        return pd.DataFrame()
    sub = long_df[long_df["現場名"] == site]
    if sub.empty:
        return pd.DataFrame()

    sub = sub.copy()
    sub["時間帯"] = sub["時間帯"].astype(int)
    sub["頭数"] = pd.to_numeric(sub["頭数"], errors="coerce").fillna(0)

    pivot = sub.pivot_table(
        index="日付", columns="時間帯", values="頭数", aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(sorted(pivot.columns), axis=1)
    pivot = pivot.sort_index()
    return pivot


_WEEKDAY_ORDER = ["月", "火", "水", "木", "金", "土", "日"]
_WEEKDAY_MAP = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}


def build_weekday_hourly_matrix(long_df: pd.DataFrame, site: str):
    """
    縦長形式のデータから、指定した現場の「曜日 × 時間帯」の表を作る。
    特定の日付どうしを比べる（例：2025-02-01 と 2026-02-01）と、
    曜日が違うために単純比較しづらいことがあるため、同じ期間内の
    同じ曜日を平均してからそろえる。

    例えば「月曜日」の行は、その期間中に含まれるすべての月曜日の
    平均人数になる。

    戻り値：pandas.DataFrame（index=曜日（月〜日の順）、
    columns=0〜47の時間帯、値=平均頭数）
    """
    if long_df.empty:
        return pd.DataFrame()
    sub = long_df[long_df["現場名"] == site]
    if sub.empty:
        return pd.DataFrame()

    sub = sub.copy()
    sub["時間帯"] = sub["時間帯"].astype(int)
    sub["頭数"] = pd.to_numeric(sub["頭数"], errors="coerce").fillna(0)
    _parsed_dates = pd.to_datetime(sub["日付"], errors="coerce")
    sub["曜日"] = _parsed_dates.dt.dayofweek.map(_WEEKDAY_MAP)
    sub = sub.dropna(subset=["曜日"])
    if sub.empty:
        return pd.DataFrame()

    # 曜日ごとに「何日ぶんのデータがあるか」を数え、時間帯ごとの合計を
    # その日数で割って平均にする（例：月曜が期間中に4回あれば4で割る）。
    n_days_per_weekday = (
        sub[["曜日", "日付"]].drop_duplicates().groupby("曜日").size())

    pivot_sum = sub.pivot_table(
        index="曜日", columns="時間帯", values="頭数", aggfunc="sum", fill_value=0)
    pivot_avg = pivot_sum.div(n_days_per_weekday, axis=0)

    pivot_avg = pivot_avg.reindex(
        [w for w in _WEEKDAY_ORDER if w in pivot_avg.index])
    pivot_avg = pivot_avg.reindex(sorted(pivot_avg.columns), axis=1)
    return pivot_avg.round(2)


def _find_peaks(values, prominence_ratio=0.15):
    """
    数値の並びから、山（極大点）の位置を探す。前後の値より高く、かつ
    全体のばらつき（最大値と最小値の差）に対して一定以上目立つ
    （prominence_ratio）山だけを数える。小さなガタつきをノイズとして
    無視するための処理。頂上が横ばい（同じ値が連続するプラトー）の
    場合も、1つの山としてまとめて検出する。
    """
    if len(values) < 3:
        return []
    vmax, vmin = max(values), min(values)
    threshold = (vmax - vmin) * prominence_ratio
    if threshold <= 0:
        return []

    # 連続する同じ値を1つの「かたまり（ラン）」にまとめる。
    # runs は (値, 開始位置) のリスト。
    runs = []
    for i, v in enumerate(values):
        if runs and runs[-1][0] == v:
            continue
        runs.append((v, i))

    peaks = []
    for r in range(1, len(runs) - 1):
        v, idx = runs[r]
        prev_v = runs[r - 1][0]
        next_v = runs[r + 1][0]
        if v > prev_v and v > next_v:
            left_min = min(values[max(0, idx - 3):idx]) if idx > 0 else v
            right_bound = runs[r + 1][1]
            right_min = min(values[right_bound:right_bound + 3]) if right_bound < len(values) else v
            prominence = v - max(left_min, right_min)
            if prominence >= threshold:
                peaks.append(idx)
    return peaks


def classify_dice_shape(values):
    """
    47時間帯（の一部でもよい）の人数の並びから、ダイスの形を
    「山型」「ロート型」「二峰型」「変則型」の4つに自動分類する。
    前の特許用アプリで人手で分類していたものを、簡易的な
    山（極大点）の検出ロジックで再現したもの。

      山型　　… 山が1つで、幅が狭い（鋭いピーク）
      ロート型 … 山が1つだが、幅が広い（なだらかに続くピーク）
      二峰型　… 山が2つ
      変則型　… 山が0または3つ以上、あるいは判定不能なほどデータが少ない

    values … 時間帯順に並んだ人数のリスト（0の時間帯を含んでいてもよい）
    戻り値：(分類名, 説明文)
    """
    active = [v for v in values if v > 0]
    if len(active) < 3:
        return "変則型", "データが少なく、形を判定できません。"

    peaks = _find_peaks(values)
    n_peaks = len(peaks)

    if n_peaks == 0:
        return "変則型", "はっきりした山が見つかりませんでした（平坦、または単調な増減）。"
    if n_peaks >= 3:
        return "変則型", f"山が{n_peaks}つあり、複雑な形をしています。"
    if n_peaks == 2:
        return "二峰型", "山が2つあります（例：ランチとディナーのように、離れた時間帯にピークが分かれるタイプ）。"

    # 山が1つ → 山型かロート型かを、ピークの「幅」で判定する。
    peak_idx = peaks[0]
    peak_val = values[peak_idx]
    vmax, vmin = max(values), min(values)
    # ピークの8割以上の高さを保っている時間帯の数を、ピークの「幅」とする。
    plateau_threshold = vmin + (vmax - vmin) * 0.8
    plateau_width = sum(1 for v in values if v >= plateau_threshold)
    active_span = len(active)

    if active_span > 0 and (plateau_width / active_span) >= 0.35:
        return "ロート型", "山は1つですが、なだらかに長く続くタイプです。"
    return "山型", "山が1つで、鋭く盛り上がるタイプです。"
