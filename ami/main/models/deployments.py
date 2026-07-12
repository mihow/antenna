import datetime
import functools
import logging
import typing
from typing import final

from django.apps import apps
from django.db import IntegrityError, models
from django.db.models import Q
from django.template.defaultfilters import filesizeformat

import ami.tasks
import ami.utils
from ami.base.models import BaseModel
from ami.main.models_future.filters import build_occurrence_default_filters_q

from .common import _POST_TITLE_MAX_LENGTH
from .projects import Project, ProjectQuerySet

if typing.TYPE_CHECKING:
    from ami.jobs.models import Job

    from .events import Event
    from .occurrences import Occurrence
    from .source_images import SourceImage
    from .taxonomy import Taxon

logger = logging.getLogger(__name__)


@final
class Device(BaseModel):
    """
    Configuration of hardware used to capture images.

    If project is null then this is a public device that can be used by any project.
    """

    name = models.CharField(max_length=_POST_TITLE_MAX_LENGTH)
    description = models.TextField(blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name="devices")

    deployments: models.QuerySet["Deployment"]

    class Meta:
        verbose_name = "Device Configuration"


@final
class Site(BaseModel):
    """Research site with multiple deployments"""

    name = models.CharField(max_length=_POST_TITLE_MAX_LENGTH)
    description = models.TextField(blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name="sites")

    deployments: models.QuerySet["Deployment"]

    def deployments_count(self) -> int:
        return self.deployments.count()

    # def boundary(self) -> Optional[models.GeometryField]:
    # @TODO if/when we use GeoDjango
    #     return None

    def boundary_rect(self) -> tuple[float, float, float, float] | None:
        # Get the minumin and maximum latitude and longitude values of all deployments
        # at this research site.
        min_lat, max_lat, min_lon, max_lon = self.deployments.aggregate(
            min_lat=models.Min("latitude"),
            max_lat=models.Max("latitude"),
            min_lon=models.Min("longitude"),
            max_lon=models.Max("longitude"),
        ).values()

        bounds = (min_lat, min_lon, max_lat, max_lon)
        if None in bounds:
            return None
        else:
            return bounds

    class Meta:
        verbose_name = "Research Site"


@final
class DeploymentManager(models.Manager.from_queryset(ProjectQuerySet)):
    """
    Custom manager that adds counts of related objects to the default queryset.
    """


def _create_source_image_for_sync(
    deployment: "Deployment",
    obj: ami.utils.s3.ObjectTypeDef,
) -> typing.Union["SourceImage", None]:
    from ami.main.models import SourceImage

    assert "Key" in obj, f"File in object store response has no Key: {obj}"

    source_image = SourceImage(
        deployment=deployment,
        path=obj["Key"],
        last_modified=obj.get("LastModified"),
        size=obj.get("Size"),
        checksum=obj.get("ETag", "").strip('"'),
        checksum_algorithm=obj.get("ChecksumAlgorithm"),
    )
    logger.debug(f"Preparing to create or update SourceImage {source_image.path}")
    source_image.update_calculated_fields()
    return source_image


def _insert_or_update_batch_for_sync(
    deployment: "Deployment",
    source_images: list["SourceImage"],
    total_files: int,
    total_size: int,
    sql_batch_size=500,
    regroup_events_per_batch=False,
):
    from ami.main.models import SourceImage, group_images_into_events

    logger.info(f"Bulk inserting or updating batch of {len(source_images)} SourceImages")
    try:
        SourceImage.objects.bulk_create(
            source_images,
            batch_size=sql_batch_size,
            update_conflicts=True,
            unique_fields=["deployment", "path"],  # type: ignore
            update_fields=["last_modified", "size", "checksum", "checksum_algorithm"],
        )
    except IntegrityError as e:
        logger.error(f"Error bulk inserting batch of SourceImages: {e}")

    if total_files > (deployment.data_source_total_files or 0):
        deployment.data_source_total_files = total_files
    if total_size > (deployment.data_source_total_size or 0):
        deployment.data_source_total_size = total_size
    deployment.data_source_last_checked = datetime.datetime.now()

    if regroup_events_per_batch:
        group_images_into_events(deployment)

    deployment.save(update_calculated_fields=False)


def _compare_totals_for_sync(deployment: "Deployment", total_files_found: int):
    from ami.main.models import SourceImage

    # @TODO compare total_files to the number of SourceImages for this deployment
    existing_file_count = SourceImage.objects.filter(deployment=deployment).count()
    delta = abs(existing_file_count - total_files_found)
    if delta > 0:
        logger.warning(
            f"Deployment '{deployment}' has {existing_file_count} SourceImages "
            f"but the data source has {total_files_found} files "
            f"(+- {delta})"
        )


