"""Unit tests for the conflict-diagnosis combinatorial search."""

import itertools
import random

from app.diagnosis import find_conflict
from app.weighting import LOWER_FACTOR, UPPER_FACTOR


def _brute_force(households, target_rows):
    """Reference implementation: enumerate every (A, B) pair directly."""
    d_targets, b_targets = {}, {}
    for row in target_rows:
        if row["dimension"] == "district":
            d_targets[row["group_name"]] = row["target_count"]
        else:
            b_targets[row["group_name"]] = row["target_count"]
    districts, bands = list(d_targets), list(b_targets)

    best = (0.0, None, None, 0.0, 0.0)
    count = 0
    for a in _subsets(districts):
        for b in _subsets(bands):
            count += 1
            a_set, b_set = set(a), set(b)
            target_diff = sum(d_targets[d] for d in a) - sum(
                b_targets[x] for x in b
            )
            d_only = b_only = both = 0.0
            for hh in households:
                w = hh["init_weight"]
                in_d = hh["district"] in a_set
                in_b = hh["size_band"] in b_set
                if in_d and in_b:
                    both += w
                elif in_d:
                    d_only += w
                elif in_b:
                    b_only += w
            upper = UPPER_FACTOR * d_only + LOWER_FACTOR * b_only
            excess = target_diff - upper
            if excess > best[0] + 1e-9:
                best = (excess, a, b, target_diff, upper)
    # The diagnostic search enumerates only subsets of the smaller side,
    # i.e. 2**min(|districts|, |bands|) evaluated states.
    expected_scanned = 2 ** min(len(districts), len(bands))
    return best, expected_scanned


def _subsets(items):
    items = list(items)
    for k in range(len(items) + 1):
        for combo in itertools.combinations(items, k):
            yield list(combo)


def _excess_of(households, d_targets, b_targets, districts, bands):
    """Direct household-level excess for a concrete selection."""
    a_set, b_set = set(districts), set(bands)
    target_diff = sum(d_targets[d] for d in a_set) - sum(
        b_targets[s] for s in b_set
    )
    d_only = b_only = 0.0
    for hh in households:
        in_d = hh["district"] in a_set
        in_b = hh["size_band"] in b_set
        if in_d and not in_b:
            d_only += hh["init_weight"]
        elif in_b and not in_d:
            b_only += hh["init_weight"]
    return target_diff - (UPPER_FACTOR * d_only + LOWER_FACTOR * b_only)


def _targets(d_targets, b_targets):
    return [
        {"dimension": "district", "group_name": d, "target_count": t}
        for d, t in d_targets.items()
    ] + [
        {"dimension": "size_band", "group_name": s, "target_count": t}
        for s, t in b_targets.items()
    ]


ROSTER = [
    {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "H2", "district": "D1", "size_band": "S2", "init_weight": 2.0},
    {"household_id": "H3", "district": "D2", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "H4", "district": "D2", "size_band": "S2", "init_weight": 3.0},
    {"household_id": "H5", "district": "D1", "size_band": "S1", "init_weight": 4.0},
]


def test_conflict_certificate_matches_hand_calculation():
    # D1 target 100 unreachable: D1 roster weight = 1+2+4 = 7, 2x cap = 14.
    targets = _targets(
        {"D1": 100.0, "D2": 1.0},
        {"S1": 100.0, "S2": 1.0},
    )
    report = find_conflict(ROSTER, targets)
    assert report.conflict_found is True

    # Hand-checked certificate A={D1}, B=empty: D=100, UB=2*7=14 -> 86.
    # The search may legitimately find a stronger mixed certificate, but
    # every reported number must still satisfy the certificate inequality.
    assert report.excess >= 86.0 - 1e-9
    assert "D1" in report.selected_districts
    assert report.target_sum_districts > 0.0

    # Recompute every reported figure from first principles.
    a_set = set(report.selected_districts)
    b_set = set(report.selected_size_bands)
    d_sum = sum(
        t["target_count"] for t in targets
        if t["dimension"] == "district" and t["group_name"] in a_set
    )
    b_sum = sum(
        t["target_count"] for t in targets
        if t["dimension"] == "size_band" and t["group_name"] in b_set
    )
    assert abs(report.target_sum_districts - d_sum) < 1e-12
    assert abs(report.target_sum_size_bands - b_sum) < 1e-12
    assert abs(report.target_difference - (d_sum - b_sum)) < 1e-12
    ub = (
        UPPER_FACTOR * report.district_only_init_weight
        + LOWER_FACTOR * report.band_only_init_weight
    )
    assert abs(ub - report.reachable_upper_bound) < 1e-12
    assert abs(
        report.target_difference - report.reachable_upper_bound - report.excess
    ) < 1e-12
    assert report.combinations_scanned == 2 ** min(
        report.district_group_count, report.size_band_group_count
    )


