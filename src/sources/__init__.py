from .essex import EssexSource
from .national_highways import NationalHighwaysSource
from .northern_ireland import NorthernIrelandSource
from .source import Source
from .tfl import TfLSource
from .traffic_scotland import TrafficScotlandSource
from .traffic_wales import TrafficWalesSource

#: every source OpenHighways knows how to build, keyed by its `name`
AVAILABLE_SOURCES: dict[str, type[Source]] = {
    EssexSource.name: EssexSource,
    NationalHighwaysSource.name: NationalHighwaysSource,
    NorthernIrelandSource.name: NorthernIrelandSource,
    TfLSource.name: TfLSource,
    TrafficScotlandSource.name: TrafficScotlandSource,
    TrafficWalesSource.name: TrafficWalesSource,
}


def load_sources(names: list[str] | None = None) -> list[Source]:
    """Instantiate the requested sources (default: all registered sources)."""

    names = names or list(AVAILABLE_SOURCES)

    unknown = [name for name in names if name not in AVAILABLE_SOURCES]

    if unknown:
        raise ValueError(
            f"Unknown source(s): {', '.join(unknown)}. "
            f"Available: {', '.join(AVAILABLE_SOURCES)}"
        )

    return [AVAILABLE_SOURCES[name]() for name in names]


__all__ = [
    "Source", "EssexSource", "NationalHighwaysSource", "NorthernIrelandSource", "TfLSource",
    "TrafficScotlandSource", "TrafficWalesSource",
    "AVAILABLE_SOURCES", "load_sources",
]
