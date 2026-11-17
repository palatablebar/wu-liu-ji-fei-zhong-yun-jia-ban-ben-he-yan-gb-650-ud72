"""票号与账单发布：确定性票号、幂等出票、只发布 final 裁决。

票号 = ``BILL-`` + sha256(run_id | waybill_no | rate_digest) 前 12 位。
同票同规则重算得到同一票号，重试只会回到同一张票，绝不重复生成；
运价更新使 digest 变化，重算后自然得到新票号，旧票在失效时已置 void。
"""

from __future__ import annotations

import hashlib

from app.modules.m8_settlement import engine


def make_bill_no(run_id: str, waybill_no: str, rate_digest: str) -> str:
    seed = f"{run_id}|{waybill_no}|{rate_digest}"
    return "BILL-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12].upper()


def publishable(decisions: list[dict]) -> list[dict]:
    """只有 final 裁决可对外；pending/stale 一律排除。"""
    return [d for d in decisions if d["decision_status"] == engine.FINAL and d["bill_no"]]


def blocked(decisions: list[dict]) -> list[dict]:
    return [d for d in decisions if d["decision_status"] != engine.FINAL]


def build_bill(run_id: str, decision_row: dict, now_iso: str) -> dict:
    return {
        "bill_no": decision_row["bill_no"],
        "run_id": run_id,
        "waybill_no": decision_row["waybill_no"],
        "rate_digest": decision_row["rate_digest"],
        "total_amount": decision_row["total_amount"],
        "currency": decision_row["currency"],
        "status": "issued",
        "created_at": now_iso,
    }
