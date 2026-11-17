"""M8 编排：多规则命中裁决、失效重算、人工裁决与幂等出票。

流程
----
1. ``settle`` 对每票可计费运单调用裁决引擎，命中规则全部留证据后落
   ``settlement_decisions``：final 可出票，pending 挂起（歧义/缺基础价）。
2. 运价集更新后 ``rate_digest`` 变化，旧 digest 的裁决由运价服务置 stale、
   已发票置 void；重算只按新 digest 出票，重试回到同一票号不重复。
3. 人工裁决只允许在同 kind 的候选规则里二选一，裁决后转 final 并留
   MANUAL_RESOLVED 证据，杜绝在页面上随便挑一条计费。
"""

from __future__ import annotations

import json
from decimal import Decimal

from app import manifest, store
from app.errors import DomainError
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable
from app.modules.m5_charges import money, service as charge_service
from app.modules.m8_settlement import billing, engine, peak as peak_mod
from app.reasons import sort_codes


def _currency() -> str:
    for card in store.rate_cards():
        return card["currency"]
    return "CNY"


def settle(run_id: str) -> dict:
    """对整个 run 重算裁决并出票。整批替换，保证一票一行、重试幂等。"""
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    weights = store.weight_lines(run_id)
    if not weights:
        raise DomainError("NO_WEIGHTS", "该运行还没有计费重", 409)

    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    base_index = rates.index()
    peak_index = peak_mod.index_by_key(store.peak_rules())
    digest = rates.rate_digest()
    set_version = store.max_rate_set_version()
    now = manifest.utc_now_iso()
    currency = _currency()

    decisions: list[dict] = []
    for weight in weights:
        if weight["source"] == billable.UNBILLED:
            continue
        waybill = waybills.get(weight["waybill_no"])
        if waybill is None:
            continue
        key = (waybill["carrier"], waybill["service"], waybill["zone"])
        pickup = rateversion.parse_utc(waybill["pickup_utc"])
        base_cards = base_index.get(key, [])
        peak_rules = peak_index.get(key, [])

        def builder(base_card, peak_rule, _weight=weight, _waybill=waybill):
            return charge_service.build_charge(base_card, _weight, _waybill, peak_rule)

        verdict = engine.decide(base_cards, peak_rules, pickup, builder)
        verdict_codes = list(verdict["codes"])
        if verdict["decision_status"] == engine.PENDING:
            verdict_codes.append("DECISION_PENDING")
        bill_no = ""
        if verdict["decision_status"] == engine.FINAL:
            bill_no = billing.make_bill_no(run_id, waybill["waybill_no"], digest)

        decisions.append({
            "waybill_no": waybill["waybill_no"],
            "decision_status": verdict["decision_status"],
            "rate_digest": digest,
            "rate_set_version": set_version,
            "base_rule_id": verdict["base_rule_id"],
            "peak_rule_id": verdict["peak_rule_id"],
            "total_amount": verdict["total_amount"],
            "currency": currency,
            "evidence": manifest.dumps(verdict["evidence"]),
            "codes": ",".join(sort_codes(verdict_codes)),
            "bill_no": bill_no,
            "decided_at": now,
        })

    store.replace_decisions(run_id, decisions)
    live_bill_nos = {row["bill_no"] for row in decisions
                     if row["decision_status"] == engine.FINAL and row["bill_no"]}
    # 先摘旧票再出新票：上一轮 digest 或已挂起的票必须 void，绝不留孤儿账单
    store.void_run_bills_not_in(run_id, live_bill_nos)
    _issue_bills(run_id, decisions, now)
    store.add_stage(run_id, "settle", now)
    return summary(run_id)


def _issue_bills(run_id: str, decisions: list[dict], now_iso: str) -> int:
    """只给 final 裁决出票；upsert 保证同票号重试不产生第二张账单。"""
    issued = 0
    for row in decisions:
        if row["decision_status"] != engine.FINAL or not row["bill_no"]:
            continue
        store.upsert_bill(billing.build_bill(run_id, row, now_iso))
        issued += 1
    return issued


