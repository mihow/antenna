import datetime
import logging
import time
import typing
import urllib.parse
from io import BytesIO
from typing import final

import PIL.Image
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import models
from django.db.models.fields.files import ImageFieldFile
from django.db.models.functions import Coalesce
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from guardian.shortcuts import get_perms
from rest_framework.request import Request

import ami.tasks
import ami.utils
from ami.base.models import BaseModel, BaseQuerySet
from ami.main.models_future.filters import build_occurrence_default_filters_q
from ami.users.models import User
from ami.utils.media import calculate_file_checksum, extract_timestamp, fetch_image_content

from .deployments import Deployment
from .events import Event
from .projects import Project

if typing.TYPE_CHECKING:
    from ami.jobs.models import Job

    from .collections import SourceImageCollection
    from .detections import Detection

logger = logging.getLogger(__name__)


def validate_filename_timestamp(filename: str) -> None:
    # Ensure filename has a timestamp
    timestamp = ami.utils.dates.get_image_timestamp_from_filename(filename)
    if not timestamp:
        raise ValidationError("Image filename does not contain a valid timestamp (e.g. YYYYMMDDHHMMSS-snapshot.jpg).")


def create_source_image_from_upload(
    image: ImageFieldFile,
    deployment: Deployment,
    request=None,
    process_now=True,
) -> "SourceImage":
    """Create a complete SourceImage from an uploaded file."""

    # Read file content once
    image.seek(0)
    file_content = image.read()

    # Calculate a checksum for the image content
    checksum, checksum_algorithm = calculate_file_checksum(file_content)

    # Create PIL image from file content (no additional file reads)
    image_stream = BytesIO(file_content)
    pil_image = PIL.Image.open(image_stream)

    timestamp = extract_timestamp(filename=image.name, image=pil_image)
    if not timestamp:
        logger.warning(
            "A valid timestamp could not be found in the image's EXIF data or filename. "
            "Please rename the file to include a timestamp "
            "(e.g. YYYYMMDDHHMMSS-snapshot.jpg). "
            "Falling back to the current time for the image captured timestamp."
        )
        timestamp = timezone.now()
    width = pil_image.width
    height = pil_image.height
    size = len(file_content)

    # get full public media url of image:
    if request:
        base_url = request.build_absolute_uri(settings.MEDIA_URL)
    else:
        base_url = settings.MEDIA_URL

    source_image = SourceImage.objects.create(
        path=image.name,  # Includes relative path from MEDIA_ROOT
        public_base_url=base_url,  # @TODO how to merge this with the data source?
        project=deployment.project,
        deployment=deployment,
        timestamp=timestamp,
        event=None,  # Will be assigned when the image is grouped into events
        size=size,
        checksum=checksum,
        checksum_algorithm=checksum_algorithm,
        width=width,
        height=height,
        # The sync path stores the object's LastModified header here; set it on
        # upload too so source-vs-derivative freshness checks have a value.
        last_modified=timezone.now(),
        test_image=True,
        uploaded_by=request.user if request else None,
    )
    deployment.save(regroup_async=False)
    if process_now:
        from ami.ml.orchestration.processing import process_single_source_image

        process_single_source_image(source_image=source_image)
    return source_image


def upload_to_with_deployment(instance, filename: str) -> str:
    """Nest uploads under subdir for a deployment."""
    return f"example_captures/{instance.deployment.pk}/{filename}"


# Existing migrations serialize these callables as "ami.main.models.<name>" (their home
# before the models package split). Pin the path so makemigrations doesn't see a change.
validate_filename_timestamp.__module__ = "ami.main.models"
upload_to_with_deployment.__module__ = "ami.main.models"


@final
class SourceImageUpload(BaseModel):
    """
    A manually uploaded image that has not yet been imported.

    The SourceImageViewSet will create a SourceImage from the uploaded file and delete the upload.
    """

    project_accessor = "deployment__project"
    image = models.ImageField(upload_to=upload_to_with_deployment)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    deployment = models.ForeignKey(Deployment, on_delete=models.CASCADE, related_name="manually_uploaded_captures")
    source_image = models.OneToOneField(
        "SourceImage", on_delete=models.CASCADE, null=True, blank=True, related_name="upload"
    )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # @TODO Use a "dirty" flag to mark the deployment as having new uploads, needs refresh
        self.deployment.save()


