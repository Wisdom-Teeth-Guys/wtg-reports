"""Unit tests for the scoring engine. Run with: python3 -m route_builder.tests.test_scoring"""
from datetime import date, timedelta

from ..config import OUTCOME_CLOSED_PERMANENT
from ..scoring import OrgRecord, compute_overdue_factor, score_org, select_top_n


def test_vip_overdue_20d():
    today = date(2026, 5, 11)
    last = today - timedelta(days=20)
    org = OrgRecord(hs_id="1", tier="VIP", last_visit_date=last)
    score_org(org, today)
    # 40 + min(20/14*20, 20) = 40 + 20 = 60 (overdue capped at 20)
    assert org.score == 60, f"got {org.score}"
    assert "VIP" in org.visit_reason
    assert "Overdue 20d" in org.visit_reason
    print(f"✓ VIP overdue 20d → score={org.score}, reason='{org.visit_reason}'")


def test_t3_with_falloff():
    today = date(2026, 5, 11)
    last = today - timedelta(days=30)
    org = OrgRecord(hs_id="2", tier="T3", last_visit_date=last, falloff_flag=True)
    score_org(org, today)
    # 10 (T3) + (30/50*20)=12 + 30 (falloff) = 52
    assert org.score == 52, f"got {org.score}"
    assert "T3" in org.visit_reason
    assert "Falloff" in org.visit_reason
    print(f"✓ T3 + falloff → score={org.score}, reason='{org.visit_reason}'")


def test_new_org_never_visited():
    today = date(2026, 5, 11)
    org = OrgRecord(hs_id="3", tier="T2", last_visit_date=None,
                    create_date=today - timedelta(days=30))
    score_org(org, today)
    # 20 (T2) + 20 (never visited overdue cap) + 15 (new) = 55
    assert org.score == 55, f"got {org.score}"
    print(f"✓ New T2 org never visited → score={org.score}, reason='{org.visit_reason}'")


def test_score_caps_at_100():
    today = date(2026, 5, 11)
    org = OrgRecord(
        hs_id="4", tier="VIP",
        last_visit_date=today - timedelta(days=200),
        falloff_flag=True, dormant_flag=True,
        create_date=today - timedelta(days=10),
        consecutive_closed_count=5,
    )
    score_org(org, today)
    # 40 + 20 (overdue) + 30 (falloff) + 20 (dormant) + 15 (new) + 10 (repeat) = 135 → capped 100
    assert org.score == 100, f"got {org.score}"
    print(f"✓ Score caps at 100 → score={org.score}, reason='{org.visit_reason}'")


def test_repeat_miss_boost():
    today = date(2026, 5, 11)
    org = OrgRecord(hs_id="5", tier="T2",
                    last_visit_date=today - timedelta(days=14),
                    consecutive_closed_count=3,
                    last_visit_outcome="closed_retry")
    score_org(org, today)
    # 20 (T2) + (14/30*20)=9 + 10 (repeat miss) = 39
    assert org.score == 39, f"got {org.score}"
    assert "Closed 3x in a row" in org.visit_reason
    print(f"✓ Repeat-miss boost → score={org.score}, reason='{org.visit_reason}'")


def test_permanent_closure_suppressed():
    today = date(2026, 5, 11)
    org = OrgRecord(hs_id="6", tier="VIP",
                    last_visit_date=today - timedelta(days=5),
                    last_visit_outcome=OUTCOME_CLOSED_PERMANENT)
    score_org(org, today)
    assert org.score == 0
    assert "SUPPRESSED" in org.visit_reason
    print(f"✓ Permanently closed VIP → score={org.score}, reason='{org.visit_reason}'")


def test_zero_tier_only_visited_recently():
    today = date(2026, 5, 11)
    org = OrgRecord(hs_id="7", tier="", last_visit_date=today - timedelta(days=3))
    score_org(org, today)
    # 0 tier + 0 overdue (3/9999 ~ 0) + no boosts = 0
    assert org.score == 0
    print(f"✓ Zero-tier recently visited → score={org.score}, reason='{org.visit_reason}'")


def test_select_top_n_filters_permanent():
    today = date(2026, 5, 11)
    orgs = [
        OrgRecord(hs_id="a", tier="VIP", last_visit_date=today - timedelta(days=30)),
        OrgRecord(hs_id="b", tier="VIP", last_visit_date=today - timedelta(days=5),
                  last_visit_outcome=OUTCOME_CLOSED_PERMANENT),
        OrgRecord(hs_id="c", tier="T1", last_visit_date=today - timedelta(days=20)),
    ]
    top = select_top_n(orgs, 5, today)
    ids = [o.hs_id for o in top]
    assert "b" not in ids, f"Permanently closed should be filtered: {ids}"
    assert ids[0] == "a", f"VIP overdue should rank first: {ids}"
    print(f"✓ select_top_n filters permanent closures → got {ids}")


def test_select_top_n_force_include():
    today = date(2026, 5, 11)
    orgs = [
        OrgRecord(hs_id="x", tier="VIP", last_visit_date=today - timedelta(days=30)),
        OrgRecord(hs_id="y", tier="T1", last_visit_date=today - timedelta(days=10)),
        OrgRecord(hs_id="z", tier="T3", last_visit_date=today - timedelta(days=1)),
    ]
    top = select_top_n(orgs, 3, today, force_include_ids={"z"})
    assert top[0].hs_id == "z", f"force-include should be first: {[o.hs_id for o in top]}"
    print(f"✓ force-include puts low-tier first → {[o.hs_id for o in top]}")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} scoring tests...\n")
    for t in tests:
        t()
    print(f"\nAll {len(tests)} tests passed.")
