import os
import sys
from dataclasses import asdict
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, send_from_directory
from flask_cors import CORS
from flask_swagger_ui import get_swaggerui_blueprint

from openapi import SPEC as OPENAPI_SPEC

# The scraper/config/db code lives in src/, not on the default import path.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import db  # noqa: E402
from config import USER_AGENT, section  # noqa: E402

API_SETTINGS = section("api")

# Some hosts (e.g. Fly.io) assign the port at deploy time rather than
# letting config.json hardcode it - the env var takes priority when set.
PORT = int(os.environ.get("PORT", API_SETTINGS["port"]))

# Only present when the frontend's build output was baked into this image
# (see Dockerfile.fly) - lets this one process serve the UI too, rather
# than needing a second always-on host just for static files.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

# Most sources' image URLs can be hotlinked directly by the browser (the
# default, bandwidth-cheap path - see get_cameras below). TrafficWatchNI's
# CCTV image host instead 403s any request without its own site as the
# Referer, so those images have to be fetched here and streamed back rather
# than loaded straight from the frontend.
IMAGE_PROXY_HEADERS = {
    "northern_ireland": {"Referer": "https://www.trafficwatchni.com/twni/cameras"},
}

# Where the interactive docs live, and where they read their spec from.
# Both are under /api/ so they survive the frontend catch-all route below.
DOCS_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.json"

app = Flask(__name__)

# Open to any origin, because this is a public, read-only, unauthenticated
# API and locking it to the map's own origin only stopped other people's
# browser apps from using it (see /api/docs). Nothing here reads a cookie,
# a session or an Authorization header, so there is no cross-site request
# an attacker could make from a victim's browser that they couldn't just as
# easily make from their own machine - which is what same-origin policy
# actually protects against.
#
# Scoped to /api/* rather than the whole app: the frontend this process may
# also be serving (see below) is same-origin with it and needs no CORS
# headers of its own.
#
# send_wildcard makes the response say `*` rather than echoing back whoever
# asked. Echoing means the response body varies by request origin, so a CDN
# or proxy has to cache a separate copy per caller (and fails closed for
# everyone if it doesn't); a literal `*` is one cacheable answer, and it is
# what is actually true here.
CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    send_wildcard=True,
    # Every endpoint is a read. Advertising DELETE/POST/PUT (flask-cors'
    # default) would suggest writes exist.
    methods=["GET", "HEAD", "OPTIONS"],
)

# Swagger UI for this API, so it can be read and tried out in a browser
# rather than only from the map frontend. flask_swagger_ui ships its own
# copy of Swagger UI's assets, so the docs page doesn't depend on a CDN
# being reachable (or on it still serving the same build next year).
app.register_blueprint(
    get_swaggerui_blueprint(
        DOCS_URL,
        OPENAPI_URL,
        config={
            "app_name": "OpenHighways API",
            # The spec is one small tag; expanding it saves every visitor a
            # click before they can see what the API actually offers.
            "docExpansion": "list",
            "defaultModelsExpandDepth": 2,
        },
    ),
)

# Reused across requests instead of a fresh requests.get() each time - avoids
# re-paying a TLS handshake to the upstream CDN on every single image poll.
_image_proxy_session = requests.Session()

# Ensure the schema exists even if sync_sources.py hasn't been run yet.
_startup_conn = db.get_connection()
db.init_db(_startup_conn)
_startup_conn.close()


