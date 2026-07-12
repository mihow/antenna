import contextlib
import datetime
import logging
import typing
import uuid
from typing import final

from django.db import models
from django.db.models import Q
from django.utils import timezone
from rest_framework.request import Request

import ami.tasks
import ami.utils
from ami.base.models import BaseModel, BaseQuerySet
from ami.main import charts
from ami.main.models_future.filters import build_occurrence_default_filters_q

from .deployments import Deployment
from .projects import Project

if typing.TYPE_CHECKING:
    from ami.jobs.models import Job

    from .occurrences import Occurrence
    from .source_images import SourceImage
    from .taxonomy import Taxon

logger = logging.getLogger(__name__)


class EventQuerySet(BaseQuerySet):
    def with_taxa_count(self, project: Project | None = None, request: Request | None = None):
        """
        Annotate each event with the number of distinct taxa observed,
        filtered by default filters (score threshold and taxa inclusion/exclusion).
        """
        if project is None:
            return self

        filter_q = build_occurrence_default_filters_q(project, request, "occurrences")

        return self.annotate(
            taxa_count=models.Count(
                "occurrences__determination",
                distinct=True,
                filter=filter_q,
            )
        )

    def with_occurrences_count(self, project: Project | None = None, request: Request | None = None):
        """
        Annotate each event with the number of occurrences,
        filtered by default filters (score threshold and taxa inclusion/exclusion).
        """
        if project is None:
            return self

        filter_q = build_occurrence_default_filters_q(project, request, "occurrences")

        return self.annotate(
            occurrences_count=models.Count(
                "occurrences",
                distinct=True,
                filter=filter_q,
            )
        )


class EventManager(models.Manager.from_queryset(EventQuerySet)):
    pass


