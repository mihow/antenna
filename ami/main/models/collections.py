import logging
import typing
from typing import final

from django.db import models

from ami.base.fields import DateStringField
from ami.base.models import BaseModel, BaseQuerySet
from ami.main.models_future.filters import build_occurrence_default_filters_q

from .common import as_choices, null_detections_q
from .detections import Detection
from .projects import Project
from .source_images import (
    SourceImage,
    sample_captures_by_interval,
    sample_captures_by_nth,
    sample_captures_by_position,
)

if typing.TYPE_CHECKING:
    from ami.jobs.models import Job

logger = logging.getLogger(__name__)

_SOURCE_IMAGE_SAMPLING_METHODS = [
    "full",
    "random",
    "stratified_random",
    "interval",
    "manual",
    "starred",
    "random_from_each_event",
    "last_and_random_from_each_event",
    "greatest_file_size_from_each_event",
    "detections_only",
    "common_combined",  # Deprecated
]


class SourceImageCollectionQuerySet(BaseQuerySet):
    def with_source_images_count(self):
        return self.annotate(
            source_images_count=models.Count(
                "images",
                distinct=True,
            )
        )

    def with_source_images_with_detections_count(self):
        return self.annotate(
            source_images_with_detections_count=models.Count(
                "images",
                filter=~null_detections_q("images__detections__"),
                distinct=True,
            )
        )

    def with_source_images_processed_count(self):
        return self.annotate(
            source_images_processed_count=models.Count(
                "images",
                filter=models.Q(images__detections__isnull=False),
                distinct=True,
            )
        )

    def with_source_images_processed_by_algorithm_count(self, algorithm_id: int):
        return self.annotate(
            source_images_processed_by_algorithm_count=models.Count(
                "images",
                filter=models.Q(images__detections__classifications__algorithm_id=algorithm_id),
                distinct=True,
            )
        )

    def with_occurrences_count(
        self, classification_threshold: float = 0, project: Project | None = None, request=None
    ):
        """
        Annotate each collection with the number of occurrences,
        filtered by default filters (score threshold and taxa inclusion/exclusion).

        Note: classification_threshold parameter is deprecated, use project default filters instead.
        """
        filter_q = build_occurrence_default_filters_q(project, request, "images__detections__occurrence")
        return self.annotate(
            occurrences_count=models.Count(
                "images__detections__occurrence",
                filter=filter_q,
                distinct=True,
            )
        )

    def with_taxa_count(self, classification_threshold: float = 0, project: Project | None = None, request=None):
        """
        Annotate each collection with the number of distinct taxa,
        filtered by default filters (score threshold and taxa inclusion/exclusion).

        Note: classification_threshold parameter is deprecated, use project default filters instead.
        """
        filter_q = build_occurrence_default_filters_q(project, request, "images__detections__occurrence")
        return self.annotate(
            taxa_count=models.Count(
                "images__detections__occurrence__determination",
                filter=filter_q,
                distinct=True,
            )
        )


class SourceImageCollectionManager(models.Manager):
    def get_queryset(self) -> SourceImageCollectionQuerySet:
        return SourceImageCollectionQuerySet(self.model, using=self._db)