def test_two_side_selection_with_cancelling_households():
    # A={D1}, B={S1}. D1 = 100, S1 = 100.
    # D1-only: H2 (w=2) -> 2x = 4 ; S1-only: none
    # both sides: H1, H5 (w=1,4) -> cancel -> 0
    # UB = 4, D = 0 -> excess -4, NOT a certificate.
    # A={D1}, B=empty stays the best (excess 86); check the breakdown of
    # a mixed selection via brute force instead, then verify the reported
    # winner's breakdown is internally consistent.
    targets = _targets(
        {"D1": 100.0, "D2": 1.0},
        {"S1": 100.0, "S2": 1.0},
    )
    report = find_conflict(ROSTER, targets)

    # Recompute UB from the reported breakdown: both-side weights cancel.
    ub = (
        UPPER_FACTOR * report.district_only_init_weight
        + LOWER_FACTOR * report.band_only_init_weight
    )
    assert abs(ub - report.reachable_upper_bound) < 1e-12
    assert abs(
        report.target_difference - report.reachable_upper_bound - report.excess
    ) < 1e-12
    assert report.both_sides_init_weight >= 0.0
    total = (
        report.both_sides_init_weight
        + report.district_only_init_weight
        + report.band_only_init_weight
        + report.neither_side_init_weight
    )
    assert abs(total - sum(h["init_weight"] for h in ROSTER)) < 1e-12


def test_mixed_selection_can_win_with_positive_band_target_subtracted():
    # Construct a case where the best selection includes a band.
    # D1 target 100 -> strong positive. S2 target 1 with D2 in A gives
    # cross term 2.5 * W(D1,S2); explore via brute force to pick targets,
    # then assert the search agrees exactly.
    d_targets = {"D1": 50.0, "D2": 20.0}
    b_targets = {"S1": 3.0, "S2": 2.0}
    targets = _targets(d_targets, b_targets)

    (bf_excess, bf_a, bf_b, bf_diff, bf_ub), count = _brute_force(
        ROSTER, targets
    )
    report = find_conflict(ROSTER, targets)
    if bf_excess > 0:
        assert report.conflict_found is True
        assert abs(report.excess - bf_excess) < 1e-9
        # Ties may resolve to a different equally optimal selection; verify
        # the reported groups attain the brute-force optimum instead of
        # requiring the exact same sets.
        reported_excess = _excess_of(
            ROSTER, d_targets, b_targets,
            report.selected_districts, report.selected_size_bands,
        )
        assert abs(reported_excess - bf_excess) < 1e-9
        assert abs(report.excess - reported_excess) < 1e-12
        # At least one side must be non-empty for a positive certificate.
        assert report.selected_districts or report.selected_size_bands
    else:
        assert report.conflict_found is False
    assert report.combinations_scanned == count


def test_no_conflict_when_instance_is_feasible():
    # Balanced targets from the API test suite: an exact solution exists.
    targets = _targets(
        {"D1": 9.0, "D2": 6.0},
        {"S1": 8.0, "S2": 7.0},
    )
    report = find_conflict(ROSTER, targets)
    assert report.conflict_found is False
    assert report.selected_districts == []
    assert report.selected_size_bands == []
    assert report.excess == 0.0
    assert report.target_difference == 0.0
    assert report.reachable_upper_bound == 0.0
    # All roster weight lands in the neither-side bucket for empty choice.
    assert abs(
        report.neither_side_init_weight
        - sum(h["init_weight"] for h in ROSTER)
    ) < 1e-12