def _run_background_tasks() -> None:
    """Run the source sync + vehicle watcher as background threads in this
    same process, instead of as separate `python ...` processes (see
    fly-start.sh). Fly's Machine kept getting OOM-killed running three
    separate Python processes, each paying the full import cost of
    numpy/opencv/shapely/pyproj again - doing it here pays that cost once.

    Opt-in via env var: docker-compose's separate backend/watcher services
    already do this, and shouldn't also run it a second time in-process.
    """

    if os.environ.get("RUN_BACKGROUND_TASKS") != "1":
        return

    import threading

    from pipeline import MasterPipeline
    from sources import load_sources

    def sync_once():
        # When a source changes how it locates its cameras, the rows already
        # on the volume are stale and nothing above will notice - the table
        # isn't empty, so the sync is skipped forever. Naming those sources
        # here re-syncs just them on the next boot.
        forced = [
            name.strip()
            for name in os.environ.get("FORCE_STARTUP_SYNC", "").split(",")
            if name.strip()
        ]

        # A restart/redeploy shouldn't force a fresh multi-minute National
        # Highways ID-range scan if the (persistent-volume) database
        # already has cameras in it - only sync when the table is empty,
        # e.g. on the very first boot against a fresh volume.
        conn = db.get_connection()
        try:
            existing = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
        finally:
            conn.close()

        if existing > 0 and not forced:
            app.logger.info("Database already has %d camera(s) - skipping startup sync", existing)
            return

        try:
            if forced:
                app.logger.info("FORCE_STARTUP_SYNC set - re-syncing %s", ", ".join(forced))

            MasterPipeline(load_sources(forced or None)).run()
        except Exception:
            app.logger.exception("Background source sync failed")

    def watch_forever():
        import vehicle_watcher
        try:
            vehicle_watcher.main()
        except Exception:
            app.logger.exception("Background vehicle watcher crashed")

    threading.Thread(target=sync_once, daemon=True).start()
    threading.Thread(target=watch_forever, daemon=True).start()


_run_background_tasks()


def _camera_dict(record):

    data = asdict(record)
    data["id"] = data.pop("master_id")

    if data["source"] in IMAGE_PROXY_HEADERS:
        data["image_url"] = f"/api/cameras/{data['id']}/image"

    return data


@app.get(OPENAPI_URL)
def get_openapi_spec():
    """This API's OpenAPI description - what /api/docs renders, and what a
    client generator would read."""

    return jsonify(OPENAPI_SPEC)


@app.get("/api/cameras")
def get_cameras():

    conn = db.get_connection()

    try:
        records = db.list_cameras(conn, active_only=True)
    finally:
        conn.close()

    # Only cameras with known coordinates can be placed on the map.
    # image_url points at the source provider directly - the frontend
    # renders it as-is rather than proxying images through this API.
    located = [
        _camera_dict(record) for record in records
        if record.latitude is not None and record.longitude is not None
    ]

    return jsonify(located)


@app.get("/api/cameras/<int:master_id>/history")
def get_camera_history(master_id):

    conn = db.get_connection()

    try:
        history = db.list_camera_images(conn, master_id)
    finally:
        conn.close()

    return jsonify(history)


@app.get("/api/cameras/<int:master_id>/image")
def get_camera_image(master_id):

    conn = db.get_connection()

    try:
        record = db.get_camera_by_master_id(conn, master_id)
    finally:
        conn.close()

    if record is None or not record.image_url:
        return "", 404

    headers = {"User-Agent": USER_AGENT, **IMAGE_PROXY_HEADERS.get(record.source, {})}

    try:
        upstream = _image_proxy_session.get(record.image_url, headers=headers, timeout=10)
        upstream.raise_for_status()
    except requests.RequestException:
        return "", 502

    return Response(upstream.content, content_type=upstream.headers.get("Content-Type", "image/jpeg"))


if FRONTEND_DIST.is_dir():

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def serve_frontend(path):
        """Serve the built frontend from this same process (see
        Dockerfile.fly) - falls back to index.html for any path that isn't
        an actual built file, e.g. a browser refresh on the app's root."""

        if path and (FRONTEND_DIST / path).is_file():
            return send_from_directory(FRONTEND_DIST, path)

        return send_from_directory(FRONTEND_DIST, "index.html")


if __name__ == "__main__":
    app.run(
        host=API_SETTINGS["host"],
        port=PORT,
        debug=True
    )
