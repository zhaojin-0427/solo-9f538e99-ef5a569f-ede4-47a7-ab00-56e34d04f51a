"""Core domain, SQLite persistence, subtotal keys, and rank queries.

This module is framework-neutral so the important guarantees can be tested
without Flask.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Iterator, Optional


WEIGHT_MIN = 0
WEIGHT_MAX = 65535
WEIGHT_SIZE = 65536  # 2 ** 16
SUBTOTAL_KEY_LIMIT = 576

SCOPE_ALL = "all"
SCOPE_ACTIVE = "active"


class ValidationError(ValueError):
    """Invalid user input. Messages are safe to show on the review page."""

    def __init__(self, message: str, errors: Optional[Iterable[str]] = None):
        super().__init__(message)
        self.message = message
        self.errors = list(errors or [])


# ---------------------------------------------------------------------------
# Date and weight canonical segments
# ---------------------------------------------------------------------------


def days_in_year(year: int) -> int:
    return 366 if date(year, 12, 31).timetuple().tm_yday == 366 else 365


def parse_iso_date(value: str, field_name: str = "日期") -> date:
    if value is None:
        raise ValidationError(f"{field_name}不能为空")
    text = str(value).strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValidationError(f"{field_name}必须是 YYYY-MM-DD 格式")
    try:
        year = int(text[0:4])
        month = int(text[5:7])
        day = int(text[8:10])
        return date(year, month, day)
    except ValueError as exc:
        raise ValidationError(f"{field_name}不是有效日期：{text}") from exc


@dataclass(frozen=True)
class DateSeg:
    year: int
    lo: int  # zero-based day-of-year offset, inclusive
    hi: int  # zero-based offset, exclusive

    @property
    def start(self) -> date:
        return date(self.year, 1, 1) + timedelta(days=self.lo)

    @property
    def end_inclusive(self) -> date:
        return date(self.year, 1, 1) + timedelta(days=self.hi - 1)

    @property
    def key(self) -> str:
        return f"date:{self.start.isoformat()}..{self.end_inclusive.isoformat()}"

    @property
    def label(self) -> str:
        if self.lo + 1 == self.hi:
            return self.start.isoformat()
        return f"{self.start.isoformat()} ~ {self.end_inclusive.isoformat()}"


def _date_seg(year: int, lo: int, hi: int) -> DateSeg:
    return DateSeg(year=year, lo=lo, hi=hi)


def date_ancestors(day: date) -> list[DateSeg]:
    """Balanced-tree segments from the full year down to one day."""
    year = day.year
    n = days_in_year(year)
    idx = (day - date(year, 1, 1)).days
    lo, hi = 0, n
    result: list[DateSeg] = []
    while True:
        result.append(_date_seg(year, lo, hi))
        if hi - lo == 1:
            return result
        mid = (lo + hi) // 2
        if idx < mid:
            hi = mid
        else:
            lo = mid


def date_cover(start: date, end: date) -> list[DateSeg]:
    """Canonical balanced-tree segments covering [start, end] inclusively."""
    if start.year != end.year:
        raise ValidationError("查询起止日必须在同一年")
    if start > end:
        raise ValidationError("起始日不能晚于结束日")

    year = start.year
    n = days_in_year(year)
    lo = (start - date(year, 1, 1)).days
    hi = (end - date(year, 1, 1)).days + 1
    if not (0 <= lo < hi <= n):
        raise ValidationError("日期超出该年范围")

    found: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = [(0, n)]
    while stack:
        a, b = stack.pop()
        if hi <= a or b <= lo:
            continue
        if lo <= a and b <= hi:
            found.append((a, b))
            continue
        mid = (a + b) // 2
        stack.append((a, mid))
        stack.append((mid, b))

    found.sort()
    return [_date_seg(year, a, b) for a, b in found]


@dataclass(frozen=True)
class WeightSeg:
    lo: int  # inclusive
    hi: int  # exclusive

    @property
    def key(self) -> str:
        return f"weight:{self.lo:05d}..{self.hi - 1:05d}"

    @property
    def label(self) -> str:
        if self.hi - self.lo == 1:
            return f"{self.lo} kg"
        return f"{self.lo}–{self.hi - 1} kg"


def weight_ancestors(weight: int) -> list[WeightSeg]:
    """Dyadic segments from the exact weight leaf up to the 0..65535 root."""
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValidationError("净重必须在 0～65535 之间")

    lo = weight
    size = 1
    result = [WeightSeg(lo, lo + size)]
    while size < WEIGHT_SIZE:
        size *= 2
        lo = (lo // size) * size
        result.append(WeightSeg(lo, lo + size))
    return result


def weight_cover(lo: int, hi: int) -> list[WeightSeg]:
    """Minimal aligned dyadic cover of the half-open range [lo, hi)."""
    if lo >= hi:
        return []
    if not (0 <= lo < hi <= WEIGHT_SIZE):
        raise ValidationError("重量区间超出 0～65535")

    result: list[WeightSeg] = []
    pos = lo
    while pos < hi:
        size = 1
        while size * 2 <= hi - pos and pos % (size * 2) == 0:
            size *= 2
        result.append(WeightSeg(pos, pos + size))
        pos += size
    return result


def weight_greater_cover(weight: int) -> list[WeightSeg]:
    return weight_cover(weight + 1, WEIGHT_SIZE)


# ---------------------------------------------------------------------------
# SQLite schema and transaction helpers
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS tickets (
    slip_date   TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    net_weight  INTEGER NOT NULL CHECK (net_weight BETWEEN 0 AND 65535),
    PRIMARY KEY (slip_date, ticket_no)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS holds (
    slip_date   TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slip_date, ticket_no)
) WITHOUT ROWID;

-- One row = one date segment × one weight band × one caliber.
CREATE TABLE IF NOT EXISTS subtotals (
    date_key     TEXT NOT NULL,
    weight_key   TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('all', 'active')),
    ticket_count INTEGER NOT NULL CHECK (ticket_count >= 0),
    PRIMARY KEY (date_key, weight_key, scope)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_holds_date ON holds(slip_date);

-- Independent per-date upload generation. Every whole-day replacement or
-- first report of a date gets a new generation, even when the ticket rows are
-- identical. Confirmations bind to the generation they were checked against.
CREATE TABLE IF NOT EXISTS day_reports (
    slip_date    TEXT PRIMARY KEY,
    generation   INTEGER NOT NULL CHECK (generation >= 1),
    ticket_count INTEGER NOT NULL DEFAULT 0 CHECK (ticket_count >= 0),
    reported_at  TEXT NOT NULL DEFAULT (datetime('now'))
) WITHOUT ROWID;

-- A compare-before-commit review. It lists every neighbor day whose ticket
-- set overlaps this report by at least 95% on both sides (and 100 tickets on
-- each side). Rows are kept after confirmation as an audit trail.
CREATE TABLE IF NOT EXISTS precheck_reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date      TEXT NOT NULL,
    target_gen       INTEGER NOT NULL,
    target_count     INTEGER NOT NULL,
    window_start     TEXT NOT NULL,
    window_end       TEXT NOT NULL,
    window_json      TEXT NOT NULL,
    ticket_json      TEXT NOT NULL,
    matches_json     TEXT NOT NULL,
    match_count      INTEGER NOT NULL,
    submission_seq   INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL CHECK (state IN ('pending', 'confirmed')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed_at     TEXT,
    submitted_at     TEXT,
    submitted_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_precheck_target
    ON precheck_reports(target_date, state);

-- Monotonic submission ordering. The write that lands for a date is decided
-- under the same write lock as the ticket replacement, so a slower submission
-- can never overwrite a later one that already committed.
CREATE TABLE IF NOT EXISTS submission_seq (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    last_value  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS day_write_order (
    slip_date    TEXT PRIMARY KEY,
    last_written INTEGER NOT NULL
) WITHOUT ROWID;
"""

