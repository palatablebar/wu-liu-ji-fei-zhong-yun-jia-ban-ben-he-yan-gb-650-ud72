"""M8 结算裁决：多规则命中可解释、歧义不发布、运价更新失效、重算不重复出票。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fixtures.expected import AMOUNT_TOTAL

from app import store
from app.errors import DomainError
from app.modules.m1_waybill import service as waybills
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import service as weights
from app.modules.m5_charges import service as charges
from app.modules.m7_export import invoice_out
from app.modules.m8_settlement import billing, engine, peak as peak_mod
from app.modules.m8_settlement import service as settlement


@pytest.fixture()
def clean_pipeline():
    """函数级夹具：每个用例从干净运价与全新 run 开始，互不污染共享库。"""
    rates.seed_sample(force=True)
    ingest = waybills.import_sample()
    run_id = ingest["run_id"]
    weights.compute_weights(run_id)
    charges.compute_charges(run_id)
    settlement.settle(run_id)
    return {"run_id": run_id}


# --- 纯函数层：匹配、分组裁决、旺季金额 ------------------------------------

def test_heterogeneous_rules_coexist_in_same_window():
    """底价与旺季加价允许同一时段并存，各自独立命中，不算冲突。"""
    cards = [{
        "version_id": "BASE-1", "effective_from": "2026-04-01T00:00:00Z",
        "effective_to": "2026-07-01T00:00:00Z",
        "first_price": "13.00", "step_price": "2.60", "min_charge": "19.00",
        "fuel_pct": "0.085", "remote_fee": "0", "insurance_pct": "0.003",
    }]
    rules = [{
        "rule_id": "PEAK-1", "effective_from": "2026-04-01T00:00:00Z",
        "effective_to": "2026-07-01T00:00:00Z",
        "charge_type": "percent", "charge_value": "0.10",
    }]
    pickup = rateversion.parse_utc("2026-04-05T00:00:00Z")
    matched = engine.match_rules(cards, rules, pickup)
    assert len(matched[engine.BASE_KIND]) == 1
    assert len(matched[engine.PEAK_KIND]) == 1
    card, code = engine.resolve_kind(engine.BASE_KIND, matched[engine.BASE_KIND])
    assert card["version_id"] == "BASE-1" and code is None
    rule, code = engine.resolve_kind(engine.PEAK_KIND, matched[engine.PEAK_KIND])
    assert rule["rule_id"] == "PEAK-1" and code is None


def test_same_kind_double_hit_is_ambiguous():
    """同 kind 两条规则同时命中 → PEAK_CONFLICT，无法自行排除歧义。"""
    rules = [
        {"rule_id": "P1", "effective_from": "2026-04-01T00:00:00Z",
         "effective_to": "2026-07-01T00:00:00Z", "charge_type": "percent",
         "charge_value": "0.10"},
        {"rule_id": "P2", "effective_from": "2026-04-10T00:00:00Z",
         "effective_to": "2026-06-15T00:00:00Z", "charge_type": "fixed",
         "charge_value": "8.00"},
    ]
    pickup = rateversion.parse_utc("2026-04-15T00:00:00Z")
    hits = rateversion.active_at(rules, pickup)
    chosen, code = engine.resolve_kind(engine.PEAK_KIND, hits)
    assert chosen is None and code == "PEAK_CONFLICT"


def test_peak_amount_percent_and_fixed():
    subtotal = Decimal("33.80")
    percent = {"charge_type": "percent", "charge_value": "0.10"}
    fixed = {"charge_type": "fixed", "charge_value": "5.00"}
    assert engine.peak_amount(subtotal, percent) == Decimal("3.38")
    assert engine.peak_amount(subtotal, fixed) == Decimal("5.00")
    assert engine.peak_amount(subtotal, None) == Decimal("0.00")


def test_decision_evidence_covers_every_rule():
    """命中规则、未采用规则、窗口外规则都要在证据里对得上。"""
    cards = [
        {"version_id": "B1", "effective_from": "2026-01-01T00:00:00Z",
         "effective_to": "2026-04-01T00:00:00Z", "first_price": "12",
         "step_price": "2.4", "min_charge": "18", "fuel_pct": "0.08",
         "remote_fee": "0", "insurance_pct": "0.003"},
        {"version_id": "B2", "effective_from": "2026-04-01T00:00:00Z",
         "effective_to": "2026-07-01T00:00:00Z", "first_price": "13",
         "step_price": "2.6", "min_charge": "19", "fuel_pct": "0.085",
         "remote_fee": "0", "insurance_pct": "0.003"},
    ]
    rules = [{"rule_id": "P1", "effective_from": "2026-04-01T00:00:00Z",
              "effective_to": "2026-07-01T00:00:00Z", "charge_type": "fixed",
              "charge_value": "5.00"}]
    pickup = rateversion.parse_utc("2026-04-05T00:00:00Z")

    def builder(card, peak):
        return {"base_amount": "13.00"}, Decimal("18.00"), []

    verdict = engine.decide(cards, rules, pickup, builder)
    statuses = {(e["rule_kind"], e["rule_id"]): (e["status"], e["reason_code"])
                for e in verdict["evidence"]}
    assert statuses[("base", "B2")] == ("adopted", "")
    assert statuses[("base", "B1")] == ("not_adopted", "RULE_OUT_OF_WINDOW")
    assert statuses[("peak", "P1")] == ("adopted", "")
    assert verdict["decision_status"] == engine.FINAL


# --- 编排层：整批裁决 -------------------------------------------------------

def test_settle_marks_every_billable_waybill_final(clean_pipeline):
    summary = settlement.settle(clean_pipeline["run_id"])
    assert summary["decided"] == 15
    assert summary["final"] == 15
    assert summary["pending"] == 0
    assert summary["stale"] == 0
    assert summary["amount_total"] == AMOUNT_TOTAL


def test_decision_evidence_links_adopted_and_rejected_rules(clean_pipeline):
    settlement.settle(clean_pipeline["run_id"])
    wb1001 = next(d for d in settlement.decisions(clean_pipeline["run_id"])
                  if d["waybill_no"] == "WB-1001")
    by_id = {e["rule_id"]: e for e in wb1001["evidence"]}
    assert by_id["SF-standard-Z1-v2"]["status"] == "adopted"
    assert by_id["SF-standard-Z1-v1"]["reason_code"] == "RULE_OUT_OF_WINDOW"
    assert by_id["PK-SF-std-Z1-q2"]["status"] == "adopted"
    assert "PEAK_APPLIED" in wb1001["codes"]
    # 无旺季规则的票（WB-1004 走 YT economy Z2）只有基础规则证据
    wb1004 = next(d for d in settlement.decisions(clean_pipeline["run_id"])
                  if d["waybill_no"] == "WB-1004")
    assert all(e["rule_kind"] != engine.PEAK_KIND for e in wb1004["evidence"])


def test_page_amount_and_billing_basis_match(clean_pipeline):
    """页面金额（charge_lines）与账单依据（decisions）必须是同一个数。"""
    charges.compute_charges(clean_pipeline["run_id"])
    settlement.settle(clean_pipeline["run_id"])
    charge_map = {r["waybill_no"]: r for r in store.charge_lines(clean_pipeline["run_id"])}
    for decision in settlement.decisions(clean_pipeline["run_id"]):
        assert decision["total_amount"] == charge_map[decision["waybill_no"]]["total_amount"]


# --- 歧义挂起与不对外发布 ---------------------------------------------------

def _inject_conflicting_peak_rules(clean_pipeline) -> list[str]:
    """绕过装载通道直接写入同类相交规则，验证裁决层是最后一道防线。"""
    raw = pipeline_dir().joinpath("peak_rules_conflict.csv").read_text()
    rules, problems, _version = peak_mod.parse_peak_rules(raw)
    assert not problems
    store.replace_peak_rules(rules)
    return rules


def pipeline_dir():
    from app import config
    return config.SAMPLE_DIR


def test_ambiguous_rules_block_publish_and_explain_candidates(clean_pipeline):
    run_id = clean_pipeline["run_id"]
    settlement.settle(run_id)
    _inject_conflicting_peak_rules(clean_pipeline)
    summary = settlement.settle(run_id)
    assert summary["pending"] == 2
    assert summary["publishable"] == 13
    pending = settlement.pending(run_id)
    assert {d["waybill_no"] for d in pending} == {"WB-1017", "WB-1018"}
    for decision in pending:
        assert decision["decision_status"] == engine.PENDING
        assert decision["bill_no"] == ""
        assert "PEAK_CONFLICT" in decision["codes"]
        assert "DECISION_PENDING" in decision["codes"]
        candidates = [e for e in decision["evidence"] if e["status"] == "candidate"]
        assert len(candidates) == 2
        assert {e["rule_id"] for e in candidates} == {
            "PK-SF-std-Z1-q2", "PK-SF-std-Z1-extra"}
        # 基础规则不受另一 kind 歧义牵连，仍标记为 adopted
        base = next(e for e in decision["evidence"] if e["rule_kind"] == engine.BASE_KIND
                    and e["status"] == "adopted")
        assert base["rule_id"] == "SF-standard-Z1-v2"
    # 挂起票绝不进对外发票
    invoice = {row["waybill_no"] for row in invoice_out.invoice_rows(run_id)}
    assert {"WB-1017", "WB-1018"}.isdisjoint(invoice)
    assert len(invoice) == 13


def test_manual_resolution_only_allows_listed_candidates(clean_pipeline):
    run_id = clean_pipeline["run_id"]
    settlement.settle(run_id)
    _inject_conflicting_peak_rules(clean_pipeline)
    settlement.settle(run_id)
    target = settlement.pending(run_id)[0]
    with pytest.raises(DomainError) as exc:
        settlement.resolve_pending(run_id, target["waybill_no"], engine.PEAK_KIND, "NOT-A-RULE")
    assert exc.value.code == "RULE_NOT_CANDIDATE"
    with pytest.raises(DomainError) as exc:
        settlement.resolve_pending(run_id, target["waybill_no"], "bogus", "X")
    assert exc.value.code == "BAD_RULE_KIND"


def test_manual_resolution_publishes_with_audit_trail(clean_pipeline):
    run_id = clean_pipeline["run_id"]
    settlement.settle(run_id)
    _inject_conflicting_peak_rules(clean_pipeline)
    settlement.settle(run_id)
    target = next(d for d in settlement.pending(run_id)
                  if d["waybill_no"] == "WB-1017")
    result = settlement.resolve_pending(
        run_id, "WB-1017", engine.PEAK_KIND, "PK-SF-std-Z1-extra")
    decision = result["decision"]
    assert decision["decision_status"] == engine.FINAL
    assert decision["peak_rule_id"] == "PK-SF-std-Z1-extra"
    assert "MANUAL_RESOLVED" in decision["codes"]
    assert decision["bill_no"].startswith("BILL-")
    by_id = {e["rule_id"]: e for e in decision["evidence"]}
    assert by_id["PK-SF-std-Z1-extra"]["status"] == "adopted"
    assert by_id["PK-SF-std-Z1-q2"]["status"] == "not_adopted"
    # 人工裁决后的票立即进入发票
    invoice = {row["waybill_no"] for row in invoice_out.invoice_rows(run_id)}
    assert "WB-1017" in invoice


# --- 运价更新失效与重算 -----------------------------------------------------

def test_rate_update_voids_bills_and_marks_decisions_stale(clean_pipeline):
    run_id = clean_pipeline["run_id"]
    settlement.settle(run_id)
    before = {b["bill_no"] for b in store.bills(run_id) if b["status"] == "issued"}
    assert len(before) == 15
    digest_before = rates.rate_digest()

    _bump_rate_set()
    assert rates.rate_digest() != digest_before
    # 未重算前：全部 stale、全部 void、发票为空
    assert all(r["decision_status"] == engine.STALE for r in store.decisions(run_id))
    assert all("RESULT_STALE" in r["codes"] for r in store.decisions(run_id))
    assert all(b["status"] == "void" for b in store.bills(run_id))
    assert invoice_out.invoice_rows(run_id) == []


def test_recompute_after_rate_update_issues_new_bills(clean_pipeline):
    run_id = clean_pipeline["run_id"]
    settlement.settle(run_id)
    old_bills = {b["bill_no"] for b in store.bills(run_id)}
    _bump_rate_set()
    from app.modules.m4_weights import service as weights
    weights.compute_weights(run_id)
    charges.compute_charges(run_id)
    summary = settlement.settle(run_id)
    assert summary["stale"] == 0 and summary["final"] == 15
    new_bills = {b["bill_no"] for b in store.bills(run_id) if b["status"] == "issued"}
    # digest 变了票号必然全换；旧票留痕为 void，不允许复活
    assert new_bills and new_bills.isdisjoint(old_bills)
    assert all(store.bill_by_no(no)["status"] == "void" for no in old_bills)


def _bump_rate_set() -> None:
    """构造 v6 运价集（改一个首重价），通过正式装载通道触发失效。"""
    from app import config
    text = (config.SAMPLE_DIR / "rate_cards.csv").read_text()
    lines = [line for line in text.splitlines() if line.strip()]
    rebuilt = [lines[0]]
    for line in lines[1:]:
        head, _old_version = line.rsplit(",", 1)
        if "SF-standard-Z1-v2" in line:
            head = head.replace("13.00", "14.00")
        rebuilt.append(f"{head},6")
    rates.import_rate_set("\n".join(rebuilt).encode("utf-8"), "rate_cards_v6.csv")


# --- 重试幂等：不重复生成账单 ----------------------------------------------

def test_repeated_settle_keeps_same_bill_numbers(clean_pipeline):
    run_id = clean_pipeline["run_id"]
    first = settlement.settle(run_id)

    def issued():
        return {b["bill_no"]: b["status"]
                for b in store.bills(run_id) if b["status"] == "issued"}

    bills_first = issued()
    settlement.settle(run_id)
    settlement.settle(run_id)
    bills_third = issued()
    # 重试不产生新票：有效票集合与状态完全一致
    assert bills_first == bills_third
    assert len(bills_third) == first["final"]
    # 票号确定性：直接复算公式一致
    for decision in settlement.decisions(run_id):
        expected = billing.make_bill_no(
            run_id, decision["waybill_no"], decision["rate_digest"])
        assert decision["bill_no"] == expected


def test_bill_no_changes_only_when_digest_changes():
    no1 = billing.make_bill_no("RUN-1", "WB-1", "digest-a")
    no1_again = billing.make_bill_no("RUN-1", "WB-1", "digest-a")
    no2 = billing.make_bill_no("RUN-1", "WB-1", "digest-b")
    assert no1 == no1_again and no1 != no2


# --- 装载期同类重叠仍被拒绝 -------------------------------------------------

def test_conflicting_peak_file_rejected_at_import(pipeline, sample_bytes):
    report = rates.audit_peak(sample_bytes("peak_rules_conflict.csv"),
                              "peak_rules_conflict.csv")
    assert report["ok"] is False
    assert len(report["overlaps"]) == 1
    with pytest.raises(DomainError) as exc:
        rates.import_peak_rules(sample_bytes("peak_rules_conflict.csv"),
                                "peak_rules_conflict.csv")
    assert exc.value.code == "RATE_OVERLAP"


def test_missing_peak_rule_does_not_block_billing(clean_pipeline):
    """旺季加价是可选项：无 peak 规则只记 info 码 PEAK_MISSING，裁决仍 final。"""
    run_id = clean_pipeline["run_id"]
    store.replace_peak_rules([])
    summary = settlement.settle(run_id)
    assert summary["pending"] == 0 and summary["final"] == 15
    first = settlement.decisions(run_id)[0]
    assert "PEAK_MISSING" in first["codes"]
    assert all(e["rule_kind"] == engine.BASE_KIND for e in first["evidence"])
    # 无旺季加价时应收回到不含旺季的口径
    assert summary["amount_total"] == "1521.04"


def test_peak_parser_normalizes_percent_forms():
    text = (
        "carrier,service,zone,rule_id,effective_from,effective_to,"
        "charge_type,charge_value,currency,rate_set_version\n"
        "SF,standard,Z1,P1,2026-04-01T00:00:00Z,2026-07-01T00:00:00Z,percent,8.5%,CNY,3\n"
        "SF,standard,Z2,P2,2026-04-01T00:00:00Z,2026-07-01T00:00:00Z,fixed,8,CNY,3\n"
    )
    rules, problems, _version = peak_mod.parse_peak_rules(text)
    assert problems == []
    assert rules[0]["charge_value"] == "0.085"
    assert rules[1]["charge_value"] == "8.00"
