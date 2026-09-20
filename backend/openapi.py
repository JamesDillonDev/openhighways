"""The OpenAPI description of this API - served as JSON at
`/api/openapi.json` and rendered as Swagger UI at `/api/docs`.

Written by hand rather than generated from the route decorators. The API is
three read-only endpoints and rarely changes, and the things an outside
caller actually needs to know - why a camera can have no coordinates, when
`image_url` points at this API instead of the provider, what they have to
credit if they use the data - are not things a decorator scraper could
infer. Keep this in step with `app.py` when an endpoint or field changes.
"""

# Mirrors sources.AVAILABLE_SOURCES. Duplicated rather than imported: that
# package pulls in shapely/pyproj/opencv, and the API process deliberately
# avoids paying those import costs (see app.py's background tasks).
SOURCE_NAMES = [
    "national_highways",
    "tfl",
    "traffic_wales",
    "northern_ireland",
    "traffic_scotland",
]

# Each provider sets its own terms for reuse and several specify the exact
# wording, so anyone consuming this API inherits the same obligations the
# map itself carries in its credits panel. Kept identical to the frontend's
# SOURCE_CREDITS - take the wording from the source's own terms rather than
# paraphrasing it.
_ATTRIBUTION = """
## Attribution

The camera data and images come from public providers who each set terms on
reuse, and those terms follow the data through this API. Anything built on
it has to carry the same credits:

- **National Highways** - "Images from National Highways’ traffic
  management cameras. © Crown copyright."
  ([notice](https://nationalhighways.co.uk/travel-updates/traffic-cameracctv-services/crown-copyright-notice/))
- **Transport for London** - "Powered by TfL Open Data. Contains OS data
  © Crown copyright and database rights 2016. Geomni UK Map data ©
  and database rights [2019]."
  ([terms](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service))
- **Traffic Wales** - "Camera data sourced from Traffic Wales."
  ([developers](https://traffic.wales/developers))
- **TrafficWatchNI** - "Camera data from the DfI Traffic Information and
  Control Centre. © Crown copyright, licensed under the Open Government
  Licence v3.0."
  ([notice](https://www.trafficwatchni.com/twni/crown-copyright))
- **OpenStreetMap** - Welsh and Northern Irish camera positions are derived
  from OSM road geometry, licensed under the ODbL.
  ([copyright](https://www.openstreetmap.org/copyright))
"""

_DESCRIPTION = f"""
The read-only HTTP API behind the
[OpenHighways](https://github.com/JamesDillonDev/openhighways) map: public
traffic CCTV cameras from across the UK, each with its position, the road it
watches, a live image URL, and a count of the vehicles last seen in its feed.

No authentication, no API key, nothing to sign up for. Every endpoint is a
plain `GET` returning JSON, except the image proxy, which returns an image.

## Using it well

- Camera *metadata* moves slowly - a position only changes when a provider
  moves a camera. Vehicle counts refresh every couple of minutes at most, so
  polling `/api/cameras` more often than every 30 seconds gains you nothing.
  The map itself polls at exactly that rate.
- **Load feed images from the URL the camera gives you, not through this
  API.** `image_url` normally points straight at the provider's own image
  host, which is what keeps this map cheap to run. The one exception is
  documented on that field.
- This runs on a single small shared-CPU machine. There is no rate limit and
  no quota, on the assumption that nobody makes one necessary.
- CORS is open to any origin, so a page on your own domain can call this
  straight from the browser - no server of your own in the middle.

{_ATTRIBUTION}
"""

_CAMERA_EXAMPLE = {
    "id": 1402,
    "source": "tfl",
    "internal_id": "JamCams_00001.00810",
    "name": "A40 Westway / Woodfield Rd",
    "road": "A40",
    "direction": "Westbound",
    "latitude": 51.520847,
    "longitude": -0.199438,
    "image_url": "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.00810.jpg",
    "active": True,
    "vehicles": 7,
    "last_seen": "2026-09-20T17:34:36.271984+00:00",
    "created_at": "2026-09-19T17:34:36.271984+00:00",
    "updated_at": "2026-09-20T18:02:11.904312+00:00",
}