@receiver(pre_delete, sender=SourceImageUpload)
def delete_source_image(sender, instance, **kwargs):
    """
    A SourceImageUpload are automatically deleted when deleting a SourceImage because of the CASCADE setting.
    However the SourceImage needs to be deleted using a signal when deleting a SourceImageUpload.
    """
    if instance.source_image:
        # Disconnect the SourceImage from the upload to prevent recursion error
        source_image = instance.source_image
        instance.source_image = None
        instance.save()
        source_image.delete()
    # @TODO Use a "dirty" flag to mark the deployment as having new uploads, needs refresh
    instance.deployment.save()


class SourceImageQuerySet(BaseQuerySet):
    def with_occurrences_count(self, project: Project | None = None, request=None):
        """
        Annotate each source image with the number of occurrences,
        filtered by default filters (score threshold and taxa inclusion/exclusion).

        Note: classification_threshold parameter is deprecated, use project default filters instead.

        Uses a subquery to avoid GROUP BY in the pagination count query, which may
        improve performance for large datasets.
        """
        filter_q = build_occurrence_default_filters_q(project, request, "")

        # Use a subquery instead of Count with joins to avoid GROUP BY in pagination count query
        # The subquery counts distinct occurrences for each source image
        # Use Coalesce to return 0 when the subquery returns NULL (no matching rows)
        # @TODO update the SourceImageCollectionQuerySet to use the same approach
        from ami.main.models import Occurrence

        occurrences_subquery = (
            Occurrence.objects.filter(detections__source_image_id=models.OuterRef("pk"))
            .filter(filter_q)
            .values("detections__source_image_id")  # Group by source_image_id to get one row per source_image
            .annotate(count=models.Count("id", distinct=True))
            .values("count")
        )

        return self.annotate(
            occurrences_count=Coalesce(models.Subquery(occurrences_subquery, output_field=models.IntegerField()), 0)
        )

    def with_taxa_count(self, project: Project | None = None, request=None):
        """
        Annotate each source image with the number of distinct taxa,
        filtered by default filters (score threshold and taxa inclusion/exclusion).

        Note: classification_threshold parameter is deprecated, use project default filters instead.

        Uses a subquery to avoid GROUP BY in the pagination count query, which may
        improve performance for large datasets.
        """
        filter_q = build_occurrence_default_filters_q(project, request, "")

        # Use a subquery instead of Count with joins to avoid GROUP BY in pagination count query
        # The subquery counts distinct taxa for each source image
        # Use Coalesce to return 0 when the subquery returns NULL (no matching rows)
        # @TODO update the SourceImageCollectionQuerySet to use the same approach
        from ami.main.models import Occurrence

        taxa_subquery = (
            Occurrence.objects.filter(detections__source_image_id=models.OuterRef("pk"))
            .filter(filter_q)
            .values("detections__source_image_id")  # Group by source_image_id to get one row per source_image
            .annotate(count=models.Count("determination_id", distinct=True))
            .values("count")
        )

        return self.annotate(
            taxa_count=Coalesce(models.Subquery(taxa_subquery, output_field=models.IntegerField()), 0)
        )

    def with_was_processed(self):
        """
        Annotate each SourceImage with a boolean `was_processed` indicating
        whether any detections exist for that image.

        This mirrors `SourceImage.get_was_processed()` but as a queryset
        annotation for efficient bulk queries.
        """
        # @TODO: this returns a was processed status for any algorithm. One the session detail view supports
        # filtering by algorithm, this should be updated to return was_processed for the selected algorithm.
        from ami.main.models import Detection

        processed_exists = models.Exists(Detection.objects.filter(source_image_id=models.OuterRef("pk")))
        return self.annotate(was_processed=processed_exists)

    def with_thumbnails(self):
        """Prefetch ``thumbnails`` so :meth:`SourceImage.thumbnail_urls` decides
        warm/cold in memory instead of firing a SELECT per row.
        """
        return self.prefetch_related("thumbnails")


class SourceImageManager(models.Manager.from_queryset(SourceImageQuerySet)):
    pass


