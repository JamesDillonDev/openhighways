"""Vehicle watcher: polls every active camera's current image and counts
vehicles with a single-frame object detector, then writes the result and a
local snapshot reference into the database.

Provider-agnostic like the rest of OpenHighways - it only ever calls
`Source.get_latest_image()`, never anything provider-specific. Runs forever
(Ctrl+C to stop); not part of sync_sources.py's one-shot sync since it never
exits.
"""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

import db
from config import CONFIG_DIR, section
from models import CameraRecord
from sources import Source, load_sources
from vehicle_detector import VehicleDetector

logger = logging.getLogger("openhighway.vehicle_watcher")

SETTINGS = section("vehicle_watcher")

INTERVAL = SETTINGS["interval_seconds"]
WORKERS = SETTINGS["workers"]
MAX_HISTORY_POINTS = SETTINGS["max_history_points"]

IMAGE_DIR = CONFIG_DIR / SETTINGS["image_dir"]
MODEL_DIR = CONFIG_DIR / SETTINGS["model_dir"]


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Bundled with the code rather than in CONFIG_DIR: Fly and docker-compose
# mount their data volume over src/config, which would hide anything
# shipped there.
PLACEHOLDER_DIR = Path(__file__).parent / "placeholders"


def _load_placeholder_hashes() -> set[str]:
    """Providers serve a stock image instead of erroring when a camera is
    down - National Highways' "unavailable" card, Traffic Scotland's
    "Currently Unavailable" and "In Operational Use" (the latter byte-for-
    byte identical across every camera showing it). Hash every one in
    PLACEHOLDER_DIR once, so each cycle can spot them without an OpenCV
    pass - there's nothing to count, and a count of the graphic itself
    would colour the map as if it were traffic."""

    return {_hash_bytes(path.read_bytes()) for path in PLACEHOLDER_DIR.glob("*.jpg")}


PLACEHOLDER_HASHES = _load_placeholder_hashes()


def _decode_frame(image_bytes: bytes) -> Optional[np.ndarray]:

    array = np.frombuffer(image_bytes, dtype=np.uint8)

    # Decode at full resolution - the detector letterboxes to a fixed
    # input_size regardless, so this costs nothing extra at inference time,
    # but a reduced decode was throwing away detail (especially on already
    # low-res cameras) before that resize ever happened.
    return cv2.imdecode(array, cv2.IMREAD_COLOR)


def _save_snapshot(master_id: int, image_bytes: bytes) -> str:
    """Overwrite this camera's one on-disk snapshot - only the latest image
    is ever kept, not a full history, to avoid unbounded disk growth."""

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGE_DIR / f"{master_id}.jpg"
    path.write_bytes(image_bytes)

    return str(path)


def _fetch_camera_image(camera: CameraRecord, source: Source) -> Optional[bytes]:
    return source.get_latest_image(camera.internal_id, image_url=camera.image_url)


def _fetch_batch(
    executor: ThreadPoolExecutor,
    batch: list[tuple[CameraRecord, Source]],
) -> list[tuple[CameraRecord, bytes]]:
    """Fetch one batch of cameras' images concurrently - pure network I/O,
    no OpenCV involved, so this is safe to parallelise heavily."""

    fetched = []

    future_to_camera = {
        executor.submit(_fetch_camera_image, camera, source): camera
        for camera, source in batch
    }

    for future in as_completed(future_to_camera):

        camera = future_to_camera[future]

        try:
            image_bytes = future.result()
        except Exception as error:
            logger.warning("Camera %s (%s): fetch failed: %s", camera.master_id, camera.source, error)
            continue

        if not image_bytes:
            continue

        fetched.append((camera, image_bytes))

    return fetched


def _is_placeholder(image_bytes: bytes) -> bool:
    return _hash_bytes(image_bytes) in PLACEHOLDER_HASHES


def _process_image(conn, camera: CameraRecord, image_bytes: bytes, detector: VehicleDetector, keep_snapshot: bool) -> bool:
    """Count one fetched image - or, for a placeholder, clear the camera's
    count so the map shows it grey (no data) rather than keeping whatever
    colour it had before it went down. Placeholders add no history point:
    the chart shows a gap, not a dip to zero."""

    if _is_placeholder(image_bytes):
        db.set_vehicle_count(conn, camera.master_id, None)
        return False

    return _analyse_and_save(conn, camera, image_bytes, detector, keep_snapshot=keep_snapshot)


