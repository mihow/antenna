import datetime
import functools
import logging
import typing
from typing import final

from django.db import models
from django.db.models import Exists, OuterRef, Q
from rest_framework.request import Request

import ami.tasks
import ami.utils
from ami.base.models import BaseModel, BaseQuerySet
from ami.main.models_future.filters import (
    build_occurrence_default_filters_q,
    build_occurrence_score_threshold_q,
    build_taxa_recursive_filter_q,
)
from ami.utils.requests import get_apply_default_filters_flag, get_default_classification_threshold

from .classifications import Classification
from .common import BEST_IDENTIFICATION_ORDER, BEST_MACHINE_PREDICTION_ORDER, get_media_url
from .deployments import Deployment
from .detections import Detection
from .events import Event
from .identifications import Identification
from .projects import Project
from .source_images import SourceImage

if typing.TYPE_CHECKING:
    from .taxonomy import Taxon

logger = logging.getLogger(__name__)


class OccurrenceQuerySet(BaseQuerySet):
    def valid(self):
        """
        Occurrences fit to surface in API responses: at least one real detection AND
        a determination set.

        Excludes:
          - Occurrences with no detections at all (empty occurrences)
          - Occurrences whose only detections are null-marker sentinels (Issue #1310:
            field bug created phantom occurrences with no real bounding box backing
            them)
          - Occurrences with determination__isnull=True (no taxonomic identification,
            same field bug shape)
        """
        has_valid_detection = Exists(Detection.objects.valid().filter(occurrence_id=OuterRef("pk")))
        return self.filter(has_valid_detection).exclude(determination__isnull=True)

    def with_detections_count(self):
        return self.annotate(detections_count=models.Count("detections", distinct=True))

    def with_timestamps(self):
        """
        These are timestamps used for filtering and ordering in the UI.
        """
        return self.annotate(
            first_appearance_timestamp=models.Min("detections__timestamp"),
            last_appearance_timestamp=models.Max("detections__timestamp"),
            first_appearance_time=models.Min("detections__timestamp__time"),
            duration=models.ExpressionWrapper(
                models.F("last_appearance_timestamp") - models.F("first_appearance_timestamp"),
                output_field=models.DurationField(),
            ),
        )

    def with_identifications(self):
        return self.prefetch_related(
            "identifications",
            "identifications__taxon",
            "identifications__user",
        )

    def with_list_prefetches(self):
        """Add prefetches the list serializer needs (detection paths, classifications)."""
        from ami.main.models_future.occurrence import prefetch_detections_for_list

        return self.prefetch_related(prefetch_detections_for_list())

    def with_detail_prefetches(self):
        """Add prefetches the detail serializer needs (detections + source_image + classifications)."""
        from ami.main.models_future.occurrence import prefetch_detections_for_detail

        return self.prefetch_related(prefetch_detections_for_detail())

    def with_best_detection(self):
        """
        Annotate the queryset with fields from the best detection.
        The best detection is the one with the highest classification score.

        Adds the following annotations:
        - best_detection_path: The path to the detection image
        - best_detection_bbox: The bounding box of the detection as a list [x1, y1, x2, y2]
        - best_detection_capture_path: The path of the source capture image
        - best_detection_capture_public_base_url: The public base URL of the source capture image
        """
        # Subquery to get the path of the best detection
        # Use id as secondary sort to ensure deterministic results
        best_detection_path_subquery = (
            Detection.objects.filter(occurrence=OuterRef("pk"))
            .order_by("-classifications__score", "id")
            .values("path")[:1]
        )

        # Subquery to get the bbox of the best detection
        # Use id as secondary sort to ensure deterministic results
        best_detection_bbox_subquery = (
            Detection.objects.filter(occurrence=OuterRef("pk"))
            .order_by("-classifications__score", "id")
            .values("bbox")[:1]
        )

        # Subquery to get the source capture path and public_base_url for the best detection
        best_detection_capture_path_subquery = (
            Detection.objects.filter(occurrence=OuterRef("pk"))
            .order_by("-classifications__score", "id")
            .values("source_image__path")[:1]
        )
        best_detection_capture_public_base_url_subquery = (
            Detection.objects.filter(occurrence=OuterRef("pk"))
            .order_by("-classifications__score", "id")
            .values("source_image__public_base_url")[:1]
        )

        return self.annotate(
            best_detection_path=models.Subquery(best_detection_path_subquery),
            best_detection_bbox=models.Subquery(best_detection_bbox_subquery),
            best_detection_capture_path=models.Subquery(best_detection_capture_path_subquery),
            best_detection_capture_public_base_url=models.Subquery(best_detection_capture_public_base_url_subquery),
        )

    def with_best_machine_prediction(self):
        """
        Annotate the queryset with fields from the best machine prediction.

        Uses BEST_MACHINE_PREDICTION_ORDER to pick the winner: terminal classifications
        first, then highest score, with pk as the deterministic tiebreaker.

        Adds the following annotations:
        - best_machine_prediction_name: The taxon name of the best prediction
        - best_machine_prediction_score: The confidence score
        - best_machine_prediction_algorithm: The algorithm name
        - best_machine_prediction_taxon_id: The taxon ID (for determination_matches comparison)
        """
        best_prediction_subquery = Classification.objects.filter(detection__occurrence=OuterRef("pk")).order_by(
            *BEST_MACHINE_PREDICTION_ORDER
        )

        return self.annotate(
            best_machine_prediction_name=models.Subquery(best_prediction_subquery.values("taxon__name")[:1]),
            best_machine_prediction_score=models.Subquery(best_prediction_subquery.values("score")[:1]),
            best_machine_prediction_algorithm=models.Subquery(best_prediction_subquery.values("algorithm__name")[:1]),
            best_machine_prediction_taxon_id=models.Subquery(best_prediction_subquery.values("taxon_id")[:1]),
        )

    def with_verification_info(self):
        """
        Annotate the queryset with verification/identification fields.

        Adds the following annotations:
        - verified_by_name: The name of the user who made the best identification
        - participant_count: The count of distinct users who made non-withdrawn identifications
        - agreed_with_algorithm_name: The algorithm name the identifier agreed with
        - agreed_with_user_email: The email of the prior identifier the best identification agreed with
        """
        best_identification_subquery = Identification.objects.filter(
            occurrence=OuterRef("pk"), withdrawn=False
        ).order_by(*BEST_IDENTIFICATION_ORDER)

        return self.annotate(
            verified_by_name=models.Subquery(best_identification_subquery.values("user__name")[:1]),
            participant_count=models.Count(
                "identifications__user",
                filter=Q(identifications__withdrawn=False),
                distinct=True,
            ),
            agreed_with_algorithm_name=models.Subquery(
                best_identification_subquery.values("agreed_with_prediction__algorithm__name")[:1]
            ),
            agreed_with_user_email=models.Subquery(
                best_identification_subquery.values("agreed_with_identification__user__email")[:1]
            ),
        )

    def unique_taxa(self, project: Project | None = None):
        qs = self
        if project:
            qs = self.filter(project=project)
        qs = (
            qs.filter(determination__isnull=False, event__isnull=False)
            .order_by("determination_id")
            .distinct("determination_id")
        )
        return qs

    def filter_by_score_threshold(self, project: Project | None = None, request: Request | None = None):
        """
        Filter occurrences by score threshold.

        This is a convenience method for applying only the score threshold filter.
        Respects the apply_defaults flag - if False, filtering is bypassed.
        """
        if project is None:
            return self

        # Check if default filters should be bypassed
        if get_apply_default_filters_flag(request) is False:
            return self

        score_threshold = get_default_classification_threshold(project, request)
        logger.debug(f"Filtering occurrences by determination score threshold of {score_threshold}")
        filter_q = build_occurrence_score_threshold_q(score_threshold, occurrence_accessor="")
        return self.filter(filter_q)

    def filter_by_project_default_taxa(self, project: Project | None = None, request: Request | None = None):
        """
        Filter occurrences by project's default include/exclude taxa lists.

        Respects the apply_defaults flag - if False, filtering is bypassed.
        """
        if project is None:
            return self

        # Check if default filters should be bypassed
        if get_apply_default_filters_flag(request) is False:
            return self

        qs = self

        # Apply taxa inclusion/exclusion filter
        include_taxa = project.default_filters_include_taxa.all()
        exclude_taxa = project.default_filters_exclude_taxa.all()
        taxa_q = build_taxa_recursive_filter_q(include_taxa, exclude_taxa, taxon_accessor="determination")
        if taxa_q:
            qs = qs.filter(taxa_q)

        return qs

    def apply_default_filters(self, project: Project | None = None, request: Request | None = None):
        """
        Apply all default filters to occurrences based on project settings.

        This is the standard method for filtering occurrences according to project defaults.
        It applies both score threshold and taxa filters in a single call.

        Args:
            project: The project whose default filters should be applied
            request: The request object (optional, used to check for apply_defaults=false)

        Returns:
            Filtered queryset with both score and taxa filters applied

        Example:
            # In a viewset
            qs = Occurrence.objects.apply_default_filters(project, self.request)

            # In a model method
            qs = Occurrence.objects.apply_default_filters(self.project, None)
        """
        if project is None:
            return self

        # Check if default filters should be bypassed entirely
        if get_apply_default_filters_flag(request) is False:
            return self

        # Use build_occurrence_default_filters_q to get the combined filter and apply it
        filter_q = build_occurrence_default_filters_q(project, request, occurrence_accessor="")
        return self.filter(filter_q)


