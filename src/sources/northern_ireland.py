"""Northern Ireland source: discovers CCTV cameras from trafficwatchni.com,
the Department for Infrastructure's public traffic camera site.

Simpler than Traffic Wales: a single HTML page (grouped by region in the UI,
but all regions are present in one response) lists every camera with both
its display name and a direct, unauthenticated image URL already embedded -
no per-camera page fetch, ID range scan, or road-geometry lookup needed.

TrafficWatchNI's own interactive map plots these same cameras with real
coordinates, but that data comes from a CSRF-token-gated AJAX endpoint tied
to a browser session - not worth reverse engineering for what the public
HTML listing already gives us. Coordinates come from OpenStreetMap instead,
via the shared geocoder in `geocoder.py`.

Unlike Traffic Wales, though, most of these cameras aren't at a place at
all - they watch an urban crossroads, and are named for it: "Falls Road -
Donegall Road". Geocoding that gets nowhere, because it isn't a place. So
the two street names are pulled apart and looked up in OSM instead, and
where their geometry crosses is the camera, to within a few metres. Only
the labels that aren't junctions - a stretch of the M2, a named spot in
Omagh - fall through to geocoding.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import CONFIG_DIR, USER_AGENT, section
from models import SourceCamera

from .geocoder import RoadSnappingGeocoder
from .source import Source


class NorthernIrelandSource(Source):

    name = "northern_ireland"

    ROAD_RE = re.compile(r"\b((?:M|A|B)\d+(?:\(M\))?)\b", re.IGNORECASE)
    TITLE_PREFIX_RE = re.compile(r"^View camera\s*:\s*", re.IGNORECASE)
    ONCLICK_RE = re.compile(r'addToPreview\(\d+,\s*"([^"]+)",\s*"\d+",\s*"([^"]+)"')

    #: The separators TrafficWatchNI puts between the two halves of a
    #: junction name - " - ", a slash, or a bare hyphen between words.
    JUNCTION_SPLIT_RE = re.compile(r"\s+-\s+|\s*/\s*|(?<=[a-z])-(?=[A-Z])")

    #: Shorthand used in camera labels, spelled out the way OSM writes it.
    ABBREVIATIONS = {
        r"\bRd\b": "Road",
        r"\bSt\b": "Street",
        r"\bAve?\b": "Avenue",
        r"\bDr\b": "Drive",
        r"\bLn\b": "Lane",
        r"\bSq\b": "Square",
        r"\bNth\b": "North",
        r"\bSth\b": "South",
        r"\bUpp\b": "Upper",
        r"\bC'way\b": "Causeway",
        r"\bR'bout\b": "Roundabout",
        r"\bN'ards\b": "Newtownards",
        r"\bK'Breda\b": "Knockbreda",
    }

    def __init__(self) -> None:

        super().__init__()

        settings = section("sources")["northern_ireland"]

        self.base_url = settings["base_url"]
        self.index_path = settings["index_path"]
        self.request_timeout = settings.get("request_timeout", 20)

        self.session = self._build_session(USER_AGENT, workers=1)

        self.geocoder = RoadSnappingGeocoder(
            self.session,
            self.logger,
            settings,
            CONFIG_DIR / "northern_ireland_geocode_cache.json",
            CONFIG_DIR / "northern_ireland_road_cache.json",
        )

        # cctv.trafficwatchni.com 403s any image request without its own
        # site as the Referer - unlike every other source, this isn't
        # optional metadata, so it's set once here rather than per-request.
        self.session.headers.update({"Referer": urljoin(self.base_url, self.index_path)})

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "display_name": "TrafficWatchNI",
            "coverage": "Northern Ireland trunk road network",
        }

    # ------------------------------------------------------------------
    # Source interface
    # ------------------------------------------------------------------

    def discover_cameras(self) -> list[SourceCamera]:
        return list(self._scan().values())

    def get_camera(self, internal_id: str) -> Optional[SourceCamera]:
        return self._scan().get(internal_id)

    def get_latest_image(self, internal_id: str, image_url: Optional[str] = None) -> Optional[bytes]:

        # Image URLs are stable (plain numeric filenames), so a cached one
        # skips re-fetching and re-parsing the whole camera listing.
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
    # Discovery: one HTML listing page already has every camera + image URL
    # ------------------------------------------------------------------

    def _region_by_group(self, soup: BeautifulSoup) -> dict[str, str]:
        """{"group2": "Greater Belfast", ...} from the region filter dropdown."""

        regions = {}

        for checkbox in soup.select(".dropdown-menu .form-check-input"):

            group_id = checkbox.get("data-groupid")
            label = checkbox.find_parent("label")

            if group_id and label:
                regions[group_id] = label.get_text(strip=True)

        return regions

    def _parse_fragment(self, fragment, region_by_group: dict[str, str]) -> Optional[dict]:

        camera_id = fragment.get("data-cctv-id")
        link = fragment.select_one("#cameraLink")
        button = fragment.select_one('button[onclick^="addToPreview"]')

        if not camera_id or not link or not button:
            return None

        match = self.ONCLICK_RE.search(button.get("onclick", ""))

        if not match:
            return None

        # The onclick attribute's JS string literal escapes its slashes
        # (e.g. "https:\/\/cctv.trafficwatchni.com") - unescape before use.
        image_base = match.group(1).replace("\\/", "/")
        image_file = match.group(2).replace("\\/", "/")
        name = self.TITLE_PREFIX_RE.sub("", link.get("title", "")).strip()

        group = fragment.find_parent(class_="camera-group-container")
        region = region_by_group.get(group.get("id")) if group else None

        return {
            "id": camera_id,
            "name": name or None,
            "image_url": f"{image_base}/{image_file}",
            "region": region,
        }

    def _scan(self) -> dict[str, SourceCamera]:

        url = urljoin(self.base_url, self.index_path)
        response = self.session.get(url, timeout=self.request_timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        region_by_group = self._region_by_group(soup)

        cameras: dict[str, SourceCamera] = {}

        for fragment in soup.select(".cameraFragment"):

            raw_camera = self._parse_fragment(fragment, region_by_group)

            if raw_camera:
                cameras[raw_camera["id"]] = self._normalise(raw_camera)

        # Locating hits external, rate-limited services (Overpass and
        # Nominatim) - do it as a separate sequential pass, and only for
        # cameras and streets not already cached from a past run.
        for camera in cameras.values():
            camera.latitude, camera.longitude = self._locate(
                camera.name, camera.road, camera.extra.get("region")
            )

        self.geocoder.save()

        return cameras

    def _normalise(self, raw_camera: dict) -> SourceCamera:

        road_match = self.ROAD_RE.search(raw_camera["name"] or "")
        road = road_match.group(1).upper() if road_match else None

        return SourceCamera(
            internal_id=raw_camera["id"],
            name=raw_camera["name"],
            latitude=None,
            longitude=None,
            road=road,
            direction=None,
            image_url=raw_camera["image_url"],
            extra={"region": raw_camera["region"]},
        )

    # ------------------------------------------------------------------
    # Locating: most of these cameras watch a junction rather than sit at a
    # place, so try the crossing first and fall back to geocoding a name.
    # ------------------------------------------------------------------

    def _locate(
        self,
        name: Optional[str],
        road: Optional[str],
        region: Optional[str],
    ) -> tuple[Optional[float], Optional[float]]:

        streets = self._junction_streets(name)

        if streets:

            # The first street doubles as the anchor - it tells the
            # geocoder roughly which town to search for the crossing in.
            crossing = self.geocoder.locate_junction(
                *streets, anchor_query=f"{streets[0]}, Northern Ireland, UK"
            )

            if crossing:
                return crossing

            self.logger.debug("No crossing found for %s x %s", *streets)

        cleaned_name = self._clean_name(name)

        queries = []

        if name:
            queries.append(f"{name}, Northern Ireland, UK")
        if cleaned_name and cleaned_name != name:
            queries.append(f"{cleaned_name}, Northern Ireland, UK")
        if cleaned_name and region:
            queries.append(f"{cleaned_name}, {region}, Northern Ireland, UK")

        return self.geocoder.locate(queries, road)

    def _junction_streets(self, name: Optional[str]) -> Optional[tuple[str, str]]:
        """The two street names in a junction label, or None if it isn't one.

        TrafficWatchNI writes these as "Falls Road - Donegall Road", with
        enough variation ("Orritor Street/William Street, Cookstown",
        "Donegall Square South-Adelaide Street") to be worth handling, and
        enough abbreviation ("Andersonstown Rd - Finaghy Rd Nth") that the
        halves need expanding before OSM will recognise them.
        """

        if not name:
            return None

        parts = [part for part in self.JUNCTION_SPLIT_RE.split(name) if part]

        if len(parts) < 2:
            return None

        streets = []

        for part in parts[:2]:
            # A trailing town ("..., Cookstown") and an equipment code
            # ("(0B14)") both belong to the camera, not to the street.
            part = part.split(",")[0]
            part = re.sub(r"\(.*?\)", " ", part)
            street = self._expand(part)

            # One bare word is as likely to be half a mangled name as a real
            # street, and a wrong crossing is worse than no crossing.
            if not street or " " not in street:
                return None

            streets.append(street)

        return streets[0], streets[1]

    def _expand(self, street: str) -> str:
        """Spell out the abbreviations TrafficWatchNI uses, since OSM's
        `name` tag is always written out in full."""

        expanded = street.replace("’", "'").strip()

        for pattern, full in self.ABBREVIATIONS.items():
            expanded = re.sub(pattern, full, expanded, flags=re.IGNORECASE)

        return re.sub(r"\s+", " ", expanded).strip(" -/")

    def _clean_name(self, name: Optional[str]) -> Optional[str]:
        """Strip road-prefixes/junction codes that hurt Nominatim matches
        without adding any real place information (e.g. "A2 - Tillysburn"
        -> "Tillysburn", "M1 Stockmans Lane - J2" -> "Stockmans Lane")."""

        if not name:
            return None

        cleaned = re.sub(r"^[A-Z]\d+(?:\([A-Z]\))?\s*-\s*", "", name)
        cleaned = re.sub(r"\s*-\s*J\d+[A-Z]?$", "", cleaned)
        cleaned = re.sub(r"\(.*?\)", " ", cleaned)
        cleaned = re.sub(r"\b(Junction|Jct)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned)

        return cleaned.strip() or None