def _analyse_and_save(
    conn,
    camera: CameraRecord,
    image_bytes: bytes,
    detector: VehicleDetector,
    keep_snapshot: bool = True,
) -> bool:
    """Decode + analyse one image and write the result. Never called
    concurrently with itself - OpenCV isn't safe to hammer from many
    threads at once.

    With keep_snapshot=False the image is only ever held in memory for the
    detector and then dropped - nothing is written to disk."""

    frame = _decode_frame(image_bytes)

    if frame is None:
        return False

    local_path = _save_snapshot(camera.master_id, image_bytes) if keep_snapshot else None
    vehicle_count = detector.count_vehicles(frame)

    try:
        db.record_vehicle_observation(
            conn, camera.master_id,
            image_url=camera.image_url, local_path=local_path,
            vehicle_count=vehicle_count, keep_history=MAX_HISTORY_POINTS,
        )
        return True

    except Exception as error:
        # A DB hiccup on one camera must not derail the rest of the cycle.
        logger.warning("Camera %s: failed to save result: %s", camera.master_id, error)

    return False


#: monotonic time each source was last polled - for sources that set a
#: poll_interval_seconds longer than the watcher's own cycle
_last_polled: dict[str, float] = {}


def _due_sources(sources_by_name: dict[str, Source]) -> dict[str, Source]:
    """Sources whose poll_interval_seconds has elapsed since they were last
    polled - marking them polled now, before fetching, so a slow or failed
    fetch still counts against the provider's download limit."""

    now = time.monotonic()
    due = {}

    for name, source in sources_by_name.items():

        last = _last_polled.get(name)

        if last is None or now - last >= source.poll_interval_seconds:
            _last_polled[name] = now
            due[name] = source

    return due


def _run_streamed_source(conn, source: Source, cameras: list[CameraRecord], detector: VehicleDetector) -> int:
    """Poll a source that downloads its whole batch over one connection
    (e.g. Traffic Scotland's FTP, which bans per-image logins), analysing
    each image as it arrives rather than holding them all in memory."""

    by_internal_id = {camera.internal_id: camera for camera in cameras}
    updated = 0

    for internal_id, image_bytes in source.iter_latest_images(list(by_internal_id)):

        camera = by_internal_id.get(internal_id)

        if camera is None:
            continue

        if _process_image(conn, camera, image_bytes, detector, source.keep_snapshots):
            updated += 1

    print(f"[BATCH] {source.name}: {updated}/{len(cameras)} cameras processed")

    return updated


def run_cycle(
    conn,
    sources_by_name: dict[str, Source],
    detector: VehicleDetector,
    executor: ThreadPoolExecutor,
) -> int:

    due = _due_sources(sources_by_name)
    cameras = db.list_cameras(conn, active_only=True)
    updated = 0

    # Sources with a batch downloader are polled over a single connection
    # each, never fanned out one request per camera.
    for name, source in due.items():

        if not hasattr(source, "iter_latest_images"):
            continue

        source_cameras = [camera for camera in cameras if camera.source == name]

        if source_cameras:
            updated += _run_streamed_source(conn, source, source_cameras, detector)

    fetch_targets = [
        (camera, due[camera.source])
        for camera in cameras
        if camera.source in due and not hasattr(due[camera.source], "iter_latest_images")
    ]

    # Process in batches rather than fetching every camera before analysing
    # any of them - with thousands of cameras (and some sources serialising
    # their requests) a single all-at-once fetch phase can take minutes
    # before a single result is written, which looks like the watcher has
    # stalled. Batching means results (and map colours) stream in steadily.
    batch_size = max(WORKERS * 2, 1)

    for start in range(0, len(fetch_targets), batch_size):

        batch = fetch_targets[start:start + batch_size]
        fetched = _fetch_batch(executor, batch)

        for camera, image_bytes in fetched:
            if _process_image(conn, camera, image_bytes, detector, due[camera.source].keep_snapshots):
                updated += 1

        print(f"[BATCH] {min(start + batch_size, len(fetch_targets))}/{len(fetch_targets)} cameras processed")

    return updated


def main():

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    sources_by_name = {source.name: source for source in load_sources()}

    conn = db.get_connection()
    db.init_db(conn)

    print("Loading vehicle detection model (downloading it on first run)...")
    detector = VehicleDetector(
        MODEL_DIR,
        input_size=SETTINGS["input_size"],
        confidence_threshold=SETTINGS["confidence_threshold"],
        nms_threshold=SETTINGS["nms_threshold"],
    )

    print(f"Watching cameras every {INTERVAL}s (Ctrl+C to stop)...")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:

        try:

            while True:

                start = time.monotonic()

                try:
                    updated = run_cycle(conn, sources_by_name, detector, executor)
                    print(f"[CYCLE] {updated} camera(s) with a fresh vehicle count")
                except Exception:
                    # A bad cycle (e.g. a transient DB error) must never kill the
                    # watcher outright - log it and try again next interval.
                    logger.error("Cycle failed", exc_info=True)

                elapsed = time.monotonic() - start
                time.sleep(max(0, INTERVAL - elapsed))

        except KeyboardInterrupt:
            pass

    conn.close()


if __name__ == "__main__":
    main()