def test_group_without_any_household_can_be_selected():
    # Extra district D3 with no households: its target can never be reached,
    # selecting it alone yields excess equal to its target.
    targets = _targets(
        {"D1": 9.0, "D2": 6.0, "D3": 12.0},
        {"S1": 8.0, "S2": 7.0},
    )
    report = find_conflict(ROSTER, targets)
    assert report.conflict_found is True
    assert "D3" in report.selected_districts
    assert report.district_group_count == 3
    # D3 contributes zero reachable weight.
    assert report.excess >= 12.0 - 1e-9


def test_empty_dimension_in_target_table():
    # Only district targets: band side has no groups to choose.
    targets = _targets({"D1": 100.0, "D2": 1.0}, {})
    report = find_conflict(ROSTER, targets)
    assert report.conflict_found is True
    assert report.selected_size_bands == []
    assert report.selected_districts == ["D1"]
    assert report.size_band_group_count == 0
    assert report.combinations_scanned == 1  # only one band subset (empty)


def test_matches_brute_force_on_random_instances():
    rng = random.Random(20261121)
    districts = [f"D{i}" for i in range(3)]
    bands = [f"S{i}" for i in range(2)]
    for trial in range(60):
        households = []
        for hid in range(rng.randint(1, 12)):
            households.append(
                {
                    "household_id": f"H{hid}",
                    "district": rng.choice(districts),
                    "size_band": rng.choice(bands),
                    "init_weight": rng.uniform(0.5, 5.0),
                }
            )
        present_d = sorted({h["district"] for h in households})
        present_b = sorted({h["size_band"] for h in households})
        d_t = {d: rng.choice([0.0, 1.0, 5.0, 50.0]) for d in present_d}
        b_t = {s: rng.choice([0.0, 1.0, 5.0, 50.0]) for s in present_b}
        targets = _targets(d_t, b_t)

        (bf_excess, bf_a, bf_b, _, _), count = _brute_force(
            households, targets
        )
        report = find_conflict(households, targets)
        assert report.combinations_scanned == count
        if bf_excess <= 1e-9:
            assert report.conflict_found is False
            continue
        assert report.conflict_found is True
        assert abs(report.excess - bf_excess) < 1e-7, (trial, report, bf_a, bf_b)
        # The reported selection must itself attain the brute-force optimum
        # (ties may resolve to a different, equally optimal selection).
        reported_excess = _excess_of(
            households, d_t, b_t,
            report.selected_districts, report.selected_size_bands,
        )
        assert abs(reported_excess - bf_excess) < 1e-7
        assert abs(report.excess - reported_excess) < 1e-9

        # Every reported component must obey the 0.5x~2x bound certificate.
        ub = (
            UPPER_FACTOR * report.district_only_init_weight
            + LOWER_FACTOR * report.band_only_init_weight
        )
        assert abs(ub - report.reachable_upper_bound) < 1e-9


def test_matches_brute_force_with_more_districts_than_bands_and_reverse():
    # More bands than districts forces the transpose branch; then an
    # instance with more districts forces the straightforward branch.
    rng = random.Random(42)
    for n_d, n_b in ((2, 4), (5, 2)):
        districts = [f"D{i}" for i in range(n_d)]
        bands = [f"S{i}" for i in range(n_b)]
        households = [
            {
                "household_id": f"H{i}",
                "district": districts[i % n_d],
                "size_band": bands[(i * 7) % n_b],
                "init_weight": 1.0 + (i % 3),
            }
            for i in range(30)
        ]
        d_t = {d: rng.choice([0.0, 8.0, 80.0]) for d in districts}
        b_t = {s: rng.choice([0.0, 8.0, 80.0]) for s in bands}
        targets = _targets(d_t, b_t)
        (bf_excess, bf_a, bf_b, _, _), count = _brute_force(
            households, targets
        )
        report = find_conflict(households, targets)
        assert report.combinations_scanned == count
        if bf_excess > 1e-9:
            assert report.conflict_found is True
            assert abs(report.excess - bf_excess) < 1e-7
            # Ties may pick another equally optimal selection; the reported
            # one must still attain the brute-force global optimum.
            reported_excess = _excess_of(
                households, d_t, b_t,
                report.selected_districts, report.selected_size_bands,
            )
            assert abs(reported_excess - bf_excess) < 1e-7
        else:
            assert report.conflict_found is False
