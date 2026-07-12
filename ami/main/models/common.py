import logging
from typing import Final

from django.core.files.storage import default_storage
from django.db.models import Q

logger = logging.getLogger(__name__)

# Constants
_POST_TITLE_MAX_LENGTH: Final = 80

# Ordering for "best machine prediction" selection used by
# OccurrenceQuerySet.with_best_machine_prediction(). Terminal classifications win
# over non-terminal, then highest score, with pk as the deterministic tiebreaker.
BEST_MACHINE_PREDICTION_ORDER: Final = ("-terminal", "-score", "-pk")

# Ordering for "best identification" selection used by Occurrence.best_identification,
# OccurrenceQuerySet.with_verification_info(), and best_identification_from_prefetch().
# Most recent non-withdrawn identification wins, with pk as the deterministic tiebreaker.
BEST_IDENTIFICATION_ORDER: Final = ("-created_at", "-pk")


def bbox_is_null(bbox) -> bool:
    """In-memory equivalent of null_detections_q() for an already-fetched bbox value."""
    return bbox is None


def null_detections_q(prefix: str = "") -> Q:
    """
    Return a Q expression matching null-marker Detection rows, optionally prefixed
    for use across relations (e.g. null_detections_q("images__detections__") for an
    aggregate filter on a parent table). For Detection queries directly, prefer
    Detection.objects.null_markers() / .valid() instead.

    Null markers are stored as SQL NULL (bbox IS NULL); that is the only sentinel form.
    """
    return Q(**{f"{prefix}bbox__isnull": True})


# Single source of truth for "this Detection is a null marker", shared by
# DetectionQuerySet.valid() / .null_markers(). Defined via null_detections_q() so the
# constant and the helper cannot drift apart.
NULL_DETECTIONS_FILTER = null_detections_q()


def get_media_url(path: str) -> str:
    """
    If path is a full URL, return it as-is.
    Otherwise, join it with the MEDIA_URL setting.
    """
    # @TODO use settings
    # urllib.parse.urljoin(settings.MEDIA_URL, self.path)
    if path.startswith("http"):
        url = path
    else:
        # @TODO add a file field to the Detection model and use that to get the URL
        url = default_storage.url(path.lstrip("/"))
    return url


as_choices = lambda x: [(i, i) for i in x]  # noqa: E731
