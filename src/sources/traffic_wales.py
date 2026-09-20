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

The snap is what makes the geocode usable. On its own Nominatim resolves a
camera label to whatever it can find - often a village centre, sometimes a
same-named place on the other side of Wales - and a bare road name to one
arbitrary point on a road that may be 100km long. Snapping pins each camera
to the nearest point on the road it is actually on, and a geocode that
lands further than `road_snap_max_km` from that road is discarded as a
mismatch rather than trusted. These are still approximate positions along
the road, not exact pole locations.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pyproj import Transformer
from shapely.geometry import MultiLineString, Point
from shapely.ops import nearest_points

from config import CONFIG_DIR, USER_AGENT, section
from models import SourceCamera

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

        self.geocode_base_url = settings["geocode_base_url"]
        self.geocode_delay = settings.get("geocode_delay_seconds", 1.0)
        self._geocode_cache_path = CONFIG_DIR / "traffic_wales_geocode_cache.json"
        self._geocode_cache = self._load_geocode_cache()

        self.overpass_base_url = settings["overpass_base_url"]
        self.overpass_timeout = settings.get("overpass_timeout", 180)
        self.road_bbox = settings["road_bbox"]
        self.road_snap_max_km = settings.get("road_snap_max_km", 5.0)
        # traffic.wales writes some road names differently to OSM's `ref`
        # tag (e.g. "A48M" vs "A48(M)") - too few to be worth deriving.
        self.road_ref_overrides = settings.get("road_ref_overrides", {})
        self._road_cache_path = CONFIG_DIR / "traffic_wales_road_cache.json"
        self._road_cache = self._load_road_cache()
        # Measuring in degrees is meaningless, so road geometry is projected
        # to British National Grid (metres) before anything is compared.
        self._to_metres = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
        self._to_degrees = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
        self._road_shapes: dict[str, Optional[MultiLineString]] = {}

        self.session = self._build_session(USER_AGENT, workers=self.workers)

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
            camera.latitude, camera.longitude = self._geocode(camera.name, camera.road)

        self._save_geocode_cache()

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
    # Geocoding: approximate coordinates via OpenStreetMap's free Nominatim
    # search, cached to disk since it's rate-limited to ~1 request/second.
    # ------------------------------------------------------------------

    def _load_geocode_cache(self) -> dict:

        if not self._geocode_cache_path.exists():
            return {}

        try:
            return json.loads(self._geocode_cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_geocode_cache(self) -> None:

        self._geocode_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._geocode_cache_path.write_text(
            json.dumps(self._geocode_cache, indent=2), encoding="utf-8"
        )

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

    def _geocode(self, name: Optional[str], road: Optional[str]) -> tuple[Optional[float], Optional[float]]:
        """Approximate coordinates for one camera: geocode its name, then pin
        the result to the road the camera is on.

        Deliberately no bare "{road}, Wales, UK" fallback. Nominatim happily
        answers it, but with one arbitrary point on a road that can be 100km
        long - every M4 camera whose name didn't resolve used to pile up on
        the Prince of Wales Bridge. A camera with no position is honest; a
        camera confidently placed 50km from where it is, is not.
        """

        cleaned_name = self._clean_name(name)

        attempts = []

        if name:
            attempts.append(f"{name}, Wales, UK")
        if cleaned_name and cleaned_name != name:
            attempts.append(f"{cleaned_name}, Wales, UK")

        road_shape = self._road_geometry(road)

        for query in attempts:

            latitude, longitude = self._geocode_once(query)

            if latitude is None:
                continue

            # Without geometry there's nothing to check the hit against, so
            # take it as-is rather than throwing away the only answer.
            if road_shape is None:
                return latitude, longitude

            snapped = self._nearest_point_on_road(latitude, longitude, road_shape)

            if snapped:
                return snapped

            # Nominatim found *a* place, but nowhere near this camera's road
            # - almost always a same-named place elsewhere in Wales.
            self.logger.debug(
                "Discarded '%s' for %s camera: %.4f,%.4f is over %.1fkm from the road",
                query, road, latitude, longitude, self.road_snap_max_km,
            )

        return None, None

    def _geocode_once(self, query: str) -> tuple[Optional[float], Optional[float]]:
        """One Nominatim lookup, served from the on-disk cache when possible."""

        if query in self._geocode_cache:
            cached = self._geocode_cache[query]
            return (cached["lat"], cached["lon"]) if cached else (None, None)

        latitude, longitude = self._query_nominatim(query)

        self._geocode_cache[query] = (
            {"lat": latitude, "lon": longitude} if latitude is not None else None
        )
        # Persist as we go - geocoding hundreds of cameras at ~1 req/sec
        # takes minutes, and an interrupted run shouldn't have to redo the
        # lookups it already paid for.
        self._save_geocode_cache()

        return latitude, longitude

    def _query_nominatim(self, query: str) -> tuple[Optional[float], Optional[float]]:

        try:
            # Nominatim's usage policy caps unauthenticated use at ~1
            # request/second - only hit the network for a real cache miss.
            time.sleep(self.geocode_delay)

            response = self.session.get(
                self.geocode_base_url,
                params={"q": query, "format": "json", "limit": 1},
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            results = response.json()

        except requests.RequestException as error:
            self.logger.warning("Geocoding failed for '%s': %s", query, error)
            return None, None

        if not results:
            return None, None

        return float(results[0]["lat"]), float(results[0]["lon"])

    # ------------------------------------------------------------------
    # Road geometry: each road's shape from OpenStreetMap via Overpass,
    # used to pull a loose geocode onto the road the camera is really on.
    # ------------------------------------------------------------------

    def _load_road_cache(self) -> dict:

        if not self._road_cache_path.exists():
            return {}

        try:
            return json.loads(self._road_cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_road_cache(self) -> None:

        self._road_cache_path.parent.mkdir(parents=True, exist_ok=True)
        # No indent - this is bulk coordinate data, not something to read.
        self._road_cache_path.write_text(
            json.dumps(self._road_cache), encoding="utf-8"
        )

    def _road_geometry(self, road: Optional[str]) -> Optional[MultiLineString]:
        """`road`'s shape, projected to metres and ready to measure against,
        or None if we have no geometry for it."""

        if not road:
            return None

        ref = self.road_ref_overrides.get(road, road)

        if ref in self._road_shapes:
            return self._road_shapes[ref]

        lines = self._road_cache.get(ref)

        if lines is None:

            lines = self._query_overpass(ref)

            # Only cache a real answer. Overpass is frequently overloaded,
            # and caching one 504 would silently leave every camera on this
            # road unsnapped on every future run.
            if lines:
                self._road_cache[ref] = lines
                self._save_road_cache()

        self._road_shapes[ref] = self._project(lines) if lines else None

        return self._road_shapes[ref]

    def _project(self, lines: list[list[list[float]]]) -> MultiLineString:
        """Lat/lon polylines -> one geometry in British National Grid, whose
        units are metres, so distances come out in something meaningful."""

        projected = []

        for line in lines:
            # Transform each way's vertices in one call - pyproj is far
            # quicker over a sequence than point by point, and a road can
            # run to tens of thousands of vertices.
            eastings, northings = self._to_metres.transform(
                [vertex[1] for vertex in line],
                [vertex[0] for vertex in line],
            )
            projected.append(list(zip(eastings, northings)))

        return MultiLineString(projected)

    def _query_overpass(self, ref: str) -> list[list[list[float]]]:

        south, west, north, east = self.road_bbox

        # Anchored on ';' as well as the ends because OSM concatenates the
        # refs of two roads sharing a carriageway into one tag ("A470;A465").
        query = f"""
            [out:json][timeout:{self.overpass_timeout}];
            way({south},{west},{north},{east})
                ["ref"~"(^|;){re.escape(ref)}(;|$)"]
                ["highway"~"^(motorway|trunk|primary)(_link)?$"];
            out geom;
        """

        try:
            response = self.session.post(
                self.overpass_base_url,
                data={"data": query},
                timeout=self.overpass_timeout,
            )
            response.raise_for_status()
            elements = response.json().get("elements", [])

        except (requests.RequestException, ValueError) as error:
            self.logger.warning("Overpass lookup failed for %s: %s", ref, error)
            return []

        lines = [
            # 5dp is ~1m - far finer than these positions deserve, and it
            # keeps the cache file to a sane size.
            [[round(point["lat"], 5), round(point["lon"], 5)] for point in geometry]
            for geometry in (element.get("geometry") for element in elements)
            if geometry and len(geometry) > 1
        ]

        self.logger.info("Fetched %d stretches of %s from Overpass", len(lines), ref)

        return lines

    def _nearest_point_on_road(
        self,
        latitude: float,
        longitude: float,
        road: MultiLineString,
    ) -> Optional[tuple[float, float]]:
        """Closest point on `road` to the given position, or None if the road
        never comes within `road_snap_max_km` of it."""

        camera = Point(self._to_metres.transform(longitude, latitude))

        if road.distance(camera) > self.road_snap_max_km * 1000:
            return None

        snapped = nearest_points(road, camera)[0]
        longitude, latitude = self._to_degrees.transform(snapped.x, snapped.y)

        return latitude, longitude
