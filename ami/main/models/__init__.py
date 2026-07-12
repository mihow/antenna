"""
Domain models for the main app, split into logical submodules.

This package replaces the old monolithic ``ami/main/models.py``. Every public
name is re-exported here so ``from ami.main.models import X`` keeps working for
external code and for the module paths recorded in historic migrations.

Submodules are imported in dependency order: each module only imports earlier
modules at import time (mirroring the definition order of the original single
file); references to later modules are deferred inside function bodies.
"""

from ami.base.models import BaseModel  # noqa: F401  (re-exported for backwards compatibility)
from ami.main.models_future.projects import (  # noqa: F401  (re-exported for backwards compatibility)
    ProjectSettingsMixin,
)
from ami.users.models import User  # noqa: F401  (re-exported for backwards compatibility)

from .classifications import Classification, ClassificationManager, ClassificationQuerySet, ClassificationResult
from .collections import SourceImageCollection, SourceImageCollectionManager, SourceImageCollectionQuerySet
from .common import (
    BEST_IDENTIFICATION_ORDER,
    BEST_MACHINE_PREDICTION_ORDER,
    NULL_DETECTIONS_FILTER,
    as_choices,
    bbox_is_null,
    get_media_url,
    null_detections_q,
)
from .deployments import Deployment, DeploymentManager, Device, S3StorageSource, Site
from .detections import Detection, DetectionManager, DetectionQuerySet
from .events import (
    DEFAULT_MAX_EVENT_DURATION,
    REGROUP_LOCK_TTL_SECONDS,
    Event,
    EventManager,
    EventQuerySet,
    _regroup_lock,
    audit_event_lengths,
    delete_empty_events,
    deployment_events_need_update,
    group_images_into_events,
    sample_events,
    update_calculated_fields_for_events,
)
from .identifications import Identification, user_agrees_with_identification
from .occurrences import Occurrence, OccurrenceManager, OccurrenceQuerySet, update_occurrence_determination
from .pages import BlogPost, Page
from .projects import (
    Project,
    ProjectFeatureFlags,
    ProjectManager,
    ProjectQuerySet,
    UserProjectMembership,
    get_default_feature_flags,
    get_or_create_default_collection,
    get_or_create_default_deployment,
    get_or_create_default_device,
    get_or_create_default_project,
    get_or_create_default_research_site,
    get_project_default_filters,
)
from .source_images import (
    SourceImage,
    SourceImageManager,
    SourceImageQuerySet,
    SourceImageThumbnail,
    SourceImageUpload,
    create_source_image_from_upload,
    sample_captures_by_interval,
    sample_captures_by_nth,
    sample_captures_by_position,
    set_dimensions_for_collection,
    update_detection_counts,
    upload_to_with_deployment,
    validate_filename_timestamp,
)
from .taxonomy import (
    DEFAULT_RANKS,
    Tag,
    TaxaList,
    TaxaListManager,
    TaxaListQuerySet,
    Taxon,
    TaxonManager,
    TaxonParent,
    TaxonQuerySet,
    TaxonRank,
)

__all__ = [
    "BaseModel",
    "ProjectSettingsMixin",
    "User",
    # common
    "BEST_IDENTIFICATION_ORDER",
    "BEST_MACHINE_PREDICTION_ORDER",
    "NULL_DETECTIONS_FILTER",
    "as_choices",
    "bbox_is_null",
    "get_media_url",
    "null_detections_q",
    # projects
    "Project",
    "ProjectFeatureFlags",
    "ProjectManager",
    "ProjectQuerySet",
    "UserProjectMembership",
    "get_default_feature_flags",
    "get_or_create_default_collection",
    "get_or_create_default_deployment",
    "get_or_create_default_device",
    "get_or_create_default_project",
    "get_or_create_default_research_site",
    "get_project_default_filters",
    # deployments
    "Deployment",
    "DeploymentManager",
    "Device",
    "S3StorageSource",
    "Site",
    # events
    "DEFAULT_MAX_EVENT_DURATION",
    "REGROUP_LOCK_TTL_SECONDS",
    "Event",
    "EventManager",
    "EventQuerySet",
    "_regroup_lock",
    "audit_event_lengths",
    "delete_empty_events",
    "deployment_events_need_update",
    "group_images_into_events",
    "sample_events",
    "update_calculated_fields_for_events",
    # source_images
    "SourceImage",
    "SourceImageManager",
    "SourceImageQuerySet",
    "SourceImageThumbnail",
    "SourceImageUpload",
    "create_source_image_from_upload",
    "sample_captures_by_interval",
    "sample_captures_by_nth",
    "sample_captures_by_position",
    "set_dimensions_for_collection",
    "update_detection_counts",
    "upload_to_with_deployment",
    "validate_filename_timestamp",
    # identifications
    "Identification",
    "user_agrees_with_identification",
    # classifications
    "Classification",
    "ClassificationManager",
    "ClassificationQuerySet",
    "ClassificationResult",
    # detections
    "Detection",
    "DetectionManager",
    "DetectionQuerySet",
    # occurrences
    "Occurrence",
    "OccurrenceManager",
    "OccurrenceQuerySet",
    "update_occurrence_determination",
    # taxonomy
    "DEFAULT_RANKS",
    "Tag",
    "TaxaList",
    "TaxaListManager",
    "TaxaListQuerySet",
    "Taxon",
    "TaxonManager",
    "TaxonParent",
    "TaxonQuerySet",
    "TaxonRank",
    # pages
    "BlogPost",
    "Page",
    # collections
    "SourceImageCollection",
    "SourceImageCollectionManager",
    "SourceImageCollectionQuerySet",
]
