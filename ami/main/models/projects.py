import logging
import typing
from typing import final

import pydantic
from django.conf import settings
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.db import models, transaction
from django_pydantic_field import SchemaField

from ami.base.models import BaseModel, BaseQuerySet
from ami.main import charts
from ami.main.models_future.projects import ProjectSettingsMixin
from ami.users.models import User

from .common import _POST_TITLE_MAX_LENGTH

if typing.TYPE_CHECKING:
    from ami.jobs.models import Job
    from ami.ml.models import Pipeline, ProcessingService

    from .collections import SourceImageCollection
    from .deployments import Deployment, Device, Site
    from .events import Event
    from .occurrences import Occurrence
    from .source_images import SourceImage
    from .taxonomy import Tag, TaxaList, Taxon

logger = logging.getLogger(__name__)


def get_or_create_default_device(project: "Project") -> "Device":
    """Create a default device for a project."""
    from ami.main.models import Device

    device, _created = Device.objects.get_or_create(name="Default Device", project=project)
    logger.info(f"Created default device for project {project}")
    return device


def get_or_create_default_research_site(project: "Project") -> "Site":
    """Create a default research site for a project."""
    from ami.main.models import Site

    site, _created = Site.objects.get_or_create(name="Default Site", project=project)
    logger.info(f"Created default research site for project {project}")
    return site


def get_or_create_default_deployment(
    project: "Project", site: "Site | None" = None, device: "Device | None" = None
) -> "Deployment":
    """Create a default deployment for a project."""
    from ami.main.models import Deployment

    deployment, _created = Deployment.objects.get_or_create(
        name="Default Station",
        project=project,
        research_site=site,
        device=device,
        latitude=0,
        longitude=0,
    )
    logger.info(f"Created default deployment for project {project}")
    return deployment


def get_or_create_default_collection(project: "Project") -> "SourceImageCollection":
    """
    Create a default collection for a project for all images.

    @TODO Consider ways to update this collection automatically. With a query-only collection
    or a periodic task that runs the populate_collection method.
    """
    from ami.main.models import SourceImageCollection

    collection, _created = SourceImageCollection.objects.get_or_create(
        name="All Images",
        project=project,
        method="full",
    )
    logger.info(f"Created default capture set for project {project}")
    return collection


def get_project_default_filters():
    """
    Read default taxa names from Django settings (read from environment variables)
    and return corresponding Taxon objects.
    """
    from ami.main.models import Taxon

    include_taxa = list(Taxon.objects.filter(name__in=settings.DEFAULT_INCLUDE_TAXA))
    exclude_taxa = list(Taxon.objects.filter(name__in=settings.DEFAULT_EXCLUDE_TAXA))

    return {"default_include_taxa": include_taxa, "default_exclude_taxa": exclude_taxa}


def get_or_create_default_project(user: User) -> "Project":
    """
    Create a default project for a user.

    When a new project is created, default related objects (device, site,
    deployment, collection, processing service) and default taxa filters are
    initialized explicitly. ``get_or_create`` bypasses ``ProjectManager.create``,
    so we call ``create_related_defaults`` here instead of relying on the manager.
    """
    project, created = Project.objects.get_or_create(name="Scratch Project", owner=user)
    if created:
        logger.info(f"Created default project for user {user}")
        Project.objects.create_related_defaults(project)
        defaults = get_project_default_filters()

        if defaults["default_include_taxa"]:
            project.default_filters_include_taxa.set(defaults["default_include_taxa"])
            logger.info(f"Set {len(defaults['default_include_taxa'])} default include taxa for project {project}")
        if defaults["default_exclude_taxa"]:
            project.default_filters_exclude_taxa.set(defaults["default_exclude_taxa"])
            logger.info(f"Set {len(defaults['default_exclude_taxa'])} default exclude taxa for project {project}")
        project.save()
    else:
        logger.info(f"Loaded existing default project for user {user}")
    return project