# Legacy databases carry tickets but no day_reports rows. One INSERT..SELECT
# per date backfills the generation/count metadata so old reports take part in
# the overlap precheck without a per-ticket loop. Runs once per startup.
SCHEMA_BACKFILL = """
INSERT OR IGNORE INTO day_reports(slip_date, generation, ticket_count, reported_at)
SELECT slip_date, 1, COUNT(*), datetime('now')
FROM tickets
GROUP BY slip_date;
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(SCHEMA_BACKFILL)


def init_db_path(db_path: str) -> None:
    if db_path != ":memory:":
        import os

        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        # Nested use (e.g. confirm → replace) joins the caller's transaction so
        # the whole operation still commits or rolls back as one unit.
        savepoint = f"sp_{id(object()):x}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            conn.execute(f"RELEASE {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

TICKET_HEADERS = {
    "单号",
    "秤单号",
    "提单号",
    "票号",
    "车单号",
    "ticket",
    "ticketno",
    "ticket_no",
    "id",
    "no",
}
WEIGHT_HEADERS = {
    "净重",
    "净重kg",
    "净重(kg)",
    "净重（kg）",
    "重量",
    "weight",
    "netweight",
    "net_weight",
    "kg",
}


def _normalise_header(value: object) -> str:
    return "".join(str(value or "").strip().lower().replace(" ", "").split())


def parse_weight(value: object, row_number: int) -> int:
    text = str(value if value is not None else "").strip()
    compact = text.replace(",", "").replace("_", "")
    if not compact or not (compact[0] in "+-" and compact[1:].isdigit() or compact.isdigit()):
        raise ValidationError(f"第 {row_number} 行净重不是整数：{text!r}")
    weight = int(compact)
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValidationError(
            f"第 {row_number} 行净重 {weight} 超出范围（0～65535 千克）"
        )
    return weight


def parse_csv_text(text: str) -> list[tuple[str, int]]:
    if text is None or not text.strip():
        raise ValidationError("CSV 为空，请选择包含“单号,净重”数据的文件")

    # Support common Chinese Excel exports which use GB18030.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        reader = csv.reader(io.StringIO(text), dialect)
    except csv.Error:
        reader = csv.reader(io.StringIO(text))

    raw_rows = list(reader)
    rows = [row for row in raw_rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise ValidationError("CSV 没有有效数据行")

    first = [_normalise_header(cell) for cell in rows[0]]
    ticket_idx = next((i for i, h in enumerate(first) if h in TICKET_HEADERS), None)
    weight_idx = next((i for i, h in enumerate(first) if h in WEIGHT_HEADERS), None)

    if ticket_idx is not None and weight_idx is not None:
        data_rows = rows[1:]
        start_line = 2
    else:
        # Headerless format is explicitly supported: first two columns are
        # ticket number then net weight.
        ticket_idx, weight_idx = 0, 1
        data_rows = rows
        start_line = 1

    tickets: list[tuple[str, int]] = []
    seen: set[str] = set()
    errors: list[str] = []

    for offset, row in enumerate(data_rows, start=start_line):
        line_no = offset
        if len(row) <= max(ticket_idx, weight_idx):
            errors.append(f"第 {line_no} 行缺少单号或净重：{row!r}")
            continue

        ticket_no = str(row[ticket_idx]).strip()
        weight_text = row[weight_idx]
        if not ticket_no:
            errors.append(f"第 {line_no} 行单号为空")
            continue
        try:
            weight = parse_weight(weight_text, line_no)
        except ValidationError as exc:
            errors.append(exc.message)
            continue

        if ticket_no in seen:
            errors.append(f"第 {line_no} 行单号在同一 CSV 内重复：{ticket_no}")
            continue
        seen.add(ticket_no)
        tickets.append((ticket_no, weight))

    if errors:
        raise ValidationError("CSV 校验未通过，未写入任何数据", errors=errors)
    if not tickets:
        raise ValidationError("CSV 没有可导入的数据行")
    return tickets


def decode_csv_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Aggregate updates
# ---------------------------------------------------------------------------


def _add_delta(delta: dict[tuple[str, str, str], int], key: tuple[str, str, str], value: int) -> None:
    delta[key] = delta.get(key, 0) + value


def add_ticket_to_delta(
    delta: dict[tuple[str, str, str], int],
    slip_date: str | date,
    weight: int,
    held: bool,
    sign: int,
    include_all: bool = True,
    include_active: bool | None = None,
) -> None:
    day = slip_date if isinstance(slip_date, date) else parse_iso_date(slip_date, "单据日期")
    date_keys = [seg.key for seg in date_ancestors(day)]
    weight_keys = [seg.key for seg in weight_ancestors(weight)]
    if include_active is None:
        include_active = not held

    for date_key in date_keys:
        for weight_key in weight_keys:
            if include_all:
                _add_delta(delta, (date_key, weight_key, SCOPE_ALL), sign)
            if include_active:
                _add_delta(delta, (date_key, weight_key, SCOPE_ACTIVE), sign)


def apply_delta(conn: sqlite3.Connection, delta: dict[tuple[str, str, str], int]) -> int:
    changes = 0
    for (date_key, weight_key, scope), amount in delta.items():
        if amount == 0:
            continue
        if amount > 0:
            conn.execute(
                """
                INSERT INTO subtotals(date_key, weight_key, scope, ticket_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date_key, weight_key, scope) DO UPDATE SET
                    ticket_count = subtotals.ticket_count + excluded.ticket_count
                """,
                (date_key, weight_key, scope, amount),
            )
        else:
            # Do not INSERT a negative proposed count: SQLite can evaluate the
            # CHECK before UPSERT conflict handling. A decremented key must
            # already exist because the corresponding original ticket exists.
            cursor = conn.execute(
                """
                UPDATE subtotals
                SET ticket_count = ticket_count + ?
                WHERE date_key = ? AND weight_key = ? AND scope = ?
                  AND ticket_count + ? >= 0
                """,
                (amount, date_key, weight_key, scope, amount),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    f"缺少或无法扣减小计键：{date_key} / {weight_key} / {scope} / {amount}"
                )
        changes += 1
    conn.execute("DELETE FROM subtotals WHERE ticket_count = 0")
    return changes


def adjust_existing_ticket_active(
    conn: sqlite3.Connection, slip_date: str, weight: int, sign: int
) -> None:
    delta: dict[tuple[str, str, str], int] = {}
    add_ticket_to_delta(
        delta,
        slip_date,
        weight,
        held=False,
        sign=sign,
        include_all=False,
        include_active=True,
    )
    apply_delta(conn, delta)


# ---------------------------------------------------------------------------
# Upload / replacement and holds
# ---------------------------------------------------------------------------


class _Superseded(Exception):
    """A newer submission has already written this date."""


def _allocate_submission_seq(conn: sqlite3.Connection) -> int:
    """Allocate the next submission order while holding the write lock.

    The counter is captured when a report is first submitted (precheck) and
    travels with that submission. A write therefore always orders after every
    submission that started earlier, and a late confirmation carrying an old
    counter can be rejected instead of overwriting a newer report.
    """
    conn.execute(
        """
        INSERT INTO submission_seq(id, last_value) VALUES (1, 1)
        ON CONFLICT(id) DO UPDATE SET last_value = submission_seq.last_value + 1
        """
    )
    row = conn.execute("SELECT last_value FROM submission_seq WHERE id = 1").fetchone()
    return int(row["last_value"])


def _claim_write_order(
    conn: sqlite3.Connection, slip_date: str, submission_seq: int
) -> None:
    """Refuse a write older than one already committed for this date."""
    row = conn.execute(
        "SELECT last_written FROM day_write_order WHERE slip_date = ?",
        (slip_date,),
    ).fetchone()
    if row is not None and int(row["last_written"]) > submission_seq:
        raise _Superseded(slip_date)
    conn.execute(
        """
        INSERT INTO day_write_order(slip_date, last_written) VALUES (?, ?)
        ON CONFLICT(slip_date) DO UPDATE SET
            last_written = MAX(day_write_order.last_written, excluded.last_written)
        """,
        (slip_date, submission_seq),
    )


def _bump_generation(
    conn: sqlite3.Connection, slip_date: str, ticket_count: int
) -> int:
    """Assign a new upload generation to a date inside the current write tx."""
    conn.execute(
        """
        INSERT INTO day_reports(slip_date, generation, ticket_count, reported_at)
        VALUES (?, 1, ?, datetime('now'))
        ON CONFLICT(slip_date) DO UPDATE SET
            generation = day_reports.generation + 1,
            ticket_count = excluded.ticket_count,
            reported_at = excluded.reported_at
        """,
        (slip_date, ticket_count),
    )
    row = conn.execute(
        "SELECT generation FROM day_reports WHERE slip_date = ?", (slip_date,)
    ).fetchone()
    return int(row["generation"])


def _replace_day_tickets_locked(
    conn: sqlite3.Connection,
    date_text: str,
    tickets: list[tuple[str, int]],
    submission_seq: Optional[int] = None,
) -> dict[str, object]:
    """Whole-day replacement body. Caller must hold a write transaction."""
    # The order check and the write share one lock, so the last submission
    # before the write is the report that lands. A direct call without an
    # explicit sequence is treated as the newest submission.
    seq = submission_seq if submission_seq is not None else _allocate_submission_seq(conn)
    _claim_write_order(conn, date_text, seq)

    old_rows = conn.execute(
        "SELECT ticket_no, net_weight FROM tickets WHERE slip_date = ?",
        (date_text,),
    ).fetchall()
    held_rows = conn.execute(
        "SELECT ticket_no FROM holds WHERE slip_date = ?",
        (date_text,),
    ).fetchall()
    held_numbers = {row["ticket_no"] for row in held_rows}

    delta: dict[tuple[str, str, str], int] = {}
    for row in old_rows:
        add_ticket_to_delta(
            delta,
            date_text,
            row["net_weight"],
            held=row["ticket_no"] in held_numbers,
            sign=-1,
        )
    for ticket_no, weight in tickets:
        add_ticket_to_delta(
            delta,
            date_text,
            weight,
            held=ticket_no in held_numbers,
            sign=+1,
        )

    conn.execute("DELETE FROM tickets WHERE slip_date = ?", (date_text,))
    conn.executemany(
        "INSERT INTO tickets(slip_date, ticket_no, net_weight) VALUES (?, ?, ?)",
        [(date_text, no, weight) for no, weight in tickets],
    )
    changed_keys = apply_delta(conn, delta)
    generation = _bump_generation(conn, date_text, len(tickets))

    # Any pending precheck whose window covers this date is now stale. No
    # per-window row is written: the generation bump above is the staleness
    # signal, so a report never waits on a comparison and its cost does not
    # grow with the number of tickets on neighbor days.
    affected = conn.execute(
        """
        SELECT id FROM precheck_reports
        WHERE state = 'pending'
          AND window_start <= ? AND window_end >= ?
        """,
        (date_text, date_text),
    ).fetchall()

    return {
        "date": date_text,
        "old_count": len(old_rows),
        "new_count": len(tickets),
        "generation": generation,
        "subtotal_delta_keys": changed_keys,
        "invalidated_pending": [int(row["id"]) for row in affected],
    }


def replace_day_tickets(
    conn: sqlite3.Connection, slip_date: str, tickets: list[tuple[str, int]]
) -> dict[str, object]:
    day = parse_iso_date(slip_date, "上传日期")
    date_text = day.isoformat()
    with write_transaction(conn):
        return _replace_day_tickets_locked(conn, date_text, tickets)

def _get_ticket_row(conn: sqlite3.Connection, day: str, ticket_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT slip_date, ticket_no, net_weight
        FROM tickets
        WHERE slip_date = ? AND ticket_no = ?
        """,
        (day, ticket_no),
    ).fetchone()


