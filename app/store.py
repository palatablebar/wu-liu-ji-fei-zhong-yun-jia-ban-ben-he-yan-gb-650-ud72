"""SQLite 存取：运行清单、运价版本、运单、隔离区、计费重行、账单行。

只放数据访问，不放业务规则。每次调用开一个连接，用完即关。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  rules_version TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  rows_in INTEGER NOT NULL DEFAULT 0,
  rows_ok INTEGER NOT NULL DEFAULT 0,
  rows_quarantined INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  rate_digest TEXT NOT NULL DEFAULT '',
  stages TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rate_sets (
  carrier TEXT PRIMARY KEY,
  rate_set_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  version_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  first_weight_kg TEXT NOT NULL,
  first_price TEXT NOT NULL,
  step_weight_kg TEXT NOT NULL,
  step_price TEXT NOT NULL,
  min_charge TEXT NOT NULL,
  volume_divisor INTEGER NOT NULL,
  fuel_pct TEXT NOT NULL,
  remote_fee TEXT NOT NULL,
  insurance_pct TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (carrier, service, zone, version_id)
);
CREATE TABLE IF NOT EXISTS waybills (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  pickup_utc TEXT NOT NULL,
  weight_kg TEXT NOT NULL,
  length_cm TEXT NOT NULL,
  width_cm TEXT NOT NULL,
  height_cm TEXT NOT NULL,
  declared_value TEXT NOT NULL,
  pieces INTEGER NOT NULL,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS quarantine (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, line_no, code)
);
CREATE TABLE IF NOT EXISTS weight_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000',
  source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS charge_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  peak_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS peak_rules (
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  charge_type TEXT NOT NULL DEFAULT 'percent',
  charge_value TEXT NOT NULL DEFAULT '0.00',
  currency TEXT NOT NULL DEFAULT 'CNY',
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (carrier, service, zone, rule_id)
);
CREATE TABLE IF NOT EXISTS settlement_decisions (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  decision_status TEXT NOT NULL,
  rate_digest TEXT NOT NULL DEFAULT '',
  rate_set_version INTEGER NOT NULL DEFAULT 0,
  base_rule_id TEXT NOT NULL DEFAULT '',
  peak_rule_id TEXT NOT NULL DEFAULT '',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  currency TEXT NOT NULL DEFAULT 'CNY',
  evidence TEXT NOT NULL DEFAULT '[]',
  codes TEXT NOT NULL DEFAULT '',
  bill_no TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS invoice_bills (
  bill_no TEXT NOT NULL,
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  rate_digest TEXT NOT NULL DEFAULT '',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  currency TEXT NOT NULL DEFAULT 'CNY',
  status TEXT NOT NULL DEFAULT 'issued',
  created_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (bill_no)
);
"""

