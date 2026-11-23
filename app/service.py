"""Domain service: source ingest, delivery, splitting and correction sheets.

Numbering rules
---------------
* A source row is ``(table_name, row_no)`` and carries the training team's
  internally *merged* student number.
* A project has its own namespace.  The first time a row becomes known to the
  project it receives an anonymous project row number that never changes.
  Each merged student receives one project student number, shared by every
  table of that project.
* A split moves selected rows of one merged student onto a freshly allocated
  project student number.  All other rows keep their number.
* Every delivery registers unique ``(project, row, student)`` items.  A split
  scans those items for the rows it moves: if a row was successfully delivered
  under the old number, a correction line is written in the *same transaction*.
  Later deliveries read the row's current number and therefore need no
  correction.
* A correction line stores the original anonymous project row number and the
  new student number, so a correction sheet is an immutable record and can be
  fetched verbatim later.
* Registration version switches are append-only.  Each published version keeps
  an old-row -> new-row mapping that also records the original source row
  number, so a versioned delivery line is resolved to its source row before any
  project numbering happens.  The same source row therefore keeps the same
  project row/student numbers across every version, and row numbers may be
  reused between versions.

Every mutation runs inside ``BEGIN IMMEDIATE``: a failure rolls back the
reassignment, the split record and any correction lines together.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import write_tx
from .errors import ApiError


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Lookup helpers (call inside a transaction)
# ---------------------------------------------------------------------------

def require_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "project_not_found", f"课题 {project_id!r} 不存在")
    return row


def _next_row_no(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT next_row_no FROM project_counters WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    number = int(row["next_row_no"])
    conn.execute(
        "UPDATE project_counters SET next_row_no = ? WHERE project_id = ?",
        (number + 1, project_id),
    )
    return number


def _next_student_no(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT next_student_no FROM project_counters WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    number = int(row["next_student_no"])
    conn.execute(
        "UPDATE project_counters SET next_student_no = ? WHERE project_id = ?",
        (number + 1, project_id),
    )
    return number


def _student_no_for_merged(
    conn: sqlite3.Connection, project_id: str, merged_student_no: str
) -> int:
    row = conn.execute(
        """
        SELECT student_no FROM project_student_numbers
        WHERE project_id = ? AND merged_student_no = ?
        """,
        (project_id, merged_student_no),
    ).fetchone()
    if row is not None:
        return int(row["student_no"])
    number = _next_student_no(conn, project_id)
    conn.execute(
        """
        INSERT INTO project_student_numbers
            (project_id, student_no, kind, merged_student_no,
             origin_merged_student_no, split_id, created_at)
        VALUES (?, ?, 'merged', ?, ?, NULL, ?)
        """,
        (project_id, number, merged_student_no, merged_student_no, utcnow()),
    )
    return number


def _origin_merged_for_student(
    conn: sqlite3.Connection, project_id: str, student_no: int
) -> str:
    """The training team's merged number a project student number came from."""
    row = conn.execute(
        """
        SELECT origin_merged_student_no FROM project_student_numbers
        WHERE project_id = ? AND student_no = ?
        """,
        (project_id, student_no),
    ).fetchone()
    if row is None:
        raise ApiError(500, "internal_inconsistency", "学员号缺少归并来源")
    return row["origin_merged_student_no"]


def _delivered_keys(
    conn: sqlite3.Connection, project_id: str
) -> set[tuple[str, int]]:
    rows = conn.execute(
        "SELECT DISTINCT table_name, row_no FROM delivery_items WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    return {(r["table_name"], int(r["row_no"])) for r in rows}


def _split_keys(conn: sqlite3.Connection, project_id: str) -> set[tuple[str, int]]:
    rows = conn.execute(
        "SELECT table_name, row_no FROM split_rows WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    return {(r["table_name"], int(r["row_no"])) for r in rows}


def _validate_unique_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, int]] = set()
    duplicates: list[dict[str, Any]] = []
    for row in rows:
        key = (row["table_name"], int(row["row_no"]))
        if key in seen:
            duplicates.append({"table_name": key[0], "row_no": key[1]})
        seen.add(key)
    if duplicates:
        raise ApiError(
            422,
            "duplicate_rows",
            "同一批次中出现了重复的表名与行号",
            {"duplicates": duplicates},
        )


# ---------------------------------------------------------------------------
# Registration versioning
# ---------------------------------------------------------------------------

REGISTRATION_TABLE = "registration"


def _active_version_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM registration_versions WHERE is_active = 1"
    ).fetchone()


def _get_version_row(
    conn: sqlite3.Connection, version_no: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM registration_versions WHERE version_no = ?",
        (version_no,),
    ).fetchone()


