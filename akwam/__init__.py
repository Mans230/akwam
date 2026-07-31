"""حزمة سكرابر موقع أكوام (akwam) — عميل async + parsers خالصة."""

from .client import AkwamClient, NotFoundError
from .models import (
    DirectLink,
    Episode,
    EpisodeDetails,
    MovieDetails,
    QualityLink,
    SearchResult,
    SeriesDetails,
)

__all__ = [
    "AkwamClient",
    "NotFoundError",
    "SearchResult",
    "QualityLink",
    "MovieDetails",
    "Episode",
    "SeriesDetails",
    "EpisodeDetails",
    "DirectLink",
]