# 老库可能缺新列：按列名幂等补齐，补列只给默认值，不改历史行语义。
_COLUMN_PATCHES = {
    "charge_lines": [("peak_amount", "TEXT NOT NULL DEFAULT '0.00'")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, patches in _COLUMN_PATCHES.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in patches:
            if table in {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()} and name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    got = rows(sql, params)
    return got[0] if got else None


def execute(sql: str, params: tuple = ()) -> None:
    with session() as conn:
        conn.execute(sql, params)


def executemany(sql: str, seq) -> None:
    with session() as conn:
        conn.executemany(sql, list(seq))


# --- 运行清单 -------------------------------------------------------------

RUN_COLUMNS = (
    "input_name", "input_sha256", "rules_version", "profile",
    "rows_in", "rows_ok", "rows_quarantined", "amount_total",
    "rate_digest", "stages", "notes", "created_at", "updated_at",
)


def get_run(run_id: str) -> dict | None:
    return one("SELECT * FROM runs WHERE run_id = ?", (run_id,))


def list_runs(limit: int = 20) -> list[dict]:
    return rows("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,))


def upsert_run(run_id: str, data: dict) -> None:
    with session() as conn:
        existing = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if existing:
            fields = [c for c in RUN_COLUMNS if c in data]
            assignments = ", ".join(f"{c} = ?" for c in fields)
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                tuple(data[c] for c in fields) + (run_id,),
            )
        else:
            payload = {c: data.get(c, "") for c in RUN_COLUMNS}
            payload["rows_in"] = int(payload.get("rows_in") or 0)
            payload["rows_ok"] = int(payload.get("rows_ok") or 0)
            payload["rows_quarantined"] = int(payload.get("rows_quarantined") or 0)
            cols = ("run_id",) + RUN_COLUMNS
            conn.execute(
                f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (run_id,) + tuple(payload[c] for c in RUN_COLUMNS),
            )


def add_stage(run_id: str, stage: str, updated_at: str) -> None:
    run = get_run(run_id)
    if not run:
        return
    parts = [p for p in (run.get("stages") or "").split(",") if p]
    if stage not in parts:
        parts.append(stage)
    execute(
        "UPDATE runs SET stages = ?, updated_at = ? WHERE run_id = ?",
        (",".join(parts), updated_at, run_id),
    )


# --- 运价版本 -------------------------------------------------------------

CARD_COLUMNS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)


def replace_rate_cards(cards: list[dict], rate_set_version: int, updated_at: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM rate_cards")
        conn.executemany(
            f"INSERT INTO rate_cards ({', '.join(CARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in CARD_COLUMNS)})",
            [tuple(card[c] for c in CARD_COLUMNS) for card in cards],
        )
        carriers = sorted({card["carrier"] for card in cards})
        conn.execute("DELETE FROM rate_sets")
        conn.executemany(
            "INSERT INTO rate_sets (carrier, rate_set_version, updated_at) VALUES (?, ?, ?)",
            [(c, rate_set_version, updated_at) for c in carriers],
        )


def rate_cards() -> list[dict]:
    return rows("SELECT * FROM rate_cards ORDER BY carrier, service, zone, effective_from")


def rate_set_version(carrier: str) -> int:
    got = one("SELECT rate_set_version FROM rate_sets WHERE carrier = ?", (carrier,))
    return int(got["rate_set_version"]) if got else 0


def max_rate_set_version() -> int:
    got = one("SELECT MAX(rate_set_version) AS v FROM rate_sets")
    return int(got["v"]) if got and got["v"] is not None else 0


# --- 运单 / 隔离区 / 计费行 ------------------------------------------------

def replace_waybills(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM waybills WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO waybills (run_id, line_no, waybill_no, carrier, service, zone, "
            "pickup_utc, weight_kg, length_cm, width_cm, height_cm, declared_value, pieces, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["line_no"], it["waybill_no"], it["carrier"], it["service"],
                    it["zone"], it["pickup_utc"], it["weight_kg"], it["length_cm"],
                    it["width_cm"], it["height_cm"], it["declared_value"], it["pieces"],
                    it["codes"],
                )
                for it in items
            ],
        )


def waybills(run_id: str) -> list[dict]:
    return rows("SELECT * FROM waybills WHERE run_id = ? ORDER BY line_no", (run_id,))


def replace_quarantine(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM quarantine WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO quarantine (run_id, line_no, waybill_no, code, detail, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, it["line_no"], it["waybill_no"], it["code"], it["detail"], it["raw"])
                for it in items
            ],
        )


def quarantine(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM quarantine WHERE run_id = ? ORDER BY line_no, code", (run_id,)
    )


def replace_weight_lines(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM weight_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO weight_lines (run_id, waybill_no, version_id, actual_kg, volumetric_kg, "
            "source, billable_kg, steps, codes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["version_id"], it["actual_kg"],
                    it["volumetric_kg"], it["source"], it["billable_kg"], it["steps"],
                    it.get("codes", ""),
                )
                for it in items
            ],
        )


def weight_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM weight_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def replace_charge_lines(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM charge_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO charge_lines (run_id, waybill_no, version_id, base_amount, step_amount, "
            "remote_amount, fuel_amount, insurance_amount, peak_amount, total_amount, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["version_id"], it["base_amount"],
                    it["step_amount"], it["remote_amount"], it["fuel_amount"],
                    it["insurance_amount"], it.get("peak_amount", "0.00"),
                    it["total_amount"], it["codes"],
                )
                for it in items
            ],
        )


def charge_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM charge_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def latest_run_id() -> str:
    got = one("SELECT run_id FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1")
    return got["run_id"] if got else ""


# --- 旺季加价规则 ----------------------------------------------------------

PEAK_COLUMNS = (
    "carrier", "service", "zone", "rule_id", "effective_from", "effective_to",
    "charge_type", "charge_value", "currency", "rate_set_version",
)