def hold_ticket(
    conn: sqlite3.Connection,
    slip_date: str,
    ticket_no: str,
    reason: Optional[str] = None,
) -> dict[str, object]:
    day = parse_iso_date(slip_date, "单据日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    with write_transaction(conn):
        existing_hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (day, ticket_no),
        ).fetchone()
        ticket = _get_ticket_row(conn, day, ticket_no)

        if existing_hold:
            action = "already_held"
            if reason is not None:
                conn.execute(
                    "UPDATE holds SET reason = ? WHERE slip_date = ? AND ticket_no = ?",
                    (reason.strip() or None, day, ticket_no),
                )
        else:
            action = "held"
            conn.execute(
                "INSERT INTO holds(slip_date, ticket_no, reason) VALUES (?, ?, ?)",
                (day, ticket_no, (reason or "").strip() or None),
            )
            if ticket is not None:
                adjust_existing_ticket_active(conn, day, ticket["net_weight"], -1)

        hold = conn.execute(
            "SELECT slip_date, ticket_no, reason, created_at FROM holds WHERE slip_date=? AND ticket_no=?",
            (day, ticket_no),
        ).fetchone()

    return {
        "action": action,
        "hold": dict(hold) if hold else None,
        "ticket": dict(ticket) if ticket else None,
        "original_missing": ticket is None,
    }