@final
class SourceImageCollection(BaseModel):
    """
    A subset of source images for review, processing, etc.

    Examples:
        - Random subset
        - Stratified random sample from all deployments
        - Images sampled based on a time interval (every 30 minutes)


    Collections are saved so that they can be reviewed or re-used later.

    """

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # dataset_type = models.CharField(
    #     max_length=255,
    #     choices=as_choices(["Curated", "Dynamic", "Sampling"]),
    # )
    images = models.ManyToManyField("SourceImage", related_name="collections", blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sourceimage_collections")
    method = models.CharField(
        max_length=255,
        choices=as_choices(_SOURCE_IMAGE_SAMPLING_METHODS),
        default="full",
    )
    # @TODO this should be a JSON field with a schema, use a pydantic model
    kwargs = models.JSONField(
        "Arguments",
        null=True,
        blank=True,
        help_text="Arguments passed to the sampling function (JSON dict)",
        default=dict,
    )

    objects = SourceImageCollectionManager()

    jobs: models.QuerySet["Job"]

    def infer_dataset_type(self):
        if "starred" in self.name.lower():
            return "curated"
        else:
            return "sampling"

    @property
    def dataset_type(self):
        return self.infer_dataset_type()

    def source_images_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        # return self.images.count()
        return None

    def source_images_with_detections_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        return None

    def source_images_processed_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        return None

    def occurrences_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        return None

    def taxa_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        return None

    def get_queryset(
        self,
        *args,
        **kwargs,
    ):
        return SourceImage.objects.filter(project=self.project)

    @classmethod
    def sampling_methods(cls):
        return [method for method in dir(cls) if method.startswith("sample_")]

    def populate_sample(self, job: "Job | None" = None):
        """Create a sample of source images based on the method and kwargs"""
        kwargs = self.kwargs or {}

        if job:
            task_logger = job.logger
        else:
            task_logger = logger

        method_name = f"sample_{self.method}"
        if not hasattr(self, method_name):
            raise ValueError(f"Invalid sampling method: {self.method}. Choices are: {_SOURCE_IMAGE_SAMPLING_METHODS}")
        else:
            task_logger.info(f"Sampling using method '{method_name}' with params: {kwargs}")
            method = getattr(self, method_name)
            task_logger.info(f"Sampling and saving captures to {self}")
            self.images.set(method(**kwargs))
            self.save()
            task_logger.info(f"Done sampling and saving captures to {self}")

    def _filter_sample(
        self,
        qs: models.QuerySet,
        hour_start: int | None = None,
        hour_end: int | None = None,
        month_start: int | None = None,
        month_end: int | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        deployment_ids: list[int] | None = None,
        research_site_ids: list[int] | None = None,
        event_ids: list[int] | None = None,
    ):
        if deployment_ids is not None:
            qs = qs.filter(deployment__in=deployment_ids)
        if research_site_ids is not None:
            qs = qs.filter(deployment__research_site__in=research_site_ids)
        if event_ids is not None:
            qs = qs.filter(event__in=event_ids)
        if date_start is not None:
            qs = qs.filter(timestamp__date__gte=DateStringField.to_date(date_start))
        if date_end is not None:
            qs = qs.filter(timestamp__date__lte=DateStringField.to_date(date_end))

        if month_start is not None:
            qs = qs.filter(timestamp__month__gte=month_start)
        if month_end is not None:
            qs = qs.filter(timestamp__month__lte=month_end)

        if hour_start is not None and hour_end is not None:
            if hour_start < hour_end:
                # Hour range within the same day (e.g., 08:00 to 15:00)
                qs = qs.filter(timestamp__hour__gte=hour_start, timestamp__hour__lte=hour_end)
            else:
                # Hour range has Midnight crossover: (e.g., 17:00 to 06:00)
                qs = qs.filter(models.Q(timestamp__hour__gte=hour_start) | models.Q(timestamp__hour__lte=hour_end))
        elif hour_start is not None:
            qs = qs.filter(timestamp__hour__gte=hour_start)
        elif hour_end is not None:
            qs = qs.filter(timestamp__hour__lte=hour_end)

        return qs

    def sample_random(
        self,
        size: int = 100,
        hour_start: int | None = None,
        hour_end: int | None = None,
        month_start: int | None = None,
        month_end: int | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        deployment_ids: list[int] | None = None,
        research_site_ids: list[int] | None = None,
        event_ids: list[int] | None = None,
    ):
        """Create a random sample of source images"""

        qs = self.get_queryset()
        qs = self._filter_sample(
            qs=qs,
            hour_start=hour_start,
            hour_end=hour_end,
            month_start=month_start,
            month_end=month_end,
            date_start=date_start,
            date_end=date_end,
            deployment_ids=deployment_ids,
            research_site_ids=research_site_ids,
            event_ids=event_ids,
        )
        return qs.order_by("?")[:size]

    def sample_manual(self, image_ids: list[int]):
        """Create a sample of source images based on a list of source image IDs"""

        qs = self.get_queryset()
        return qs.filter(id__in=image_ids)

    # Deprecated
    def sample_common_combined(
        self,
        minute_interval: int | None = None,
        max_num: int | None = None,
        shuffle: bool = True,  # This is applicable if max_num is set and minute_interval is not set
        hour_start: int | None = None,
        hour_end: int | None = None,
        month_start: int | None = None,
        month_end: int | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        deployment_ids: list[int] | None = None,
        research_site_ids: list[int] | None = None,
        event_ids: list[int] | None = None,
    ) -> models.QuerySet | typing.Generator[SourceImage, None, None]:
        qs = self.get_queryset()
        qs = self._filter_sample(
            qs=qs,
            hour_start=hour_start,
            hour_end=hour_end,
            month_start=month_start,
            month_end=month_end,
            date_start=date_start,
            date_end=date_end,
            deployment_ids=deployment_ids,
            research_site_ids=research_site_ids,
            event_ids=event_ids,
        )

        if minute_interval is not None:
            # @TODO can this be done in the database and return a queryset?
            # this currently returns a list of source images
            # Ensure the queryset is limited to the project
            qs = qs.filter(project=self.project)
            qs = sample_captures_by_interval(minute_interval=minute_interval, qs=qs, max_num=max_num)
        else:
            if max_num is not None:
                if shuffle:
                    qs = qs.order_by("?")
                qs = qs[:max_num]

        return qs

    def sample_interval(
        self,
        minute_interval: int = 10,
        exclude_events: list[int] = [],
        deployment_id: int | None = None,  # Deprecated
        hour_start: int | None = None,
        hour_end: int | None = None,
        month_start: int | None = None,
        month_end: int | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        deployment_ids: list[int] | None = None,
        research_site_ids: list[int] | None = None,
        event_ids: list[int] | None = None,
    ):
        """Create a sample of source images based on a time interval"""

        qs = self.get_queryset()
        qs = self._filter_sample(
            qs=qs,
            hour_start=hour_start,
            hour_end=hour_end,
            month_start=month_start,
            month_end=month_end,
            date_start=date_start,
            date_end=date_end,
            deployment_ids=deployment_ids,
            research_site_ids=research_site_ids,
            event_ids=event_ids,
        )
        if deployment_id:
            qs = qs.filter(deployment=deployment_id)
        qs = qs.exclude(event__in=exclude_events)
        # Limit to project
        qs = qs.filter(project=self.project)

        # Sample per-deployment so the minute interval applies independently per station.
        # If specific deployment ids are provided, use them; otherwise iterate over all deployments
        # in the project and sample each separately.
        if deployment_ids is not None:
            deps = deployment_ids
        elif deployment_id is not None:
            deps = [deployment_id]
        else:
            deps = list(self.project.deployments.values_list("id", flat=True))

        captures: set[SourceImage] = set()
        for dep in deps:
            dep_qs = qs.filter(deployment=dep)
            for c in sample_captures_by_interval(minute_interval=minute_interval, qs=dep_qs):
                captures.add(c)

        # Return results in a deterministic order. Sort by timestamp (oldest first),
        # then by primary key to stabilize ordering when timestamps are equal.
        captures_list = sorted(captures, key=lambda s: (s.timestamp is None, s.timestamp, s.pk))
        return captures_list

    def sample_positional(self, position: int = -1):
        """Sample the single nth source image from all events in the project"""

        qs = self.get_queryset()
        return sample_captures_by_position(position=position, qs=qs)

    def sample_nth(self, nth: int):
        """Sample every nth source image from all events in the project"""

        qs = self.get_queryset()
        return sample_captures_by_nth(nth=nth, qs=qs)

    def sample_random_from_each_event(self, num_each: int = 10):
        """Sample n random source images from each event in the project."""

        qs = self.get_queryset()
        captures = set()
        for event in self.project.events.all():
            captures.update(qs.filter(event=event).order_by("?")[:num_each])
        return captures

    def sample_last_and_random_from_each_event(self, num_each: int = 1):
        """Sample the last image from each event and n random from each event."""

        qs = self.get_queryset()
        captures = set()
        for event in self.project.events.all():
            last_capture = qs.filter(event=event).order_by("timestamp").last()
            if not last_capture:
                # This event has no captures
                continue
            captures.add(last_capture)
            random_captures = qs.filter(event=event).exclude(pk=last_capture.pk).order_by("?")[:num_each]
            captures.update(random_captures)
        return captures

    def sample_greatest_file_size_from_each_event(self, num_each: int = 1):
        """Sample the image with the greatest file size from each event."""

        qs = self.get_queryset()
        captures = set()
        for event in self.project.events.all():
            captures.update(qs.filter(event=event).order_by("-size")[:num_each])
        return captures

    def sample_detections_only(self):
        """Sample all source images with at least one real (non-null-marker) detection."""

        qs = self.get_queryset()
        valid_detection_image_ids = Detection.objects.valid().values("source_image_id")
        return qs.filter(pk__in=valid_detection_image_ids).distinct()

    def sample_full(
        self,
        hour_start: int | None = None,
        hour_end: int | None = None,
        month_start: int | None = None,
        month_end: int | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        deployment_ids: list[int] | None = None,
        research_site_ids: list[int] | None = None,
        event_ids: list[int] | None = None,
    ):
        """Sample all source images"""

        qs = self.get_queryset()
        qs = self._filter_sample(
            qs=qs,
            hour_start=hour_start,
            hour_end=hour_end,
            month_start=month_start,
            month_end=month_end,
            date_start=date_start,
            date_end=date_end,
            deployment_ids=deployment_ids,
            research_site_ids=research_site_ids,
            event_ids=event_ids,
        )
        return qs.all().distinct()

    @classmethod
    def get_or_create_starred_collection(cls, project: Project) -> "SourceImageCollection":
        """
        Get or create a collection for starred images.
        """
        collection = (
            SourceImageCollection.objects.filter(
                project=project,
                method="starred",
            )
            .order_by("created_at")
            .first()
        )  # Use the oldest match
        if not collection:
            collection = SourceImageCollection.objects.create(
                project=project,
                method="starred",
                name="Starred Images",  # @TODO make this translatable
            )
        return collection
