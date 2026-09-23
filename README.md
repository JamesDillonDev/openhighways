# OpenHighways

Collects public traffic CCTV camera metadata and live images from across the
UK, counts vehicles in each feed with a computer-vision model, and shows it
all on a live map.

## Sources

Each source is a self-contained module under [`src/sources/`](src/sources)
that knows how to discover its own cameras, locate them, and fetch their
images - the rest of the system never has provider-specific logic in it.

| Source              | Coverage                                  | Status                                                          |
| -------------------- | ------------------------------------------ | ---------------------------------------------------------------- |
| `national_highways`  | England strategic road network (M/A roads) | Active - public, unauthenticated                                  |
| `tfl`                | Greater London (JamCams)                   | Active - public, unauthenticated (optional `TFL_APP_KEY` for a higher rate limit) |
| `traffic_wales`      | Wales trunk road network                   | Active - public, unauthenticated (coordinates approximated by geocoding camera names, then snapping them onto the camera's own road) |
| `northern_ireland`   | Northern Ireland trunk road network        | Active - public, unauthenticated (TrafficWatchNI; coordinates approximated via OpenStreetMap geocoding) |
| `traffic_scotland`   | Scotland trunk road network                | Approved-subscriber FTP (LEV service) - needs `TRAFFIC_SCOTLAND_FTP_USER`/`TRAFFIC_SCOTLAND_FTP_PASSWORD` in `.env` or the environment; images are proxied on demand, never stored (see `src/sources/traffic_scotland.py`) |

## Project structure

```
src/
  config.json            # single shared config: a section per script/source, plus global flags
  config.py               # loads config.json - modules import settings from here
  db.py                    # SQLite storage - the primary source of truth for camera data
  models.py                # typed Camera/SourceCamera data models
  pipeline.py               # provider-agnostic master pipeline: sources -> database
  sync_sources.py            # CLI entry point - runs the pipeline for one or all sources
  vehicle_watcher.py          # polls camera feeds and counts vehicles with a CV model
  vehicle_detector.py           # the single-frame vehicle detector (YOLOX ONNX model)
  sources/
    source.py                   # the Source base class every provider implements
    national_highways.py         # National Highways
    tfl.py                        # Transport for London
    traffic_wales.py               # Traffic Wales
    northern_ireland.py             # TrafficWatchNI (Northern Ireland)
    traffic_scotland.py             # Traffic Scotland (LEV FTP; placed from cameraimages.csv)
backend/
  app.py                 # Flask API serving camera data (from the database) to the frontend
  openapi.py              # the API's OpenAPI description, served at /api/docs
frontend/                 # React + Leaflet map UI
```

## Requirements

- Python 3
- `requests`, `beautifulsoup4` (HTML-scraping sources)
- `shapely`, `pyproj` (National Highways road-geometry matching)
- `opencv-python`, `numpy` (vehicle detection)
- `flask`, `flask-cors`, `flask-swagger-ui` (`backend/app.py`)
- Node.js (`frontend/`)

Install with:

```powershell
pip install requests beautifulsoup4 shapely pyproj opencv-python numpy flask flask-cors flask-swagger-ui
cd frontend; npm install
```

## Usage

The quickest way to bring everything up is [`start-dev.ps1`](start-dev.ps1)
from the repo root, which syncs sources into the database then launches the
backend, vehicle watcher and frontend each in their own window:

```powershell
.\start-dev.ps1
```

Flags: `-SkipSync` (skip the source sync - use if the database is already
populated), `-SkipWatcher`, `-SkipInstall` (skip `npm install`).

### Sync camera sources into the database

```powershell
cd src
python .\sync_sources.py
python .\sync_sources.py --sources tfl,traffic_wales
```

Runs every configured [source](#sources) (or just the ones named), matching
cameras on `(source, internal_id)`: new cameras get a permanent `master_id`,
existing ones are updated in place, and cameras no longer reported by a
source are marked `active = 0` rather than deleted - a `master_id` is never
reused. A failure in one source (e.g. Traffic Scotland, until it's
configured) never stops the others.

### Watch traffic levels

```powershell
cd src
python .\vehicle_watcher.py
```

Runs forever (Ctrl+C to stop). Every `vehicle_watcher.interval_seconds`
(default 60s) it fetches every active camera's current image and runs it
through a YOLOX object-detection model (`vehicle_detector.py`) to count
cars/motorcycles/buses/trucks directly in that single frame - no frame
history or per-camera warmup needed, so it works even on feeds that rarely
refresh. The count is written to that camera's `vehicles` field, and a
timestamped history point is kept (capped at `max_history_points`) for the
frontend's traffic-history graph. Only the latest snapshot per camera is
kept on disk (`config/camera_images/{master_id}.jpg`), not a full archive.

### Run the map frontend

```powershell
cd backend
python .\app.py
```

```powershell
cd frontend
npm run dev
```

The Flask API (`backend/app.py`, port 5000) serves `/api/cameras` and
`/api/cameras/<id>/history` straight from the SQLite database - each
camera's `image_url` points at its source directly, so the browser loads
feed images itself rather than through the backend. The React app
(`frontend/`, port 5173) plots every located camera on a map, colour-scaled
from blue (few/no vehicles) to red (heavy traffic); a per-source checkbox
panel lets you show/hide cameras by provider. Clicking a marker opens a
panel with the live image (click it to enlarge) and traffic history. The
frontend polls `/api/cameras` every 30 seconds and refreshes open camera
images every 15 seconds.

## API

The backend is a public, read-only HTTP API, and the map is just one client
of it - anyone can call it directly. It needs no API key and nothing to sign
up for, and CORS is open to any origin, so a browser app on another domain
can call it without a server of its own in the middle.

Interactive documentation (Swagger UI) is served by the API itself at
**`/api/docs`**, with the OpenAPI description behind it at
`/api/openapi.json` - point a client generator at that URL if you'd rather
not write the requests by hand.

| Endpoint                        | Returns                                                            |
| -------------------------------- | ------------------------------------------------------------------- |
| `GET /api/cameras`               | Every active, located camera with its latest vehicle count           |
| `GET /api/cameras/{id}/history`  | One camera's vehicle counts over time, oldest first                  |
| `GET /api/cameras/{id}/image`    | One camera's current image, fetched from its provider                |
| `GET /api/openapi.json`          | The OpenAPI 3 description of all of the above                        |

Two things worth knowing before building on it:

- **Load camera images from each camera's own `image_url`, not through
  `/api/cameras/{id}/image`.** That URL normally points straight at the
  provider's image host, which is what keeps this cheap to run; the proxy
  endpoint exists only for TrafficWatchNI, whose image host rejects requests
  that aren't referred from its own site, and those cameras already have
  their `image_url` set to it.
- **The providers' terms follow the data.** Each source sets conditions on
  reuse and several specify the exact wording (see the credits panel on the
  map, or the Attribution section in `/api/docs`) - anything built on this
  API has to carry the same credits.

The spec lives in [`backend/openapi.py`](backend/openapi.py), written by
hand rather than generated from the route decorators - update it there when
an endpoint or a field changes.

## Database

[`src/db.py`](src/db.py) is the primary source of truth (SQLite,
`src/config/openhighway.sqlite3`) - not the old JSON files. Two tables:

- **`cameras`** - one row per camera, keyed by `master_id` (a permanent,
  auto-incrementing OpenHighways ID that's never reused) plus
  `(source, internal_id)` for matching a provider's own camera ID.
  `active` tracks whether the last sync still saw this camera; `vehicles`
  caches its latest count.
- **`camera_images`** - a timestamped `vehicle_count` history per camera,
  pruned to `max_history_points`.

`db.export_to_json()` can dump the database to JSON for debugging/backups,
but normal operation never reads or writes JSON directly.

## Adding a new source

1. Create `sources/<name>.py` with a class inheriting from `Source`
   (see [`sources/source.py`](src/sources/source.py) for the interface -
   `discover_cameras`, `get_camera`, `get_latest_image`).
2. Register it in [`sources/__init__.py`](src/sources/__init__.py)'s
   `AVAILABLE_SOURCES`.
3. Add a `sources.<name>` section to `config.json` for anything it needs
   (base URL, timeouts, etc.) - secrets like API keys/passwords should come
   from environment variables instead, never config.json (see `tfl.py`'s
   `TFL_APP_KEY` or `traffic_scotland.py`'s FTP credentials for examples).

The database, master ID assignment and pipeline never need to change.

## Configuration

All settings live in [src/config.json](src/config.json): a `global` section,
one section per source under `sources`, plus `vehicle_watcher` and `api`.

| Section                          | Setting              | Description                                        |
| --------------------------------- | ---------------------- | ----------------------------------------------------- |
| `global`                          | `config_dir`          | Directory (relative to `src/`) for the database/images |
| `global`                          | `database_file`       | SQLite filename                                        |
| `global`                          | `user_agent`          | User-Agent header sent with every request              |
| `master`                          | `default_steps`       | Default step(s) run when none are specified            |
| `sources.national_highways`       | `base_url`            | Base URL of the camera feed site                       |
| `sources.national_highways`       | `start_id` / `end_id` | Camera ID range to scan                                |
| `sources.national_highways`       | `workers`             | Concurrent threads used for scraping                   |
| `sources.national_highways`       | `feature_server`      | National Highways Network Model FeatureServer URL      |
| `sources.tfl`                     | `base_url`            | TfL Unified API base URL                               |
| `sources.traffic_wales`           | `base_url`/`index_path` | Road-cameras index page                              |
| `sources.traffic_wales`           | `geocode_base_url`    | Nominatim endpoint used to approximate coordinates     |
| `sources.traffic_wales`           | `geocode_delay_seconds` | Delay between geocoding requests (rate-limit friendly) |
| `sources.traffic_wales`           | `overpass_base_url`   | Overpass endpoint road geometry is fetched from        |
| `sources.traffic_wales`           | `road_bbox`           | Bounding box road geometry is searched within          |
| `sources.traffic_wales`           | `road_snap_max_km`    | Geocodes further than this from the road are discarded |
| `sources.traffic_wales`           | `road_ref_overrides`  | Road names traffic.wales and OSM spell differently     |
| `sources.northern_ireland`        | `junction_search_km`  | How far around a street to look for its crossing       |
| `sources.traffic_scotland`        | `ftp_host`/`camera_list`/`image_directory`/`poll_interval_seconds` | FTP feed location and watcher poll rate (never below 600s; credentials via env vars) |
| `vehicle_watcher`                 | `interval_seconds`    | How often to re-check every camera (seconds)           |
| `vehicle_watcher`                 | `workers`             | Concurrent threads used for fetching images             |
| `vehicle_watcher`                 | `input_size`          | Detector input resolution (smaller = faster, less accurate) |
| `vehicle_watcher`                 | `confidence_threshold`/`nms_threshold` | Detection thresholds                  |
| `vehicle_watcher`                 | `max_history_points`  | History points kept per camera                         |
| `api`                              | `host` / `port`       | Where `backend/app.py` listens                         |

`src/config.py` just loads `config.json` and exposes it to the modules - edit
`config.json` to change any setting, not `config.py`.

## Deployment

### Docker Compose (self-hosted)

`docker-compose.yml` runs four services from the same [`Dockerfile`](Dockerfile)/
[`frontend/Dockerfile`](frontend/Dockerfile): `backend`, `watcher` and
`frontend`, plus a one-off `sync` service (`docker compose run --rm sync`).
`backend` and `watcher` share one Docker volume (`openhighway-data`) mounted
at `/app/src/config`, so they both read/write the same SQLite database.

```powershell
docker compose up -d --build
```

### Fly.io

[`fly.toml`](fly.toml)/[`Dockerfile.fly`](Dockerfile.fly) run everything -
built frontend, Flask API and vehicle watcher - on a single, cheap Fly
Machine. `backend/app.py` runs the source sync and vehicle watcher as
background threads in the same gunicorn process (`RUN_BACKGROUND_TASKS=1`,
see [`fly-start.sh`](fly-start.sh)) rather than as separate `python ...`
processes - three separate processes each re-paying the full
numpy/opencv/shapely/pyproj import cost was enough to OOM-kill the Machine
on its own. `backend/app.py` also serves the built frontend itself whenever
`frontend/dist` exists in the image, so there's no separate frontend host
or CORS setup needed. A 1 GB [volume](https://fly.io/docs/volumes/overview/)
is mounted at `/app/src/config` for the SQLite database, camera image cache
and geocode caches - like Render's disks, Fly volumes only attach to one
Machine, which is why everything runs together here rather than as
separate services.

The startup sync only runs when the database is empty, so a redeploy won't
re-scan a volume that already has cameras. When a source changes *where* it
puts its cameras, though, those existing rows are stale and nothing will
notice. Name the affected sources in `FORCE_STARTUP_SYNC` to re-sync just
those on the next boot, and clear it once it has run:

```bash
fly secrets set FORCE_STARTUP_SYNC=traffic_wales,northern_ireland
fly secrets unset FORCE_STARTUP_SYNC
```

```powershell
fly launch --no-deploy   # first time only - creates the app, skips auto-deploy
fly deploy
```

`auto_stop_machines = "off"` in `fly.toml` is important - the watcher must
keep running even when there's no HTTP traffic, so the Machine can't be
allowed to idle-stop the way a typical stateless web app would. Optionally
set `TFL_APP_KEY` and/or the Traffic Scotland FTP credentials with
`fly secrets set TFL_APP_KEY=...` (secrets, not `[env]` in `fly.toml`).
Locally they go in a gitignored `.env` at the repo root, which
`src/config.py` loads and `docker-compose.yml` passes through.

Traffic Scotland's FTP server auto-bans accounts that log in too often or
download more than one full set per 10 minutes. That's why the watcher polls
it over a single session no more than once every 10 minutes, and why the
image proxy keeps each viewed image in memory for 5 minutes.

**After every `fly deploy`**, verify the Machine actually got the memory
`[[vm]]` in `fly.toml` specifies - `fly deploy` on an *existing* Machine has
not been reliably applying `[[vm]]` changes in practice:

```powershell
fly machine status <id> --display-config | Select-String memory_mb
fly scale memory 1024   # if it shows 512 instead
```

## Troubleshooting

- **`NameResolutionError` / `getaddrinfo failed` on every request** — this is
  a DNS/network connectivity issue on your machine, not a bug in the script.
  Check your internet connection and try again.