def release_ticket(conn: sqlite3.Connection, slip_date: str, ticket_no: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "单据日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    with write_transaction(conn):
        existing_hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (day, ticket_no),
        ).fetchone()
        ticket = _get_ticket_row(conn, day, ticket_no)

        if existing_hold:
            action = "released"
            conn.execute(
                "DELETE FROM holds WHERE slip_date = ? AND ticket_no = ?",
                (day, ticket_no),
            )
            if ticket is not None:
                adjust_existing_ticket_active(conn, day, ticket["net_weight"], +1)
        else:
            action = "not_held"

    return {
        "action": action,
        "ticket": dict(ticket) if ticket else None,
        "original_missing": ticket is None,
    }


def get_day_readback(conn: sqlite3.Connection, slip_date: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "日期").isoformat()
    generation_row = conn.execute(
        "SELECT generation FROM day_reports WHERE slip_date = ?", (day,)
    ).fetchone()
    tickets = conn.execute(
        """
        SELECT t.slip_date, t.ticket_no, t.net_weight,
               CASE WHEN h.ticket_no IS NULL THEN 0 ELSE 1 END AS held,
               h.reason
        FROM tickets AS t
        LEFT JOIN holds AS h
          ON h.slip_date = t.slip_date AND h.ticket_no = t.ticket_no
        WHERE t.slip_date = ?
        ORDER BY t.ticket_no
        """,
        (day,),
    ).fetchall()
    missing_holds = conn.execute(
        """
        SELECT h.slip_date, h.ticket_no, h.reason, h.created_at
        FROM holds AS h
        LEFT JOIN tickets AS t
          ON t.slip_date = h.slip_date AND t.ticket_no = h.ticket_no
        WHERE h.slip_date = ? AND t.ticket_no IS NULL
        ORDER BY h.ticket_no
        """,
        (day,),
    ).fetchall()
    return {
        "date": day,
        "generation": int(generation_row["generation"]) if generation_row else 0,
        "tickets": [dict(row) for row in tickets],
        "missing_holds": [dict(row) for row in missing_holds],
    }


