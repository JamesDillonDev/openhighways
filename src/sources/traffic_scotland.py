"""Traffic Scotland source: CCTV cameras from Traffic Scotland's Live Eye
View (LEV) Traffic Camera Image Access Service.

Unlike the other sources this feed is NOT publicly open - it's an FTP
server for approved subscribers only, and the credentials are secrets
(TRAFFIC_SCOTLAND_FTP_USER / TRAFFIC_SCOTLAND_FTP_PASSWORD, read from the
environment or the repo's .env - never config.json). Terms of use forbid
passing them to anyone else.

The server's layout:

    /cameraimages.csv       PresentationName,ImageName,LocationX,LocationY
    /current/<ImageName>    latest JPEG per camera, refreshed every 5-10 min

LocationX/Y are British National Grid eastings/northings, so every camera
is placed straight from the CSV - no geocoding. Files in current/ that the
CSV doesn't list have no location and are ignored.

The server bans accounts/IPs automatically (see its README) for: more than
one complete download set per 10 minutes, too many logins (every file
wanted should come down in one session), not logging out, and more than 6
failed logins in 24 hours. Hence:

  * every connection is a `with` block, so QUIT is always sent;
  * `iter_latest_images` pulls a whole batch in one session, for the
    vehicle watcher, which polls this source at most once per 10 minutes;
  * a rejected login stops this source trying again until restart, rather
    than burning through the failed-login allowance.
"""

from __future__ import annotations

import csv
import io
import os
import re
from contextlib import closing
from ftplib import FTP, error_perm
from typing import Iterable, Iterator, Optional

from pyproj import Transformer

from config import section
from models import SourceCamera

from .source import Source