@final
class Deployment(BaseModel):
    """
    Class that describes a deployment of a device (camera & hardware) at a research site.
    """

    name = models.CharField(max_length=_POST_TITLE_MAX_LENGTH)
    description = models.TextField(blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    image = models.ImageField(upload_to="deployments", blank=True, null=True)

    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name="deployments")

    # @TODO consider sharing only the "data source auth/config" then a one-to-one config for each deployment
    # Or a pydantic model with nested attributes about each data source relationship
    data_source = models.ForeignKey(
        "S3StorageSource", on_delete=models.SET_NULL, null=True, blank=True, related_name="deployments"
    )

    # Pre-calculated values from the data source
    data_source_total_files = models.IntegerField(blank=True, null=True)
    data_source_total_size = models.BigIntegerField(blank=True, null=True)
    data_source_subdir = models.CharField(max_length=255, blank=True, null=True)
    data_source_regex = models.CharField(max_length=255, blank=True, null=True)
    data_source_last_checked = models.DateTimeField(blank=True, null=True)
    # data_source_start_date = models.DateTimeField(blank=True, null=True)
    # data_source_end_date = models.DateTimeField(blank=True, null=True)
    # data_source_last_check_duration = models.DurationField(blank=True, null=True)
    # data_source_last_check_status = models.CharField(max_length=255, blank=True, null=True)
    # data_source_last_check_notes = models.TextField(max_length=255, blank=True, null=True)

    # Pre-calculated values
    events_count = models.IntegerField(blank=True, null=True)
    occurrences_count = models.IntegerField(blank=True, null=True)
    captures_count = models.IntegerField(blank=True, null=True)
    detections_count = models.IntegerField(blank=True, null=True)
    taxa_count = models.IntegerField(blank=True, null=True)
    first_capture_timestamp = models.DateTimeField(blank=True, null=True)
    last_capture_timestamp = models.DateTimeField(blank=True, null=True)

    research_site = models.ForeignKey(
        Site,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployments",
    )

    device = models.ForeignKey(
        Device,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployments",
    )

    events: models.QuerySet["Event"]
    captures: models.QuerySet["SourceImage"]
    occurrences: models.QuerySet["Occurrence"]
    jobs: models.QuerySet["Job"]

    objects = DeploymentManager()

    class Meta:
        ordering = ["name"]

    def taxa(self) -> models.QuerySet["Taxon"]:
        from ami.main.models import Taxon

        return Taxon.objects.filter(Q(occurrences__deployment=self)).distinct()

    def first_capture(self) -> typing.Optional["SourceImage"]:
        from ami.main.models import SourceImage

        return SourceImage.objects.filter(deployment=self).order_by("timestamp").first()

    def last_capture(self) -> typing.Optional["SourceImage"]:
        from ami.main.models import SourceImage

        return SourceImage.objects.filter(deployment=self).order_by("timestamp").last()

    def get_first_and_last_timestamps(self) -> tuple[datetime.datetime, datetime.datetime]:
        from ami.main.models import SourceImage

        # Retrieve the timestamps of the first and last capture in a single query
        first, last = (
            SourceImage.objects.filter(deployment=self)
            .aggregate(first=models.Min("timestamp"), last=models.Max("timestamp"))
            .values()
        )
        return (first, last)

    def get_detections_count(self) -> int | None:
        """
        Return detections count filtered by project default filters.

        Excludes null-bbox placeholder detections (records indicating an image
        was processed and no detections were found) to stay consistent with
        ``SourceImage.get_detections_count`` and ``Event.get_detections_count``.
        """
        from ami.main.models import Detection

        qs = Detection.objects.filter(source_image__deployment=self).valid()
        filter_q = build_occurrence_default_filters_q(
            project=self.project,
            request=None,
            occurrence_accessor="occurrence",
        )

        return qs.filter(filter_q).distinct().count()

    def first_date(self) -> datetime.date | None:
        return self.first_capture_timestamp.date() if self.first_capture_timestamp else None

    def last_date(self) -> datetime.date | None:
        return self.last_capture_timestamp.date() if self.last_capture_timestamp else None

    def data_source_uri(self) -> str | None:
        if self.data_source:
            uri = self.data_source.uri().rstrip("/")
            if self.data_source_subdir:
                uri = f"{uri}/{self.data_source_subdir.strip('/')}/"
            if self.data_source_regex:
                uri = f"{uri}?regex={self.data_source_regex}"
        else:
            uri = None
        return uri

    def data_source_total_size_display(self) -> str:
        if self.data_source_total_size is None:
            return filesizeformat(0)
        else:
            return filesizeformat(self.data_source_total_size)

    def sync_captures(
        self,
        batch_size=1000,
        regroup_events_per_batch=False,
        regroup_after=True,
        job: "Job | None" = None,
    ) -> int:
        """
        Import images from the deployment's data source.

        Set ``regroup_after=False`` when the caller (e.g. ``DataStorageSyncJob``)
        will run regrouping as a tracked stage of its own so the work is visible
        in the Jobs UI instead of buried inside ``Deployment.save()``.
        """

        deployment = self
        assert deployment.data_source, f"Deployment {deployment.name} has no data source configured"

        s3_config = deployment.data_source.config
        total_size = 0
        total_files = 0
        failed = 0
        source_images = []
        django_batch_size = batch_size
        sql_batch_size = 1000

        if job:
            job.logger.info(f"Syncing captures for deployment {deployment}")
            job.update_progress()
            job.save()

        for obj, file_index in ami.utils.s3.list_files_paginated(
            s3_config,
            subdir=self.data_source_subdir,
            regex_filter=self.data_source_regex,
        ):
            logger.debug(f"Processing file {file_index}: {obj}")
            if not obj:
                continue
            try:
                source_image = _create_source_image_for_sync(deployment, obj)
            except Exception:
                failed += 1
                msg = f"Failed to process {obj.get('Key', '?')}"
                if job:
                    job.logger.exception(msg)
                else:
                    logger.exception(msg)
                continue

            if source_image:
                # Skip images with unparseable timestamps — they can't be grouped into events
                if source_image.timestamp is None:
                    failed += 1
                    msg = f"No timestamp parsed from filename: {obj['Key']}"
                    if job:
                        job.logger.error(msg)
                    else:
                        logger.error(msg)
                    continue
                elif source_image.timestamp.year < 2000:
                    msg = f"Suspicious timestamp ({source_image.timestamp.year}) for: {obj['Key']}"
                    if job:
                        job.logger.warning(msg)
                    else:
                        logger.warning(msg)

                total_files += 1
                total_size += obj.get("Size", 0)
                source_images.append(source_image)

            if len(source_images) >= django_batch_size:
                _insert_or_update_batch_for_sync(
                    deployment, source_images, total_files, total_size, sql_batch_size, regroup_events_per_batch
                )
                source_images = []
                if job:
                    job.logger.info(f"Processed {total_files} files")
                    job.progress.update_stage(job.job_type().key, total_files=total_files, failed=failed)
                    job.update_progress()

        if source_images:
            # Insert/update the last batch
            _insert_or_update_batch_for_sync(
                deployment, source_images, total_files, total_size, sql_batch_size, regroup_events_per_batch
            )
        if job:
            job.logger.info(f"Processed {total_files} files")
            job.progress.update_stage(job.job_type().key, total_files=total_files, failed=failed)
            job.update_progress()

        _compare_totals_for_sync(deployment, total_files)

        # @TODO decide if we should delete SourceImages that are no longer in the data source

        if regroup_after:
            if job:
                job.logger.info("Saving and recalculating sessions for deployment")
                job.progress.update_stage(job.job_type().key, progress=1)
                job.progress.add_stage("Update deployment cache")
                job.update_progress()

            # If new images were added, ensure the regroup happens now, not queued as an async task.
            self.save(regroup_async=False)

            if job:
                job.progress.update_stage("Update deployment cache", progress=1)
                job.update_progress()
        else:
            # Caller (e.g. DataStorageSyncJob) is responsible for running regroup as
            # an explicit stage. Skip Deployment.save's autoregroup but still
            # refresh cached counts and realign child Event/Occurrence/SourceImage
            # project pointers — those normally run inside Deployment.save's
            # update_calculated_fields branch, which we bypass here.
            self.save(regroup_async=False, update_calculated_fields=False)
            self.update_calculated_fields(save=True)
            if self.project_id:
                self.update_children()

        return total_files

    def audit_subdir_of_captures(self, ignore_deepest=False) -> dict[str, int]:
        """
        Review the subdirs of all captures that belong to this deployment in an efficient query.

        Group all captures by their subdir and count the number of captures in each group.
        `ignore_deepest` will exclude the deepest subdir from the audit (usually the date folder)
        """

        class SubdirExtractAll(models.Func):
            function = "REGEXP_REPLACE"
            template = "%(function)s(%(expressions)s, '/[^/]*$', '')"

        class SubdirExtractParent(models.Func):
            # Attempts failed to dynamically set the depth of the last directories to ignore.
            # so this is a hardcoded version that ignores the last one directory.
            # this is useful for ignoring the date folder in the path.
            function = "REGEXP_REPLACE"
            template = "%(function)s(%(expressions)s, '/[^/]*/[^/]*$', '')"

        extract_func = SubdirExtractParent if ignore_deepest else SubdirExtractAll

        subdirs_audit = (
            self.captures.annotate(
                subdir=models.Case(
                    models.When(path__contains="/", then=extract_func(models.F("path"))),
                    default=models.Value(""),
                    output_field=models.CharField(),
                )
            )
            .values("subdir")
            .annotate(count=models.Count("id"))
            .exclude(subdir="")
            .order_by("-count")
        )

        # Convert QuerySet to dictionary
        return {item["subdir"]: item["count"] for item in subdirs_audit}

    def update_subdir_of_captures(self, previous_subdir: str, new_subdir: str):
        """
        Update the relative directory in the path of all captures that belong to this deployment in a single query.

        This is useful when moving images to a new location in the data source. It is not run
        automatically when the deployment's data source configuration is updated. But admins can
        run it manually from the Django shell or a maintenance script.

        Reminder: the public_base_url includes the path that precedes the subdir within the full file path.

        Warning: this is essentially a find & replace operation on the path field of SourceImage objects.
        """
        from ami.main.models import SourceImage

        # Sanitize the subdir strings. Ensure that they end with a slash. This is are only protection against
        # accidentally modifying the filename.
        # Relative paths are stored without a leading slash.
        previous_subdir = previous_subdir.strip("/") + "/"
        new_subdir = new_subdir.strip("/") + "/"

        # Update the path of all captures that belong to this deployment
        captures = SourceImage.objects.filter(deployment=self, path__startswith=previous_subdir)
        logger.info(f"Updating subdir of {captures.count()} captures from '{previous_subdir}' to '{new_subdir}'")
        previous_count = captures.count()
        captures.update(
            path=models.functions.Replace(
                models.F("path"),
                models.Value(previous_subdir),
                models.Value(new_subdir),
            )
        )
        # Re-query the captures to ensure the path has been updated
        unchanged_count = SourceImage.objects.filter(deployment=self, path__startswith=previous_subdir).count()
        changed_count = SourceImage.objects.filter(deployment=self, path__startswith=new_subdir).count()

        if unchanged_count:
            raise ValueError(f"{unchanged_count} captures were not updated to new subdir: {new_subdir}")

        if changed_count != previous_count:
            raise ValueError(f"Only {changed_count} captures were updated to new subdir: {new_subdir}")

    def update_children(self):
        """
        Update all attribute on all child objects that should be equal to their deployment values.

        e.g. Events, Occurrences, SourceImages must belong to same project as their deployment. But
        they have their own copy of that attribute to reduce the number of joins required to query them.
        """

        # All the child models that have a foreign key to project
        child_models = [
            "Event",
            "Occurrence",
            "SourceImage",
        ]
        for model_name in child_models:
            model = apps.get_model("main", model_name)
            qs = model.objects.filter(deployment=self).exclude(project=self.project)
            project_values = set(qs.values_list("project", flat=True).distinct())
            if len(project_values):
                logger.warning(
                    f"Deployment {self} has alternate projects set on {model_name} "
                    f"objects: {project_values}. Updating them!"
                )
            qs.update(project=self.project)

    def update_calculated_fields(self, save=False):
        """Update calculated fields on the deployment."""

        self.data_source_total_files = self.captures.count()
        self.data_source_total_size = self.captures.aggregate(total_size=models.Sum("size")).get("total_size")

        self.events_count = self.events.count()
        self.captures_count = self.data_source_total_files or self.captures.count()
        self.detections_count = self.get_detections_count()
        occ_qs = self.occurrences.filter(event__isnull=False).apply_default_filters(  # type: ignore
            project=self.project,
            request=None,
        )  # type: ignore

        self.occurrences_count = occ_qs.distinct().count()

        self.taxa_count = occ_qs.values("determination_id").distinct().count()

        self.first_capture_timestamp, self.last_capture_timestamp = self.get_first_and_last_timestamps()

        if save:
            self.save(update_calculated_fields=False)

    def save(self, update_calculated_fields=True, regroup_async=True, *args, **kwargs):
        from ami.main.models import deployment_events_need_update, group_images_into_events

        super().save(*args, **kwargs)
        if self.pk and update_calculated_fields:
            if deployment_events_need_update(self):
                logger.info(f"Deployment {self} has events that need to be regrouped")
                if regroup_async:
                    ami.tasks.regroup_events.delay(self.pk)
                else:
                    group_images_into_events(self)
            self.update_calculated_fields(save=True)
            if self.project:
                self.update_children()
                # @TODO this isn't working as a background task
                # ami.tasks.model_task.delay("Project", self.project.pk, "update_children_project")