# ---------------------------------------------------------------------------
# Compare-before-commit precheck
# ---------------------------------------------------------------------------

PRECHECK_DAYS = 7
PRECHECK_MIN_TICKETS = 100
PRECHECK_MIN_RATIO = 95  # percent, enforced on each side independently
STATE_PENDING = "pending"
STATE_CONFIRMED = "confirmed"


def precheck_window(day: date) -> tuple[date, date]:
    """Seven calendar days before and after, crossing year boundaries."""
    return day - timedelta(days=PRECHECK_DAYS), day + timedelta(days=PRECHECK_DAYS)


def _ticket_pairs(ticket_map: dict[str, int]) -> list[list[object]]:
    return [[no, ticket_map[no]] for no in sorted(ticket_map)]


def _meets_overlap(target_count: int, other_count: int, matched: int) -> bool:
    """Both ratios must independently reach the threshold.

    A day only qualifies when it has at least the ticket floor itself, so a
    hundred-ticket target and a ten-ticket neighbor can never hit it. That
    keeps the per-day comparison work bounded by candidate size only.
    """
    if target_count < PRECHECK_MIN_TICKETS or other_count < PRECHECK_MIN_TICKETS:
        return False
    return (
        matched * 100 >= PRECHECK_MIN_RATIO * target_count
        and matched * 100 >= PRECHECK_MIN_RATIO * other_count
    )


def _overlap_payload(
    other_day: str, target_count: int, other_count: int, matched: int
) -> dict[str, object]:
    return {
        "date": other_day,
        "target_count": target_count,
        "other_count": other_count,
        "matched": matched,
        "target_ratio": round(matched * 100.0 / target_count, 2),
        "other_ratio": round(matched * 100.0 / other_count, 2),
    }


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(text: str) -> object:
    return json.loads(text)


