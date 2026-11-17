"""M5 编排：按运单组合分区价与附加费，落库账单行并回写运行合计。

金额口径集中在 :func:`build_charge`，裁决模块（M8）复用同一函数，
保证页面账单行与结算裁决依据永远是同一套分项算法。
"""

from __future__ import annotations

from decimal import Decimal

from app import manifest, store
from app.errors import DomainError
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable
from app.modules.m5_charges import addon, money, zone
from app.modules.m8_settlement import engine, peak as peak_mod
from app.reasons import sort_codes


def build_charge(card: dict, weight_row: dict, waybill: dict,
                 peak_rule: dict | None = None) -> tuple[dict, Decimal, list[str]]:
    """按一张已采用的基础运价卡算五个分项，可选叠旺季加价，返回 (分项, 合计, 原因码)。

    纯函数：同样的卡、计费重、运单、旺季规则必得同样金额，供 M5 落库与 M8 裁决共用。
    """
    steps = int(weight_row["steps"])
    first = zone.first_amount(card)
    step_part = zone.step_amount(steps, card)
    topup = zone.min_topup(first + step_part, card)
    base = money.q2(first + topup)
    remote = addon.remote_amount(card)
    fuel = addon.fuel_amount(base, step_part, card)
    declared = Decimal(waybill["declared_value"])
    insurance = addon.insurance_amount(declared, card)
    peak = engine.peak_amount(base + step_part, peak_rule)
    amount = money.total([base, step_part, remote, fuel, insurance, peak])

    row_codes: list[str] = []
    if topup > 0:
        row_codes.append("MIN_CHARGE_APPLIED")
    if remote > 0:
        row_codes.append("ZONE_REMOTE")
    if weight_row["source"] == billable.VOLUMETRIC:
        row_codes.append("VOLUMETRIC_APPLIED")
    if peak_rule is not None:
        row_codes.append("PEAK_APPLIED")

    parts = {
        "base_amount": str(base),
        "step_amount": str(step_part),
        "remote_amount": str(remote),
        "fuel_amount": str(fuel),
        "insurance_amount": str(insurance),
        "peak_amount": str(peak),
    }
    return parts, amount, row_codes


def compute_charges(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    weights = store.weight_lines(run_id)
    if not weights:
        raise DomainError("NO_WEIGHTS", "该运行还没有计费重", 409)
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    index_map = rates.index()
    peak_index = peak_mod.index_by_key(store.peak_rules())

    lines: list[dict] = []
    for weight in weights:
        if weight["source"] == billable.UNBILLED:
            continue
        waybill = waybills.get(weight["waybill_no"])
        if waybill is None:
            continue
        pickup = rateversion.parse_utc(waybill["pickup_utc"])
        key = (waybill["carrier"], waybill["service"], waybill["zone"])
        base_hits = rateversion.active_at(index_map.get(key, []), pickup)
        peak_hits = rateversion.active_at(peak_index.get(key, []), pickup)
        base_card, base_code = engine.resolve_kind(engine.BASE_KIND, base_hits)
        peak_rule, peak_code = engine.resolve_kind(engine.PEAK_KIND, peak_hits)
        if base_card is None:
            # 缺版或同 kind 歧义：本行不出账，原因由 M8 裁决统一挂账解释
            continue

        parts, amount, row_codes = build_charge(base_card, weight, waybill, peak_rule)
        row_codes.extend(c for c in (base_code, peak_code) if c)

        lines.append({
            "waybill_no": weight["waybill_no"],
            "version_id": weight["version_id"],
            **parts,
            "total_amount": str(amount),
            "codes": ",".join(sort_codes(row_codes)),
        })

    store.replace_charge_lines(run_id, lines)
    grand = money.total([Decimal(line["total_amount"]) for line in lines])
    now = manifest.utc_now_iso()
    store.upsert_run(run_id, {"amount_total": str(grand), "updated_at": now})
    store.add_stage(run_id, "charges", now)
    return {
        "run_id": run_id,
        "billed": len(lines),
        "amount_total": str(grand),
        "rate_digest": rates.rate_digest(),
    }


def lines(run_id: str) -> list[dict]:
    return store.charge_lines(run_id) if run_id else []


def amounts(run_id: str) -> dict:
    items = lines(run_id)
    parts = {
        "base": money.total([Decimal(i["base_amount"]) for i in items]),
        "step": money.total([Decimal(i["step_amount"]) for i in items]),
        "remote": money.total([Decimal(i["remote_amount"]) for i in items]),
        "fuel": money.total([Decimal(i["fuel_amount"]) for i in items]),
        "insurance": money.total([Decimal(i["insurance_amount"]) for i in items]),
        "peak": money.total([Decimal(i.get("peak_amount", "0")) for i in items]),
    }
    grand = money.total([Decimal(i["total_amount"]) for i in items])
    return {"count": len(items), "parts": parts, "total": grand,
            "conserved": money.conserved(grand, parts.values())}
