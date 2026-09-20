"""Approximate camera coordinates by geocoding a place name and then
pinning the answer onto the road the camera is on.

Two sources need this. Traffic Wales and TrafficWatchNI both publish camera
names and road numbers but no coordinates - the real ones sit behind a
subscriber feed and a CSRF-gated endpoint respectively - so both fall back
to OpenStreetMap. Geocoding alone is not enough: a camera label resolves to
a village centre at best, and to a same-named place at the other end of the
country at worst. Snapping the hit onto the road's own geometry fixes both,
and gives a distance to sanity-check against - a geocode that lands miles
from the road the camera is on is a mismatch, not a position.

Northern Ireland's cameras need a second trick. Most of them are named for
the junction they watch rather than a place - "Falls Road - Donegall Road"
is a crossroads, and no amount of geocoding will find it, because it isn't
a place Nominatim has a record of. But the two streets are both in OSM, and
where their geometry crosses *is* the camera's position, to within a few
metres. `locate_junction` does that.

What stays with each source is knowing how to turn its own camera labels
into something worth looking up; the conventions differ enough that there's
nothing shared to factor out. Everything downstream of that question - the
lookups, the caches, the geometry, the projection - is the same job for
both, and lives here.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Optional

import requests
from pyproj import Transformer
from shapely.geometry import MultiLineString, Point
from shapely.ops import nearest_points


class RoadSnappingGeocoder:

    #: Roads the cameras sit on. Motorways through tertiary covers the
    #: A- and B-road numbering both providers use.
    DEFAULT_HIGHWAY_TYPES = "motorway|trunk|primary|secondary|tertiary"

    def __init__(
        self,
        session: requests.Session,
        logger,
        settings: dict,
        geocode_cache_path: Path,
        road_cache_path: Path,
    ) -> None:

        self.session = session
        self.logger = logger

        self.request_timeout = settings.get("request_timeout", 20)

        self.geocode_base_url = settings["geocode_base_url"]
        self.geocode_delay = settings.get("geocode_delay_seconds", 1.0)

        self.overpass_base_url = settings["overpass_base_url"]
        self.overpass_timeout = settings.get("overpass_timeout", 180)
        self.overpass_delay = settings.get("overpass_delay_seconds", 2.0)
        self.overpass_retries = settings.get("overpass_retries", 4)
        self.overpass_max_delay = settings.get("overpass_max_delay_seconds", 60.0)
        self.junction_search_km = settings.get("junction_search_km", 4.0)
        self.road_bbox = settings["road_bbox"]
        self.road_snap_max_km = settings.get("road_snap_max_km", 5.0)
        self.highway_types = settings.get("road_highway_types", self.DEFAULT_HIGHWAY_TYPES)
        # Providers sometimes write a road differently to OSM's `ref` tag
        # (e.g. "A48M" vs "A48(M)") - too few to be worth deriving.
        self.road_ref_overrides = settings.get("road_ref_overrides", {})

        self._geocode_cache_path = geocode_cache_path
        self._geocode_cache = self._load_cache(geocode_cache_path)
        self._road_cache_path = road_cache_path
        # Drop any road number cached as having no geometry. Earlier versions
        # wrote that on an Overpass failure, and a bad regex escape did the
        # same for "A48(M)" - either way it's wrong, and left on disk it
        # would outlive the fix. Street names are kept: nothing found there
        # really does mean OSM has no street by that name.
        self._road_cache = {
            key: lines
            for key, lines in self._load_cache(road_cache_path).items()
            if lines or key.startswith("name:")
        }

        # Measuring in degrees is meaningless, so road geometry is projected
        # to British National Grid (metres) before anything is compared.
        self._to_metres = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
        self._to_degrees = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
        self._road_shapes: dict[str, Optional[MultiLineString]] = {}

    # ------------------------------------------------------------------
    # The one thing callers need
    # ------------------------------------------------------------------

    def locate(
        self,
        queries: list[str],
        road: Optional[str],
    ) -> tuple[Optional[float], Optional[float]]:
        """Try each query in turn and return the first hit that sits on
        `road`, or the first hit at all when there's no road to check it
        against. (None, None) if nothing survives.

        `queries` runs most specific first - the caller knows which of its
        labels is worth the most.
        """

        road_shape = self._road_geometry(road)

        for query in queries:

            latitude, longitude = self._geocode(query)

            if latitude is None:
                continue

            # Without geometry there's nothing to check the hit against, so
            # take it as-is rather than throwing away the only answer.
            if road_shape is None:
                return latitude, longitude

            snapped = self._nearest_point_on_road(latitude, longitude, road_shape)

            if snapped:
                return snapped

            # Nominatim found *a* place, just nowhere near this camera's
            # road - nearly always a same-named place somewhere else.
            self.logger.debug(
                "Discarded '%s' for a %s camera: %.4f,%.4f is over %.1fkm from the road",
                query, road, latitude, longitude, self.road_snap_max_km,
            )

        return None, None

    def locate_junction(
        self,
        first: str,
        second: str,
        anchor_query: str,
    ) -> Optional[tuple[float, float]]:
        """Where two named streets cross, or None if we can't pin it down.

        Overpass has no index on `name`, so asking it for a street by name
        across a whole country means scanning every way in the box - about
        25 seconds a street, which doesn't scale to a hundred of them. But
        Nominatim *is* indexed on names and answers in one. So Nominatim is
        asked first, purely to find roughly where to look, and Overpass is
        then asked only about a few kilometres around that - which is fast,
        and small enough to fetch both streets in a single query.

        The crossing itself still comes from the road geometry, so the
        position is precise even though the anchor is not.
        """

        latitude, longitude = self._geocode(anchor_query)

        if latitude is None:
            return None

        # Anchored on where Nominatim thinks the first street is, so two
        # junctions that share a street name in different towns don't share
        # a cache entry.
        key = f"junction:{first}|{second}@{latitude:.3f},{longitude:.3f}"
        streets = self._road_cache.get(key)

        if streets is None:

            streets = self._query_streets([first, second], latitude, longitude)

            if streets is None:
                return None

            self._road_cache[key] = streets
            self._save_cache(self._road_cache_path, self._road_cache)

        one = self._project(streets.get(first) or [])
        two = self._project(streets.get(second) or [])

        if one is None or two is None:
            return None

        crossing = one.intersection(two)

        if crossing.is_empty:
            return None

        # A pair of streets can cross more than once, and a crossing can be a
        # short overlapping stretch rather than a single point. The centroid
        # is the honest summary of either.
        point = crossing.centroid
        longitude, latitude = self._to_degrees.transform(point.x, point.y)

        return latitude, longitude

    def _query_streets(
        self,
        names: list[str],
        latitude: float,
        longitude: float,
    ) -> Optional[dict[str, list]]:
        """Geometry of each named street near a point, in one query."""

        # Rough degrees per km at UK latitudes - this only sets how far to
        # look, so it doesn't need to be exact.
        span_lat = self.junction_search_km / 110.6
        span_lon = self.junction_search_km / (111.3 * math.cos(math.radians(latitude)))

        bbox = (
            f"{latitude - span_lat:.4f},{longitude - span_lon:.4f},"
            f"{latitude + span_lat:.4f},{longitude + span_lon:.4f}"
        )

        clauses = "".join(
            f'way({bbox})["name"="{self._literal(name)}"]["highway"];'
            for name in dict.fromkeys(names)
        )

        elements = self._overpass(f"({clauses});")

        if elements is None:
            self.logger.warning("Overpass gave up on %s", " x ".join(names))
            return None

        streets: dict[str, list] = {name: [] for name in names}

        for element in elements:
            name = element.get("tags", {}).get("name")
            geometry = element.get("geometry") or []

            if name in streets and len(geometry) > 1:
                streets[name].append(self._vertices(geometry))

        return streets

    def save(self) -> None:
        """Flush both caches. Called once a run has finished with us."""

        self._save_cache(self._geocode_cache_path, self._geocode_cache, indent=2)
        self._save_cache(self._road_cache_path, self._road_cache)

    # ------------------------------------------------------------------
    # Caches: both are plain JSON files, both survive between runs
    # ------------------------------------------------------------------

    @staticmethod
    def _load_cache(path: Path) -> dict:

        if not path.exists():
            return {}

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _save_cache(path: Path, cache: dict, indent: Optional[int] = None) -> None:

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=indent), encoding="utf-8")

    # ------------------------------------------------------------------
    # Geocoding via Nominatim
    # ------------------------------------------------------------------

    def _geocode(self, query: str) -> tuple[Optional[float], Optional[float]]:
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
        self._save_cache(self._geocode_cache_path, self._geocode_cache, indent=2)

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

        except (requests.RequestException, ValueError) as error:
            self.logger.warning("Geocoding failed for '%s': %s", query, error)
            return None, None

        if not results:
            return None, None

        return float(results[0]["lat"]), float(results[0]["lon"])

    # ------------------------------------------------------------------
    # Road geometry from OpenStreetMap via Overpass
    # ------------------------------------------------------------------

    def _road_geometry(self, road: Optional[str]) -> Optional[MultiLineString]:
        """The shape of the road with this number, or None if we don't have
        it. Applies any spelling override the provider needs first."""

        if not road:
            return None

        return self._geometry("ref", self.road_ref_overrides.get(road, road))

    def _geometry(self, tag: str, value: str) -> Optional[MultiLineString]:
        """Geometry of every way whose `tag` is `value` - "ref" for a road
        number, "name" for a street - projected to metres."""

        key = f"{tag}:{value}"

        if key in self._road_shapes:
            return self._road_shapes[key]

        lines = self._road_cache.get(key)

        if lines is None:

            lines = self._query_overpass(tag, value)

            if lines is None:
                # Overpass never answered. Remember that for the rest of
                # this run so a street shared by several cameras doesn't pay
                # the retries again, but keep it out of the on-disk cache -
                # "we don't know" must not harden into "there's nothing
                # there" and leave the camera unplaceable forever.
                self._road_shapes[key] = None
                return None

            # An empty answer is still an answer for a street name: OSM
            # genuinely has no way called that, and asking again next run
            # won't change it. For a road number it isn't - we only ever
            # look up numbers a camera claims to be on, so nothing found
            # means the query was wrong, not the road absent. Caching that
            # would outlive the fix, so leave it to be asked again.
            if lines or tag == "name":
                self._road_cache[key] = lines
                self._save_cache(self._road_cache_path, self._road_cache)

        self._road_shapes[key] = self._project(lines)

        return self._road_shapes[key]

    def _clause(self, tag: str, value: str) -> str:
        """One `way(...)[...]` line of an Overpass query, for a road number.

        Anchored on ';' as well as the ends, because OSM concatenates the
        refs of two roads sharing a carriageway into one tag ("A470;A465")
        and the one we want can be on either side of the semicolon.
        """

        south, west, north, east = self.road_bbox

        # Escaped twice over: once so the regex treats "A48(M)"'s brackets as
        # literals, and again because Overpass strips a level of backslashes
        # off the surrounding string literal before the regex ever sees it.
        # Missing the second pass turns "A48\(M\)" into the pattern A48(M),
        # which matches the ref "A48M" - a road that doesn't exist, so the
        # lookup quietly returned nothing.
        pattern = self._literal(re.escape(value))

        return (
            f'way({south},{west},{north},{east})'
            f'["{tag}"~"(^|;){pattern}(;|$)"]'
            f'["highway"~"^({self.highway_types})(_link)?$"];'
        )

    @staticmethod
    def _literal(value: str) -> str:
        """Escape a value for an Overpass double-quoted string."""

        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _vertices(geometry: list[dict]) -> list[list[float]]:
        # 5dp is ~1m - far finer than these positions deserve, and it keeps
        # the cache file to a sane size.
        return [[round(point["lat"], 5), round(point["lon"], 5)] for point in geometry]

    def _query_overpass(self, tag: str, value: str) -> Optional[list[list[list[float]]]]:

        elements = self._overpass(self._clause(tag, value))

        if elements is None:
            self.logger.warning("Overpass gave up on %s=%s", tag, value)
            return None

        lines = [
            self._vertices(geometry)
            for geometry in (element.get("geometry") for element in elements)
            if geometry and len(geometry) > 1
        ]

        self.logger.info(
            "Fetched %d ways matching %s=%s from Overpass", len(lines), tag, value
        )

        return lines

    def _overpass(self, body: str) -> Optional[list[dict]]:
        """Run an Overpass query, or return None if it never answered.

        Overpass sheds load by returning 504s when it's busy rather than by
        queueing. Space requests out, and treat a refusal as "try again
        shortly" - the alternative is a camera silently losing its position
        because of one bad afternoon on someone else's server.
        """

        query = f"[out:json][timeout:{self.overpass_timeout}];{body}out geom;"
        delay = self.overpass_delay

        for attempt in range(1, self.overpass_retries + 2):

            time.sleep(delay)

            try:
                response = self.session.post(
                    self.overpass_base_url,
                    data={"data": query},
                    timeout=self.overpass_timeout,
                )
                response.raise_for_status()
                return response.json().get("elements", [])

            except (requests.RequestException, ValueError) as error:
                self.logger.debug(
                    "Overpass attempt %d/%d failed: %s",
                    attempt, self.overpass_retries + 1, error,
                )
                delay = min(self.overpass_delay * 2 ** attempt, self.overpass_max_delay)

        return None

    def _project(self, lines: list[list[list[float]]]) -> Optional[MultiLineString]:
        """Lat/lon polylines -> one geometry in British National Grid, whose
        units are metres, so distances come out in something meaningful."""

        if not lines:
            return None

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