def _version_mappings(
    conn: sqlite3.Connection, version_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT old_row_no, new_row_no, source_row_no
        FROM registration_version_rows
        WHERE version_no = ?
        ORDER BY ordinal
        """,
        (version_no,),
    ).fetchall()


def switch_registration_version(
    conn: sqlite3.Connection,
    current_version: str,
    new_version: str,
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    with write_tx(conn):
        return switch_registration_version_tx(
            conn, current_version, new_version, mappings
        )


def switch_registration_version_tx(
    conn: sqlite3.Connection,
    current_version: str,
    new_version: str,
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    # Version numbers can never be reused, even when the rest of the request
    # is stale or invalid.
    if _get_version_row(conn, new_version) is not None:
        raise ApiError(
            409,
            "version_no_reused",
            "新版号已存在，版号不得复用",
            {"new_version": new_version},
        )

    # Both columns of the mapping must be unique on their own.
    old_seen: set[int] = set()
    new_seen: set[int] = set()
    duplicate_old: list[int] = []
    duplicate_new: list[int] = []
    for mapping in mappings:
        old_no = int(mapping["old_row_no"])
        new_no = int(mapping["new_row_no"])
        if old_no in old_seen:
            duplicate_old.append(old_no)
        if new_no in new_seen:
            duplicate_new.append(new_no)
        old_seen.add(old_no)
        new_seen.add(new_no)
    if duplicate_old or duplicate_new:
        raise ApiError(
            422,
            "duplicate_version_row_no",
            "对照中的旧行号或新行号必须各自唯一",
            {"duplicate_old_row_nos": duplicate_old,
             "duplicate_new_row_nos": duplicate_new},
        )

    active = _active_version_row(conn)
    if active is None:
        # First switch: every currently registered registration row must be
        # covered by the mapping, and old row numbers must match the rows in
        # the source table at this moment.
        if _get_version_row(conn, current_version) is not None:
            raise ApiError(
                409,
                "version_not_active",
                "当前版号已被换下，不能再次换版",
                {"current_version": current_version, "active_version": None},
            )
        registered = conn.execute(
            "SELECT row_no FROM source_rows WHERE table_name = ? ORDER BY row_no",
            (REGISTRATION_TABLE,),
        ).fetchall()
        registered_nos = {int(r["row_no"]) for r in registered}
        if registered_nos != old_seen:
            raise ApiError(
                422,
                "version_mapping_incomplete",
                "首次换版的对照必须覆盖库中全部报名行",
                {"missing_old_row_nos": sorted(registered_nos - old_seen),
                 "unknown_old_row_nos": sorted(old_seen - registered_nos)},
            )
        # On the first switch the source row number is the old row number.
        source_by_new = {int(m["new_row_no"]): int(m["old_row_no"])
                         for m in mappings}
        from_version = current_version
    else:
        active_version = active["version_no"]
        if active_version != current_version:
            raise ApiError(
                409,
                "version_not_active",
                "当前版号已被换下，不能再次换版",
                {"current_version": current_version,
                 "active_version": active_version},
            )
        current_rows = _version_mappings(conn, active_version)
        current_new_nos = {int(r["new_row_no"]) for r in current_rows}
        if current_new_nos != old_seen:
            raise ApiError(
                422,
                "version_mapping_incomplete",
                "对照必须覆盖当前版的全部行",
                {"missing_old_row_nos": sorted(current_new_nos - old_seen),
                 "unknown_old_row_nos": sorted(old_seen - current_new_nos)},
            )
        # Trace every new row number back to the original registered source
        # row through the current version's mapping.
        source_by_current_new = {
            int(r["new_row_no"]): int(r["source_row_no"]) for r in current_rows
        }
        source_by_new = {
            int(m["new_row_no"]): source_by_current_new[int(m["old_row_no"])]
            for m in mappings
        }
        from_version = active_version

    now = utcnow()
    if active is not None:
        conn.execute(
            "UPDATE registration_versions SET is_active = 0 WHERE version_no = ?",
            (active["version_no"],),
        )
    cursor = conn.execute(
        """
        INSERT INTO registration_versions
            (version_no, from_version, is_active, row_count, created_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (new_version, from_version, len(mappings), now),
    )
    version_id = int(cursor.lastrowid)
    for ordinal, mapping in enumerate(mappings, start=1):
        old_no = int(mapping["old_row_no"])
        new_no = int(mapping["new_row_no"])
        conn.execute(
            """
            INSERT INTO registration_version_rows
                (version_no, old_row_no, new_row_no, source_row_no,
                 ordinal, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_version, old_no, new_no, source_by_new[new_no], ordinal, now),
        )
    return _version_switch_response(conn, version_id)


def _version_switch_response(
    conn: sqlite3.Connection, version_id: int
) -> dict[str, Any]:
    version = conn.execute(
        "SELECT * FROM registration_versions WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if version is None:
        raise ApiError(404, "version_not_found", "报名版号不存在")
    rows = _version_mappings(conn, version["version_no"])
    return {
        "version_no": version["version_no"],
        "from_version": version["from_version"],
        "is_active": bool(version["is_active"]),
        "row_count": int(version["row_count"]),
        "created_at": version["created_at"],
        "mappings": [
            {
                "old_row_no": int(r["old_row_no"]),
                "new_row_no": int(r["new_row_no"]),
                "source_row_no": int(r["source_row_no"]),
            }
            for r in rows
        ],
    }


def list_registration_versions(conn: sqlite3.Connection) -> dict[str, Any]:
    versions = conn.execute(
        "SELECT version_id FROM registration_versions ORDER BY version_id"
    ).fetchall()
    active = _active_version_row(conn)
    return {
        "total": len(versions),
        "active_version": active["version_no"] if active is not None else None,
        "items": [
            _version_switch_response(conn, int(r["version_id"])) for r in versions
        ],
    }


def _resolve_registration_item(
    conn: sqlite3.Connection, item: dict[str, Any]
) -> tuple[int, str | None]:
    """Resolve a delivery line to its original registered source row number.

    A registration line without ``version`` is taken at its original row
    number (this also covers rows registered after the latest switch).  A
    versioned line walks the published mapping of that version.  The active
    version keeps serving rows after it has been superseded: every published
    mapping is immutable.
    """
    version_no = item.get("version")
    row_no = int(item["row_no"])
    if version_no is None:
        return row_no, None
    version = _get_version_row(conn, version_no)
    if version is None:
        raise ApiError(
            404,
            "unknown_version",
            "交付明细引用了未知的报名版号",
            {"version": version_no},
        )
    mapping = conn.execute(
        """
        SELECT source_row_no FROM registration_version_rows
        WHERE version_no = ? AND new_row_no = ?
        """,
        (version_no, row_no),
    ).fetchone()
    if mapping is None:
        raise ApiError(
            404,
            "unknown_version_row",
            "交付明细的行号在该报名版号中不存在",
            {"version": version_no, "row_no": row_no},
        )
    return int(mapping["source_row_no"]), version_no


# ---------------------------------------------------------------------------
# Source rows
# ---------------------------------------------------------------------------

def upsert_source_rows(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    with write_tx(conn):
        return upsert_source_rows_tx(conn, rows)


def upsert_source_rows_tx(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    _validate_unique_rows(rows)

    created = updated = unchanged = 0
    now = utcnow()
    out: list[dict[str, Any]] = []
    for row in rows:
        table_name = row["table_name"]
        row_no = int(row["row_no"])
        merged = row["merged_student_no"]
        existing = conn.execute(
            "SELECT * FROM source_rows WHERE table_name = ? AND row_no = ?",
            (table_name, row_no),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO source_rows
                    (table_name, row_no, merged_student_no, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (table_name, row_no, merged, now, now),
            )
            created += 1
            record = {
                "table_name": table_name,
                "row_no": row_no,
                "merged_student_no": merged,
                "created_at": now,
                "updated_at": now,
            }
        elif existing["merged_student_no"] == merged:
            unchanged += 1
            record = dict(existing)
        else:
            owners = conn.execute(
                """
                SELECT DISTINCT project_id FROM project_rows
                WHERE table_name = ? AND row_no = ?
                ORDER BY project_id
                """,
                (table_name, row_no),
            ).fetchall()
            if owners:
                raise ApiError(
                    409,
                    "source_row_locked",
                    "该行已进入课题，不能修改归并学员号",
                    {
                        "table_name": table_name,
                        "row_no": row_no,
                        "project_ids": [r["project_id"] for r in owners],
                    },
                )
            conn.execute(
                """
                UPDATE source_rows SET merged_student_no = ?, updated_at = ?
                WHERE table_name = ? AND row_no = ?
                """,
                (merged, now, table_name, row_no),
            )
            updated += 1
            record = {
                "table_name": table_name,
                "row_no": row_no,
                "merged_student_no": merged,
                "created_at": existing["created_at"],
                "updated_at": now,
            }
        out.append(record)
    return {
        "received": len(rows),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "rows": out,
    }


def list_source_rows(
    conn: sqlite3.Connection, limit: int, offset: int, table_name: str | None
) -> dict[str, Any]:
    where = "WHERE table_name = ?" if table_name is not None else ""
    params: list[Any] = [table_name] if table_name is not None else []
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM source_rows {where}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT table_name, row_no, merged_student_no, created_at, updated_at
        FROM source_rows {where}
        ORDER BY table_name, row_no LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    with write_tx(conn):
        return create_project_tx(conn, name)


def create_project_tx(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    now = utcnow()
    project_id = "prj_" + fingerprint({"name": name, "at": now})[:20]
    conn.execute(
        "INSERT INTO projects (project_id, name, created_at) VALUES (?, ?, ?)",
        (project_id, name, now),
    )
    conn.execute(
        """
        INSERT INTO project_counters (project_id, next_row_no, next_student_no)
        VALUES (?, 1, 1)
        """,
        (project_id,),
    )
    return {"project_id": project_id, "name": name, "created_at": now}


def list_projects(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT project_id, name, created_at FROM projects
        ORDER BY created_at, project_id
        """
    ).fetchall()
    items = [dict(r) for r in rows]
    return {"total": len(items), "items": items}


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = require_project(conn, project_id)
    return {
        "project_id": row["project_id"],
        "name": row["name"],
        "created_at": row["created_at"],
    }


def find_project_by_name(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT project_id FROM projects WHERE name = ? ORDER BY created_at LIMIT 1",
        (name,),
    ).fetchone()
    return None if row is None else row["project_id"]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _delivery_response(conn: sqlite3.Connection, delivery_id: int) -> dict[str, Any]:
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    if delivery is None:
        raise ApiError(404, "delivery_not_found", "交付记录不存在")
    lines = conn.execute(
        """
        SELECT ordinal, table_name, project_row_no, student_no, first_registration
        FROM delivery_items WHERE delivery_id = ? ORDER BY ordinal
        """,
        (delivery_id,),
    ).fetchall()
    return {
        "delivery_id": int(delivery["delivery_id"]),
        "project_id": delivery["project_id"],
        "request_id": delivery["request_id"],
        "created_at": delivery["created_at"],
        "item_count": int(delivery["requested_count"]),
        "new_project_row_count": int(delivery["new_project_row_count"]),
        "items": [
            {
                "ordinal": int(line["ordinal"]),
                "table_name": line["table_name"],
                "project_row_no": int(line["project_row_no"]),
                "student_no": int(line["student_no"]),
                "first_registration": bool(line["first_registration"]),
            }
            for line in lines
        ],
    }


def deliver(
    conn: sqlite3.Connection,
    project_id: str,
    items: list[dict[str, Any]],
    request_id: str | None,
) -> dict[str, Any]:
    with write_tx(conn):
        return deliver_tx(conn, project_id, items, request_id)


def deliver_tx(
    conn: sqlite3.Connection,
    project_id: str,
    items: list[dict[str, Any]],
    request_id: str | None,
) -> dict[str, Any]:
    require_project(conn, project_id)

    # Resolve versioned registration lines to their original source rows.
    # A line without a version keeps its row number as the source row number,
    # which also covers registration rows added after the latest switch.
    resolved_items: list[dict[str, Any]] = []
    for item in items:
        if item.get("version") and item["table_name"] != REGISTRATION_TABLE:
            raise ApiError(
                422,
                "version_only_for_registration",
                "版号只能用于 registration 表的交付明细",
                {"table_name": item["table_name"], "version": item["version"]},
            )
        if item["table_name"] == REGISTRATION_TABLE and item.get("version"):
            source_row_no, version_no = _resolve_registration_item(conn, item)
            resolved_items.append(
                {
                    "table_name": REGISTRATION_TABLE,
                    "row_no": source_row_no,
                    "merged_student_no": item.get("merged_student_no"),
                    "version": version_no,
                    "submitted_row_no": int(item["row_no"]),
                }
            )
        else:
            resolved_items.append(
                {
                    "table_name": item["table_name"],
                    "row_no": int(item["row_no"]),
                    "merged_student_no": item.get("merged_student_no"),
                    "version": item.get("version"),
                    "submitted_row_no": int(item["row_no"]),
                }
            )

    signature = fingerprint(
        {
            "items": [
                {
                    "table_name": item["table_name"],
                    "row_no": int(item["row_no"]),
                    "merged_student_no": item.get("merged_student_no"),
                    "version": item.get("version"),
                }
                for item in items
            ]
        }
    )

    if request_id is not None:
        existing = conn.execute(
            "SELECT * FROM deliveries WHERE project_id = ? AND request_id = ?",
            (project_id, request_id),
        ).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != signature:
                raise ApiError(
                    409,
                    "request_id_conflict",
                    "request_id 已用于另一批交付内容",
                    {"delivery_id": int(existing["delivery_id"])},
                )
            return _delivery_response(conn, int(existing["delivery_id"]))

    # Validate the whole batch before writing anything.  Dedup is on the
    # resolved source coordinates, so two versions of the same registration
    # row inside one batch are rejected as duplicates.
    _validate_unique_rows(resolved_items)

    sources: dict[tuple[str, int], sqlite3.Row] = {}
    missing: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for item in resolved_items:
        key = (item["table_name"], int(item["row_no"]))
        source = conn.execute(
            "SELECT * FROM source_rows WHERE table_name = ? AND row_no = ?", key
        ).fetchone()
        if source is None:
            missing.append({"table_name": key[0], "row_no": key[1]})
            continue
        sources[key] = source
        requested_merged = item.get("merged_student_no")
        if requested_merged is not None and requested_merged != source["merged_student_no"]:
            mismatches.append(
                {
                    "table_name": key[0],
                    "row_no": key[1],
                    "expected": source["merged_student_no"],
                    "provided": requested_merged,
                }
            )
    if missing:
        raise ApiError(
            404,
            "source_row_not_found",
            "交付明细中有未登记的表名与行号",
            {"missing": missing},
        )
    if mismatches:
        raise ApiError(
            409,
            "merged_student_mismatch",
            "交付明细中的归并学员号与登记不一致",
            {"mismatches": mismatches},
        )

    now = utcnow()
    cursor = conn.execute(
        """
        INSERT INTO deliveries
            (project_id, request_id, request_fingerprint, requested_count,
             new_project_row_count, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (project_id, request_id, signature, len(items), now),
    )
    delivery_id = int(cursor.lastrowid)

    new_project_row_count = 0
    for ordinal, item in enumerate(resolved_items, start=1):
        table_name = item["table_name"]
        row_no = int(item["row_no"])
        source = sources[(table_name, row_no)]

        project_row = conn.execute(
            """
            SELECT * FROM project_rows
            WHERE project_id = ? AND table_name = ? AND row_no = ?
            """,
            (project_id, table_name, row_no),
        ).fetchone()

        if project_row is None:
            project_row_no = _next_row_no(conn, project_id)
            student_no = _student_no_for_merged(
                conn, project_id, source["merged_student_no"]
            )
            conn.execute(
                """
                INSERT INTO project_rows
                    (project_id, table_name, row_no, project_row_no,
                     current_student_no, original_student_no,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    table_name,
                    row_no,
                    project_row_no,
                    student_no,
                    student_no,
                    now,
                    now,
                ),
            )
            new_project_row_count += 1
        else:
            project_row_no = int(project_row["project_row_no"])
            student_no = int(project_row["current_student_no"])

        # Every successful delivery line is stored once and never rewritten.
        # NULL delivery_item_id is impossible here because deliveries are the
        # only writer of this table and each request is validated up front.
        conn.execute(
            """
            INSERT INTO delivery_items
                (delivery_id, project_id, ordinal, table_name, row_no,
                 project_row_no, student_no, source_merged_student_no,
                 first_registration, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                project_id,
                ordinal,
                table_name,
                row_no,
                project_row_no,
                student_no,
                source["merged_student_no"],
                1 if project_row is None else 0,
                now,
            ),
        )

    conn.execute(
        "UPDATE deliveries SET new_project_row_count = ? WHERE delivery_id = ?",
        (new_project_row_count, delivery_id),
    )
    return _delivery_response(conn, delivery_id)


def list_deliveries(
    conn: sqlite3.Connection, project_id: str, limit: int, offset: int
) -> dict[str, Any]:
    require_project(conn, project_id)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE project_id = ?", (project_id,)
    ).fetchone()["n"]
    ids = conn.execute(
        """
        SELECT delivery_id FROM deliveries WHERE project_id = ?
        ORDER BY delivery_id LIMIT ? OFFSET ?
        """,
        (project_id, limit, offset),
    ).fetchall()
    return {
        "total": int(total),
        "items": [_delivery_response(conn, int(r["delivery_id"])) for r in ids],
    }


def get_delivery(
    conn: sqlite3.Connection, project_id: str, delivery_id: int
) -> dict[str, Any]:
    require_project(conn, project_id)
    row = conn.execute(
        "SELECT delivery_id FROM deliveries WHERE delivery_id = ? AND project_id = ?",
        (delivery_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "delivery_not_found", "交付记录不存在")
    return _delivery_response(conn, delivery_id)


# ---------------------------------------------------------------------------
# Project rows
# ---------------------------------------------------------------------------

def list_project_rows(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int,
    offset: int,
    table_name: str | None,
    student_no: int | None,
) -> dict[str, Any]:
    require_project(conn, project_id)
    where = ["pr.project_id = ?"]
    params: list[Any] = [project_id]
    if table_name is not None:
        where.append("pr.table_name = ?")
        params.append(table_name)
    if student_no is not None:
        where.append("pr.current_student_no = ?")
        params.append(student_no)
    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM project_rows pr WHERE {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT pr.*, sr.merged_student_no AS source_merged_student_no
        FROM project_rows pr
        JOIN source_rows sr
          ON sr.table_name = pr.table_name AND sr.row_no = pr.row_no
        WHERE {clause}
        ORDER BY pr.project_row_no LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    split_keys = _split_keys(conn, project_id)
    delivered = _delivered_keys(conn, project_id)
    items = []
    for row in rows:
        key = (row["table_name"], int(row["row_no"]))
        items.append(
            {
                "project_row_no": int(row["project_row_no"]),
                "table_name": row["table_name"],
                "source_row_no": int(row["row_no"]),
                "merged_student_no": row["source_merged_student_no"],
                "current_student_no": int(row["current_student_no"]),
                "original_student_no": int(row["original_student_no"]),
                "is_split_out": key in split_keys,
                "is_delivered": key in delivered,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return {"total": int(total), "limit": limit, "offset": offset, "items": items}


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

def _resolve_split_rows(
    conn: sqlite3.Connection, project_id: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalise split references to ``table_name`` + ``row_no``.

    A row may be addressed by its source coordinates or by the original
    anonymous project row number printed on a correction sheet.
    """
    resolved: list[dict[str, Any]] = []
    unknown_anonymous: list[int] = []
    for row in rows:
        if row.get("original_row_no") is not None:
            project_row = conn.execute(
                """
                SELECT table_name, row_no FROM project_rows
                WHERE project_id = ? AND project_row_no = ?
                """,
                (project_id, int(row["original_row_no"])),
            ).fetchone()
            if project_row is None:
                unknown_anonymous.append(int(row["original_row_no"]))
                continue
            resolved.append(
                {
                    "table_name": project_row["table_name"],
                    "row_no": int(project_row["row_no"]),
                }
            )
        else:
            resolved.append(
                {"table_name": row["table_name"], "row_no": int(row["row_no"])}
            )
    if unknown_anonymous:
        raise ApiError(
            404,
            "anonymous_row_not_found",
            "拆分请求中的原匿名行号在课题中不存在",
            {"original_row_nos": unknown_anonymous},
        )
    _validate_unique_rows(resolved)
    return resolved


def _split_response(conn: sqlite3.Connection, split_id: int) -> dict[str, Any]:
    split = conn.execute(
        "SELECT * FROM splits WHERE split_id = ?", (split_id,)
    ).fetchone()
    if split is None:
        raise ApiError(404, "split_not_found", "拆分记录不存在")
    project_id = split["project_id"]
    old_student_no = int(split["old_student_no"])
    merged = _origin_merged_for_student(conn, project_id, old_student_no)

    rows = conn.execute(
        "SELECT * FROM split_rows WHERE split_id = ? ORDER BY table_name, row_no",
        (split_id,),
    ).fetchall()
    correction_keys = {
        (r["table_name"], int(r["row_no"]))
        for r in conn.execute(
            "SELECT table_name, row_no FROM correction_items WHERE split_id = ?",
            (split_id,),
        ).fetchall()
    }
    delivered_keys = {
        (r["table_name"], int(r["row_no"]))
        for r in conn.execute(
            """
            SELECT table_name, row_no FROM delivery_items
            WHERE project_id = ? AND student_no = ?
            """,
            (project_id, old_student_no),
        ).fetchall()
    }
    row_items = []
    for row in rows:
        key = (row["table_name"], int(row["row_no"]))
        row_items.append(
            {
                "table_name": row["table_name"],
                "source_row_no": int(row["row_no"]),
                "project_row_no": int(row["project_row_no"]),
                "old_student_no": int(row["old_student_no"]),
                "new_student_no": int(row["new_student_no"]),
                "was_delivered_under_old_no": key in delivered_keys,
                "correction_registered": key in correction_keys,
            }
        )
    return {
        "split_id": int(split["split_id"]),
        "project_id": project_id,
        "request_id": split["request_id"],
        "merged_student_no": merged,
        "old_student_no": old_student_no,
        "new_student_no": int(split["new_student_no"]),
        "row_count": int(split["row_count"]),
        "correction_count": int(split["correction_count"]),
        "created_at": split["created_at"],
        "rows": row_items,
    }


def split_student(
    conn: sqlite3.Connection,
    project_id: str,
    merged_student_no: str | None,
    rows: list[dict[str, Any]],
    request_id: str | None,
) -> dict[str, Any]:
    with write_tx(conn):
        return split_student_tx(
            conn, project_id, merged_student_no, rows, request_id
        )


def split_student_tx(
    conn: sqlite3.Connection,
    project_id: str,
    merged_student_no: str | None,
    rows: list[dict[str, Any]],
    request_id: str | None,
) -> dict[str, Any]:
    require_project(conn, project_id)

    # Resolution only reads; it also makes request retries stable when the
    # client mixes addressing styles.
    resolved = _resolve_split_rows(conn, project_id, rows)
    signature = fingerprint(
        {"merged_student_no": merged_student_no, "rows": resolved}
    )

    if request_id is not None:
        existing = conn.execute(
            "SELECT * FROM splits WHERE project_id = ? AND request_id = ?",
            (project_id, request_id),
        ).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != signature:
                raise ApiError(
                    409,
                    "request_id_conflict",
                    "request_id 已用于另一批拆分内容",
                    {"split_id": int(existing["split_id"])},
                )
            return _split_response(conn, int(existing["split_id"]))

    # Validate row existence and merged-student membership.
    sources: dict[tuple[str, int], sqlite3.Row] = {}
    missing: list[dict[str, Any]] = []
    for key in resolved:
        source = conn.execute(
            "SELECT * FROM source_rows WHERE table_name = ? AND row_no = ?",
            (key["table_name"], key["row_no"]),
        ).fetchone()
        if source is None:
            missing.append(dict(key))
        else:
            sources[(key["table_name"], key["row_no"])] = source
    if missing:
        raise ApiError(
            404,
            "source_row_not_found",
            "拆分明细中有未登记的表名与行号",
            {"missing": missing},
        )

    source_merged = {
        key: source["merged_student_no"] for key, source in sources.items()
    }
    distinct_merged = sorted(set(source_merged.values()))
    if merged_student_no is None:
        if len(distinct_merged) != 1:
            raise ApiError(
                409,
                "rows_span_multiple_students",
                "拆分行分属多个归并学员号，请明确指定 merged_student_no",
                {"merged_student_nos": distinct_merged},
            )
        merged_student_no = distinct_merged[0]
    else:
        mismatched = [
            {
                "table_name": table_name,
                "row_no": row_no,
                "source_merged_student_no": source_merged[(table_name, row_no)],
            }
            for (table_name, row_no) in source_merged
            if source_merged[(table_name, row_no)] != merged_student_no
        ]
        if mismatched:
            raise ApiError(
                409,
                "merged_student_mismatch",
                "拆分行不属于指定的归并学员号",
                {"merged_student_no": merged_student_no, "rows": mismatched},
            )

    old_student_no = _student_no_for_merged(conn, project_id, merged_student_no)
    origin_merged = merged_student_no

    # Rows already known to the project must still belong to that group.  A
    # row that was split out earlier is in a different group and cannot be
    # split again under the same merged number: the caller's view is stale.
    existing_rows: dict[tuple[str, int], sqlite3.Row] = {}
    conflicts: list[dict[str, Any]] = []
    for key in resolved:
        project_row = conn.execute(
            """
            SELECT * FROM project_rows
            WHERE project_id = ? AND table_name = ? AND row_no = ?
            """,
            (project_id, key["table_name"], key["row_no"]),
        ).fetchone()
        if project_row is not None:
            existing_rows[(key["table_name"], key["row_no"])] = project_row
            if int(project_row["current_student_no"]) != old_student_no:
                conflicts.append(
                    {
                        "table_name": key["table_name"],
                        "row_no": key["row_no"],
                        "current_student_no": int(
                            project_row["current_student_no"]
                        ),
                    }
                )
    if conflicts:
        raise ApiError(
            409,
            "row_not_in_student",
            "部分行已不属于该归并学员号（可能已被拆分）",
            {
                "merged_student_no": merged_student_no,
                "old_student_no": old_student_no,
                "conflicts": conflicts,
            },
        )

    # ---- atomic split -----------------------------------------------------
    now = utcnow()
    new_student_no = _next_student_no(conn, project_id)
    cursor = conn.execute(
        """
        INSERT INTO splits
            (project_id, request_id, request_fingerprint, old_student_no,
             new_student_no, row_count, correction_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            project_id,
            request_id,
            signature,
            old_student_no,
            new_student_no,
            len(resolved),
            now,
        ),
    )
    split_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO project_student_numbers
            (project_id, student_no, kind, merged_student_no,
             origin_merged_student_no, split_id, created_at)
        VALUES (?, ?, 'split', NULL, ?, ?, ?)
        """,
        (project_id, new_student_no, origin_merged, split_id, now),
    )

    correction_count = 0
    for key in resolved:
        table_name = key["table_name"]
        row_no = key["row_no"]
        project_row = existing_rows.get((table_name, row_no))
        if project_row is not None:
            project_row_no = int(project_row["project_row_no"])
            conn.execute(
                """
                UPDATE project_rows
                SET current_student_no = ?, updated_at = ?
                WHERE project_id = ? AND table_name = ? AND row_no = ?
                """,
                (new_student_no, now, project_id, table_name, row_no),
            )
            was_in_project = 1
        else:
            project_row_no = _next_row_no(conn, project_id)
            conn.execute(
                """
                INSERT INTO project_rows
                    (project_id, table_name, row_no, project_row_no,
                     current_student_no, original_student_no,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    table_name,
                    row_no,
                    project_row_no,
                    new_student_no,
                    old_student_no,
                    now,
                    now,
                ),
            )
            was_in_project = 0

        conn.execute(
            """
            INSERT INTO split_rows
                (split_id, project_id, table_name, row_no, project_row_no,
                 old_student_no, new_student_no, was_in_project)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                split_id,
                project_id,
                table_name,
                row_no,
                project_row_no,
                old_student_no,
                new_student_no,
                was_in_project,
            ),
        )

        # Every successfully registered delivery of this row under the old
        # number becomes a correction line, whatever the delivery/split order:
        # rows delivered before the split already used the old number, and a
        # row delivered several times gets one line per delivery record.
        delivered_items = conn.execute(
            """
            SELECT delivery_item_id, delivery_id, project_row_no
            FROM delivery_items
            WHERE project_id = ? AND table_name = ? AND row_no = ?
              AND student_no = ?
            ORDER BY delivery_item_id
            """,
            (project_id, table_name, row_no, old_student_no),
        ).fetchall()
        for delivered in delivered_items:
            conn.execute(
                """
                INSERT INTO correction_items
                    (project_id, split_id, delivery_item_id, delivery_id,
                     table_name, row_no, original_row_no, old_student_no,
                     new_student_no, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    split_id,
                    int(delivered["delivery_item_id"]),
                    int(delivered["delivery_id"]),
                    table_name,
                    row_no,
                    int(delivered["project_row_no"]),
                    old_student_no,
                    new_student_no,
                    now,
                ),
            )
            correction_count += 1

    conn.execute(
        "UPDATE splits SET correction_count = ? WHERE split_id = ?",
        (correction_count, split_id),
    )
    return _split_response(conn, split_id)


def list_splits(
    conn: sqlite3.Connection, project_id: str, limit: int, offset: int
) -> dict[str, Any]:
    require_project(conn, project_id)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM splits WHERE project_id = ?", (project_id,)
    ).fetchone()["n"]
    ids = conn.execute(
        """
        SELECT split_id FROM splits WHERE project_id = ?
        ORDER BY split_id LIMIT ? OFFSET ?
        """,
        (project_id, limit, offset),
    ).fetchall()
    return {
        "total": int(total),
        "items": [_split_response(conn, int(r["split_id"])) for r in ids],
    }


def get_split(
    conn: sqlite3.Connection, project_id: str, split_id: int
) -> dict[str, Any]:
    require_project(conn, project_id)
    row = conn.execute(
        "SELECT split_id FROM splits WHERE split_id = ? AND project_id = ?",
        (split_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "split_not_found", "拆分记录不存在")
    return _split_response(conn, split_id)


# ---------------------------------------------------------------------------
# Correction sheets
# ---------------------------------------------------------------------------

def correction_sheet(
    conn: sqlite3.Connection,
    project_id: str,
    split_id: int | None,
    delivery_id: int | None,
) -> dict[str, Any]:
    require_project(conn, project_id)
    where = ["project_id = ?"]
    params: list[Any] = [project_id]

    if split_id is not None:
        split = conn.execute(
            "SELECT split_id FROM splits WHERE split_id = ? AND project_id = ?",
            (split_id, project_id),
        ).fetchone()
        if split is None:
            raise ApiError(404, "split_not_found", "拆分记录不存在")
        where.append("split_id = ?")
        params.append(split_id)

    if delivery_id is not None:
        delivery = conn.execute(
            "SELECT delivery_id FROM deliveries WHERE delivery_id = ? AND project_id = ?",
            (delivery_id, project_id),
        ).fetchone()
        if delivery is None:
            raise ApiError(404, "delivery_not_found", "交付记录不存在")
        where.append("delivery_id = ?")
        params.append(delivery_id)

    if split_id is not None:
        scope = "split"
    elif delivery_id is not None:
        scope = "delivery"
    else:
        scope = "project"

    rows = conn.execute(
        f"""
        SELECT correction_id, split_id, table_name, original_row_no,
               old_student_no, new_student_no, delivery_id, recorded_at
        FROM correction_items
        WHERE {' AND '.join(where)}
        ORDER BY correction_id
        """,
        params,
    ).fetchall()
    lines = [
        {
            "correction_id": int(r["correction_id"]),
            "split_id": int(r["split_id"]),
            "table_name": r["table_name"],
            "original_row_no": int(r["original_row_no"]),
            "old_student_no": int(r["old_student_no"]),
            "new_student_no": int(r["new_student_no"]),
            "delivery_id": int(r["delivery_id"]),
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]
    return {
        "project_id": project_id,
        "scope": scope,
        "split_id": split_id,
        "delivery_id": delivery_id,
        "line_count": len(lines),
        "digest_sha256": fingerprint(lines),
        "lines": lines,
    }