@final
class SourceImage(BaseModel):
    """A single image captured during a monitoring session"""

    path = models.CharField(max_length=255, blank=True)
    public_base_url = models.CharField(max_length=255, blank=True, null=True)
    timestamp = models.DateTimeField(null=True, blank=True, db_index=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    # File metadata read from the source file in storage, populated by the sync
    # flow and upload handler. Writable in the API for manual SourceImage
    # creation; read-only in the admin.
    size = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Size of the image file in bytes, read from the source file in image storage.",
    )
    last_modified = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the image file was last modified, read from the source file in image storage.",
    )
    checksum = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Checksum of the image file, read from the source file in image storage.",
    )
    checksum_algorithm = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Algorithm used for the checksum (e.g. MD5, SHA256).",
    )
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    test_image = models.BooleanField(default=False)

    # Precaclulated values
    detections_count = models.IntegerField(null=True, blank=True)

    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name="captures")
    deployment = models.ForeignKey(Deployment, on_delete=models.SET_NULL, null=True, related_name="captures")
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        related_name="captures",
        db_index=True,
        blank=True,
    )

    event_id: int | None
    detections: models.QuerySet["Detection"]
    collections: models.QuerySet["SourceImageCollection"]
    jobs: models.QuerySet["Job"]

    objects = SourceImageManager()

    def __str__(self) -> str:
        return f"{self.__class__.__name__} #{self.pk} {self.path}"

    @staticmethod
    def build_public_url(base_url: str, path: str) -> str:
        """Join a public base URL with a stored object path.

        Shared with callers that have annotated `public_base_url` + `path` onto a
        queryset row and want to skip loading the SourceImage instance.
        """
        return urllib.parse.urljoin(base_url, path.lstrip("/"))

    def public_url(self, raise_errors=False) -> str | None:
        """
        Return the public URL for this image.

        The base URL is determined by the deployment's data source and is cached
        on the source image. If the deployment's data source changes, the URLs
        for all source images will be updated.

        @TODO add support for thumbnail URLs here?
        @TODO consider if we ever need to access the original image directly!
        @TODO every source image request requires joins for the deployment and data source, is this necessary?
        """
        # Get presigned URL if access keys are configured
        data_source = self.deployment.data_source if self.deployment and self.deployment.data_source else None
        if (
            data_source is not None
            and not data_source.public_base_url
            and data_source.access_key
            and data_source.secret_key
        ):
            url = ami.utils.s3.get_presigned_url(data_source.config, key=self.path)
        elif self.public_base_url:
            url = self.build_public_url(self.public_base_url, self.path)
        else:
            msg = f"Public URL for {self} is not available. Public base URL: '{self.public_base_url}'"
            if raise_errors:
                raise ValueError(msg)
            else:
                logger.error(msg)
                return None
        # Ensure url has a scheme
        if not urllib.parse.urlparse(url).netloc:
            msg = f"Public URL for {self} is invalid: {url}. Public base URL: '{self.public_base_url}'"
            if raise_errors:
                raise ValueError(msg)
            else:
                logger.error(msg)
                return None
        else:
            return url

    # backwards compatibility
    url = public_url

    def size_display(self) -> str:
        """
        Return the size of the image in human-readable format.
        """
        if self.size is None:
            return filesizeformat(0)
        else:
            return filesizeformat(self.size)

    def get_detections_count(self) -> int:
        """
        Return detections count filtered by project default filters.

        Excludes detections without bounding boxes — those are placeholder records
        indicating the image was successfully processed and no detections were found.
        """
        qs = self.detections.all().valid()
        project = self.project
        if not project:
            return qs.distinct().count()

        q = build_occurrence_default_filters_q(
            project=project,
            request=None,
            occurrence_accessor="occurrence",
        )
        return qs.filter(q).distinct().count()

    def get_was_processed(self, algorithm_key: str | None = None) -> bool:
        """
        Return True if this image has been processed by any algorithm (or a specific one).

        Uses the ``was_processed`` annotation when available (set by
        ``SourceImageQuerySet.with_was_processed()``). Falls back to a DB query otherwise.

        Do not call in bulk without the annotation — use ``with_was_processed()``
        on the queryset instead so each row does not trigger its own DB query.

        :param algorithm_key: If provided, only detections from this algorithm are checked.
                              The annotation does not filter by algorithm; per-algorithm
                              checks always use a DB query.
        """
        if algorithm_key is None and hasattr(self, "was_processed"):
            return self.was_processed  # type: ignore[return-value]
        if algorithm_key:
            return self.detections.filter(detection_algorithm__key=algorithm_key).exists()
        return self.detections.exists()

    def get_base_url(self) -> str | None:
        """
        Determine the public URL from the deployment's data source.

        If there is no data source, return None

        If the public_base_url is None, a presigned URL will be generated for each request.
        """
        if self.deployment and self.deployment.data_source and self.deployment.data_source.public_base_url:
            return self.deployment.data_source.public_base_url
        else:
            return None

    def extract_timestamp(self) -> datetime.datetime | None:
        """
        Extract a timestamp from the filename or EXIF data
        """
        # @TODO use EXIF data if necessary (use methods in AMI data companion repo)
        timestamp = ami.utils.dates.get_image_timestamp_from_filename(self.path)
        if not timestamp:
            # timestamp = ami.utils.dates.get_image_timestamp_from_exif(self.path)
            msg = f"No timestamp could be extracted from the filename or EXIF data of {self.path}"
            logger.error(msg)
        return timestamp

    def event_next_capture_id(self) -> int | None:
        """
        Return the next capture in the event.

        This should be populated by the query in the ViewSet
        but here is the query for reference:
        return SourceImage.objects.filter(
        event=self.event, timestamp__gt=self.timestamp).order_by("timestamp").values("id").first()
        """
        return None

    def event_prev_capture_id(self) -> int | None:
        """
        Return the previous capture in the event.

        This will be populated by the query in the ViewSet but here is the query for reference:
        return SourceImage.objects.filter(
        event=self.event, timestamp__lt=self.timestamp).order_by("-timestamp").values("id").first()
        """
        return None

    def event_current_capture_index(self) -> int | None:
        """
        Return the index of the current capture in the event.

        This will be populated by the query in the ViewSet but here is the query for reference:
        return SourceImage.objects.filter(
        event=self.event, timestamp__lt=self.timestamp).count()
        or using window functions:
        return SourceImage.objects.filter(
            event=self.event, timestamp__lt=self.timestamp).annotate(
            index=models.Window(
            expression=models.functions.RowNumber(),
            order_by=models.F("timestamp").desc(),
        )
        ).values("index").first()
        """
        return None

    def event_total_captures(self) -> int | None:
        """
        Return the total number of captures in the event.

        This will be populated by the query in the ViewSet but here is the query for reference:
        return SourceImage.objects.filter(event=self.event).count()

        These values are used to help navigate between images in the event.

        @TODO Can we remove these methods? Seems to be a requirement for DRF serializers.
        """
        return None

    def get_dimensions(self) -> tuple[int | None, int | None]:
        """Calculate the width and height of the original image."""
        if self.path and self.deployment and self.deployment.data_source:
            config = self.deployment.data_source.config
            try:
                img = ami.utils.s3.read_image(config=config, key=self.path)
            except Exception as e:
                logger.error(f"Could not determine image dimensions for {self.path}: {e}")
            else:
                self.width, self.height = img.size
                self.save()
                return self.width, self.height
        return None, None

    def occurrences_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        return None

    def taxa_count(self) -> int | None:
        # This should always be pre-populated using queryset annotations
        return None

    def update_calculated_fields(self, save=False):
        if self.path and not self.timestamp:
            self.timestamp = self.extract_timestamp()
        if self.path and not self.public_base_url:
            self.public_base_url = self.get_base_url()
        if not self.project and self.deployment:
            self.project = self.deployment.project
        if self.pk is not None:
            self.detections_count = self.get_detections_count()
        if save:
            self.save(update_calculated_fields=False)

    def save(self, update_calculated_fields=True, *args, **kwargs):
        super().save(*args, **kwargs)
        if update_calculated_fields:
            self.update_calculated_fields(save=True)

    def check_custom_permission(self, user, action: str) -> bool:
        project = self.get_project() if hasattr(self, "get_project") else None
        if action in ["star", "unstar"]:
            return user.has_perm(Project.Permissions.STAR_SOURCE_IMAGE, project)

    def get_custom_user_permissions(self, user) -> list[str]:
        project = self.get_project()
        if not project:
            return []

        custom_perms = set()
        perms = get_perms(user, project)
        for perm in perms:
            # permissions are in the format "action_modelname"
            if perm.endswith("_sourceimage"):
                # process_single_image_sourceimage
                action = perm.split("_", 1)[0]
                # make sure to exclude standard CRUD actions
                if action not in ["view", "create", "update", "delete"]:
                    custom_perms.add(action)
        if Project.Permissions.RUN_SINGLE_IMAGE_JOB in perms:
            custom_perms.add(Project.Permissions.RUN_SINGLE_IMAGE_JOB)
        return list(custom_perms)

    def thumbnail_is_valid(self, spec: dict, thumb: "SourceImageThumbnail | None") -> bool:
        """Whether ``thumb`` satisfies ``spec`` and need not be regenerated.

        ``thumb.width`` stores the requested spec width (see the generator), so the
        comparison is strict equality; legacy encoder-width rows read invalid and
        self-heal on next generation. A None ``last_modified`` on either side means
        "no signal of change" (matches ``NULL < x`` → ``False`` in SQL).
        """
        if thumb is None or not thumb.path or thumb.width != spec["width"]:
            return False
        source_changed = (
            self.last_modified is not None
            and thumb.last_modified is not None
            and thumb.last_modified < self.last_modified
        )
        return not source_changed

    def thumbnail_urls(self, request: Request | None = None) -> dict[str, str]:
        """Per-label ``{label: url}`` for this capture's thumbnails.

        Warm (cached row valid for the spec) → direct storage URL. Cold/stale →
        route URL into the thumbnail viewset, which (re)generates lazily.

        The warm path needs prefetched ``thumbnails``
        (:meth:`SourceImageQuerySet.with_thumbnails`). Without the prefetch — a
        freshly created instance in a write response, or a caller that skipped
        ``with_thumbnails`` — every label falls back to the route URL without
        querying. This never lazily loads per object (which would be an N+1 in
        list contexts); list endpoints must apply ``with_thumbnails`` to get the
        warm storage URLs, and the list query-count tests pin that.
        """
        # Local import avoids a models ↔ serializers cycle at module load time.
        from ami.base.serializers import reverse_with_params

        prefetched = "thumbnails" in getattr(self, "_prefetched_objects_cache", {})
        thumbs: dict[str, "SourceImageThumbnail"] = {t.label: t for t in self.thumbnails.all()} if prefetched else {}

        out: dict[str, str] = {}
        for label, spec in settings.THUMBNAILS["SIZES"].items():
            thumb = thumbs.get(label)
            if self.thumbnail_is_valid(spec, thumb):
                out[label] = default_storage.url(thumb.path)
            else:
                # Qualified ``api:`` namespace so this also resolves when ``request``
                # is None (management commands, template tags).
                out[label] = reverse_with_params(
                    "api:sourceimagethumbnail-detail",
                    args=(self.pk,),
                    request=request,
                    params={"label": label},
                )
        return out

    def find_or_generate_thumbnail_for_label(self, label):
        try:
            thumb = self.thumbnails.get(label=label)
        except SourceImageThumbnail.DoesNotExist:
            thumb = None
        size = settings.THUMBNAILS["SIZES"].get(label)
        prefix = settings.THUMBNAILS["STORAGE_PREFIX"]

        # The row is trusted without a storage existence check; an orphan row (blob
        # deleted out of band) shows a broken image until the row is removed.
        if not self.thumbnail_is_valid(size, thumb):
            img = PIL.Image.open(BytesIO(fetch_image_content(self.public_url(raise_errors=True))))
            # JPEG only supports L, RGB, CMYK — convert other modes (e.g. RGBA PNGs)
            # or PIL raises ``OSError: cannot write mode <X> as JPEG``.
            if img.mode not in ("L", "RGB", "CMYK"):
                img = img.convert("RGB")
            # Make the thumbnail
            orig_width, orig_height = img.size
            width = size["width"]
            height = size.get("height", None)
            if not height:
                height = int(orig_height * (width / float(orig_width)))
            new_size = (width, height)
            img.thumbnail(new_size)

            buffer = BytesIO()
            img.save(buffer, format="JPEG", progressive=True, optimize=True, quality=82)
            contents = buffer.getvalue()
            file_size = len(contents)

            # Snapshot the prior blob path before the upsert, for cleanup below.
            prior_path = self.thumbnails.filter(label=label).values_list("path", flat=True).first()

            # Storage backends may overwrite the key in place or suffix on collision —
            # record whichever path the backend returns.
            buffer.seek(0)
            thumbnail_key = f"{prefix}capture_{self.pk}/{label}.jpg"
            thumbnail_path = default_storage.save(thumbnail_key, buffer)

            # Atomic upsert: concurrent generation races on the (source_image, label)
            # unique constraint. ``width`` stores the requested spec width, not the
            # encoder output — PIL's rounding (e.g. 239 for a 240 spec) would otherwise
            # fail the strict regen gate above on every request. ``height`` is informational.
            thumb, _created = self.thumbnails.update_or_create(
                label=label,
                defaults={
                    "path": thumbnail_path,
                    "width": size["width"],
                    "height": img.size[1],
                    "size": file_size,
                },
            )
            # ``last_modified`` is ``auto_now_add`` (set on INSERT only) — force-bump it
            # on UPDATE or the freshness check re-triggers regen on every request.
            if not _created:
                type(thumb).objects.filter(pk=thumb.pk).update(last_modified=timezone.now())
                thumb.refresh_from_db(fields=["last_modified"])

            # Best-effort cleanup of the replaced blob; failure must not break the response.
            if prior_path and prior_path != thumbnail_path:
                try:
                    default_storage.delete(prior_path)
                except Exception as e:
                    logger.warning(f"Could not delete prior thumbnail blob at {prior_path}: {e}")
        return thumb

    class Meta:
        ordering = ("deployment", "event", "timestamp")

        # Add two "unique together" constraints to prevent duplicate images
        constraints = [
            # deployment + path (only one image per deployment with a given file path)
            models.UniqueConstraint(fields=["deployment", "path"], name="unique_deployment_path"),
        ]

        indexes = [
            models.Index(fields=["deployment", "timestamp"]),
            models.Index(fields=["event", "timestamp"]),
            models.Index(fields=["timestamp"]),
            # Backs the project "recent captures" sort: a per-project max(timestamp)
            # lookup (see ProjectViewSet ordering "last_capture_timestamp").
            models.Index(fields=["project", "-timestamp"], name="main_source_proj_ts_desc_idx"),
        ]


