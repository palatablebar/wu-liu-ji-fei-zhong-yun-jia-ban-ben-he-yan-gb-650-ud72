"""旺季加价规则解析：列契约、半开时间窗、百分比/固定额两种加价写法。

与基础运价表相互独立：基础价与旺季价允许落在同一时段（异类规则可共存），
是否冲突由裁决引擎按规则种类分组判断，而不是在装载期整份拒收。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation

PEAK_COLUMNS = (
    "carrier", "service", "zone", "rule_id", "effective_from", "effective_to",
    "charge_type", "charge_value", "currency", "rate_set_version",
)

CHARGE_TYPES = frozenset({"percent", "fixed"})
PERCENT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%$")
Q2 = Decimal("0.01")


def dec(text: str) -> Decimal:
    raw = unicodedata.normalize("NFKC", text or "").strip().replace(",", "")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def _charge_value(raw_type: str, raw_value: str) -> str:
    raw = (raw_value or "").strip()
    is_percent_suffix = bool(PERCENT_RE.match(raw))
    value = dec(raw[:-1].strip() if is_percent_suffix else raw)
    if raw_type == "percent":
        # 带 % 后缀一律按百分数除 100；裸数字与基础运价 fuel_pct 同口径：
        # >1 视为百分数（8.5 → 0.085），<=1 视为已是小数（0.085 → 0.085）。
        # 百分比是费率不是金额，保留原始精度，不做 0.01 量化。
        if is_percent_suffix or value > 1:
            value = value / Decimal("100")
        return str(value)
    return str(value.quantize(Q2))


def parse_peak_rules(text: str) -> tuple[list[dict], list[str], int]:
    """返回 (旺季规则, 解析问题 ``行号:代码``, 运价集版本)。"""
    reader = csv.reader(io.StringIO(text, newline=""))
    problems: list[str] = []
    header: list[str] | None = None
    rules: list[dict] = []
    rate_set_version = 0

    for cells in reader:
        if not cells or not any((c or "").strip() for c in cells):
            continue
        if header is None:
            header = [(c or "").strip().lower() for c in cells]
            missing = [c for c in PEAK_COLUMNS if c not in header]
            if missing:
                problems.append("表头缺列:" + ",".join(missing))
                return [], problems, 0
            continue

        row = dict(zip(header, [c if i < len(cells) else "" for i, c in enumerate(cells)]))
        line_no = reader.line_num
        carrier = (row.get("carrier") or "").strip().upper()
        service = (row.get("service") or "").strip().lower()
        zone = unicodedata.normalize("NFKC", row.get("zone") or "").strip().upper()
        rule_id = (row.get("rule_id") or "").strip()
        effective_from = (row.get("effective_from") or "").strip()
        effective_to = (row.get("effective_to") or "").strip()
        if not carrier or not service or not zone or not rule_id:
            problems.append(f"{line_no}:MISSING_KEY")
            continue
        if not effective_from or not effective_to:
            problems.append(f"{line_no}:MISSING_KEY")
            continue
        if effective_from >= effective_to:
            problems.append(f"{line_no}:RATE_OVERLAP")
            continue
        charge_type = (row.get("charge_type") or "percent").strip().lower()
        if charge_type not in CHARGE_TYPES:
            problems.append(f"{line_no}:MISSING_KEY")
            continue
        rule = {
            "carrier": carrier,
            "service": service,
            "zone": zone,
            "rule_id": rule_id,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "charge_type": charge_type,
            "charge_value": _charge_value(charge_type, row.get("charge_value")),
            "currency": (row.get("currency") or "CNY").strip().upper(),
            "rate_set_version": int(dec(row.get("rate_set_version"))),
        }
        rate_set_version = max(rate_set_version, rule["rate_set_version"])
        rules.append(rule)

    if not rules and not problems:
        problems.append("0:EMPTY_FILE")
    return rules, problems, rate_set_version


def index_by_key(rules: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    index: dict[tuple[str, str, str], list[dict]] = {}
    for rule in rules:
        index.setdefault((rule["carrier"], rule["service"], rule["zone"]), []).append(rule)
    for bucket in index.values():
        bucket.sort(key=lambda r: (r["effective_from"], r["rule_id"]))
    return index