def resolve_pending(run_id: str, waybill_no: str, kind: str, rule_id: str,
                    operator: str = "settlement-desk") -> dict:
    """人工裁决：只能在挂起票的同 kind 候选规则中指定采用哪一条。"""
    if kind not in (engine.BASE_KIND, engine.PEAK_KIND):
        raise DomainError("BAD_RULE_KIND", f"未知规则种类：{kind}", 400)
    row = store.decision(run_id, waybill_no)
    if row is None:
        raise DomainError("DECISION_NOT_FOUND", f"无此运单裁决：{waybill_no}", 404)
    if row["decision_status"] != engine.PENDING:
        raise DomainError(
            "DECISION_NOT_PENDING",
            f"裁决状态为 {row['decision_status']}，仅挂起票可人工裁决", 409,
        )

    evidence = json.loads(row["evidence"] or "[]")
    candidates = [e for e in evidence
                  if e["rule_kind"] == kind and e["status"] == "candidate"]
    chosen = next((e for e in candidates if e["rule_id"] == rule_id), None)
    if chosen is None:
        raise DomainError(
            "RULE_NOT_CANDIDATE",
            f"{rule_id} 不是该票 {kind} 的候选规则，不能凭空指定", 409,
        )

    # 其余候选与未采用规则改写证据；选定规则转 adopted
    for ev in evidence:
        if ev["rule_kind"] != kind:
            continue
        if ev["rule_id"] == rule_id:
            ev["status"] = "adopted"
            ev["reason_code"] = "MANUAL_RESOLVED"
        elif ev["status"] == "candidate":
            ev["status"] = "not_adopted"
            ev["reason_code"] = "RULE_SUPERSEDED"

    waybills = {w["waybill_no"]: w for w in store.waybills(run_id)}
    weights = {w["waybill_no"]: w for w in store.weight_lines(run_id)}
    waybill, weight = waybills.get(waybill_no), weights.get(waybill_no)
    if waybill is None or weight is None:
        raise DomainError("SOURCE_ROW_MISSING", "运单或计费重行缺失，无法重算", 409)

    base_index, peak_index = rates.index(), peak_mod.index_by_key(store.peak_rules())
    key = (waybill["carrier"], waybill["service"], waybill["zone"])
    base_card = _find_rule(base_index.get(key, []), row["base_rule_id"], rule_id, kind,
                           engine.BASE_KIND)
    peak_rule = _find_rule(peak_index.get(key, []), row["peak_rule_id"], rule_id, kind,
                           engine.PEAK_KIND)
    if base_card is None:
        raise DomainError("BASE_RULE_GONE", "基础运价已失效，请先重算裁决", 409)

    parts, total, codes = charge_service.build_charge(base_card, weight, waybill, peak_rule)
    codes = sort_codes(set(codes) | {"MANUAL_RESOLVED"})
    digest = rates.rate_digest()
    bill_no = billing.make_bill_no(run_id, waybill_no, digest)
    now = manifest.utc_now_iso()
    store.update_decision(run_id, waybill_no, {
        "decision_status": engine.FINAL,
        "base_rule_id": base_card["version_id"],
        "peak_rule_id": peak_rule["rule_id"] if peak_rule else "",
        "total_amount": str(money.q2(total)),
        "evidence": manifest.dumps(evidence),
        "codes": ",".join(codes),
        "bill_no": bill_no,
        "decided_at": now,
    })
    refreshed = store.decision(run_id, waybill_no)
    store.upsert_bill(billing.build_bill(run_id, refreshed, now))
    return {"ok": True, "decision": _public(refreshed), "operator": operator}


def _find_rule(rules: list[dict], current_id: str, chosen_id: str,
               chosen_kind: str, self_kind: str) -> dict | None:
    """人工裁决后取该 kind 应采用的规则：本 kind 用选定 id，另一 kind 维持原采用。"""
    if chosen_kind != self_kind:
        return next((r for r in rules if (r.get("version_id") or r.get("rule_id")) == current_id), None)
    return next((r for r in rules if (r.get("version_id") or r.get("rule_id")) == chosen_id), None)


def summary(run_id: str) -> dict:
    rows = store.decisions(run_id)
    counts = {engine.FINAL: 0, engine.PENDING: 0, engine.STALE: 0}
    total = Decimal("0.00")
    for row in rows:
        counts[row["decision_status"]] = counts.get(row["decision_status"], 0) + 1
        if row["decision_status"] == engine.FINAL:
            total += Decimal(row["total_amount"])
    current_digest = rates.rate_digest()
    return {
        "run_id": run_id,
        "decided": len(rows),
        "final": counts[engine.FINAL],
        "pending": counts[engine.PENDING],
        "stale": counts[engine.STALE],
        "publishable": counts[engine.FINAL],
        "blocked": counts[engine.PENDING] + counts[engine.STALE],
        "amount_total": str(money.q2(total)),
        "rate_digest": current_digest,
        "stale_digest_present": any(r["rate_digest"] != current_digest for r in rows),
    }


def decisions(run_id: str) -> list[dict]:
    return [_public(row) for row in store.decisions(run_id)]


def pending(run_id: str) -> list[dict]:
    return [_public(row) for row in store.decisions(run_id)
            if row["decision_status"] == engine.PENDING]


def _public(row: dict) -> dict:
    data = dict(row)
    try:
        data["evidence"] = json.loads(row.get("evidence") or "[]")
    except json.JSONDecodeError:
        data["evidence"] = []
    data["codes"] = [c for c in (row.get("codes") or "").split(",") if c]
    return data


def bills(run_id: str = "") -> list[dict]:
    return store.bills(run_id)
