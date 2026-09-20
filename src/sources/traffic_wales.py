"""Traffic Wales source: discovers CCTV cameras from traffic.wales, which
lists cameras grouped by road on public HTML pages, each with a direct,
unauthenticated image URL - no API key or subscription required.

Genuinely public, unlike Traffic Scotland's subscriber-gated FTP feed, and
structured differently again from National Highways (no ID range to scan -
traffic.wales already groups cameras by road) and TfL (no JSON API - plain
HTML with <img> tags).

traffic.wales's own map plots cameras via a licensed third-party widget
(Elgin/one.network) authenticated with the site's own embed credentials -
reusing those isn't something we can do. Traffic Wales does publish real
camera coordinates in a DATEX II feed, but only to registered subscribers;
until that access comes through, coordinates are derived by geocoding each
camera's name with OpenStreetMap's free Nominatim search and then snapping
the result onto the camera's own road, whose geometry comes from OSM via
Overpass.

Both halves of that - the lookup and the snap - are shared with the
Northern Ireland source and live in `geocoder.py`; what stays here is
turning a traffic.wales camera label into something worth asking about.
These are still approximate positions along the road, not exact poles.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import CONFIG_DIR, USER_AGENT, section
from models import SourceCamera

from .geocoder import RoadSnappingGeocoder
from .source import Source


class TrafficWalesSource(Source):

    name = "traffic_wales"

    IMAGE_ID_RE = re.compile(r"camera(\d+)\.jpg", re.IGNORECASE)
    DIRECTION_RE = re.compile(r"\((\w+bound)\)", re.IGNORECASE)
    ROAD_LINK_RE = re.compile(r"^/(cctv-cameras|taxonomy/term)/")

    #: Direction words and road furniture that show up in camera labels but
    #: are no part of any place name Nominatim knows about.
    NAME_NOISE_RE = re.compile(
        r"\b("
        r"(north|south|east|west)bound|"
        r"on[\s-]?slip|off[\s-]?slip|slip[\s-]?road|slip|"
        r"junction|jct|interchange|roundabout|"
        r"footbridge|overbridge|underpass|culvert|gantry|lay-?by|"
        r"traffic\s+lights"
        r")\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:

        super().__init__()

        settings = section("sources")["traffic_wales"]

        self.base_url = settings["base_url"]
        self.index_path = settings["index_path"]
        self.workers = settings.get("workers", 8)
        self.request_timeout = settings.get("request_timeout", 20)

        self.session = self._build_session(USER_AGENT, workers=self.workers)

        self.geocoder = RoadSnappingGeocoder(
            self.session,
            self.logger,
            settings,
            CONFIG_DIR / "traffic_wales_geocode_cache.json",
            CONFIG_DIR / "traffic_wales_road_cache.json",
        )

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "display_name": "Traffic Wales",
            "coverage": "Wales trunk road network",
        }

    # ------------------------------------------------------------------
    # Source interface
    # ------------------------------------------------------------------

    def discover_cameras(self) -> list[SourceCamera]:
        return list(self._scan().values())

    def get_camera(self, internal_id: str) -> Optional[SourceCamera]:
        return self._scan().get(internal_id)

    def get_latest_image(self, internal_id: str, image_url: Optional[str] = None) -> Optional[bytes]:

        # Most cameras use a plain camera{id}.jpg URL, but some are prefixed
        # with their road code (e.g. a40camera7197.jpg) - not deterministic
        # enough to guess, so without a cached URL this needs a re-scan.
        if not image_url:
            camera = self.get_camera(internal_id)
            image_url = camera.image_url if camera else None

        if not image_url:
            return None

        try:
            response = self.session.get(image_url, timeout=self.request_timeout)
            response.raise_for_status()
            return response.content

        except requests.RequestException as error:
            self.logger.warning("Failed to fetch image for %s: %s", internal_id, error)
            return None

    # ------------------------------------------------------------------
    # Discovery: road index page -> one page per road -> camera <img> tags
    # ------------------------------------------------------------------


    def _road_pages(self) -> list[tuple[str, str]]:
        """[(road_label, road_page_url), ...] from the road-cameras index."""

        url = urljoin(self.base_url, self.index_path)
        response = self.session.get(url, timeout=self.request_timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        return [
            (a.get_text(strip=True), urljoin(self.base_url, a["href"]))
            for a in soup.find_all("a", href=True)
            if self.ROAD_LINK_RE.match(a["href"])
        ]

    def _cameras_on_page(self, road_label: str, page_url: str) -> list[dict]:

        try:
            response = self.session.get(page_url, timeout=self.request_timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            self.logger.warning("Failed to fetch road page %s: %s", page_url, error)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        cameras = []

        for img in soup.find_all("img"):

            src = img.get("src") or ""
            match = self.IMAGE_ID_RE.search(src)

            if not match:
                continue

            cameras.append({
                "id": match.group(1),
                "image_url": src,
                "alt": (img.get("alt") or "").strip(),
                "road": road_label,
            })

        return cameras

    def _scan(self) -> dict[str, SourceCamera]:

        cameras: dict[str, SourceCamera] = {}

        with ThreadPoolExecutor(max_workers=self.workers) as executor:

            futures = [
                executor.submit(self._cameras_on_page, road_label, page_url)
                for road_label, page_url in self._road_pages()
            ]

            for future in as_completed(futures):

                for raw_camera in future.result():

                    # A camera near a junction can appear on more than one
                    # road's page - first one found wins, it's still the
                    # same physical camera.
                    if raw_camera["id"] not in cameras:
                        cameras[raw_camera["id"]] = self._normalise(raw_camera)

        # Locating hits two external, rate-limited services (Nominatim and
        # Overpass) - do it as a separate sequential pass rather than inside
        # the concurrent scan above, and only for cameras and roads not
        # already cached from a past run.
        for camera in cameras.values():
            camera.latitude, camera.longitude = self._locate(camera.name, camera.road)

        self.geocoder.save()

        return cameras

    def _normalise(self, raw_camera: dict) -> SourceCamera:

        alt = raw_camera["alt"]

        direction_match = self.DIRECTION_RE.search(alt)
        direction = direction_match.group(1).capitalize() if direction_match else None

        # "J24 Coldra (Eastbound) Camera" -> "J24 Coldra"
        name = self.DIRECTION_RE.sub("", alt)
        name = re.sub(r"\s*Camera\s*$", "", name, flags=re.IGNORECASE).strip()

        return SourceCamera(
            internal_id=raw_camera["id"],
            name=name or None,
            latitude=None,
            longitude=None,
            road=raw_camera["road"],
            direction=direction,
            image_url=raw_camera["image_url"],
        )

    # ------------------------------------------------------------------
    # Locating: turn a camera label into queries worth geocoding, and let
    # the shared geocoder pin the answer onto the camera's road.
    # ------------------------------------------------------------------

    def _clean_name(self, name: Optional[str]) -> Optional[str]:
        """Reduce a camera label to just the place name in it, which is the
        only part Nominatim has any chance with (e.g. "A465 - Dowlais
        Eastbound Slip" -> "Dowlais", "South of Raglan Junction" -> "Raglan").

        Returns None when nothing but road furniture is left - "J29 - J30
        Eastbound" names a stretch of carriageway, not a place, and asking
        Nominatim about it only invites a wrong answer.
        """

        if not name:
            return None

        # Nominatim matches the typewriter apostrophe far more reliably than
        # the typographic one traffic.wales uses ("Taff's Well").
        cleaned = name.replace("’", "'")

        # Leading road code: "A449 - Abernant", "A48(M) - Pentwyn".
        cleaned = re.sub(r"^[A-Z]\d+[A-Z]?(\(M\))?\s*[-–]\s*", "", cleaned)
        # "West of J32 Coryton" -> "J32 Coryton"
        cleaned = re.sub(r"^(north|south|east|west)\s+of\s+", "", cleaned, flags=re.IGNORECASE)
        # "... Onslip from A470" - drop the road being joined, not just its code.
        cleaned = re.sub(r"\b(from|to)\s+[AM]\d{1,4}(\(M\))?\b", " ", cleaned, flags=re.IGNORECASE)
        # Junction numbers and road codes anywhere else in the label.
        cleaned = re.sub(r"\bJ\d+[A-Z]?\b", " ", cleaned)
        cleaned = re.sub(r"\b[AM]\d{1,4}(\(M\))?\b", " ", cleaned)

        cleaned = self.NAME_NOISE_RE.sub(" ", cleaned)

        # Trailing position markers left over once the furniture has gone:
        # "Taff's Well North 2" -> "Taff's Well".
        cleaned = re.sub(r"\s*\d+\s*$", "", cleaned)
        cleaned = re.sub(r"\b(north|south|east|west)\b\s*$", "", cleaned, flags=re.IGNORECASE)

        # Tidy up the separators all that removal leaves behind.
        cleaned = re.sub(r"[\s\-–/]+", " ", cleaned).strip(" -/")

        return cleaned or None

    def _locate(self, name: Optional[str], road: Optional[str]) -> tuple[Optional[float], Optional[float]]:
        """Approximate coordinates for one camera.

        Deliberately no bare "{road}, Wales, UK" query. Nominatim happily
        answers it, but with one arbitrary point on a road that can be 100km
        long - every M4 camera whose name didn't resolve used to pile up on
        the Prince of Wales Bridge. A camera with no position is honest; a
        camera confidently placed 50km from where it is, is not.
        """

        cleaned_name = self._clean_name(name)

        queries = []

        if name:
            queries.append(f"{name}, Wales, UK")
        if cleaned_name and cleaned_name != name:
            queries.append(f"{cleaned_name}, Wales, UK")

        return self.geocoder.locate(queries, road)