class ProjectQuerySet(BaseQuerySet):
    def filter_by_user(self, user: User):
        """
        Filters projects to include only those where the given user is a member.
        """
        return self.filter(members=user)


class ProjectManager(models.Manager.from_queryset(ProjectQuerySet)):
    pass

    def create(self, create_defaults: bool = True, **kwargs) -> "Project":
        """
        Create a new Project and related models with defaults.

        Args:
            create_defaults: Whether to create default related models
            **kwargs: Model field values

        Returns:
            Created Project instance
        """
        with transaction.atomic():
            project_instance = super().create(**kwargs)
            logger.info(f"Created project: {project_instance.name}")

            if create_defaults:
                self.create_related_defaults(project_instance)

            return project_instance

    def create_related_defaults(self, project: "Project"):
        """Create default device, and other related models for this project if they don't exist."""
        device = get_or_create_default_device(project=project)
        site = get_or_create_default_research_site(project=project)
        if not project.deployments.exists():
            get_or_create_default_deployment(project=project, site=site, device=device)
        if not project.sourceimage_collections.exists():
            get_or_create_default_collection(project=project)
        if not project.processing_services.exists():
            from ami.ml.models.processing_service import get_or_create_default_processing_service

            get_or_create_default_processing_service(project=project)


class ProjectFeatureFlags(pydantic.BaseModel):
    """
    Feature flags for the project.
    """

    tags: bool = False  # Whether the project supports tagging taxa
    reprocess_existing_detections: bool = False  # Whether to reprocess existing detections
    default_filters: bool = False  # Whether to show default filters form in UI
    # Feature flag for jobs to reprocess all images in the project, even if already processed
    reprocess_all_images: bool = False
    async_pipeline_workers: bool = True  # Whether to use async pipeline workers that pull tasks from a queue


def get_default_feature_flags() -> ProjectFeatureFlags:
    return ProjectFeatureFlags()


# Existing migrations serialize these as "ami.main.models.<name>" (their home before
# the models package split). Pin the path so makemigrations doesn't see a field change.
ProjectFeatureFlags.__module__ = "ami.main.models"
get_default_feature_flags.__module__ = "ami.main.models"