def update_detection_counts(
    qs: models.QuerySet[SourceImage] | None = None,
    null_only=False,
    project: "Project | None" = None,
) -> int:
    """
    Update the detection count for all source images using a bulk update query.

    When ``project`` is provided, the count is filtered by that project's default
    filters so the cached ``SourceImage.detections_count`` stays consistent with
    ``SourceImage.get_detections_count()``.

    @TODO Needs testing.
    """
    from ami.main.models import Detection

    qs = qs or SourceImage.objects.all()
    if null_only:
        qs = qs.filter(detections_count__isnull=True)

    detection_qs = Detection.objects.filter(source_image_id=models.OuterRef("pk")).valid()
    if project is not None:
        filter_q = build_occurrence_default_filters_q(
            project=project,
            request=None,
            occurrence_accessor="occurrence",
        )
        detection_qs = detection_qs.filter(filter_q)
    subquery = models.Subquery(
        detection_qs.values("source_image_id").annotate(count=models.Count("id")).values("count"),
        output_field=models.IntegerField(),
    )
    start_time = time.time()
    # Use Coalesce to default to 0 instead of NULL
    num_updated = qs.update(detections_count=models.functions.Coalesce(subquery, models.Value(0)))
    end_time = time.time()
    elapsed_time = end_time - start_time
    logger.info(f"Updated detection counts for {num_updated} source images in {elapsed_time:.2f} seconds")
    return num_updated


