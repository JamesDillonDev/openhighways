"""Essex Highways source: discovers CCTV cameras from essexhighways.org,
Essex County Council's public traffic camera listing.

A single HTML page lists every camera, grouped under a heading per town
(Basildon, Braintree, Chelmsford, ...), each with a direct, unauthenticated
image URL already embedded in the page - no per-camera lookup, ID range
scan, or road-geometry lookup needed to find the cameras themselves.

The page gives no coordinates and, for most cameras, no road either - just a
four-digit code and a free-text label ("1006 - A127 Noak Bridge", "1002 -
Sadlers Farm West"). Coordinates come from OpenStreetMap instead, via the
shared geocoder in `geocoder.py`: the camera's label (cleaned of any road
number) is geocoded with the town as an anchor, and snapped onto the road's
own geometry when a road number is present in the label.
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


class EssexSource(Source):

    name = "essex"

    #: "1006 - A127 Noak Bridge" -> ("1006", "A127 Noak Bridge"). The page's
    #: CMS writes the separator inconsistently (hyphen, en dash, doubled
    #: spaces, a stray &nbsp;) - collapsed to plain spaces before matching.
    TITLE_RE = re.compile(r"^(\d{3,4})\s*[-–]\s*(.+)$")

    ROAD_RE = re.compile(r"\b((?:M|A|B)\d+(?:\(M\))?)\b", re.IGNORECASE)
    ROAD_TOKEN_RE = re.compile(r"\b[AMB]\d+(?:\(M\))?\b", re.IGNORECASE)
    JUNCTION_RE = re.compile(r"\bJ\d+[A-Z]?\b", re.IGNORECASE)
    TRAILING_DIRECTION_RE = re.compile(r"\s+(north|south|east|west)$", re.IGNORECASE)

    def __init__(self) -> None:

        super().__init__()

        settings = section("sources")["essex"]

        self.base_url = settings["base_url"]
        self.index_path = settings["index_path"]
        self.request_timeout = settings.get("request_timeout", 20)

        self.session = self._build_session(USER_AGENT, workers=1)

        self.geocoder = RoadSnappingGeocoder(
            self.session,
            self.logger,
            settings,
            CONFIG_DIR / "essex_geocode_cache.json",
            CONFIG_DIR / "essex_road_cache.json",
        )

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "display_name": "Essex Highways",
            "coverage": "Essex county road network",
        }

    # ------------------------------------------------------------------
    # Source interface
    # ------------------------------------------------------------------

    def discover_cameras(self) -> list[SourceCamera]:
        return list(self._scan().values())

    def get_camera(self, internal_id: str) -> Optional[SourceCamera]:
        return self._scan().get(internal_id)

    def get_latest_image(self, internal_id: str, image_url: Optional[str] = None) -> Optional[bytes]:

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
    # Discovery: one HTML listing page, headed per town, has every camera
    # ------------------------------------------------------------------

    def _scan(self) -> dict[str, SourceCamera]:

        url = urljoin(self.base_url, self.index_path)
        response = self.session.get(url, timeout=self.request_timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        container = soup.select_one(".cctv-images") or soup

        cameras: dict[str, SourceCamera] = {}
        town: Optional[str] = None

        # The caption <p> beside each camera's image is the reliable source
        # of its "1001 - Fortune of War" label - the <img>'s own `title`
        # sometimes matches it, is sometimes missing the ID entirely, and is
        # occasionally absent altogether (07K05, in Chelmsford).
        for element in container.find_all(["h3", "p"]):

            if element.name == "h3":
                town = element.get_text(strip=True).title() or None
                continue

            raw_camera = self._parse_caption(element, town)

            if raw_camera and raw_camera["id"] not in cameras:
                cameras[raw_camera["id"]] = self._normalise(raw_camera)

        # Locating hits external, rate-limited services (Overpass and
        # Nominatim) - do it as a separate sequential pass, and only for
        # cameras and roads not already cached from a past run.
        for camera in cameras.values():
            camera.latitude, camera.longitude = self._locate(
                camera.name, camera.road, camera.extra.get("town")
            )

        self.geocoder.save()

        return cameras

    def _parse_caption(self, caption, town: Optional[str]) -> Optional[dict]:

        img = caption.parent.find("img") if caption.parent else None
        src = img.get("src") if img else None

        text = caption.get_text().replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()

        if not src:
            return None

        match = self.TITLE_RE.match(text)

        if not match:
            self.logger.debug("Couldn't parse camera caption %r", text)
            return None

        return {
            "id": match.group(1),
            "name": match.group(2).strip(),
            "image_url": urljoin(self.base_url, src),
            "town": town,
        }

    def _normalise(self, raw_camera: dict) -> SourceCamera:

        road_match = self.ROAD_RE.search(raw_camera["name"])

        return SourceCamera(
            internal_id=raw_camera["id"],
            name=raw_camera["name"] or None,
            latitude=None,
            longitude=None,
            road=road_match.group(1).upper() if road_match else None,
            direction=None,
            image_url=raw_camera["image_url"],
            extra={"town": raw_camera["town"]},
        )

    # ------------------------------------------------------------------
    # Locating: geocode the camera's label (anchored on its town), snapped
    # onto its road's geometry when a road number is known.
    # ------------------------------------------------------------------

    def _clean_name(self, name: Optional[str]) -> Optional[str]:
        """Reduce a camera label to the part worth geocoding as a place,
        e.g. "A127 Noak Bridge" -> "Noak Bridge". Road furniture with no
        place name left over at all ("M11 J7 north") reduces to nothing -
        see the comment in `_locate` for why that isn't papered over with
        the raw label instead.
        """

        if not name:
            return None

        cleaned = self.ROAD_TOKEN_RE.sub(" ", name)
        cleaned = self.JUNCTION_RE.sub(" ", cleaned)
        cleaned = self.TRAILING_DIRECTION_RE.sub("", cleaned)
        cleaned = re.sub(r"[\s\-/]+", " ", cleaned).strip(" -/")

        return cleaned or None

    def _locate(
        self,
        name: Optional[str],
        road: Optional[str],
        town: Optional[str],
    ) -> tuple[Optional[float], Optional[float]]:

        cleaned_name = self._clean_name(name)

        queries = []

        if name and town:
            queries.append(f"{name}, {town}, Essex, UK")
        if cleaned_name and cleaned_name != name:
            if town:
                queries.append(f"{cleaned_name}, {town}, Essex, UK")
            queries.append(f"{cleaned_name}, Essex, UK")
        # A townless "{name}, Essex, UK" fallback is deliberately not tried
        # when cleaning stripped the name down to nothing (e.g. "M11 J7
        # south") - that leaves only road furniture, and Nominatim answering
        # it anyway (matching on the road number alone) has produced a hit
        # nowhere near this camera, which the snap-to-road check below can't
        # catch when the bogus point happens to still sit near that same
        # (long) road. A camera with no position is honest; one confidently
        # placed elsewhere on the M11 is not.
        elif name and not town:
            queries.append(f"{name}, Essex, UK")

        return self.geocoder.locate(queries, road)