def _day_generation(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute(
        "SELECT generation FROM day_reports WHERE slip_date = ?", (day,)
    ).fetchone()
    return int(row["generation"]) if row else 0


def _window_reports(
    conn: sqlite3.Connection, window_start: str, window_end: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT slip_date, generation, ticket_count
        FROM day_reports
        WHERE slip_date BETWEEN ? AND ?
        ORDER BY slip_date
        """,
        (window_start, window_end),
    ).fetchall()


def _compare_with_window(
    conn: sqlite3.Connection,
    target_day: str,
    target_map: dict[str, int],
    window_start: str,
    window_end: str,
) -> tuple[list[dict[str, object]], int]:
    """Compare a report against reported neighbors inside the window.

    Returns (hits, inspected). Only days that are already reported, inside the
    window, and at the 100-ticket floor are inspected; the target date itself
    is skipped so its outgoing copy is never compared with itself.

    The incoming ticket numbers go into a connection-local temp table and the
    match is an indexed EXISTS probe. Work therefore scales with the incoming
    report and the matches, not with the number of non-overlapping tickets a
    neighbor happens to carry.
    """
    rows = _window_reports(conn, window_start, window_end)
    target_count = len(target_map)
    hits: list[dict[str, object]] = []
    inspected = 0

    # Candidate days are decided from the small day_reports metadata, never
    # from their ticket rows. Days below the floor or outside the window are
    # never scanned, so the daily comparison cost does not grow with tickets
    # on neighbor days that cannot share the incoming batch.
    candidates = [
        row
        for row in rows
        if row["slip_date"] != target_day
        and int(row["ticket_count"]) >= PRECHECK_MIN_TICKETS
    ]
    if target_count < PRECHECK_MIN_TICKETS or not candidates:
        return hits, inspected

    conn.execute("DROP TABLE IF EXISTS temp.precheck_incoming")
    conn.execute(
        """
        CREATE TEMP TABLE precheck_incoming (
            ticket_no  TEXT PRIMARY KEY,
            net_weight INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.executemany(
        "INSERT INTO precheck_incoming(ticket_no, net_weight) VALUES (?, ?)",
        [(no, weight) for no, weight in target_map.items()],
    )

    for row in candidates:
        other_day = row["slip_date"]
        other_count = int(row["ticket_count"])
        inspected += 1
        # The EXISTS probe drives the incoming temp table and looks each
        # ticket up by the tickets primary key. It touches only matching rows,
        # so a neighbor with many non-overlapping tickets costs nothing extra.
        matched = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM precheck_incoming AS i
                WHERE EXISTS (
                    SELECT 1 FROM tickets AS t
                    WHERE t.slip_date = ?
                      AND t.ticket_no = i.ticket_no
                      AND t.net_weight = i.net_weight
                )
                """,
                (other_day,),
            ).fetchone()[0]
        )
        if _meets_overlap(target_count, other_count, matched):
            hits.append(_overlap_payload(other_day, target_count, other_count, matched))

    conn.execute("DROP TABLE IF EXISTS temp.precheck_incoming")
    hits.sort(key=lambda item: (-int(item["matched"]), str(item["date"])))
    return hits, inspected


def allocate_submission(conn: sqlite3.Connection) -> int:
    """Public entry point: stamp an upload with its submission order."""
    with write_transaction(conn):
        return _allocate_submission_seq(conn)


def precheck_tickets(
    conn: sqlite3.Connection,
    slip_date: str,
    tickets: list[tuple[str, int]],
    *,
    submission_seq: Optional[int] = None,
) -> dict[str, object]:
    """Dry-run a day report against neighbors without writing any tickets.

    If a neighbor hits the overlap rule, a pending precheck stores the exact
    ticket list and overlap evidence for a quality reviewer. A hit precheck
    never writes tickets and never touches the subtotal tables. The caller may
    pass the submission order it already stamped; otherwise one is allocated
    here so a later upload can still supersede this one.
    """
    day = parse_iso_date(slip_date, "上传日期")
    target_day = day.isoformat()
    window_start, window_end = (d.isoformat() for d in precheck_window(day))
    target_map = {no: weight for no, weight in tickets}

    # The submission is stamped first, in a very short write transaction. The
    # comparison itself then runs in a read transaction: in WAL mode a writer
    # outside the window (or even inside it) is not blocked while the slower
    # comparison is still running.
    if submission_seq is None:
        with write_transaction(conn):
            seq = _allocate_submission_seq(conn)
    else:
        seq = submission_seq

    with read_transaction(conn):
        # Snapshot the window generations *before* comparing. Both come from
        # the same read snapshot, so if any window day is re-reported while the
        # comparison runs, confirmation later sees the newer generation and
        # refuses. Reading them afterwards would let a concurrent write slip
        # into the stored baseline and make a stale review look current.
        window_generations = {
            row["slip_date"]: int(row["generation"])
            for row in _window_reports(conn, window_start, window_end)
        }
        hits, inspected = _compare_with_window(
            conn, target_day, target_map, window_start, window_end
        )

    superseded: list[int] = []
    precheck_id: Optional[int] = None
    if hits:
        with write_transaction(conn):
            # A newer dry run replaces earlier undecided prechecks for the same
            # report date; confirmed rows stay as an audit trail. If a newer
            # submission already wrote this date while the comparison ran, the
            # result is stale before it is even stored.
            latest_write = conn.execute(
                "SELECT last_written FROM day_write_order WHERE slip_date = ?",
                (target_day,),
            ).fetchone()
            if latest_write is not None and int(latest_write["last_written"]) > seq:
                raise PrecheckStale(
                    "核对期间该日已有更晚提交的日报写入，本次预检已过期，请重新上传核对",
                    stale_dates=[target_day],
                )
            stale = conn.execute(
                "SELECT id FROM precheck_reports WHERE target_date = ? AND state = ?",
                (target_day, STATE_PENDING),
            ).fetchall()
            for row in stale:
                superseded.append(int(row["id"]))
                conn.execute(
                    "DELETE FROM precheck_reports WHERE id = ?", (int(row["id"]),)
                )

            cursor = conn.execute(
                """
                INSERT INTO precheck_reports(
                    target_date, target_gen, target_count, window_start, window_end,
                    window_json, ticket_json, matches_json, match_count,
                    submission_seq, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_day,
                    _day_generation(conn, target_day),
                    len(target_map),
                    window_start,
                    window_end,
                    _json_text(window_generations),
                    _json_text(_ticket_pairs(target_map)),
                    _json_text(hits),
                    len(hits),
                    seq,
                    STATE_PENDING,
                ),
            )
            precheck_id = int(cursor.lastrowid)

    return {
        "date": target_day,
        "window_start": window_start,
        "window_end": window_end,
        "ticket_count": len(target_map),
        "inspected_days": inspected,
        "matches": hits,
        "hit": bool(hits),
        "match_count": len(hits),
        "precheck_id": precheck_id,
        "submission_seq": seq,
        "superseded_pending": superseded,
        "required_tickets": PRECHECK_MIN_TICKETS,
        "required_ratio": PRECHECK_MIN_RATIO,
    }


def commit_reported_day(
    conn: sqlite3.Connection,
    slip_date: str,
    tickets: list[tuple[str, int]],
    *,
    submission_seq: Optional[int] = None,
    submitted_at: Optional[str] = None,
    submitted_by: Optional[str] = None,
) -> dict[str, object]:
    """Commit a hit-free report directly.

    The report keeps the submission order captured when it was uploaded, so
    if a later submission already wrote this date, the older one is refused.
    Otherwise the whole-day replacement runs in one write transaction: the
    last report submitted before the write is the one that lands. The
    precheck only gates hit reports; holds and subtotals stay transactionally
    consistent with the ticket rows.
    """
    day = parse_iso_date(slip_date, "上传日期")
    target_day = day.isoformat()
    with write_transaction(conn):
        seq = (
            submission_seq
            if submission_seq is not None
            else _allocate_submission_seq(conn)
        )
        before = conn.execute(
            "SELECT generation FROM day_reports WHERE slip_date = ?", (target_day,)
        ).fetchone()
        try:
            result = _replace_day_tickets_locked(conn, target_day, tickets, seq)
        except _Superseded:
            raise PrecheckStale(
                "该日已有更晚提交的日报写入，本次上传已过期，请重新核对",
                stale_dates=[target_day],
            ) from None
        result["reviewed_generation"] = int(before["generation"]) if before else 0
        result["submission_seq"] = seq
        result["submitted_at"] = submitted_at
        result["submitted_by"] = submitted_by
    return result


class PrecheckStale(ValidationError):
    """A pending precheck no longer describes the current window."""

    def __init__(self, message: str, stale_dates: Optional[list[str]] = None):
        super().__init__(message)
        self.stale_dates = list(stale_dates or [])


def _stale_dates_for(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> list[str]:
    """Days this precheck is no longer a valid review basis for.

    The target day is checked by generation, so any new report or replacement
    after the precheck invalidates it even when the ticket set is identical.
    Neighbor days use the same stored generation snapshot. Both checks are
    O(days in window) and independent of ticket volume.
    """
    window_generations: dict[str, int] = _json_load(row["window_json"])  # type: ignore[assignment]
    current: dict[str, int] = {
        report_row["slip_date"]: int(report_row["generation"])
        for report_row in _window_reports(
            conn, row["window_start"], row["window_end"]
        )
    }
    changed: list[str] = []
    for day in sorted(set(window_generations) | set(current)):
        if window_generations.get(day) != current.get(day):
            changed.append(day)
    return changed


def confirm_precheck(
    conn: sqlite3.Connection,
    precheck_id: int,
    *,
    submitted_at: Optional[str] = None,
    submitted_by: Optional[str] = None,
) -> dict[str, object]:
    """Commit the exact ticket list a reviewer saw, if still current.

    Stale means the target day itself or any reported day inside the
    seven-day window got a new report after this precheck was computed, or a
    later submission already wrote the target day. In all cases confirmation
    is refused and the report must be prechecked again.
    """
    with write_transaction(conn):
        row = conn.execute(
            "SELECT * FROM precheck_reports WHERE id = ?", (precheck_id,)
        ).fetchone()
        if row is None:
            raise ValidationError("预检记录不存在，请重新上传核对")
        if row["state"] != STATE_PENDING:
            raise ValidationError("该预检已确认过，请勿重复提交")

        stale_dates = _stale_dates_for(conn, row)
        if stale_dates:
            raise PrecheckStale(
                "预检后目标日或窗口内有日报新报或换报，必须重新核对后才能确认",
                stale_dates=stale_dates,
            )

        target_day = row["target_date"]
        ticket_pairs = _json_load(row["ticket_json"])
        tickets = [(str(no), int(weight)) for no, weight in ticket_pairs]
        # Confirmation keeps the submission order recorded when the reviewer
        # started, so a newer report that already committed wins over an older
        # confirmation that arrives late.
        seq = int(row["submission_seq"])
        if seq <= 0:
            seq = _allocate_submission_seq(conn)
        try:
            result = _replace_day_tickets_locked(conn, target_day, tickets, seq)
        except _Superseded:
            raise PrecheckStale(
                "该日已有更晚提交的日报写入，本次确认已过期，必须重新核对",
                stale_dates=[target_day],
            ) from None
        conn.execute(
            """
            UPDATE precheck_reports
            SET state = ?, confirmed_at = datetime('now'),
                submitted_at = COALESCE(?, datetime('now')), submitted_by = ?
            WHERE id = ?
            """,
            (STATE_CONFIRMED, submitted_at, submitted_by, precheck_id),
        )

    return {
        "precheck_id": precheck_id,
        "date": target_day,
        "matches": _json_load(row["matches_json"]),
        "match_count": int(row["match_count"]),
        "generation": int(result["generation"]),
        "old_count": int(result["old_count"]),
        "new_count": int(result["new_count"]),
    }


def _pending_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "target_date": row["target_date"],
        "target_count": int(row["target_count"]),
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "matches": _json_load(row["matches_json"]),
        "match_count": int(row["match_count"]),
        "state": row["state"],
        "created_at": row["created_at"],
        "stale_dates": _stale_dates_for(conn, row),
        "tickets": _json_load(row["ticket_json"]),
    }


def list_pending_prechecks(
    conn: sqlite3.Connection, target_date: Optional[str] = None
) -> list[dict[str, object]]:
    if target_date:
        day = parse_iso_date(target_date, "日期").isoformat()
        rows = conn.execute(
            """
            SELECT * FROM precheck_reports
            WHERE state = ? AND target_date = ?
            ORDER BY id DESC
            """,
            (STATE_PENDING, day),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM precheck_reports WHERE state = ? ORDER BY id DESC",
            (STATE_PENDING,),
        ).fetchall()
    return [_pending_payload(conn, row) for row in rows]


def get_precheck(conn: sqlite3.Connection, precheck_id: int) -> Optional[dict[str, object]]:
    row = conn.execute(
        "SELECT * FROM precheck_reports WHERE id = ?", (precheck_id,)
    ).fetchone()
    return _pending_payload(conn, row) if row is not None else None


# ---------------------------------------------------------------------------
# Rank query and counted subtotal access
# ---------------------------------------------------------------------------


@dataclass
class KeyMeter:
    accessed: int = 0
    empty: int = 0
    details: list[dict[str, object]] = field(default_factory=list)

    def get(
        self,
        conn: sqlite3.Connection,
        date_seg: DateSeg,
        weight_seg: WeightSeg,
        scope: str,
    ) -> int:
        self.accessed += 1
        row = conn.execute(
            """
            SELECT ticket_count
            FROM subtotals
            WHERE date_key = ? AND weight_key = ? AND scope = ?
            """,
            (date_seg.key, weight_seg.key, scope),
        ).fetchone()
        is_empty = row is None
        if is_empty:
            self.empty += 1
            value = 0
        else:
            value = int(row["ticket_count"])
        self.details.append(
            {
                "n": self.accessed,
                "scope": scope,
                "date_segment": date_seg.label,
                "date_key": date_seg.key,
                "weight_band": weight_seg.label,
                "weight_key": weight_seg.key,
                "count": value,
                "empty": is_empty,
            }
        )
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "accessed": self.accessed,
            "empty": self.empty,
            "limit": SUBTOTAL_KEY_LIMIT,
            "within_limit": self.accessed <= SUBTOTAL_KEY_LIMIT,
            "details": self.details,
        }


def _rank_payload(total: int, higher: int, equal: int) -> dict[str, object]:
    if equal <= 0:
        return {
            "total": total,
            "higher": higher,
            "equal": equal,
            "rank_start": None,
            "rank_end": None,
            "rank_text": "无法计算",
        }
    start_rank = higher + 1
    end_rank = higher + equal
    if start_rank == end_rank:
        rank_text = f"第 {start_rank} 名"
    else:
        rank_text = f"第 {start_rank}-{end_rank} 名（{equal} 单同重）"
    return {
        "total": total,
        "higher": higher,
        "equal": equal,
        "rank_start": start_rank,
        "rank_end": end_rank,
        "rank_text": rank_text,
    }


def query_rank(
    conn: sqlite3.Connection,
    slip_date: str,
    ticket_no: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, object]:
    target_day = parse_iso_date(slip_date, "单据日期")
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    start = parse_iso_date(start_date, "起始日") if start_date else date(target_day.year, 1, 1)
    end = parse_iso_date(end_date, "结束日") if end_date else date(target_day.year, 12, 31)

    if start.year != end.year:
        raise ValidationError("起止日必须在同一年")
    if not (start <= target_day <= end):
        raise ValidationError("单据日期必须包含在起止区间内（含首尾）")
    if start.year != target_day.year:
        raise ValidationError("起止日与单据日期必须在同一年")

    date_segs = date_cover(start, end)
    root_weight = WeightSeg(0, WEIGHT_SIZE)
    greater_weights: Optional[list[WeightSeg]] = None
    equal_weight: Optional[WeightSeg] = None
    meter = KeyMeter()

    with read_transaction(conn):
        target = _get_ticket_row(conn, target_day.isoformat(), ticket_no)
        hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (target_day.isoformat(), ticket_no),
        ).fetchone()
        held = hold is not None

        if target is None:
            return {
                "found": False,
                "held": held,
                "query": {
                    "date": target_day.isoformat(),
                    "ticket_no": ticket_no,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "date_segments": [seg.label for seg in date_segs],
                "date_segment_count": len(date_segs),
                "meter": meter.to_dict(),
                "all": None,
                "active": {
                    "held": held,
                    "rank_text": "暂扣中，名次留空" if held else "原单不存在，无法排名",
                },
            }

        weight = int(target["net_weight"])
        greater_weights = weight_greater_cover(weight)
        equal_weight = WeightSeg(weight, weight + 1)

        all_total = all_higher = all_equal = 0
        active_total = active_higher = active_equal = 0

        for ds in date_segs:
            all_total += meter.get(conn, ds, root_weight, SCOPE_ALL)
            for ws in greater_weights:
                all_higher += meter.get(conn, ds, ws, SCOPE_ALL)
            all_equal += meter.get(conn, ds, equal_weight, SCOPE_ALL)

            active_total += meter.get(conn, ds, root_weight, SCOPE_ACTIVE)
            if not held:
                for ws in greater_weights:
                    active_higher += meter.get(conn, ds, ws, SCOPE_ACTIVE)
                active_equal += meter.get(conn, ds, equal_weight, SCOPE_ACTIVE)

    all_result = _rank_payload(all_total, all_higher, all_equal)
    active_result: dict[str, object]
    if held:
        active_result = {
            "total": active_total,
            "higher": None,
            "equal": None,
            "rank_start": None,
            "rank_end": None,
            "rank_text": "",
            "held": True,
            "note": "目标单已暂扣；未暂扣口径只显示总数，名次按要求留空。",
        }
    else:
        active_result = _rank_payload(active_total, active_higher, active_equal)
        active_result["held"] = False

    return {
        "found": True,
        "held": held,
        "target": dict(target),
        "query": {
            "date": target_day.isoformat(),
            "ticket_no": ticket_no,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "weight": weight,
        },
        "date_segments": [seg.label for seg in date_segs],
        "date_segment_count": len(date_segs),
        "weight_greater_key_count": len(greater_weights),
        "meter": meter.to_dict(),
        "all": all_result,
        "active": active_result,
    }