def set_dimensions_for_collection(
    event: Event, replace_existing: bool = False, width: int | None = None, height: int | None = None
):
    """
    Set the width & height of all of the images in the event based on one image.

    This will look for the first image in the event that already has dimensions.
    If no images have dimensions, the first image be retrieved from the data source.

    This is much more practical than fetching each image. However if a deployment
    does ever have images with mixed dimensions, another method will be needed.

    @TODO consider adding "assumed image dimensions" to the Deployment instance itself.
    """

    if not width or not height:
        # Try retrieving dimensions from deployment
        width, height = getattr(event.deployment, "assumed_image_dimensions", (None, None))

    if not width or not height:
        # Try retrieving dimensions from the first image that has them already
        image = event.captures.exclude(width__isnull=True, height__isnull=True).first()
        if image:
            width, height = image.width, image.height

    if not width or not height:
        image = event.captures.first()
        if image:
            width, height = image.get_dimensions()

    if width and height:
        logger.info(
            f"Setting dimensions for {event.captures.count()} images in event {event.pk} to " f"{width}x{height}"
        )
        if replace_existing:
            captures = event.captures.all()
        else:
            captures = event.captures.filter(width__isnull=True, height__isnull=True)
        captures.update(width=width, height=height)

    else:
        logger.warning(
            f"Could not determine image dimensions for event {event.pk}. "
            f"Width & height will not be set on any source images."
        )


