"""运价版本编排：装载、校验、择版入口、旺季加价与失效标记。"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from app import config, manifest, store
from app.errors import DomainError
from app.modules.m2_rates import ratecard, version
from app.modules.m8_settlement import peak as peak_mod


def _read_sample(name: str) -> bytes:
    path = config.SAMPLE_DIR / name
    if not path.exists():
        raise DomainError("SAMPLE_MISSING", f"样例文件不存在：{name}", 404)
    return path.read_bytes()


def seed_sample(force: bool = False) -> dict:
    """启动时把随仓库的运价表装进库；已有运价且非强制时不动。"""
    if store.rate_cards() and not force:
        return {"applied": False, "count": len(store.rate_cards()),
                "rate_set_version": store.max_rate_set_version()}
    raw = _read_sample(config.SAMPLE_RATES)
    text, codes = ratecard.read_text(raw)
    cards, problems, set_version = ratecard.parse_cards(text)
    if problems:
        raise DomainError("RATE_PARSE_FAILED", ";".join(problems[:5]), 422)
    peak_raw = _read_sample(config.SAMPLE_PEAK_RATES)
    peak_rules, peak_problems, peak_version = peak_mod.parse_peak_rules(
        ratecard.read_text(peak_raw)[0])
    if peak_problems:
        raise DomainError("RATE_PARSE_FAILED", ";".join(peak_problems[:5]), 422)
    store.replace_rate_cards(cards, max(set_version, peak_version), manifest.utc_now_iso())
    store.replace_peak_rules(peak_rules)
    return {"applied": True, "count": len(cards), "peak_count": len(peak_rules),
            "rate_set_version": max(set_version, peak_version), "codes": codes}


def audit(raw: bytes, name: str = "") -> dict:
    """只校验不落库：解析问题、版本重叠、陈旧版本。"""
    text, codes = ratecard.read_text(raw)
    cards, problems, set_version = ratecard.parse_cards(text)
    overlaps = version.detect_overlaps(cards)
    current = store.max_rate_set_version()
    stale = bool(current) and set_version < current
    keys = sorted({(c["carrier"], c["service"], c["zone"]) for c in cards})
    return {
        "file": name,
        "codes": codes,
        "count": len(cards),
        "rate_set_version": set_version,
        "current_rate_set_version": current,
        "problems": problems,
        "overlaps": overlaps,
        "stale": stale,
        "coverage": len(keys),
        "ok": not problems and not overlaps and not stale,
        "digest": hashlib.sha256(raw).hexdigest(),
    }


def import_rate_set(raw: bytes, name: str = "", peak_raw: bytes | None = None) -> dict:
    """校验通过才整体替换；陈旧版本拒绝覆盖（TRAP-26）。

    旺季加价可随运价集一起提交（``peak_raw``），也可单独由
    :func:`import_peak_rules` 装载。替换成功后，旧 digest 的裁决与已发票
    统一失效，保证运价规则更新后原结果必须重算。
    """
    report = audit(raw, name)
    if report["stale"]:
        raise DomainError(
            "RATE_STALE",
            f"运价集版本 {report['rate_set_version']} 低于现有 {report['current_rate_set_version']}",
            409,
        )
    if report["problems"]:
        raise DomainError("RATE_PARSE_FAILED", ";".join(report["problems"][:5]), 422)
    if report["overlaps"]:
        first = report["overlaps"][0]
        raise DomainError(
            "RATE_OVERLAP",
            f"{first['carrier']}/{first['service']}/{first['zone']} "
            f"{first['left']} 与 {first['right']} 生效区间重叠",
            409,
        )

    peak_rules: list[dict] = []
    peak_version = 0
    if peak_raw is not None:
        parsed, peak_problems, peak_version = peak_mod.parse_peak_rules(
            ratecard.read_text(peak_raw)[0])
        if peak_problems:
            raise DomainError("RATE_PARSE_FAILED", ";".join(peak_problems[:5]), 422)
        if _peak_overlaps(parsed):
            raise DomainError("RATE_OVERLAP", "同类旺季加价规则生效区间重叠", 409)
        peak_rules = parsed

    previous_digest = rate_digest()
    text, _codes = ratecard.read_text(raw)
    cards, _problems, set_version = ratecard.parse_cards(text)
    applied_version = max(set_version, peak_version)
    store.replace_rate_cards(cards, applied_version, manifest.utc_now_iso())
    if peak_raw is not None:
        store.replace_peak_rules(peak_rules)
    _invalidate_previous(previous_digest)
    return {"applied": True, "count": len(cards),
            "peak_count": len(peak_rules), "rate_set_version": applied_version}


def import_peak_rules(raw: bytes, name: str = "") -> dict:
    """单独装载旺季加价表：同类规则区间重叠拒绝，装载后旧裁决失效重算。"""
    parsed, problems, set_version = peak_mod.parse_peak_rules(ratecard.read_text(raw)[0])
    if problems:
        raise DomainError("RATE_PARSE_FAILED", ";".join(problems[:5]), 422)
    overlaps = _peak_overlaps(parsed)
    if overlaps:
        first = overlaps[0]
        raise DomainError(
            "RATE_OVERLAP",
            f"{first['carrier']}/{first['service']}/{first['zone']} "
            f"旺季规则 {first['left']} 与 {first['right']} 生效区间重叠", 409,
        )
    current = store.max_rate_set_version()
    if current and set_version < current:
        raise DomainError(
            "RATE_STALE",
            f"旺季规则版本 {set_version} 低于现有运价集 {current}", 409,
        )
    previous_digest = rate_digest()
    store.replace_peak_rules(parsed)
    _invalidate_previous(previous_digest)
    return {"applied": True, "peak_count": len(parsed),
            "rate_set_version": max(current, set_version)}


def _peak_overlaps(rules: list[dict]) -> list[dict]:
    """同类（同承运商/服务/分区）旺季规则区间相交即歧义，装载期拒绝。"""
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for rule in rules:
        grouped.setdefault((rule["carrier"], rule["service"], rule["zone"]), []).append(rule)
    found: list[dict] = []
    for key, bucket in sorted(grouped.items()):
        ordered = sorted(bucket, key=lambda r: (r["effective_from"], r["effective_to"]))
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                left, right = ordered[i], ordered[j]
                left_end = version.parse_utc(left["effective_to"])
                right_start = version.parse_utc(right["effective_from"])
                if left_end and right_start and right_start < left_end:
                    found.append({
                        "carrier": key[0], "service": key[1], "zone": key[2],
                        "left": left["rule_id"], "right": right["rule_id"],
                        "from": right["effective_from"], "to": left["effective_to"],
                        "code": "RATE_OVERLAP",
                    })
    return found


def _invalidate_previous(previous_digest: str) -> None:
    """运价规则更新 → 旧 digest 的裁决置 stale、已发票置 void，强制重算。"""
    if not previous_digest:
        return
    store.mark_stale_by_digest(previous_digest)


def cards() -> list[dict]:
    return store.rate_cards()


def index() -> dict:
    return ratecard.index_by_key(cards())


def key_index() -> set[tuple[str, str, str]]:
    return {(c["carrier"], c["service"], c["zone"]) for c in cards()}


def overlaps() -> list[dict]:
    return version.detect_overlaps(cards())


def select_card(index_map: dict, carrier: str, service: str, zone: str,
                pickup_utc) -> tuple[dict | None, list[str]]:
    bucket = index_map.get((carrier, service, zone), [])
    return version.select(bucket, pickup_utc)


def rate_digest() -> str:
    """运价指纹：基础运价与旺季加价共同入参，任一更新都会改变 digest。"""
    payload = manifest.dumps([
        [c["carrier"], c["service"], c["zone"], c["version_id"], c["effective_from"],
         c["effective_to"], c["first_price"], c["step_price"], c["min_charge"],
         c["volume_divisor"], c["fuel_pct"], c["remote_fee"], c["insurance_pct"]]
        for c in cards()
    ] + [
        [r["carrier"], r["service"], r["zone"], r["rule_id"], r["effective_from"],
         r["effective_to"], r["charge_type"], r["charge_value"]]
        for r in store.peak_rules()
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def coverage() -> list[dict]:
    return version.window_summary(cards())


def money_of(card: dict, field: str) -> Decimal:
    return Decimal(card[field])


def sample_names() -> dict:
    return {"rates": config.SAMPLE_RATES, "overlap": config.SAMPLE_RATES_OVERLAP}


def carriers() -> set[str]:
    return {c["carrier"] for c in cards()}


def peak_rules() -> list[dict]:
    return store.peak_rules()


def peak_overlaps() -> list[dict]:
    return _peak_overlaps(store.peak_rules())


def audit_peak(raw: bytes, name: str = "") -> dict:
    """只校验旺季加价表不落库。"""
    parsed, problems, set_version = peak_mod.parse_peak_rules(ratecard.read_text(raw)[0])
    overlaps = _peak_overlaps(parsed)
    current = store.max_rate_set_version()
    return {
        "file": name,
        "count": len(parsed),
        "rate_set_version": set_version,
        "current_rate_set_version": current,
        "problems": problems,
        "overlaps": overlaps,
        "stale": bool(current) and set_version < current,
        "ok": not problems and not overlaps and not (bool(current) and set_version < current),
        "digest": hashlib.sha256(raw).hexdigest(),
    }