CAMERA_SCHEMA = {
    "type": "object",
    "description": "One traffic camera, as OpenHighways stores it.",
    "properties": {
        "id": {
            "type": "integer",
            "description": (
                "OpenHighways' own permanent camera ID. Assigned once and never "
                "reused, and stable across syncs even if the provider renames or "
                "re-numbers the camera - this is the ID to store if you keep your "
                "own records."
            ),
            "example": 1402,
        },
        "source": {
            "type": "string",
            "enum": SOURCE_NAMES,
            "description": "The provider this camera came from.",
            "example": "tfl",
        },
        "internal_id": {
            "type": "string",
            "description": (
                "The provider's own ID for this camera. Opaque, and only "
                "meaningful combined with `source` - two providers can use the "
                "same internal ID for entirely different cameras."
            ),
            "example": "JamCams_00001.00810",
        },
        "name": {
            "type": "string",
            "nullable": True,
            "description": "The provider's name for the camera, usually its location.",
            "example": "A40 Westway / Woodfield Rd",
        },
        "road": {
            "type": "string",
            "nullable": True,
            "description": "The road the camera watches, where the provider states one.",
            "example": "A40",
        },
        "direction": {
            "type": "string",
            "nullable": True,
            "description": (
                "Which way the camera faces. Providers word this differently - "
                "compass points (`N`), carriageway directions (`Westbound`) and "
                "junction descriptions all appear."
            ),
            "example": "Westbound",
        },
        "latitude": {
            "type": "number",
            "format": "double",
            "description": (
                "WGS84 latitude. Never null in this response - see the endpoint "
                "description."
            ),
            "example": 51.520847,
        },
        "longitude": {
            "type": "number",
            "format": "double",
            "description": "WGS84 longitude. Never null in this response.",
            "example": -0.199438,
        },
        "image_url": {
            "type": "string",
            "nullable": True,
            "description": (
                "Where to fetch this camera's latest image. Usually an absolute "
                "URL on the provider's own image host, which you should request "
                "directly.\n\n"
                "For `northern_ireland` cameras it is instead a relative path on "
                "this API (`/api/cameras/{id}/image`): that provider's image host "
                "rejects any request not referred from its own site, so those "
                "images have to be proxied. Resolve the path against the API's "
                "base URL."
            ),
            "example": "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.00810.jpg",
        },
        "active": {
            "type": "boolean",
            "description": (
                "Whether the last sync still found this camera at its provider. "
                "`/api/cameras` only returns active cameras, so this is always "
                "`true` there."
            ),
            "example": True,
        },
        "vehicles": {
            "type": "integer",
            "nullable": True,
            "description": (
                "Vehicles counted in this camera's most recent image by the "
                "detector. Null until the watcher has processed the camera at "
                "least once, or if its feed couldn't be read. A count of `0` means "
                "an image was read and no vehicles were found in it - that is not "
                "the same as null."
            ),
            "example": 7,
        },
        "last_seen": {
            "type": "string",
            "format": "date-time",
            "description": "When a sync last saw this camera at its provider (ISO 8601, UTC).",
            "example": "2026-09-20T17:34:36.271984+00:00",
        },
        "created_at": {
            "type": "string",
            "format": "date-time",
            "description": "When this camera first entered OpenHighways (ISO 8601, UTC).",
            "example": "2026-09-19T17:34:36.271984+00:00",
        },
        "updated_at": {
            "type": "string",
            "format": "date-time",
            "description": (
                "When any of this camera's stored fields last changed, including "
                "its vehicle count (ISO 8601, UTC)."
            ),
            "example": "2026-09-20T18:02:11.904312+00:00",
        },
    },
    "required": [
        "id", "source", "internal_id", "name", "road", "direction",
        "latitude", "longitude", "image_url", "active", "vehicles",
        "last_seen", "created_at", "updated_at",
    ],
    "example": _CAMERA_EXAMPLE,
}

HISTORY_POINT_SCHEMA = {
    "type": "object",
    "description": (
        "One vehicle count at one moment. The keys are deliberately short - a "
        "busy camera accumulates a lot of these."
    ),
    "properties": {
        "t": {
            "type": "string",
            "format": "date-time",
            "description": "When the image behind this count was taken (ISO 8601, UTC).",
            "example": "2026-09-20T18:02:11.904312+00:00",
        },
        "v": {
            "type": "integer",
            "nullable": True,
            "description": "Vehicles counted, or null if that image couldn't be read.",
            "example": 7,
        },
    },
    "required": ["t", "v"],
}

SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "OpenHighways API",
        "version": "1.0.0",
        "description": _DESCRIPTION,
        "license": {
            "name": "MIT - the software. The camera data carries its providers' own terms.",
            "url": "https://github.com/JamesDillonDev/openhighways/blob/main/LICENSE",
        },
    },
    # Relative, so Swagger UI's "Try it out" calls whichever deployment is
    # serving these docs rather than a hardcoded host.
    "servers": [{"url": "/", "description": "This deployment"}],
    "tags": [
        {
            "name": "Cameras",
            "description": "Camera metadata, traffic history and images.",
        },
    ],
    "paths": {
        "/api/cameras": {
            "get": {
                "tags": ["Cameras"],
                "summary": "Every camera on the map",
                "operationId": "listCameras",
                "description": (
                    "All active cameras that have a known position, across every "
                    "source, with their latest vehicle count. This is the whole "
                    "dataset in one response - thousands of cameras, a few MB of "
                    "JSON - and there is no pagination, because the map needs all "
                    "of it at once to draw itself.\n\n"
                    "Cameras the last sync no longer found at their provider are "
                    "left out, as are cameras whose position is unknown. Position "
                    "is genuinely unknown for a fair number of cameras: some "
                    "providers publish coordinates, others publish only a name "
                    "that OpenHighways has to geocode and snap onto the right "
                    "road, and that doesn't always succeed. Nothing here "
                    "distinguishes an un-locatable camera from one that no longer "
                    "exists - both are simply absent."
                ),
                "responses": {
                    "200": {
                        "description": "Every active, located camera.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Camera"},
                                },
                                "example": [_CAMERA_EXAMPLE],
                            },
                        },
                    },
                },
            },
        },
        "/api/cameras/{id}/history": {
            "get": {
                "tags": ["Cameras"],
                "summary": "A camera's traffic history",
                "operationId": "getCameraHistory",
                "description": (
                    "Vehicle counts recorded for one camera over time, oldest "
                    "first. Only a limited number of recent points is kept per "
                    "camera (`vehicle_watcher.max_history_points` in the server's "
                    "config), so this is a rolling window rather than an archive - "
                    "for long-term history, poll this and keep your own.\n\n"
                    "Points are spaced by however long the watcher takes to work "
                    "through every camera, so they are roughly, not exactly, "
                    "evenly spaced. An unknown camera ID returns an empty array "
                    "rather than a 404."
                ),
                "parameters": [{"$ref": "#/components/parameters/CameraId"}],
                "responses": {
                    "200": {
                        "description": (
                            "The camera's history, oldest first. Empty if the "
                            "camera has never been processed, or doesn't exist."
                        ),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/HistoryPoint"},
                                },
                                "example": [
                                    {"t": "2026-09-20T17:58:04.118273+00:00", "v": 4},
                                    {"t": "2026-09-20T18:02:11.904312+00:00", "v": 7},
                                ],
                            },
                        },
                    },
                },
            },
        },
        "/api/cameras/{id}/image": {
            "get": {
                "tags": ["Cameras"],
                "summary": "A camera's latest image, proxied",
                "operationId": "getCameraImage",
                "description": (
                    "Fetches this camera's current image from its provider and "
                    "streams it back.\n\n"
                    "**Prefer the camera's own `image_url`.** This endpoint exists "
                    "for the sources that can't be hotlinked - TrafficWatchNI's "
                    "image host rejects requests not referred from its own site - "
                    "and those cameras already have their `image_url` pointing "
                    "here. Routing images that don't need it through this endpoint "
                    "just moves the provider's bandwidth onto a small shared-CPU "
                    "machine, and costs you a round trip.\n\n"
                    "The image is whatever the provider currently serves; it is "
                    "fetched on each request and not cached here."
                ),
                "parameters": [{"$ref": "#/components/parameters/CameraId"}],
                "responses": {
                    "200": {
                        "description": (
                            "The provider's image, with its own content type (in "
                            "practice always JPEG)."
                        ),
                        "content": {
                            "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                        },
                    },
                    "404": {
                        "description": "No such camera, or it has no image URL. The body is empty.",
                    },
                    "502": {
                        "description": (
                            "The provider's image host couldn't be reached, timed "
                            "out, or returned an error. The body is empty."
                        ),
                    },
                },
            },
        },
    },
    "components": {
        "schemas": {
            "Camera": CAMERA_SCHEMA,
            "HistoryPoint": HISTORY_POINT_SCHEMA,
        },
        "parameters": {
            "CameraId": {
                "name": "id",
                "in": "path",
                "required": True,
                "description": "The camera's OpenHighways ID, as returned by `/api/cameras`.",
                "schema": {"type": "integer", "minimum": 1},
                "example": 1402,
            },
        },
    },
}