def sample_captures_by_interval(
    minute_interval: int,
    qs: models.QuerySet[SourceImage],
    max_num: int | None = None,
) -> typing.Generator[SourceImage, None, None]:
    """
    Return a sample of captures from the deployment, evenly spaced apart by minute_interval.
    """

    last_capture = None
    total = 0

    qs = qs.exclude(timestamp=None).order_by("timestamp")

    for capture in qs.all():
        if max_num and total >= max_num:
            break
        if not last_capture:
            total += 1
            yield capture
            last_capture = capture
        else:
            assert capture.timestamp and last_capture.timestamp
            delta: datetime.timedelta = capture.timestamp - last_capture.timestamp
            if delta.total_seconds() >= minute_interval * 60:
                total += 1
                yield capture
                last_capture = capture


def sample_captures_by_position(
    position: int,
    qs: models.QuerySet[SourceImage],
) -> typing.Generator[SourceImage | None, None, None]:
    """
    Return the n-th position capture from each event.

    For example if position = 0, the first capture from each event will be returned.
    If position = -1, the last capture from each event will be returned.
    """

    qs = qs.exclude(timestamp=None).order_by("timestamp")

    events = Event.objects.filter(captures__in=qs).distinct()
    for event in events:
        qs = qs.filter(event=event)
        if position < 0:
            # Negative positions are relative to the end of the queryset
            # e.g. -1 is the last item, -2 is the second last item, etc.
            # but querysets do not support negative indexing, so we
            # sort the queryset in reverse order and then use positive indexing.
            # e.g. -1 becomes 0, -2 becomes 1, etc.
            position = abs(position) - 1
            qs = qs.order_by("-timestamp")
        else:
            qs = qs.order_by("timestamp")
        try:
            capture = qs[position]
        except IndexError:
            # If the position is out of range, just return the last capture
            capture = qs.last()

        yield capture


