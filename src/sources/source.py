"""Base class every camera provider must implement.

Subclasses own everything specific to their provider - discovery, IDs,
coordinates, images. The rest of OpenHighways only ever deals with the
normalised `SourceCamera` objects returned here, never provider internals.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models import SourceCamera


class Source(ABC):

    #: stable key stored in the database's `source` column - set by subclasses
    name: str = ""

    #: whether the vehicle watcher may keep this source's latest image on
    #: disk - False for sources whose terms only allow fetching on demand
    keep_snapshots: bool = True

    #: minimum seconds between vehicle watcher polls of this source (0 =
    #: every cycle) - for providers that limit how often you may download
    poll_interval_seconds: float = 0

    def __init__(self) -> None:

        if not self.name:
            raise ValueError(f"{type(self).__name__} must set a `name`")

        self.logger = logging.getLogger(f"openhighway.sources.{self.name}")

    @abstractmethod
    def discover_cameras(self) -> list[SourceCamera]:
        """Return every camera this source currently knows about, normalised."""

    @abstractmethod
    def get_camera(self, internal_id: str) -> Optional[SourceCamera]:
        """Return a single camera by its provider-specific ID, or None."""

    @abstractmethod
    def get_latest_image(self, internal_id: str, image_url: Optional[str] = None) -> Optional[bytes]:
        """Return the latest raw image bytes for a camera, or None.

        `image_url` is an optional already-known URL (e.g. cached from a
        previous sync) - sources whose image URL is otherwise only
        discoverable via an extra lookup should use it to skip that lookup.
        """

    def metadata(self) -> dict:
        """Static info about this source - override to add more detail."""
        return {"name": self.name}

    @staticmethod
    def _build_session(user_agent: str, workers: int = 10, retries: int = 3) -> requests.Session:
        """Shared helper for a pooled, retrying requests.Session - the only
        genuinely provider-agnostic behaviour a source needs from the base class."""

        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})

        adapter = HTTPAdapter(
            pool_connections=workers,
            pool_maxsize=workers,
            max_retries=Retry(
                total=retries,
                connect=retries,
                read=retries,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                raise_on_status=False
            )
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session
