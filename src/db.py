"""SQLite-backed storage for OpenHighways cameras - the primary source of
truth. Cameras are matched on (source, internal_id); master_id is assigned
once by the database (via AUTOINCREMENT, which never reuses an id) and stays
with a camera even if it later disappears from its source.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from config import DATABASE_FILE
from models import CameraRecord, SourceCamera

logger = logging.getLogger("openhighway.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    master_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    internal_id TEXT NOT NULL,
    name TEXT,
    road TEXT,
    direction TEXT,
    latitude REAL,
    longitude REAL,
    image_url TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    vehicles INTEGER,
    last_seen TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, internal_id)
);

CREATE TABLE IF NOT EXISTS camera_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    master_camera_id INTEGER NOT NULL REFERENCES cameras (master_id),
    timestamp TEXT NOT NULL,
    image_url TEXT,
    local_path TEXT,
    vehicle_count INTEGER
);

-- Every history read/write filters by camera and orders by time - without
-- this, both scale linearly with the whole table's size, not just one
-- camera's history, once there are thousands of cameras.
CREATE INDEX IF NOT EXISTS idx_camera_images_camera_ts
    ON camera_images (master_camera_id, timestamp);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Open a connection, creating the containing directory if needed.

    Pass ":memory:" for a throwaway in-memory database (used by tests).
    """

    db_path = db_path if db_path is not None else DATABASE_FILE

    if db_path != ":memory:":
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if db_path != ":memory:":
        # WAL lets the API read while the pipeline/watcher writes without
        # either side blocking; busy_timeout retries instead of raising
        # immediately if a write does briefly collide with another connection.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _row_to_record(row: sqlite3.Row) -> CameraRecord:

    return CameraRecord(
        master_id=row["master_id"],
        source=row["source"],
        internal_id=row["internal_id"],
        name=row["name"],
        road=row["road"],
        direction=row["direction"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        image_url=row["image_url"],
        active=bool(row["active"]),
        vehicles=row["vehicles"],
        last_seen=row["last_seen"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def upsert_camera(
    conn: sqlite3.Connection,
    source: str,
    camera: SourceCamera,
    seen_at: Optional[str] = None,
) -> int:
    """Insert a new camera, or update an existing one matched on
    (source, internal_id). Returns the camera's master_id either way - a
    fresh one for a never-seen-before camera, the existing one otherwise.
    """

    seen_at = seen_at or _now()

    conn.execute(
        """
        INSERT INTO cameras (
            source, internal_id, name, road, direction,
            latitude, longitude, image_url, active,
            last_seen, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT (source, internal_id) DO UPDATE SET
            name = excluded.name,
            road = excluded.road,
            direction = excluded.direction,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            image_url = excluded.image_url,
            active = 1,
            last_seen = excluded.last_seen,
            updated_at = excluded.updated_at
        """,
        (
            source, camera.internal_id, camera.name, camera.road, camera.direction,
            camera.latitude, camera.longitude, camera.image_url,
            seen_at, seen_at, seen_at,
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT master_id FROM cameras WHERE source = ? AND internal_id = ?",
        (source, camera.internal_id),
    ).fetchone()

    return row["master_id"]


def deactivate_missing(
    conn: sqlite3.Connection,
    source: str,
    seen_internal_ids: Iterable[str],
    seen_at: Optional[str] = None,
) -> int:
    """Mark active cameras of `source` as inactive if this run didn't see
    them - never deletes, so master IDs are never reused."""

    seen_at = seen_at or _now()
    seen_ids = list(seen_internal_ids)

    if seen_ids:
        placeholders = ",".join("?" for _ in seen_ids)
        query = (
            f"UPDATE cameras SET active = 0, updated_at = ? "
            f"WHERE source = ? AND active = 1 AND internal_id NOT IN ({placeholders})"
        )
        params = [seen_at, source, *seen_ids]
    else:
        query = "UPDATE cameras SET active = 0, updated_at = ? WHERE source = ? AND active = 1"
        params = [seen_at, source]

    cursor = conn.execute(query, params)
    conn.commit()

    return cursor.rowcount


def get_camera(conn: sqlite3.Connection, source: str, internal_id: str) -> Optional[CameraRecord]:

    row = conn.execute(
        "SELECT * FROM cameras WHERE source = ? AND internal_id = ?",
        (source, internal_id),
    ).fetchone()

    return _row_to_record(row) if row else None


def get_camera_by_master_id(conn: sqlite3.Connection, master_id: int) -> Optional[CameraRecord]:

    row = conn.execute(
        "SELECT * FROM cameras WHERE master_id = ?",
        (master_id,),
    ).fetchone()

    return _row_to_record(row) if row else None


def list_cameras(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    active_only: bool = False,
) -> list[CameraRecord]:

    query = "SELECT * FROM cameras WHERE 1 = 1"
    params: list = []

    if source is not None:
        query += " AND source = ?"
        params.append(source)

    if active_only:
        query += " AND active = 1"

    query += " ORDER BY master_id"

    rows = conn.execute(query, params).fetchall()

    return [_row_to_record(row) for row in rows]


def set_vehicle_count(
    conn: sqlite3.Connection,
    master_id: int,
    vehicle_count: Optional[int],
    timestamp: Optional[str] = None,
) -> None:
    """Cache a camera's latest vehicle count for quick reads by the API."""

    conn.execute(
        "UPDATE cameras SET vehicles = ?, updated_at = ? WHERE master_id = ?",
        (vehicle_count, timestamp or _now(), master_id),
    )
    conn.commit()


def add_camera_image(
    conn: sqlite3.Connection,
    master_id: int,
    image_url: Optional[str] = None,
    local_path: Optional[str] = None,
    vehicle_count: Optional[int] = None,
    timestamp: Optional[str] = None,
) -> int:

    timestamp = timestamp or _now()

    cursor = conn.execute(
        """
        INSERT INTO camera_images (master_camera_id, timestamp, image_url, local_path, vehicle_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (master_id, timestamp, image_url, local_path, vehicle_count),
    )
    conn.commit()

    return cursor.lastrowid


def list_camera_images(
    conn: sqlite3.Connection,
    master_id: int,
    limit: Optional[int] = None,
) -> list[dict]:
    """A camera's snapshot/vehicle-count history, oldest first - shaped for
    the frontend's traffic history graph (`t`/`v` keys)."""

    query = (
        "SELECT timestamp, vehicle_count FROM camera_images "
        "WHERE master_camera_id = ? ORDER BY timestamp DESC"
    )
    params: list = [master_id]

    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    return [
        {"t": row["timestamp"], "v": row["vehicle_count"]}
        for row in reversed(rows)
    ]


def prune_camera_images(conn: sqlite3.Connection, master_id: int, keep: int) -> int:
    """Delete all but the most recent `keep` history rows for a camera, so
    the table doesn't grow forever."""

    cursor = conn.execute(
        """
        DELETE FROM camera_images
        WHERE master_camera_id = ? AND id NOT IN (
            SELECT id FROM camera_images
            WHERE master_camera_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        )
        """,
        (master_id, master_id, keep),
    )
    conn.commit()

    return cursor.rowcount


def record_vehicle_observation(
    conn: sqlite3.Connection,
    master_id: int,
    image_url: Optional[str],
    local_path: Optional[str],
    vehicle_count: Optional[int],
    keep_history: int,
    timestamp: Optional[str] = None,
) -> None:
    """Insert a history row, prune old ones, and cache the latest count on
    the camera - one commit instead of three, since the watcher does this
    for every camera on every cycle."""

    timestamp = timestamp or _now()

    conn.execute(
        """
        INSERT INTO camera_images (master_camera_id, timestamp, image_url, local_path, vehicle_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (master_id, timestamp, image_url, local_path, vehicle_count),
    )

    conn.execute(
        """
        DELETE FROM camera_images
        WHERE master_camera_id = ? AND id NOT IN (
            SELECT id FROM camera_images
            WHERE master_camera_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        )
        """,
        (master_id, master_id, keep_history),
    )

    # A null count is written through too: it means the camera was
    # unavailable this time, and must clear the old count rather than
    # leave the map showing traffic from before it went down.
    conn.execute(
        "UPDATE cameras SET vehicles = ?, updated_at = ? WHERE master_id = ?",
        (vehicle_count, timestamp, master_id),
    )

    conn.commit()


def export_to_json(conn: sqlite3.Connection, path: Union[str, Path], source: Optional[str] = None) -> int:
    """Dump the database to JSON - useful for debugging, backups and testing.
    Not required for normal operation; the database is the source of truth."""

    records = list_cameras(conn, source=source)

    payload = {
        "exported_at": _now(),
        "camera_count": len(records),
        "cameras": [
            {
                "master_id": record.master_id,
                "source": record.source,
                "internal_id": record.internal_id,
                "name": record.name,
                "road": record.road,
                "direction": record.direction,
                "latitude": record.latitude,
                "longitude": record.longitude,
                "image_url": record.image_url,
                "active": record.active,
                "vehicles": record.vehicles,
                "last_seen": record.last_seen,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            for record in records
        ],
    }

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)

    return len(records)