def sample_captures_by_nth(
    nth: int,
    qs: models.QuerySet[SourceImage],
) -> typing.Generator[SourceImage, None, None]:
    """
    Return every nth capture from each event.

    For example if nth = 1, every capture from each event will be returned.
    If nth = 5, every 5th capture from each event will be returned.
    """

    qs = qs.exclude(timestamp=None).order_by("timestamp")

    events = Event.objects.filter(captures__in=qs).distinct()
    for event in events:
        qs = qs.filter(event=event).order_by("timestamp")
        yield from qs[::nth]


@final
class SourceImageThumbnail(BaseModel):
    """A thumbnail cache of a SourceImage"""

    path = models.CharField(max_length=255, blank=True)
    label = models.CharField(max_length=255)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    last_modified = models.DateTimeField(null=True, blank=True, auto_now_add=True)

    # CASCADE: thumbnails are pure derivatives of their source. SET_NULL leaves
    # orphan rows + dangling storage blobs forever and nothing else reaps them.
    # The pre_delete signal in ``ami.main.signals`` cleans the storage blob.
    source_image = models.ForeignKey(SourceImage, on_delete=models.CASCADE, related_name="thumbnails")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_image", "label"],
                name="unique_source_image_thumbnail_label",
            ),
        ]
        indexes = [
            models.Index(fields=["source_image", "label"]),
        ]


# @final
# class IdentificationHistory(BaseModel):
#     """A history of identifications for an occurrence."""
#
#     # @TODO
#     pass