def replace_peak_rules(items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM peak_rules")
        if items:
            conn.executemany(
                f"INSERT INTO peak_rules ({', '.join(PEAK_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in PEAK_COLUMNS)})",
                [tuple(item[c] for c in PEAK_COLUMNS) for item in items],
            )


def peak_rules() -> list[dict]:
    return rows("SELECT * FROM peak_rules ORDER BY carrier, service, zone, effective_from")


# --- 结算裁决 --------------------------------------------------------------

DECISION_COLUMNS = (
    "waybill_no", "decision_status", "rate_digest", "rate_set_version",
    "base_rule_id", "peak_rule_id", "total_amount", "currency",
    "evidence", "codes", "bill_no", "decided_at",
)


def replace_decisions(run_id: str, items: list[dict]) -> None:
    """整批重算：先清旧裁决再写新裁决，保证同 run_id 一票一行、无重复。"""
    with session() as conn:
        conn.execute("DELETE FROM settlement_decisions WHERE run_id = ?", (run_id,))
        if items:
            conn.executemany(
                f"INSERT INTO settlement_decisions (run_id, {', '.join(DECISION_COLUMNS)}) "
                f"VALUES (?, {', '.join('?' for _ in DECISION_COLUMNS)})",
                [(run_id,) + tuple(item[c] for c in DECISION_COLUMNS) for item in items],
            )


def decisions(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM settlement_decisions WHERE run_id = ? ORDER BY waybill_no", (run_id,)
    )


def decision(run_id: str, waybill_no: str) -> dict | None:
    return one(
        "SELECT * FROM settlement_decisions WHERE run_id = ? AND waybill_no = ?",
        (run_id, waybill_no),
    )


def update_decision(run_id: str, waybill_no: str, fields: dict) -> None:
    if not fields:
        return
    keys = tuple(fields)
    assignments = ", ".join(f"{key} = ?" for key in keys)
    execute(
        f"UPDATE settlement_decisions SET {assignments} "
        "WHERE run_id = ? AND waybill_no = ?",
        tuple(fields[key] for key in keys) + (run_id, waybill_no),
    )


def mark_stale_by_digest(digest: str) -> int:
    """运价更新后，旧 digest 的非失效裁决一律置 stale，并摘票防重复出票。

    同时挂上 RESULT_STALE 原因码，让失效状态在证据 CSV 里可解释。
    """
    with session() as conn:
        cur = conn.execute(
            "UPDATE settlement_decisions SET decision_status = 'stale', bill_no = '', "
            "codes = CASE WHEN codes LIKE '%RESULT_STALE%' THEN codes "
            "ELSE codes || CASE WHEN codes = '' THEN '' ELSE ',' END || 'RESULT_STALE' END "
            "WHERE rate_digest = ? AND decision_status != 'stale'",
            (digest,),
        )
        changed = cur.rowcount
        conn.execute(
            "UPDATE invoice_bills SET status = 'void' WHERE rate_digest = ? AND status = 'issued'",
            (digest,),
        )
        return changed


def void_run_bills_not_in(run_id: str, live_bill_nos: set[str]) -> int:
    """重算后摘除本 run 中已不在 final 集合里的旧票，防止孤儿账单对外发布。"""
    with session() as conn:
        rows = conn.execute(
            "SELECT bill_no FROM invoice_bills WHERE run_id = ? AND status = 'issued'",
            (run_id,),
        ).fetchall()
        stale = [row["bill_no"] for row in rows if row["bill_no"] not in live_bill_nos]
        if stale:
            conn.executemany(
                "UPDATE invoice_bills SET status = 'void' WHERE bill_no = ?",
                [(bill_no,) for bill_no in stale],
            )
        return len(stale)


# --- 账单票号 --------------------------------------------------------------

def upsert_bill(bill: dict) -> None:
    with session() as conn:
        existing = conn.execute(
            "SELECT bill_no FROM invoice_bills WHERE bill_no = ?", (bill["bill_no"],)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE invoice_bills SET run_id = ?, waybill_no = ?, rate_digest = ?, "
                "total_amount = ?, currency = ?, status = ? WHERE bill_no = ?",
                (bill["run_id"], bill["waybill_no"], bill["rate_digest"],
                 bill["total_amount"], bill["currency"], bill.get("status", "issued"),
                 bill["bill_no"]),
            )
        else:
            conn.execute(
                "INSERT INTO invoice_bills (bill_no, run_id, waybill_no, rate_digest, "
                "total_amount, currency, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bill["bill_no"], bill["run_id"], bill["waybill_no"], bill["rate_digest"],
                 bill["total_amount"], bill["currency"], bill.get("status", "issued"),
                 bill["created_at"]),
            )


def bills(run_id: str = "") -> list[dict]:
    if run_id:
        return rows(
            "SELECT * FROM invoice_bills WHERE run_id = ? ORDER BY bill_no", (run_id,)
        )
    return rows("SELECT * FROM invoice_bills ORDER BY bill_no")


def bill_by_no(bill_no: str) -> dict | None:
    return one("SELECT * FROM invoice_bills WHERE bill_no = ?", (bill_no,))