class OccurrenceManager(models.Manager.from_queryset(OccurrenceQuerySet)):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "determination",
                "deployment",
                "project",
            )
        )


@final
class Occurrence(BaseModel):
    """An occurrence of a taxon, a sequence of one or more detections"""

    # @TODO change Determination to a nested field with a Taxon, User, Identification, etc like the serializer
    # this could be a OneToOneField to a Determination model or a JSONField validated by a Pydantic model
    determination = models.ForeignKey("Taxon", on_delete=models.SET_NULL, null=True, related_name="occurrences")
    determination_score = models.FloatField(null=True, blank=True)

    event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, related_name="occurrences")
    deployment = models.ForeignKey(Deployment, on_delete=models.SET_NULL, null=True, related_name="occurrences")
    project = models.ForeignKey("Project", on_delete=models.SET_NULL, null=True, related_name="occurrences")

    detections: models.QuerySet[Detection]
    identifications: models.QuerySet[Identification]

    objects = OccurrenceManager()

    def __str__(self) -> str:
        name = f"Occurrence #{self.pk}"
        if self.deployment:
            name += f" ({self.deployment.name})"
        if self.determination:
            name += f" ({self.determination.name})"
        return name

    def detections_count(self) -> int | None:
        # Annotations don't seem to work with nested serializers
        return self.detections.count()

    @functools.cached_property
    def first_appearance(self) -> SourceImage | None:
        # @TODO it appears we only need the first timestamp, that could be an annotated value
        first = self.detections.order_by("timestamp").select_related("source_image").first()
        if first:
            return first.source_image

    @functools.cached_property
    def last_appearance(self) -> SourceImage | None:
        # @TODO it appears we only need the last timestamp, that could be an annotated value
        last = self.detections.order_by("timestamp").select_related("source_image").last()
        if last:
            return last.source_image

    def first_appearance_timestamp(self) -> datetime.datetime | None:
        """
        Return the timestamp of the first appearance.
        ONLY if it has been added with a query annotation.
        """
        return None

    def first_appearance_time(self) -> datetime.time | None:
        """
        Return the time part only of the first appearance.
        ONLY if it has been added with a query annotation.
        """
        return None

    def last_appearance_timestamp(self) -> datetime.datetime | None:
        """
        Return the timestamp of the last appearance.
        ONLY if it has been added with a query annotation.
        """
        return None

    def duration(self) -> datetime.timedelta | None:
        first = self.first_appearance
        last = self.last_appearance
        if first and last and first.timestamp and last.timestamp:
            return last.timestamp - first.timestamp
        else:
            return None

    def duration_label(self) -> str | None:
        """
        If duration has been calculated by a query annotation, use that value
        otherwise call the duration() method to calculate it.
        """
        duration = self.duration() if callable(self.duration) else self.duration
        return ami.utils.dates.format_timedelta(duration)

    def detection_images(self, limit=None):
        for path in (
            Detection.objects.filter(occurrence=self).exclude(path=None).values_list("path", flat=True)[:limit]
        ):
            yield get_media_url(path)

    @functools.cached_property
    def best_detection(self):
        return Detection.objects.filter(occurrence=self).order_by("-classifications__score").first()

    @functools.cached_property
    def best_prediction(self):
        """
        Use the best prediction as the best identification if there are no human identifications.

        Uses the highest scoring classification (from any algorithm) as the best prediction.
        Considers terminal classifications first, then non-terminal ones.
        (Terminal classifications are the final classifications of a pipeline, non-terminal are intermediate models.)
        """
        return self.predictions().order_by("-terminal", "-score").first()

    @functools.cached_property
    def best_identification(self):
        """
        The most recent human identification is used as the best identification.

        @TODO this could use a confidence level chosen manually by the users/experts.
        """
        return (
            Identification.objects.filter(occurrence=self, withdrawn=False)
            .order_by(*BEST_IDENTIFICATION_ORDER)
            .first()
        )

    def get_determination_score(self) -> float | None:
        if not self.determination:
            return None
        elif self.best_identification:
            return self.best_identification.score
        elif self.best_prediction:
            return self.best_prediction.score
        else:
            return None

    def predictions(self):
        # Retrieve the classification with the max score for each algorithm.
        # select_related avoids per-row taxon/algorithm lazy loads when callers
        # serialize the result (e.g. OccurrenceListSerializer.best_prediction).
        classifications = (
            Classification.objects.filter(detection__occurrence=self)
            .select_related("taxon", "algorithm")
            .filter(
                score__in=models.Subquery(
                    Classification.objects.filter(detection__occurrence=self)
                    .values("algorithm")
                    .annotate(max_score=models.Max("score"))
                    .values("max_score")
                )
            )
            .order_by("-created_at")
        )
        return classifications

    def context_url(self):
        detection = self.best_detection
        if detection and detection.source_image and detection.source_image.event:
            # @TODO this was a temporary hack. Use settings and reverse().
            return f"https://app.preview.insectai.org/sessions/{detection.source_image.event.pk}?capture={detection.source_image.pk}&occurrence={self.pk}"  # noqa E501
        else:
            return None

    def url(self):
        # @TODO this was a temporary hack. Use settings and reverse().
        return f"https://app.preview.insectai.org/occurrences/{self.pk}"

    def save(self, update_determination=True, *args, **kwargs):
        super().save(*args, **kwargs)
        if update_determination:
            update_occurrence_determination(
                self,
                current_determination=self.determination,
                save=True,
            )

        if self.determination and not self.determination_score:
            # This may happen for legacy occurrences that were created
            # before the determination_score field was added
            # @TODO remove
            self.determination_score = self.get_determination_score()
            if not self.determination_score:
                logger.warning(f"Could not determine score for {self}")
            else:
                self.save(update_determination=False)

    class Meta:
        ordering = ["-determination_score"]
        indexes = [
            # Composite index for taxa queries filtered by project
            # Optimizes the taxa list query which executes correlated subqueries for each taxon
            # Pattern: WHERE determination_id=? AND project_id=? AND event_id IS NOT NULL AND determination_score>=?
            models.Index(
                fields=["determination_id", "project_id", "event_id", "determination_score"],
                name="occur_det_proj_evt_score",
            ),
            # Composite index for timestamp queries (last_detected)
            # Optimizes queries that join with detections table for timestamps
            # Pattern: WHERE determination_id=? AND project_id=? AND event_id IS NOT NULL
            models.Index(
                fields=["determination_id", "project_id", "event_id"],
                name="occur_det_proj_evt",
            ),
            # Supports sorting projects by their most recently updated occurrence
            # (see ProjectViewSet ordering "last_occurrence_updated_at").
            models.Index(fields=["project", "-updated_at"], name="occur_proj_updated_desc_idx"),
            # Backs the default occurrence list sort: per-project, ordered by
            # determination_score DESC (Meta.ordering). Without it the planner
            # sorts the whole project's occurrences per page (on-disk merge sort
            # on large projects). DESC = NULLS FIRST to match the ORM's ORDER BY.
            models.Index(fields=["project", "-determination_score"], name="occur_proj_score_desc_idx"),
        ]


