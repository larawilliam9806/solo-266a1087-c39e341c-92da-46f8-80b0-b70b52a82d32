"""冲突诊断：在历史快照上寻找「分组目标互相打架」的证据。

业务背景
--------
单个区县/人数档的目标看似都能达到，但两个维度的合约束可能联合无解。
本模块实现一种 Farkas 式的充分证书：

* 从总体目标表中选出若干区县 ``A`` 与若干家庭人数档 ``B``（允许一边为空，
  名单里没有住户的组也允许入选）；
* 把 ``A`` 中各组的加权户数等式取正号、``B`` 中各组取负号后相加，得到

      sum_{i: d_i in A} w_i - sum_{i: s_i in B} w_i
          = sum_{d in A} t_d - sum_{s in B} t_s        (= 目标差值 D)

* 每户权重只能在初始权重的 ``0.5x ~ 2x`` 之间，因此左端存在可达上界：
  - 区县、人数档同时入选的住户系数为 0（两边相抵）；
  - 仅入选区县侧的住户系数为 +1，最大取 ``2 * init_weight``；
  - 仅入选人数档侧的住户系数为 -1，最小取 ``0.5 * init_weight``；
  - 两边都不选的住户不参与。

  即 ``UB(A, B) = 2*W(A-only) + 0.5*W(B-only)``。

* 若 ``D(A, B) > UB(A, B)``，超出量 ``excess`` 为正，则任何满足 0.5~2 倍
  权重约束的方案都不可能同时命中这些组目标——这就是冲突证据。
  所有选择下都没有正的超出量时，本方法未找到冲突证据。

搜索算法
--------
记 ``w_d`` / ``w_s`` 为区县 d / 人数档 s 内住户初始权重合计，``W(d, s)``
为同时落在 (d, s) 单元格内的权重合计。直接逐户展开可严格验证：

    E(A, B) = D(A, B) - UB(A, B)
            = sum_{d in A} (t_d - 2*w_d)
            + sum_{s in B} (-t_s - 0.5*w_s)
            + 2.5 * W(A, B)

固定枚举侧 ``U``（取分组数较少的维度，通常是人数档）后，另一侧每个组是否
入选只取决于其对 E 的边际贡献正负，因此枚举 ``U`` 的全部子集即可：
``O(2^|U| * |U| * |V|)``。人数档通常只有几个，2^|U| 极小；每个格雷码状态
都从所选 U 直接重算分数，避免增量翻转在零权重单元格上的状态残留。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import WeightingError
from .weighting import LOWER_FACTOR, UPPER_FACTOR

# 枚举侧分组数上限：2^20 次状态已属极端体量，实际人数档通常不超过个位数。
MAX_ENUMERATED_GROUPS = 20
# 超出量判定的相对浮点容差（按目标/权重量级缩放）。
EXCESS_REL_TOL = 1e-9

CROSS_FACTOR = UPPER_FACTOR + LOWER_FACTOR  # 2.0 + 0.5 = 2.5


@dataclass
class ConflictReport:
    conflict_found: bool
    selected_districts: list[str]
    selected_size_bands: list[str]
    target_sum_districts: float
    target_sum_size_bands: float
    target_difference: float
    reachable_upper_bound: float
    excess: float
    # 上界构成（均为初始权重口径），便于调查员复核相抵规则。
    both_sides_init_weight: float
    district_only_init_weight: float
    band_only_init_weight: float
    neither_side_init_weight: float
    district_group_count: int
    size_band_group_count: int
    combinations_scanned: int


def _target_maps(target_rows: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    district_targets: dict[str, float] = {}
    band_targets: dict[str, float] = {}
    for row in target_rows:
        if row["dimension"] == "district":
            district_targets[row["group_name"]] = float(row["target_count"])
        else:
            band_targets[row["group_name"]] = float(row["target_count"])
    return district_targets, band_targets


def find_conflict(
    households: list[dict], target_rows: list[dict]
) -> ConflictReport:
    """在给定名单与目标表上搜索超出量最大的分组选择。"""
    district_targets, band_targets = _target_maps(target_rows)
    districts = sorted(district_targets)
    bands = sorted(band_targets)
    d_index = {name: i for i, name in enumerate(districts)}
    b_index = {name: i for i, name in enumerate(bands)}

    # W[b][d]：同时落在人数档 b、区县 d 的住户初始权重合计。
    weight_bd = [[0.0] * len(districts) for _ in bands]
    band_total = [0.0] * len(bands)          # 每个人数档的全部权重 w_s
    district_total = [0.0] * len(districts)  # 每个区县的全部权重 w_d
    total_weight = 0.0
    for hh in households:
        w = float(hh["init_weight"])
        total_weight += w
        d = d_index.get(hh["district"])
        b = b_index.get(hh["size_band"])
        if d is not None:
            district_total[d] += w
        if b is not None:
            band_total[b] += w
        if d is not None and b is not None:
            weight_bd[b][d] += w

    # 枚举分组数较少的一侧 U，另一侧 V 按正贡献独立决定。两种方向的
    # 自身项符号与 V 侧基础分数不同，分别构造。
    enumerate_bands = len(bands) <= len(districts)
    if enumerate_bands:
        # U = B（人数档，枚举），V = A（区县，按正贡献决定）：
        #   E(B) = sum_{s in B}(-t_s - 0.5*w_s)
        #        + sum_d max(0, (t_d - 2*w_d) + 2.5*W(B, d))
        u_names, v_names = bands, districts
        u_targets = [band_targets[n] for n in u_names]
        v_targets = [district_targets[n] for n in v_names]
        w_uv = weight_bd  # [u=band][v=district]
        row_total_u, col_total_v = band_total, district_total
        u_sign, u_self_factor = -1.0, LOWER_FACTOR
        v_base = [
            v_targets[v] - UPPER_FACTOR * col_total_v[v]
            for v in range(len(v_names))
        ]
    else:
        # U = A（区县，枚举），V = B（人数档，按正贡献决定）：
        #   E(A) = sum_{d in A}(t_d - 2*w_d)
        #        + sum_s max(0, (-t_s - 0.5*w_s) + 2.5*W(A, s))
        u_names, v_names = districts, bands
        u_targets = [district_targets[n] for n in u_names]
        v_targets = [band_targets[n] for n in v_names]
        # 转置：W[u=district][v=band]
        w_uv = [
            [weight_bd[b][d] for b in range(len(bands))]
            for d in range(len(districts))
        ]
        row_total_u, col_total_v = district_total, band_total
        u_sign, u_self_factor = 1.0, UPPER_FACTOR
        v_base = [
            -v_targets[v] - LOWER_FACTOR * col_total_v[v]
            for v in range(len(v_names))
        ]

    n_u, n_v = len(u_names), len(v_names)
    if n_u > MAX_ENUMERATED_GROUPS:
        raise WeightingError(
            "枚举侧分组数超过冲突诊断搜索上限",
            {
                "enumerated_group_count": n_u,
                "max_groups": MAX_ENUMERATED_GROUPS,
            },
        )

    scale = max(
        [1.0, total_weight]
        + [abs(t) for t in u_targets]
        + [abs(t) for t in v_targets]
    )
    tol = EXCESS_REL_TOL * scale

    # 空选择（U 为空）下的初始解：V 侧只取基础分数为正的组。
    # 后续格雷码状态只有严格更优（超过容差）才替换，保证并列时取最简
    # 选择，且结果确定。
    best_excess = sum(max(0.0, s) for s in v_base)
    best_u_names: list[str] = []
    best_v_names = [v_names[v] for v in range(n_v) if v_base[v] > 0.0]
    combinations = 1

    selected_u = [False] * n_u
    for step in range(1, 1 << n_u):
        gray_bit = step & -step
        u = gray_bit.bit_length() - 1
        selected_u[u] = not selected_u[u]
        active_u = [i for i in range(n_u) if selected_u[i]]

        # 每个状态直接从所选 U 重算分数，避免增量翻转在零权重单元格上
        # 残留状态；分组规模下 O(2^|U|*|U|*|V|) 的开销可忽略。
        constant = 0.0
        score = list(v_base)
        for ui in active_u:
            # 自身项：区县为 (t_d - 2*w_d)，人数档为 (-t_s - 0.5*w_s)。
            # 目标项符号由 u_sign 决定；权重项恒为 -f*w（上界中该侧自身
            # 住户的系数与目标符号相反），不能整体乘 u_sign。
            constant += (
                u_sign * u_targets[ui]
                - u_self_factor * row_total_u[ui]
            )
            row = w_uv[ui]
            for v in range(n_v):
                score[v] += CROSS_FACTOR * row[v]

        chosen_v = [v for v in range(n_v) if score[v] > 0.0]
        excess = constant + sum(score[v] for v in chosen_v)
        combinations += 1

        if excess > best_excess + tol:
            best_excess = excess
            best_u_names = [u_names[ui] for ui in active_u]
            best_v_names = [v_names[v] for v in chosen_v]

    if enumerate_bands:
        chosen_bands = best_u_names
        chosen_districts = best_v_names
    else:
        chosen_districts = best_u_names
        chosen_bands = best_v_names

    chosen_districts_set = set(chosen_districts)
    chosen_bands_set = set(chosen_bands)

    target_sum_d = sum(district_targets[d] for d in chosen_districts)
    target_sum_b = sum(band_targets[b] for b in chosen_bands)
    target_difference = target_sum_d - target_sum_b

    # 直接逐户复核上界构成，避免搜索过程中的浮点累积进入返回值。
    both_w = d_only_w = b_only_w = neither_w = 0.0
    for hh in households:
        w = float(hh["init_weight"])
        in_d = hh["district"] in chosen_districts_set
        in_b = hh["size_band"] in chosen_bands_set
        if in_d and in_b:
            both_w += w
        elif in_d:
            d_only_w += w
        elif in_b:
            b_only_w += w
        else:
            neither_w += w

    upper_bound = UPPER_FACTOR * d_only_w + LOWER_FACTOR * b_only_w
    excess = target_difference - upper_bound
    conflict_found = excess > tol

    if not conflict_found:
        # 无正超出量时不报告任何分组：空选择即超出量最大（为 0）的选择。
        return ConflictReport(
            conflict_found=False,
            selected_districts=[],
            selected_size_bands=[],
            target_sum_districts=0.0,
            target_sum_size_bands=0.0,
            target_difference=0.0,
            reachable_upper_bound=0.0,
            excess=0.0,
            both_sides_init_weight=0.0,
            district_only_init_weight=0.0,
            band_only_init_weight=0.0,
            neither_side_init_weight=total_weight,
            district_group_count=len(districts),
            size_band_group_count=len(bands),
            combinations_scanned=combinations,
        )

    return ConflictReport(
        conflict_found=True,
        selected_districts=chosen_districts,
        selected_size_bands=chosen_bands,
        target_sum_districts=target_sum_d,
        target_sum_size_bands=target_sum_b,
        target_difference=target_difference,
        reachable_upper_bound=upper_bound,
        excess=excess,
        both_sides_init_weight=both_w,
        district_only_init_weight=d_only_w,
        band_only_init_weight=b_only_w,
        neither_side_init_weight=neither_w,
        district_group_count=len(districts),
        size_band_group_count=len(bands),
        combinations_scanned=combinations,
    )