class TrafficScotlandSource(Source):

    name = "traffic_scotland"

    # Images aren't hotlinkable (they're behind an FTP login), and the terms
    # only allow fetching them as needed - so the vehicle watcher analyses
    # them in memory and never writes them to disk.
    keep_snapshots = False

    # The server treats more than one full download set per 10 minutes as
    # abuse - the watcher must never poll this source faster than that.
    MIN_POLL_INTERVAL_SECONDS = 600

    ROAD_RE = re.compile(r"\b((?:M|A|B)\d+(?:\(M\))?)", re.IGNORECASE)

    #: "(N)", "N/B", "NB" etc. - the only direction hints the names carry.
    DIRECTION_RE = re.compile(r"\(([NSEW])\)|\b([NSEW])\s*/?\s*B\b", re.IGNORECASE)

    #: British National Grid northings spanning Scotland, Solway to
    #: Shetland. The CSV has had rows with the easting pasted into the
    #: northing too (Auchenshuggle, placed in mid-Wales) - better unplaced
    #: than on the wrong side of the country.
    SCOTLAND_NORTHINGS = (525_000, 1_225_000)

    def __init__(self) -> None:

        super().__init__()

        settings = section("sources")["traffic_scotland"]

        self.ftp_host = settings.get("ftp_host") or None
        self.camera_list = settings.get("camera_list", "cameraimages.csv")
        self.image_directory = settings.get("image_directory", "current")
        self.request_timeout = settings.get("request_timeout", 30)
        self.poll_interval_seconds = max(
            settings.get("poll_interval_seconds", self.MIN_POLL_INTERVAL_SECONDS),
            self.MIN_POLL_INTERVAL_SECONDS,
        )

        # Credentials are secrets - never read from config.json, only env.
        self.ftp_username = os.environ.get("TRAFFIC_SCOTLAND_FTP_USER")
        self.ftp_password = os.environ.get("TRAFFIC_SCOTLAND_FTP_PASSWORD")

        self.configured = bool(self.ftp_host and self.ftp_username and self.ftp_password)

        # Set once a login is rejected - see _connect.
        self._login_rejected = False

        self._to_degrees = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "display_name": "Traffic Scotland",
            "coverage": "Scotland trunk road network",
            "configured": self.configured,
        }

    # ------------------------------------------------------------------
    # Source interface
    # ------------------------------------------------------------------

    def discover_cameras(self) -> list[SourceCamera]:

        self._require_configured()

        with self._connect() as ftp:
            buffer = io.BytesIO()
            ftp.retrbinary(f"RETR {self.camera_list}", buffer.write)

        rows = csv.DictReader(io.StringIO(buffer.getvalue().decode("utf-8-sig", errors="replace")))

        cameras = []

        for row in rows:
            camera = self._normalise(row)
            if camera is not None:
                cameras.append(camera)

        return cameras

    def get_camera(self, internal_id: str) -> Optional[SourceCamera]:

        for camera in self.discover_cameras():
            if camera.internal_id == internal_id:
                return camera

        return None

    def get_latest_image(self, internal_id: str, image_url: Optional[str] = None) -> Optional[bytes]:
        """One camera's image, in a session of its own - for the on-demand
        image proxy. Callers must cache the result briefly (see
        backend/app.py): the map re-requests an open camera's image every
        second, and each call here is a full FTP login."""

        with closing(self.iter_latest_images([internal_id])) as images:
            for _, image_bytes in images:
                return image_bytes

        return None

    def iter_latest_images(self, internal_ids: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """Yield (internal_id, image bytes) for each camera, all downloaded
        over ONE FTP session - the server counts logins, so a batch must
        never be fetched one login at a time.

        A generator so the caller can process each image as it arrives
        rather than holding hundreds in memory; the session is closed
        (QUIT sent) when iteration ends, even if the caller stops early.
        """

        if not self.configured or self._login_rejected:
            return

        try:
            with self._connect() as ftp:

                ftp.cwd(self.image_directory)

                for internal_id in internal_ids:

                    buffer = io.BytesIO()

                    try:
                        ftp.retrbinary(f"RETR {internal_id}.jpg", buffer.write)
                    except error_perm as error:
                        # Missing file - one camera gone mustn't end the batch.
                        self.logger.warning("No image for %s: %s", internal_id, error)
                        continue

                    if buffer.getbuffer().nbytes:
                        yield internal_id, buffer.getvalue()

        except Exception as error:
            self.logger.warning("Image download failed: %s", error)

    # ------------------------------------------------------------------
    # FTP access
    # ------------------------------------------------------------------

    def _require_configured(self) -> None:

        if not self.configured:
            raise RuntimeError(
                "Traffic Scotland source is not configured - set sources.traffic_scotland.ftp_host "
                "in config.json plus TRAFFIC_SCOTLAND_FTP_USER / TRAFFIC_SCOTLAND_FTP_PASSWORD "
                "in the environment (or .env)."
            )

        if self._login_rejected:
            raise RuntimeError(
                "Traffic Scotland rejected the FTP login earlier - not retrying until restart, "
                "as repeated failed logins get the account banned."
            )

    def _connect(self) -> FTP:

        ftp = FTP(self.ftp_host, timeout=self.request_timeout)

        try:
            ftp.login(self.ftp_username, self.ftp_password)
        except error_perm:
            # A 530 here means bad credentials (or an existing ban). More
            # than 6 failures a day earns a ban, so stop trying altogether.
            self._login_rejected = True
            self.logger.error("Traffic Scotland FTP login rejected - disabling this source until restart")
            ftp.close()
            raise

        return ftp

    # ------------------------------------------------------------------
    # CSV row -> SourceCamera
    # ------------------------------------------------------------------

    def _normalise(self, row: dict) -> Optional[SourceCamera]:

        image_name = (row.get("ImageName") or "").strip()
        name = (row.get("PresentationName") or "").strip()

        if not image_name:
            return None

        try:
            easting = float(row["LocationX"])
            northing = float(row["LocationY"])
        except (KeyError, TypeError, ValueError):
            easting = northing = None

        latitude = longitude = None

        low, high = self.SCOTLAND_NORTHINGS

        if easting and northing and low <= northing <= high:
            longitude, latitude = self._to_degrees.transform(easting, northing)

        road_match = self.ROAD_RE.search(name)
        direction_match = self.DIRECTION_RE.search(name)

        return SourceCamera(
            internal_id=image_name.rsplit(".", 1)[0],
            name=name or None,
            latitude=latitude,
            longitude=longitude,
            road=road_match.group(1).upper() if road_match else None,
            direction=(direction_match.group(1) or direction_match.group(2)).upper() if direction_match else None,
            # Not hotlinkable - backend/app.py proxies it on demand instead.
            image_url=None,
            extra={"filename": image_name, "easting": easting, "northing": northing},
        )
