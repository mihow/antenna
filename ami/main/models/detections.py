import logging
import typing
from typing import final

from django.db import models

from ami.base.models import BaseModel, BaseQuerySet
from ami.ml.schemas import BoundingBox

from .common import NULL_DETECTIONS_FILTER, get_media_url
from .source_images import SourceImage

if typing.TYPE_CHECKING:
    from .classifications import Classification
    from .occurrences import Occurrence

logger = logging.getLogger(__name__)


class DetectionQuerySet(BaseQuerySet):
    def valid(self):
        """
        Detections suitable for consumer queries — excludes null-marker sentinels.

        Null markers are rows that record "an algorithm ran against this image and
        found nothing." Consumers asking "give me detections" should always go
        through .valid(). Future predicates to fold in here: soft-delete tombstones,
        detections missing an algorithm reference, detections missing classifications.
        """
        return self.exclude(NULL_DETECTIONS_FILTER)

    def null_markers(self):
        """
        Sentinel rows that record "this algorithm ran against this image and found
        nothing." Only relevant for SourceImage-level "has this been processed?"
        questions. Detection consumers should use .valid() instead.
        """
        return self.filter(NULL_DETECTIONS_FILTER)


class DetectionManager(models.Manager.from_queryset(DetectionQuerySet)):
    pass


@final
class Detection(BaseModel):
    """An object detected in an image"""

    project_accessor = "source_image__project"
    source_image = models.ForeignKey(
        SourceImage,
        on_delete=models.CASCADE,
        related_name="detections",
    )

    # @TODO use structured data for bbox
    bbox = models.JSONField(null=True, blank=True)

    # @TODO shouldn't this be automatically set by the source image?
    timestamp = models.DateTimeField(null=True, blank=True)

    # file = (
    #     models.ImageField(
    #         null=True,
    #         blank=True,
    #         upload_to="detections",
    #     ),
    # )
    path = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=(
            "Either a full URL to a cropped detection image or a relative path to a file in the default "
            "project storage. @TODO ensure all detection crops are hosted in the project storage, "
            "not the default media storage. Migrate external URLs."
        ),
    )

    occurrence = models.ForeignKey(
        "Occurrence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="detections",
    )
    frame_num = models.IntegerField(null=True, blank=True)

    detection_algorithm = models.ForeignKey(
        "ml.Algorithm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Time that the detection was created by the algorithm in the processing service
    detection_time = models.DateTimeField(null=True, blank=True)
    # @TODO not sure if this detection score is ever used
    # I think it was intended to be the score of the detection algorithm (bbox score)
    detection_score = models.FloatField(null=True, blank=True)
    # detection_job = models.ForeignKey(
    #     "Job",
    #     on_delete=models.SET_NULL,
    #     null=True,
    # )

    similarity_vector = models.JSONField(null=True, blank=True)

    # For type hints
    classifications: models.QuerySet["Classification"]
    source_image_id: int
    detection_algorithm_id: int

    objects = DetectionManager()

    NULL_BBOX = None
    """Canonical bbox value for null markers (rows that record 'an algorithm ran but
    found nothing'). Null markers are stored as SQL NULL; use Detection.build_null_marker()
    to construct them."""

    @property
    def is_null_marker(self) -> bool:
        """True for sentinel rows representing 'no detections found by this algorithm.'"""
        return self.bbox is None

    @classmethod
    def build_null_marker(cls, source_image, detection_algorithm) -> "Detection":
        """Construct (without saving) a null-marker Detection for the given image+algorithm."""
        return cls(
            source_image=source_image,
            bbox=cls.NULL_BBOX,
            detection_algorithm=detection_algorithm,
        )

    def get_bbox(self):
        if self.bbox:
            return BoundingBox(
                x1=self.bbox[0],
                y1=self.bbox[1],
                x2=self.bbox[2],
                y2=self.bbox[3],
            )
        else:
            return None

    # def bbox(self):
    #     return (
    #         self.bbox_x,
    #         self.bbox_y,
    #         self.bbox_width,
    #         self.bbox_height,
    #     )

    # def bbox_coords(self):
    #     return (
    #         self.bbox_x,
    #         self.bbox_y,
    #         self.bbox_x + self.bbox_width,
    #         self.bbox_y + self.bbox_height,
    #     )

    # def bbox_percent(self):
    #     return (
    #         self.bbox_x / self.source_image.width,
    #         self.bbox_y / self.source_image.height,
    #         self.bbox_width / self.source_image.width,
    #         self.bbox_height / self.source_image.height,
    #     )

    def width(self) -> float | None:
        """Placeholder for queryset annotation. Use BoundingBox.from_coords() for bbox validation."""
        return None

    def height(self) -> float | None:
        """Placeholder for queryset annotation. Use BoundingBox.from_coords() for bbox validation."""
        return None

    class Meta:
        ordering = [
            "frame_num",
            "timestamp",
        ]
        indexes = [
            # Supports the "last processed" subquery on the captures list: the
            # latest detection created_at per source image (index scan, top 1).
            models.Index(fields=["source_image", "-created_at"], name="det_srcimg_created_idx"),
        ]

    def best_classification(self):
        # @TODO where is this used?
        classification = (
            self.classifications.order_by("-score")
            .select_related("determination", "determination__name", "score")
            .first()
        )
        if classification and classification.taxon:
            return (str(classification.taxon), classification.score)
        else:
            return (None, None)

    def url(self) -> str | None:
        return get_media_url(self.path) if self.path else None

    def associate_new_occurrence(self) -> "Occurrence":
        """
        Create and associate a new occurrence with this detection.
        """
        from ami.main.models import Occurrence

        if self.occurrence:
            return self.occurrence

        occurrence = Occurrence.objects.create(
            event=self.source_image.event,
            deployment=self.source_image.deployment,
            project=self.source_image.project,
        )
        self.occurrence = occurrence
        self.save()
        occurrence.save()  # Need to save again to update the aggregate values
        # Update aggregate values on source image
        # @TODO this should be done async in a task with an eta of a few seconds
        # so it isn't done for every detection in a batch
        self.source_image.save()
        return occurrence

    def occurrence_meets_criteria(self) -> bool | None:
        """
        This is added an annotated field, it should not be called directly.

        If the value is None, it means it has not been annotated and not calculated.
        In that case, no detections should be returned in the API.
        """
        return None

    def update_calculated_fields(self, save=True):
        needs_update = False
        if not self.timestamp:
            self.timestamp = self.source_image.timestamp
            needs_update = True
        if save and needs_update:
            self.save(update_calculated_fields=False)

    def save(self, update_calculated_fields=True, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.pk and update_calculated_fields:
            self.update_calculated_fields(save=True)
        # if not self.occurrence:
        #     self.associate_new_occurrence()

    def __str__(self) -> str:
        return f"#{self.pk} from SourceImage #{self.source_image_id} with Algorithm #{self.detection_algorithm_id}"