@final
class S3StorageSource(BaseModel):
    """
    Per-deployment configuration for an S3 bucket.
    """

    name = models.CharField(max_length=255)
    bucket = models.CharField(max_length=255)
    region = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="AWS region (e.g., 'us-east-1', 'eu-west-1'). Leave blank for Swift/MinIO storage.",
    )
    prefix = models.CharField(max_length=255, blank=True)
    access_key = models.TextField()
    secret_key = models.TextField()
    endpoint_url = models.CharField(max_length=255, blank=True, null=True)
    public_base_url = models.CharField(max_length=255, blank=True, null=True)
    total_size = models.BigIntegerField(null=True, blank=True)
    total_files = models.BigIntegerField(null=True, blank=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    # last_check_duration = models.DurationField(null=True, blank=True)
    # use_signed_urls = models.BooleanField(default=False)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name="storage_sources")
    # @TODO allow multiple projects to share the same S3StorageSource

    deployments: models.QuerySet["Deployment"]

    @property
    def config(self) -> ami.utils.s3.S3Config:
        return ami.utils.s3.S3Config(
            bucket_name=self.bucket,
            region=self.region,
            prefix=self.prefix,
            access_key_id=self.access_key,
            secret_access_key=self.secret_key,
            endpoint_url=self.endpoint_url,
            public_base_url=self.public_base_url,
        )

    def deployments_count(self) -> int:
        return self.deployments.count()

    def total_files_indexed(self) -> int:
        return self.deployments.aggregate(total_files=models.Sum("data_source_total_files"))["total_files"]

    @functools.cache
    def total_size_indexed(self) -> int:
        return self.deployments.aggregate(total_size=models.Sum("data_source_total_size"))["total_size"]

    def total_size_indexed_display(self) -> str:
        return filesizeformat(self.total_size_indexed())

    def total_captures_indexed(self) -> int:
        return self.deployments.aggregate(total_captures=models.Sum("captures_count"))["total_captures"]

    def list_files(self, limit=None):
        """Recursively list files in the bucket/prefix."""

        return ami.utils.s3.list_files_paginated(self.config, limit=limit)

    def count_files(self):
        """Count & save the number of files in the bucket/prefix."""

        count = ami.utils.s3.count_files_paginated(self.config)
        self.total_files = count
        self.save()
        return count

    def calculate_size(self):
        """Calculate the total size and count of all files in the bucket/prefix."""

        sizes = [obj["Size"] for obj, _num_files_checked in self.list_files() if obj]  # type: ignore
        size = sum(sizes)
        count = len(sizes)
        self.total_size = size
        self.total_files = count
        self.save()
        return size

    def uri(self, path: str | None = None):
        """Return the full URI for the given path."""

        full_path = "/".join(str(part).strip("/") for part in [self.bucket, self.prefix, path] if part)
        return f"s3://{full_path}"

    def public_url(self, path: str):
        """Return the public URL for the given path."""

        return ami.utils.s3.public_url(self.config, path)

    def test_connection(
        self, subdir: str | None = None, regex_filter: str | None = None
    ) -> ami.utils.s3.ConnectionTestResult:
        """Test the connection to the S3 bucket."""

        return ami.utils.s3.test_connection(self.config, subdir=subdir, regex_filter=regex_filter)

    def save(self, *args, **kwargs):
        # If public_base_url has changed, update the urls for all source images
        if self.pk:
            old = S3StorageSource.objects.get(pk=self.pk)
            if old.public_base_url != self.public_base_url:
                for deployment in self.deployments.all():
                    ami.tasks.update_public_urls.delay(deployment.pk, self.public_base_url)
        super().save(*args, **kwargs)
