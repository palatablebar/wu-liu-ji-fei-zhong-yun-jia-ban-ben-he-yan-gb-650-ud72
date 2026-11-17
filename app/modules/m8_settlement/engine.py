"""裁决引擎：一票运单的全部命中规则 → 可解释、可发布的结算裁决。

设计要点
--------
1. 规则按 ``rule_kind`` 分两组：基础运价 ``base`` 与旺季加价 ``peak``。
   异类规则允许在同一时段共存（承运商把底价和旺季加价并排发布是正常业务）；
   同 kind 多命中才是无法自行排除的歧义，必须挂起人工裁决，不能对外发布。
2. 每条命中规则都留证据：命中/未采用、采用或未采用的原因码、窗口与参数，
   页面金额（charge_lines）与账单依据（settlement_decisions）来自同一次裁决。
3. 裁决是确定性纯函数：同输入 + 同 rate_digest 必得同结果，重跑不产生第二张票。
"""

from __future__ import annotations

from decimal import Decimal

from app.modules.m2_rates import version as rateversion
from app.modules.m5_charges import money

BASE_KIND = "base"
PEAK_KIND = "peak"

# 状态机：pending（歧义挂起）/ final（可发布）/ stale（运价更新失效，须重算）
PENDING = "pending"
FINAL = "final"
STALE = "stale"

# 同 kind 多命中时给出的 block 原因码，按种类区分，便于隔离区分流
CONFLICT_CODE = {BASE_KIND: "BASE_CONFLICT", PEAK_KIND: "PEAK_CONFLICT"}
MISSING_CODE = {BASE_KIND: "RATE_MISSING", PEAK_KIND: "PEAK_MISSING"}


def _window(card_or_rule: dict) -> str:
    return f"[{card_or_rule['effective_from']}, {card_or_rule['effective_to']})"


def _rule_id(rule: dict) -> str:
    return rule.get("version_id") or rule.get("rule_id") or ""


def match_rules(base_cards: list[dict], peak_rules: list[dict], pickup_utc) -> dict:
    """返回按种类分组的命中规则（半开区间，与择版口径完全一致）。"""
    return {
        BASE_KIND: rateversion.active_at(base_cards, pickup_utc),
        PEAK_KIND: rateversion.active_at(peak_rules, pickup_utc),
    }


def _kind_evidence(kind: str, hits: list[dict], all_rules: list[dict],
                   selected_id: str | None, kind_conflict: bool) -> list[dict]:
    """给同 kind 的每条规则落证据；未命中的规则也说明为何未采用。

    「采用」只按本 kind 是否歧义判断：另一 kind 的歧义不能把本 kind
    唯一命中的规则误标成 RULE_SUPERSEDED。
    """
    evidence: list[dict] = []
    hit_ids = {_rule_id(hit) for hit in hits}
    for rule in all_rules:
        rid = _rule_id(rule)
        if rid in hit_ids:
            if kind_conflict:
                status, code = "candidate", CONFLICT_CODE[kind]
            elif rid == selected_id:
                status, code = "adopted", ""
            else:
                status, code = "not_adopted", "RULE_SUPERSEDED"
        else:
            status, code = "not_adopted", "RULE_OUT_OF_WINDOW"
        evidence.append({
            "rule_kind": kind,
            "rule_id": rid,
            "window": _window(rule),
            "status": status,
            "reason_code": code,
            "params": _params(kind, rule),
        })
    return evidence


def _params(kind: str, rule: dict) -> dict:
    if kind == BASE_KIND:
        return {
            "first_price": rule["first_price"],
            "step_price": rule["step_price"],
            "min_charge": rule["min_charge"],
            "fuel_pct": rule["fuel_pct"],
            "remote_fee": rule["remote_fee"],
            "insurance_pct": rule["insurance_pct"],
        }
    return {"charge_type": rule["charge_type"], "charge_value": rule["charge_value"]}


def resolve_kind(kind: str, hits: list[dict]) -> tuple[dict | None, str | None]:
    """单命中直接采用；多命中返回歧义码；无命中返回缺失码。"""
    if not hits:
        return None, MISSING_CODE[kind]
    if len(hits) > 1:
        return None, CONFLICT_CODE[kind]
    return hits[0], None


def peak_amount(base_subtotal: Decimal, peak_rule: dict | None) -> Decimal:
    """旺季加价：百分比按基础+续重小计收取，固定额直接取定额。"""
    if peak_rule is None:
        return money.q2(Decimal("0"))
    value = Decimal(peak_rule["charge_value"])
    if peak_rule["charge_type"] == "fixed":
        return money.q2(value)
    return money.q2(money.q2(base_subtotal) * value)


def decide(base_cards: list[dict], peak_rules: list[dict], pickup_utc,
           charge_builder) -> dict:
    """对一票运单做完整裁决。

    ``charge_builder(base_card, peak_rule)`` 由 M5 注入，返回
    ``(分项 dict, 合计 Decimal, 原因码 list)``，把金额口径留在计费模块。
    """
    matched = match_rules(base_cards, peak_rules, pickup_utc)
    base_hits, peak_hits = matched[BASE_KIND], matched[PEAK_KIND]
    base_card, base_code = resolve_kind(BASE_KIND, base_hits)
    peak_rule, peak_code = resolve_kind(PEAK_KIND, peak_hits)

    codes: list[str] = [c for c in (base_code, peak_code) if c]
    conflict = base_code == "BASE_CONFLICT" or peak_code == "PEAK_CONFLICT"
    missing_base = base_code == "RATE_MISSING"

    parts: dict[str, str] = {}
    total = Decimal("0.00")
    if base_card is not None and not conflict:
        parts, total, extra_codes = charge_builder(base_card, peak_rule)
        codes.extend(extra_codes)
        if peak_rule is not None:
            codes.append("PEAK_APPLIED")

    evidence = _kind_evidence(
        BASE_KIND, base_hits, base_cards,
        _rule_id(base_card) if base_card else None,
        kind_conflict=base_code == "BASE_CONFLICT",
    )
    evidence.extend(_kind_evidence(
        PEAK_KIND, peak_hits, peak_rules,
        _rule_id(peak_rule) if peak_rule else None,
        kind_conflict=peak_code == "PEAK_CONFLICT",
    ))

    if conflict or missing_base:
        status = PENDING
    else:
        status = FINAL

    return {
        "decision_status": status,
        "base_rule_id": _rule_id(base_card) if base_card else "",
        "peak_rule_id": _rule_id(peak_rule) if peak_rule else "",
        "total_amount": str(money.q2(total)),
        "parts": parts,
        "codes": codes,
        "evidence": evidence,
        "matched_counts": {BASE_KIND: len(base_hits), PEAK_KIND: len(peak_hits)},
    }