def update_occurrence_determination(
    occurrence: Occurrence, current_determination: typing.Optional["Taxon"] = None, save=True
) -> bool:
    """
    Update the determination of the occurrence based on the identifications & predictions.

    If there are identifications, set the determination to the latest identification.
    If there are no identifications, set the determination to the top prediction.

    The `current_determination` is the determination currently saved in the database.
    The `occurrence` object may already have a different un-saved determination set
    so it is necessary to retrieve the current determination from the database, but
    this can also be passed in as an argument to avoid an extra database query.

    @TODO Add tests for this important method!
    """
    needs_update = False

    # Invalidate the cached properties so they will be re-calculated
    if hasattr(occurrence, "best_identification"):
        del occurrence.best_identification
    if hasattr(occurrence, "best_prediction"):
        del occurrence.best_prediction
    if hasattr(occurrence, "best_identification"):
        del occurrence.best_identification

    current_determination = (
        current_determination
        or Occurrence.objects.select_related("determination")
        .values("determination")
        .get(pk=occurrence.pk)["determination"]
    )
    new_determination = None
    new_score = None

    top_identification = occurrence.best_identification
    if top_identification and top_identification.taxon and top_identification.taxon != current_determination:
        new_determination = top_identification.taxon
        new_score = top_identification.score
    elif not top_identification:
        top_prediction = occurrence.best_prediction
        if top_prediction and top_prediction.taxon and top_prediction.taxon != current_determination:
            new_determination = top_prediction.taxon
            new_score = top_prediction.score

    if new_determination and new_determination != current_determination:
        logger.debug(f"Changing det. of {occurrence} from {current_determination} to {new_determination}")
        occurrence.determination = new_determination
        needs_update = True

    if new_score and new_score != occurrence.determination_score:
        logger.debug(f"Changing det. score of {occurrence} from {occurrence.determination_score} to {new_score}")
        occurrence.determination_score = new_score
        needs_update = True

    if not needs_update:
        if logger.getEffectiveLevel() <= logging.DEBUG:
            all_predictions = occurrence.predictions()
            all_preds_print = ", ".join([str(p) for p in all_predictions])
            logger.debug(
                f"No update needed for determination of {occurrence}. Best prediction: {occurrence.best_prediction}. "
                f"All preds: {all_preds_print}"
            )

    if save and needs_update:
        occurrence.save(update_determination=False)

    return needs_update