@final
class Project(ProjectSettingsMixin, BaseModel):
    """ """

    name = models.CharField(max_length=_POST_TITLE_MAX_LENGTH)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="projects", blank=True, null=True)
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="projects")
    members = models.ManyToManyField(
        User,
        through="UserProjectMembership",
        related_name="user_projects",
        blank=True,
    )
    draft = models.BooleanField(
        default=False,
        help_text="Indicates whether this project is in draft mode",
    )
    feature_flags = SchemaField(
        ProjectFeatureFlags,
        default=get_default_feature_flags,
        null=False,
        blank=True,
    )

    active = models.BooleanField(default=True)
    priority = models.IntegerField(default=1)

    # Backreferences for type hinting
    captures: models.QuerySet["SourceImage"]
    deployments: models.QuerySet["Deployment"]
    events: models.QuerySet["Event"]
    occurrences: models.QuerySet["Occurrence"]
    taxa: models.QuerySet["Taxon"]
    taxa_lists: models.QuerySet["TaxaList"]
    devices: models.QuerySet["Device"]
    sites: models.QuerySet["Site"]
    jobs: models.QuerySet["Job"]
    sourceimage_collections: models.QuerySet["SourceImageCollection"]
    processing_services: models.QuerySet["ProcessingService"]
    pipelines: models.QuerySet["Pipeline"]
    tags: models.QuerySet["Tag"]

    objects = ProjectManager()

    def ensure_owner_membership(self):
        """Add owner to members if they are not already a member"""
        if self.owner and not self.members.filter(id=self.owner.pk).exists():
            self.members.add(self.owner)

    @property
    def thumbnails_enabled(self) -> bool:
        """Whether captures in this project should expose server-generated thumbnail URLs.

        Draft projects aren't anonymously readable, and the thumbnail endpoint is loaded
        via an anonymous <img> tag that can't authenticate, so it would 401. Proxy for
        "anonymous can retrieve captures"; revisit if visibility decouples from `draft`.
        Serving thumbnails for private projects is tracked in #1341.
        """
        return not self.draft

    def deployments_count(self) -> int:
        return self.deployments.count()

    def taxa_count(self):
        return self.taxa.all().count()

    def summary_data(self):
        """
        Data prepared for rendering charts with plotly.js on the overview page.
        """

        return [
            {
                "id": "captures",
                "title": "Captures",
                "plots": [
                    charts.captures_per_hour(project_pk=self.pk),
                    charts.captures_per_month(project_pk=self.pk),
                ],
            },
            {
                "id": "occurrences",
                "title": "Occurrences",
                "plots": [
                    charts.detections_per_hour(project_pk=self.pk),
                    charts.average_occurrences_per_month(project_pk=self.pk),
                ],
            },
            {
                "id": "taxa",
                "title": "Taxa",
                "plots": [
                    charts.project_top_taxa(project_pk=self.pk),
                    charts.unique_species_per_month(project_pk=self.pk),
                ],
            },
        ]

    def update_related_calculated_fields(self):
        """
        Update calculated fields for all related events, deployments, and source images.
        """
        from ami.main.models import SourceImage, update_detection_counts

        # Update events
        for event in self.events.all():
            event.update_calculated_fields(save=True)

        # Update deployments
        for deployment in self.deployments.all():
            deployment.update_calculated_fields(save=True)

        # Update source image cached detection counts using the project's default filters
        # so SourceImage.detections_count stays consistent with get_detections_count().
        update_detection_counts(qs=SourceImage.objects.filter(project=self), project=self)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Add owner to members
        self.ensure_owner_membership()

    def check_custom_permission(self, user, action: str) -> bool:
        """
        Check custom permissions for actions like 'charts'.
        Charts is treated as a read-only operation, so it follows the same
        permission logic as 'retrieve'.
        """
        from ami.users.roles import BasicMember, ProjectManager

        if action == "charts":
            # Same permission logic as retrieve action
            if self.draft:
                # Allow view permission for members and owners of draft projects
                return BasicMember.has_role(user, self) or user == self.owner or user.is_superuser
            return True

        if action == "pipelines":
            # Pipeline registration requires project management permissions
            return ProjectManager.has_role(user, self) or user == self.owner or user.is_superuser

        # Fall back to default permission checking for other actions
        return super().check_custom_permission(user, action)

    class Permissions:
        """CRUD Permission names follow the convention: `create_<model>`, `update_<model>`,
        `delete_<model>`, `view_<model>`"""

        # Project permissions
        VIEW_PROJECT = "view_project"
        UPDATE_PROJECT = "update_project"
        DELETE_PROJECT = "delete_project"
        CREATE_PROJECT = "create_project"

        # Identification permissions
        CREATE_IDENTIFICATION = "create_identification"
        UPDATE_IDENTIFICATION = "update_identification"
        DELETE_IDENTIFICATION = "delete_identification"

        # Job permissions
        CREATE_JOB = "create_job"
        UPDATE_JOB = "update_job"
        RUN_ML_JOB = "run_ml_job"
        RUN_SINGLE_IMAGE_JOB = "run_single_image_ml_job"
        RUN_POPULATE_CAPTURES_COLLECTION_JOB = "run_populate_captures_collection_job"
        RUN_DATA_STORAGE_SYNC_JOB = "run_data_storage_sync_job"
        RUN_REGROUP_EVENTS_JOB = "run_regroup_events_job"
        RUN_DATA_EXPORT_JOB = "run_data_export_job"
        RUN_POST_PROCESSING_JOB = "run_post_processing_job"
        DELETE_JOB = "delete_job"

        # Deployment permissions
        CREATE_DEPLOYMENT = "create_deployment"
        DELETE_DEPLOYMENT = "delete_deployment"
        UPDATE_DEPLOYMENT = "update_deployment"
        SYNC_DEPLOYMENT = "sync_deployment"
        REGROUP_SESSIONS_DEPLOYMENT = "regroup_sessions_deployment"

        # Collection permissions
        CREATE_COLLECTION = "create_sourceimagecollection"
        UPDATE_COLLECTION = "update_sourceimagecollection"
        DELETE_COLLECTION = "delete_sourceimagecollection"
        POPULATE_COLLECTION = "populate_sourceimagecollection"

        # Source Image permissions
        CREATE_SOURCE_IMAGE = "create_sourceimage"
        UPDATE_SOURCE_IMAGE = "update_sourceimage"
        DELETE_SOURCE_IMAGE = "delete_sourceimage"
        STAR_SOURCE_IMAGE = "star_sourceimage"

        # SourceImageUpload permissions
        CREATE_SOURCE_IMAGE_UPLOAD = "create_sourceimageupload"
        UPDATE_SOURCE_IMAGE_UPLOAD = "update_sourceimageupload"
        DELETE_SOURCE_IMAGE_UPLOAD = "delete_sourceimageupload"
        # Storage permissions
        CREATE_STORAGE = "create_s3storagesource"
        DELETE_STORAGE = "delete_s3storagesource"
        UPDATE_STORAGE = "update_s3storagesource"
        TEST_STORAGE = "test_s3storagesource"

        # Site permissions
        CREATE_SITE = "create_site"
        DELETE_SITE = "delete_site"
        UPDATE_SITE = "update_site"

        # Device permissions
        CREATE_DEVICE = "create_device"
        DELETE_DEVICE = "delete_device"
        UPDATE_DEVICE = "update_device"
        # User project membership permissions
        VIEW_USER_PROJECT_MEMBERSHIP = "view_userprojectmembership"
        CREATE_USER_PROJECT_MEMBERSHIP = "create_userprojectmembership"
        UPDATE_USER_PROJECT_MEMBERSHIP = "update_userprojectmembership"
        DELETE_USER_PROJECT_MEMBERSHIP = "delete_userprojectmembership"

        # Data Export permissions
        CREATE_DATA_EXPORT = "create_dataexport"
        UPDATE_DATA_EXPORT = "update_dataexport"
        DELETE_DATA_EXPORT = "delete_dataexport"

        # Pipeline configuration permissions
        CREATE_PROJECT_PIPELINE_CONFIG = "create_projectpipelineconfig"
        UPDATE_PROJECT_PIPELINE_CONFIG = "update_projectpipelineconfig"
        DELETE_PROJECT_PIPELINE_CONFIG = "delete_projectpipelineconfig"

        # TaxaList permissions
        CREATE_TAXALIST = "create_taxalist"
        UPDATE_TAXALIST = "update_taxalist"
        DELETE_TAXALIST = "delete_taxalist"

        # Other permissions
        VIEW_PRIVATE_DATA = "view_private_data"
        DELETE_OCCURRENCES = "delete_occurrences"
        IMPORT_DATA = "import_data"

    class Meta:
        ordering = ["-priority", "created_at"]
        permissions = [
            # Identification permissions
            ("create_identification", "Can create identifications"),
            ("update_identification", "Can update identifications"),
            ("delete_identification", "Can delete identifications"),
            # Job permissions
            ("create_job", "Can create a job"),
            ("update_job", "Can update a job"),
            ("run_ml_job", "Can run/retry/cancel ML jobs"),
            ("run_populate_captures_collection_job", "Can run/retry/cancel Populate Collection jobs"),
            ("run_data_storage_sync_job", "Can run/retry/cancel Data Storage Sync jobs"),
            ("run_regroup_events_job", "Can run/retry/cancel Regroup Events jobs"),
            ("run_data_export_job", "Can run/retry/cancel Data Export jobs"),
            ("run_single_image_ml_job", "Can process a single capture"),
            ("run_post_processing_job", "Can run/retry/cancel Post-Processing jobs"),
            ("delete_job", "Can delete a job"),
            # Deployment permissions
            ("create_deployment", "Can create a deployment"),
            ("delete_deployment", "Can delete a deployment"),
            ("update_deployment", "Can update a deployment"),
            ("sync_deployment", "Can sync images to a deployment"),
            ("regroup_sessions_deployment", "Can regroup deployment captures into sessions"),
            # Collection permissions
            ("create_sourceimagecollection", "Can create a collection"),
            ("update_sourceimagecollection", "Can update a collection"),
            ("delete_sourceimagecollection", "Can delete a collection"),
            ("populate_sourceimagecollection", "Can populate a collection"),
            # Source Image permissions
            ("create_sourceimage", "Can create a source image"),
            ("update_sourceimage", "Can update a source image"),
            ("delete_sourceimage", "Can delete a source image"),
            ("star_sourceimage", "Can star a source image"),
            # SourceImageUpload permissions
            ("create_sourceimageupload", "Can create a source image upload"),
            ("update_sourceimageupload", "Can update a source image upload"),
            ("delete_sourceimageupload", "Can delete a source image upload"),
            # Storage permissions
            ("create_s3storagesource", "Can create storage"),
            ("delete_s3storagesource", "Can delete storage"),
            ("update_s3storagesource", "Can update storage"),
            ("test_s3storagesource", "Can test storage connection"),
            # Site permissions
            ("create_site", "Can create a site"),
            ("delete_site", "Can delete a site"),
            ("update_site", "Can update a site"),
            # Device permissions
            ("create_device", "Can create a device"),
            ("delete_device", "Can delete a device"),
            ("update_device", "Can update a device"),
            # User project membership permissions
            ("view_userprojectmembership", "Can view project members"),
            ("create_userprojectmembership", "Can add a user to the project"),
            ("update_userprojectmembership", "Can update a user's project membership and role in the project"),
            ("delete_userprojectmembership", "Can remove a user from the project"),
            # Data Export permissions
            ("create_dataexport", "Can create a data export"),
            ("update_dataexport", "Can update a data export"),
            ("delete_dataexport", "Can delete a data export"),
            # Pipeline configuration permissions
            ("create_projectpipelineconfig", "Can register pipelines for the project"),
            ("update_projectpipelineconfig", "Can update pipeline configurations"),
            ("delete_projectpipelineconfig", "Can remove pipelines from the project"),
            # TaxaList permissions
            ("create_taxalist", "Can create a taxa list"),
            ("update_taxalist", "Can update a taxa list"),
            ("delete_taxalist", "Can delete a taxa list"),
            # Other permissions
            ("view_private_data", "Can view private data"),
        ]


class UserProjectMembership(BaseModel):
    """
    Through model connecting User <-> Project.
    This model represents membership ONLY.
    Role assignment is handled separately via permission groups.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="project_memberships",
    )

    project = models.ForeignKey(
        "main.Project",
        on_delete=models.CASCADE,
        related_name="project_memberships",
    )

    def check_permission(self, user: AbstractUser | AnonymousUser, action: str) -> bool:
        project = self.project
        # Allow viewing membership details if the user has view permission on the project
        if action == "retrieve":
            return user.has_perm(Project.Permissions.VIEW_USER_PROJECT_MEMBERSHIP, project)
        # Allow users to delete their own membership
        if action == "destroy" and user == self.user:
            return True
        return super().check_permission(user, action)

    def get_user_object_permissions(self, user) -> list[str]:
        # Return delete permission if user is the same as the membership user
        user_permissions = super().get_user_object_permissions(user)
        if user == self.user:
            if "delete" not in user_permissions:
                user_permissions.append("delete")
        return user_permissions

    class Meta:
        unique_together = ("user", "project")
