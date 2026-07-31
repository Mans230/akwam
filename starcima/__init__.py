"""حزمة سكرابر موقع ستار سيما (starcima) — عميل async بـ JSON APIs فقط."""

from akwam.models import SearchResult

from .client import NotFoundError, StarcimaClient
from .models import (
    ExtractedStream,
    ScEpisode,
    ScMediaDetails,
    ScSeason,
    ServerLink,
)

__all__ = [
    "StarcimaClient",
    "NotFoundError",
    "SearchResult",
    "ServerLink",
    "ScMediaDetails",
    "ScSeason",
    "ScEpisode",
    "ExtractedStream",
]