@final
class Event(BaseModel):
    """A monitoring session"""

    objects: EventManager = EventManager()
    group_by = models.CharField(
        max_length=255,
        db_index=True,
        help_text=(
            "A unique identifier for this event, used to group images into events. "
            "This allows images to be prepended or appended to an existing event. "
            "The default value is the day the event started, in the format YYYY-MM-DD. "
            "However images could also be grouped by camera settings, image dimensions, hour of day, "
            "or a random sample."
        ),
    )

    start = models.DateTimeField(db_index=True, help_text="The timestamp of the first image in the event.")
    end = models.DateTimeField(null=True, blank=True, help_text="The timestamp of the last image in the event.")

    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name="events")
    deployment = models.ForeignKey(Deployment, on_delete=models.SET_NULL, null=True, related_name="events")

    captures: models.QuerySet["SourceImage"]
    occurrences: models.QuerySet["Occurrence"]

    # Pre-calculated values
    captures_count = models.IntegerField(blank=True, null=True)
    detections_count = models.IntegerField(blank=True, null=True)
    occurrences_count = models.IntegerField(blank=True, null=True)
    calculated_fields_updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["start"]
        indexes = [
            models.Index(fields=["group_by"]),
            models.Index(fields=["start"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["deployment", "group_by"], name="unique_event"),
        ]

    def __str__(self) -> str:
        return f"{self.start.strftime('%A')}, {self.date_label()}"

    def name(self) -> str:
        return str(self)

    def day(self) -> datetime.date:
        """
        Consider the start of the event to be the day it occurred on.

        Most overnight monitoring sessions will start in the evening and end the next morning.
        """
        return self.start.date()

    def date_label(self) -> str:
        """
        Format the date range for display.

        If the start and end dates are different, display them as:
        Jan 1-5, 2021
        """
        if self.end and self.end.date() != self.start.date():
            return f"{self.start.strftime('%b %-d')}-{self.end.strftime('%-d %Y')}"
        else:
            return f"{self.start.strftime('%b %-d %Y')}"

    def duration(self):
        """Return the duration of the event.

        If the event is still in progress, use the current time as the end time.
        """
        now = datetime.datetime.now(tz=self.start.tzinfo)
        if not self.end:
            return now - self.start
        return self.end - self.start

    def duration_label(self) -> str:
        """
        Format the duration for display.

        If duration was populated by a query annotation, use that
        otherwise call the duration() method to calculate it.
        """
        duration = self.duration() if callable(self.duration) else self.duration
        return ami.utils.dates.format_timedelta(duration)

    def get_captures_count(self) -> int:
        return self.captures.distinct().count()

    def get_detections_count(self) -> int | None:
        """
        Return detections count filtered by project default filters.

        Excludes null-bbox placeholder detections to stay consistent with
        ``SourceImage.get_detections_count`` and ``Deployment.get_detections_count``.
        """
        from ami.main.models import Detection

        qs = Detection.objects.filter(source_image__event=self).valid()
        filter_q = build_occurrence_default_filters_q(
            project=self.project,
            request=None,
            occurrence_accessor="occurrence",
        )

        return qs.filter(filter_q).distinct().count()

    def get_occurrences_count(self, classification_threshold: float = 0) -> int:
        """
        Get the count of occurrences for this event, filtered by default filters.

        Note: classification_threshold parameter is deprecated, use project default filters instead.
        """
        return (
            self.occurrences.distinct()
            .apply_default_filters(project=self.project, request=None)  # type: ignore
            .count()
        )

    def stats(self) -> dict[str, int | None]:
        from ami.main.models import SourceImage

        return (
            SourceImage.objects.filter(event=self)
            .annotate(count=models.Count("detections"))
            .aggregate(
                detections_max_count=models.Max("count"),
                detections_min_count=models.Min("count"),
                # detections_avg_count=models.Avg("count"),
            )
        )

    def taxa_count(self, classification_threshold: float = 0) -> int:
        # Move this to a pre-calculated field or prefetch_related in the view
        # return self.taxa(classification_threshold).count()
        return 0

    def taxa(self, classification_threshold: float = 0) -> models.QuerySet["Taxon"]:
        from ami.main.models import Taxon

        return Taxon.objects.filter(
            Q(occurrences__event=self),
            occurrences__determination_score__gte=classification_threshold,
        ).distinct()

    def first_capture(self):
        # @TODO these needs to return a source image with detections prefetched and filtered
        # based on the project settings.
        # Ideally this would be an annotated field, rather than an additional query.
        # raise NotImplementedError("This is added an annotated field, it should not be called directly.")
        # return SourceImage.objects.filter(event=self).order_by("timestamp").first().with_detections()
        # with_thumbnails() satisfies the thumbnail_urls prefetch contract for the nested serializer;
        # select_related("project") feeds its project.thumbnails_enabled guard without an extra query.
        from ami.main.models import SourceImage

        return (
            SourceImage.objects.filter(event=self)
            .select_related("project")
            .order_by("timestamp")
            .with_thumbnails()
            .first()
        )

    def summary_data(self):
        """
        Data prepared for rendering charts with plotly.js
        """
        plots = []

        plots.append(charts.event_detections_per_hour(event_pk=self.pk))
        plots.append(charts.event_top_taxa(event_pk=self.pk))

        return plots

    def update_calculated_fields(self, save=False, updated_timestamp: datetime.datetime | None = None):
        """
        Important: if you update a new field, add it to the bulk_update call in update_calculated_fields_for_events
        """
        event = self
        if not event.group_by and event.start:
            # If no group_by is set, use the start "day"
            event.group_by = str(event.start.date())

        if not event.project and event.deployment:
            event.project = event.deployment.project

        if event.pk is not None:
            # Can only update start and end times if this is an update to an existing event
            first = event.captures.order_by("timestamp").values("timestamp").first()
            last = event.captures.order_by("-timestamp").values("timestamp").first()
            if first:
                event.start = first["timestamp"]
            if last:
                event.end = last["timestamp"]

            event.captures_count = event.get_captures_count()
            event.detections_count = event.get_detections_count()
            event.occurrences_count = event.get_occurrences_count()

            event.calculated_fields_updated_at = updated_timestamp or timezone.now()

        if save:
            event.save(update_calculated_fields=False)

    def save(self, update_calculated_fields=True, *args, **kwargs):
        super().save(*args, **kwargs)
        if update_calculated_fields:
            self.update_calculated_fields(save=True)


def update_calculated_fields_for_events(
    qs: models.QuerySet[Event] | None = None,
    pks: list[typing.Any] | None = None,
    last_updated: datetime.datetime | None = None,
    save=True,
):
    """
    This function is called by a migration to update the calculated fields for all events.

    @TODO this can likely be abstracted to a more generic function that can be used for any model
    """
    to_update = []

    qs = qs or Event.objects.all()
    if pks:
        qs = qs.filter(pk__in=pks)
    if last_updated:
        # query for None or before the last updated time
        qs = qs.filter(
            Q(calculated_fields_updated_at__isnull=True) | Q(calculated_fields_updated_at__lte=last_updated)
        )

    logging.info(f"Updating pre-calculated fields for {len(to_update)} events")

    updated_timestamp = timezone.now()
    for event in qs:
        event.update_calculated_fields(save=False, updated_timestamp=updated_timestamp)
        to_update.append(event)

    if save:
        updated_count = Event.objects.bulk_update(
            to_update,
            [
                "group_by",
                "start",
                "end",
                "project",
                "captures_count",
                "detections_count",
                "occurrences_count",
                "calculated_fields_updated_at",
            ],
        )
        if updated_count != len(to_update):
            logging.error(f"Failed to update {len(to_update) - updated_count} events")
    return to_update


def audit_event_lengths(deployment: Deployment):
    logger.info("Checking for unusual event durations")

    events_over_24_hours = Event.objects.filter(
        deployment=deployment, start__lt=models.F("end") - datetime.timedelta(days=1)
    ).count()
    if events_over_24_hours:
        logger.warning(f"Found {events_over_24_hours} event(s) over 24 hours in deployment {deployment}. ")

    events_starting_before_noon = Event.objects.filter(
        deployment=deployment, start__hour__lt=12  # Before hour 12
    ).count()
    if events_starting_before_noon:
        logger.warning(
            f"Found {events_starting_before_noon} event(s) starting before noon in deployment {deployment}. "
        )

    events_ending_before_start = Event.objects.filter(deployment=deployment, start__gt=models.F("end")).count()
    if events_ending_before_start:
        logger.error(f"Found {events_ending_before_start} event(s) with start > end in deployment {deployment}")


DEFAULT_MAX_EVENT_DURATION = datetime.timedelta(hours=24)


# Regroup should finish in seconds, so keep this short: if a worker is killed
# before its finally-block releases the lock, the TTL caps how long it stays held.
# Matches the soft_time_limit on ami.tasks.regroup_events.
REGROUP_LOCK_TTL_SECONDS = 10 * 60


@contextlib.contextmanager
def _regroup_lock(deployment_id: int):
    """
    Acquire a per-deployment lock for regrouping. Token-based release —
    the lock is only deleted if the value we wrote is still there, so an
    expired-then-reacquired-by-someone-else lock doesn't get clobbered.

    Released cleanly on graceful exit and on any exception that propagates
    out of the `with` block (including Celery's `SoftTimeLimitExceeded`).
    Not released on hard worker death (OS SIGKILL, OOM-kill, pod eviction):
    the token lives only in process memory, so a finally block that never
    runs leaves the lock entry sitting until the TTL expires.

    Yields True if acquired (caller should proceed), False if another run
    already holds it (caller should short-circuit).
    """
    from django.core.cache import cache

    lock_key = f"regroup_events:lock:deployment:{deployment_id}"
    token = uuid.uuid4().hex
    acquired = cache.add(lock_key, token, timeout=REGROUP_LOCK_TTL_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            current = cache.get(lock_key)
            if current == token:
                cache.delete(lock_key)


def group_images_into_events(
    deployment: Deployment,
    max_time_gap: datetime.timedelta | None = None,
    delete_empty=True,
    max_event_duration: datetime.timedelta | None = DEFAULT_MAX_EVENT_DURATION,
    job: "Job | None" = None,
    stage_key: str | None = None,
) -> list[Event]:
    """
    Group a deployment's captures into Events based on timestamp gaps.

    Holds a per-deployment cache lock so concurrent calls (autoregroup-on-save,
    manual API/admin trigger, sync-time regroup) collapse to a single in-flight
    run rather than racing on the same rows. If the lock is already held, this
    function logs and returns an empty list without touching the DB.

    When ``job`` and ``stage_key`` are passed, summary stats (events created,
    events touched, duplicate-timestamp count, ungrouped captures) are written
    to the named stage so the Jobs UI can surface them. Pure callers (e.g.
    ``Deployment.save`` autoregroup, ``sync_captures`` per-batch) leave both
    arguments at ``None``.
    """
    with _regroup_lock(deployment.pk) as acquired:
        if not acquired:
            msg = f"group_images_into_events skipped for deployment {deployment.pk}: another regroup is in progress."
            if job:
                job.logger.warning(msg)
            else:
                logger.warning(msg)
            return []

        return _group_images_into_events_locked(
            deployment,
            max_time_gap=max_time_gap,
            delete_empty=delete_empty,
            max_event_duration=max_event_duration,
            job=job,
            stage_key=stage_key,
        )


def _group_images_into_events_locked(
    deployment: Deployment,
    max_time_gap: datetime.timedelta | None,
    delete_empty: bool,
    max_event_duration: datetime.timedelta | None,
    job: "Job | None",
    stage_key: str | None,
) -> list[Event]:
    from ami.main.models import Detection, Occurrence, SourceImage, set_dimensions_for_collection

    if max_time_gap is None:
        default_gap = datetime.timedelta(minutes=120)
        if deployment.project_id:
            gap_seconds = deployment.project.session_time_gap_seconds
            if gap_seconds is None or gap_seconds <= 0:
                logger.warning(
                    f"Project {deployment.project_id} has invalid session_time_gap_seconds "
                    f"({gap_seconds!r}); falling back to default {default_gap}"
                )
                max_time_gap = default_gap
            else:
                max_time_gap = datetime.timedelta(seconds=gap_seconds)
        else:
            max_time_gap = default_gap
    # Log a warning if multiple SourceImages have the same timestamp
    dupes = (
        SourceImage.objects.filter(deployment=deployment)
        .values("timestamp")
        .annotate(count=models.Count("id"))
        .filter(count__gt=1)
        .exclude(timestamp=None)
    )
    duplicate_timestamp_count = dupes.count()
    if duplicate_timestamp_count:
        sample = "\n".join(
            f'{d.strftime("%Y-%m-%d %H:%M:%S")} x{c}' for d, c in dupes.values_list("timestamp", "count")[:20]
        )
        logger.warning(
            f"Found {duplicate_timestamp_count} duplicate-timestamp groups in deployment '{deployment}'. "
            f"Only one image will be used per timestamp for each event. First 20:\n{sample}"
        )

    image_timestamps = list(
        SourceImage.objects.filter(deployment=deployment)
        .exclude(timestamp=None)
        .values_list("timestamp", flat=True)
        .order_by("timestamp")
        .distinct()
    )

    timestamp_groups = ami.utils.dates.group_datetimes_by_gap(
        image_timestamps,
        max_time_gap,
        max_event_duration=max_event_duration,
    )

    events = []
    events_created_count = 0
    touched_event_pks: set[int] = set()
    for group in timestamp_groups:
        if not len(group):
            continue

        start_date = group[0]
        end_date = group[-1]

        # Print debugging info about groups
        delta = end_date - start_date
        hours = round(delta.seconds / 60 / 60, 1)
        logger.debug(
            f"Found session starting at {start_date} with {len(group)} images that ran for {hours} hours.\n"
            f"From {start_date.strftime('%c')} to {end_date.strftime('%c')}."
        )

        # Creating events & assigning images
        group_by = start_date.date()
        event, was_created = Event.objects.get_or_create(
            deployment=deployment,
            group_by=group_by,
            defaults={"start": start_date, "end": end_date},
        )
        events.append(event)
        if was_created:
            events_created_count += 1
        touched_event_pks.add(event.pk)

        # Track events currently holding these captures — they'll lose captures
        # to the UPDATE below and need their cached fields refreshed at the end.
        touched_event_pks.update(
            SourceImage.objects.filter(deployment=deployment, timestamp__in=group)
            .exclude(event__isnull=True)
            .exclude(event=event)
            .values_list("event_id", flat=True)
            .distinct()
        )

        SourceImage.objects.filter(deployment=deployment, timestamp__in=group).update(event=event)
        event.save()  # Update start and end times and other cached fields
        logger.info(
            f"Created/updated event {event} with {len(group)} images for deployment {deployment}. "
            f"Duration: {event.duration_label()}"
        )

    logger.info(
        f"Done grouping {len(image_timestamps)} captures into {len(events)} events " f"for deployment {deployment}"
    )

    # Realign Occurrence.event_id with each occurrence's detections' current
    # source_image.event_id. Occurrences are bound to an event once at creation
    # time (Detection.associate_new_occurrence and Pipeline.save_results both
    # read source_image.event), and are never re-derived afterward. Without
    # this refresh, a deployment regrouped under the 24h cap keeps every
    # occurrence pointing at its original (pre-cap) event regardless of when
    # its detections actually fired — breaking every Occurrence.event-keyed
    # query (the occur_det_proj_evt index, Event.occurrences related-name,
    # event_ids= filters in TaxonQuerySet). Track the events currently
    # held by occurrences in this deployment before and after the refresh so
    # update_calculated_fields_for_events below picks up both losers and
    # gainers of occurrences when it recomputes occurrences_count.
    deployment_occurrences = Occurrence.objects.filter(deployment=deployment)
    touched_event_pks.update(
        deployment_occurrences.exclude(event__isnull=True).values_list("event_id", flat=True).distinct()
    )
    deployment_occurrences.update(
        event_id=models.Subquery(
            Detection.objects.filter(occurrence_id=models.OuterRef("pk"))
            .order_by("source_image__timestamp", "source_image_id", "pk")
            .values("source_image__event_id")[:1]
        )
    )
    touched_event_pks.update(
        deployment_occurrences.exclude(event__isnull=True).values_list("event_id", flat=True).distinct()
    )

    # Refresh cached fields on every event touched by grouping. An event reused
    # via matching group_by can lose captures to new events created by later
    # iterations above, leaving its start/end/captures_count stale — e.g. a
    # pre-existing multi-month event being re-grouped under a 24h cap.
    # (#904 is expected to rework this reuse path more thoroughly.)
    if touched_event_pks:
        update_calculated_fields_for_events(pks=list(touched_event_pks))

    events_deleted_empty = 0
    if delete_empty:
        logger.info("Deleting empty events for deployment")
        events_deleted_empty = delete_empty_events(deployment=deployment)

    for event in events:
        # Set the width and height of all images in each event based on the first image
        logger.info(f"Setting image dimensions for event {event}")
        set_dimensions_for_collection(event)

    # Refresh deployment-level cached counts. The async regroup_events task
    # never goes through Deployment.save's calculated-fields refresh, so
    # without this call the deployment list (occurrences_count, taxa_count,
    # etc.) keeps showing pre-regroup numbers until the next save touches it.
    # The save inside update_calculated_fields uses update_calculated_fields=False
    # so it doesn't re-enter the regroup path.
    logger.info("Updating cached fields on deployment")
    deployment.update_calculated_fields(save=True)

    audit_event_lengths(deployment)

    # Surface stats to the Jobs UI when this regroup is wrapped in a job.
    # "Ungrouped captures" = images with valid timestamps that didn't land in
    # any event (should be 0 unless an event delete races a capture insert;
    # useful signal). Param NAMES below are the human-readable labels shown
    # in the Jobs UI; stable retrieval keys are the slugify(name) forms (e.g.
    # "captures-grouped"). See REGROUP_STAGE_PARAM_NAMES in ami.jobs.models.
    if job and stage_key:
        ungrouped_captures_count = (
            SourceImage.objects.filter(deployment=deployment, event__isnull=True).exclude(timestamp=None).count()
        )
        no_timestamp_captures_count = SourceImage.objects.filter(deployment=deployment, timestamp=None).count()
        # Count actual SourceImage rows assigned to an event, not the count of
        # distinct timestamps in image_timestamps — two captures can share a
        # timestamp (duplicate_timestamp_count tracks this) and the UPDATE above
        # assigns both to the same event, so a distinct-timestamp count would
        # understate the work done.
        captures_grouped_count = (
            SourceImage.objects.filter(deployment=deployment, event__isnull=False).exclude(timestamp=None).count()
        )
        regroup_stats = {
            "Captures grouped": captures_grouped_count,
            "Events created": events_created_count,
            "Events touched": len(touched_event_pks),
            "Empty events deleted": events_deleted_empty,
            "Duplicate timestamps": duplicate_timestamp_count,
            "Ungrouped captures": ungrouped_captures_count,
            "Captures missing timestamp": no_timestamp_captures_count,
        }
        for param_name, value in regroup_stats.items():
            job.progress.add_or_update_stage_param(stage_key, param_name, value)
        job.update_progress()
        job.save()

    return events


def deployment_events_need_update(deployment: Deployment) -> bool:
    """
    Returns True if there are any SourceImages in the deployment
    that haven't been assigned to an `Event`.

    Note: This does not detect if images were deleted from the deployment
    after being grouped. We currently have limited support for image deletion,
    so handling that is out of scope for this check.
    """
    from ami.main.models import SourceImage

    capture_counts_differ = deployment.captures_count != deployment.captures.count()

    ungrouped_images = models.Q(event__isnull=True)

    events_last_updated = (
        deployment.events.aggregate(
            latest_updated_at=models.Max("updated_at"),
        )["latest_updated_at"]
        or deployment.updated_at
        or datetime.datetime.min
    )
    images_updated_after_events = models.Q(updated_at__gt=events_last_updated)

    new_or_ungrouped_images = (
        SourceImage.objects.filter(deployment=deployment).filter(ungrouped_images | images_updated_after_events)
    ).exists()

    images_in_deployment_but_another_event = (
        SourceImage.objects.filter(deployment=deployment).exclude(event__deployment=deployment).exists()
    )

    needs_update = new_or_ungrouped_images or capture_counts_differ or images_in_deployment_but_another_event

    if needs_update:
        logger.info(
            f"Deployment {deployment} events need updating: "
            f"capture_counts_differ={capture_counts_differ}, "
            f"new_or_ungrouped_images={new_or_ungrouped_images}, "
            f"images_in_deployment_but_another_event={images_in_deployment_but_another_event}"
        )

    return needs_update


def delete_empty_events(deployment: Deployment, dry_run=False) -> int:
    """
    Delete events that have no images, occurrences or other related records.

    Returns the number of events deleted (or that would be deleted, for dry runs).
    """

    # @TODO Search all models that have a foreign key to Event
    # related_models = [
    #     f.related_model
    #     for f in Event._meta.get_fields()
    #     if f.one_to_many or f.one_to_one or (f.many_to_many and f.auto_created)
    # ]

    events = (
        Event.objects.filter(deployment=deployment)
        .annotate(
            num_images=models.Count("captures"),
            num_occurrences=models.Count("occurrences"),
        )
        .filter(num_images=0, num_occurrences=0)
    )

    count = events.count()
    if dry_run:
        for event in events:
            logger.debug(f"Would delete event {event} (dry run)")
    else:
        logger.info(f"Deleting {count} empty events")
        events.delete()
    return count


def sample_events(deployment: Deployment, day_interval: int = 3) -> typing.Generator[Event, None, None]:
    """
    Return a sample of events from the deployment, evenly spaced apart by day_interval.
    """

    last_event = None
    for event in Event.objects.filter(deployment=deployment).order_by("start"):
        if not last_event:
            yield event
            last_event = event
        else:
            delta = event.start - last_event.start
            if delta.days >= day_interval:
                yield event
                last_event = event
