"""
Import a project from a JSON export file.

This command imports a complete project including all related data.
All user references are assigned to the importing user.

Example usage:
    docker compose run --rm django python manage.py import_project export.json --user antenna@insectai.org
    docker compose run --rm django python manage.py import_project export.json --user admin@example.com --project-name "New Project Name"
"""

import datetime
import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from ami.main.models import (
    Classification,
    Deployment,
    Detection,
    Device,
    Event,
    Identification,
    Occurrence,
    S3StorageSource,
    Site,
    SourceImage,
    SourceImageCollection,
    Tag,
    TaxaList,
    Taxon,
)
from ami.ml.models import Algorithm, AlgorithmCategoryMap, Pipeline, ProcessingService, ProjectPipelineConfig
from ami.users.models import User

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Import a project from a JSON export file"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ID mapping from old IDs to new IDs
        self.id_maps = {
            "project": {},
            "site": {},
            "device": {},
            "storage_source": {},
            "deployment": {},
            "event": {},
            "source_image": {},
            "collection": {},
            "taxon": {},
            "taxa_list": {},
            "tag": {},
            "detection": {},
            "classification": {},
            "occurrence": {},
            "identification": {},
            "algorithm": {},
            "category_map": {},
            "pipeline": {},
            "processing_service": {},
            "pipeline_config": {},
        }

    def add_arguments(self, parser):
        parser.add_argument("input_file", type=str, help="Path to the JSON export file")
        parser.add_argument(
            "--user",
            "-u",
            type=str,
            required=True,
            help="Email of the user who will own the imported project",
        )
        parser.add_argument(
            "--project-name",
            "-n",
            type=str,
            help="Override the project name (default: use name from export)",
        )
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="Skip importing source images (useful for testing structure only)",
        )
        parser.add_argument(
            "--skip-ml-data",
            action="store_true",
            help="Skip importing ML data (detections, classifications, occurrences)",
        )

    def handle(self, *args, **options):
        input_file = Path(options["input_file"])

        if not input_file.exists():
            raise CommandError(f'Input file "{input_file}" does not exist')

        # Get the importing user
        try:
            user = User.objects.get(email=options["user"])
        except User.DoesNotExist:
            raise CommandError(f'User with email "{options["user"]}" does not exist')

        self.stdout.write(f"Importing project from: {input_file}")
        self.stdout.write(f"Import user: {user.email}")

        # Load the export data
        with open(input_file, "r") as f:
            export_data = json.load(f)

        # Validate export version
        export_version = export_data.get("export_version")
        if export_version != "1.0":
            self.stdout.write(
                self.style.WARNING(f"Warning: Export version {export_version} may not be fully compatible")
            )

        # Import everything in a transaction
        with transaction.atomic():
            self.import_data(export_data, user, options)

        self.stdout.write(self.style.SUCCESS("\nImport completed successfully!"))
        self.print_summary(export_data)

    def import_data(self, export_data, user, options):
        """Import all data from the export file"""
        from ami.main.models import Project

        # Import in dependency order
        self.stdout.write("Importing data...")

        # 1. Project
        project = self.import_project(export_data["project"], user, options)

        # 2. Infrastructure
        self.import_sites(export_data["sites"], project)
        self.import_devices(export_data["devices"], project)
        self.import_storage_sources(export_data["storage_sources"], project)

        # 3. Deployments
        self.import_deployments(export_data["deployments"], project)

        # 4. Events
        self.import_events(export_data["events"], project)

        # 5. Taxa (before source images for filtering)
        self.import_taxa(export_data["taxa"], project)
        self.import_taxa_lists(export_data["taxa_lists"], project)
        self.import_tags(export_data["tags"], project)

        # 6. Source Images (can be skipped)
        if not options["skip_images"]:
            self.import_source_images(export_data["source_images"], project)
            self.import_collections(export_data["collections"], project)
        else:
            self.stdout.write(self.style.WARNING("  Skipping source images"))

        # 7. ML Data (can be skipped)
        if not options["skip_ml_data"] and not options["skip_images"]:
            self.import_algorithms(export_data["algorithms"], project)
            self.import_occurrences(export_data["occurrences"], project)
            self.import_detections(export_data["detections"], project)
            self.import_classifications(export_data["classifications"], project)
            self.import_identifications(export_data["identifications"], project, user)

            # Update occurrence determinations after creating all classifications
            self.update_occurrences(project)
        else:
            self.stdout.write(self.style.WARNING("  Skipping ML data"))

        # 8. Pipelines and services (optional)
        self.import_processing_services(export_data["processing_services"], project)
        self.import_pipelines(export_data["pipelines"], project)
        self.import_pipeline_configs(export_data["pipeline_configs"], project)

        self.stdout.write(self.style.SUCCESS(f"\nProject imported: {project.name} (ID: {project.pk})"))

    def import_project(self, project_data, user, options):
        """Import the project"""
        from ami.main.models import Project

        # Use override name if provided
        name = options.get("project_name") or project_data["name"]

        # Check if project with this name already exists
        if Project.objects.filter(name=name).exists():
            # Generate unique name
            base_name = name
            counter = 1
            while Project.objects.filter(name=name).exists():
                name = f"{base_name} ({counter})"
                counter += 1
            self.stdout.write(self.style.WARNING(f"  Project name already exists, using: {name}"))

        project = Project.objects.create(
            name=name,
            description=project_data.get("description", ""),
            owner=user,
            is_draft=project_data.get("is_draft", True),
            priority=project_data.get("priority", 0),
            image_base_url=project_data.get("image_base_url", ""),
            default_event_method=project_data.get("default_event_method"),
            default_event_time_threshold=project_data.get("default_event_time_threshold"),
            summary=project_data.get("summary", ""),
            details=project_data.get("details", ""),
            default_filters=project_data.get("default_filters"),
        )

        # Add user as member
        project.members.add(user)

        self.id_maps["project"][project_data["id"]] = project.pk
        self.stdout.write(f"  Imported project: {project.name}")
        return project

    def import_sites(self, sites_data, project):
        """Import sites"""
        for site_data in sites_data:
            site = Site.objects.create(
                project=project,
                name=site_data["name"],
                description=site_data.get("description", ""),
                latitude=site_data.get("latitude"),
                longitude=site_data.get("longitude"),
                elevation=site_data.get("elevation"),
            )
            self.id_maps["site"][site_data["id"]] = site.pk
        self.stdout.write(f"  Imported {len(sites_data)} sites")

    def import_devices(self, devices_data, project):
        """Import devices"""
        for device_data in devices_data:
            device = Device.objects.create(
                project=project,
                name=device_data["name"],
                description=device_data.get("description", ""),
                hardware_version=device_data.get("hardware_version", ""),
                software_version=device_data.get("software_version", ""),
            )
            self.id_maps["device"][device_data["id"]] = device.pk
        self.stdout.write(f"  Imported {len(devices_data)} devices")

    def import_storage_sources(self, sources_data, project):
        """Import S3 storage sources"""
        for source_data in sources_data:
            source = S3StorageSource.objects.create(
                project=project,
                name=source_data["name"],
                endpoint_url=source_data.get("endpoint_url", ""),
                bucket=source_data.get("bucket", ""),
                prefix=source_data.get("prefix", ""),
                access_key=source_data.get("access_key", ""),
                secret_key=source_data.get("secret_key", ""),
                public_base_url=source_data.get("public_base_url", ""),
            )
            self.id_maps["storage_source"][source_data["id"]] = source.pk
        self.stdout.write(f"  Imported {len(sources_data)} storage sources")

    def import_deployments(self, deployments_data, project):
        """Import deployments"""
        for deployment_data in deployments_data:
            # Map foreign keys
            research_site_id = self.id_maps["site"].get(deployment_data.get("research_site_id"))
            device_id = self.id_maps["device"].get(deployment_data.get("device_id"))
            data_source_id = self.id_maps["storage_source"].get(deployment_data.get("data_source_id"))

            deployment = Deployment.objects.create(
                project=project,
                name=deployment_data["name"],
                description=deployment_data.get("description", ""),
                research_site_id=research_site_id,
                device_id=device_id,
                data_source_id=data_source_id,
                data_source_subdir=deployment_data.get("data_source_subdir", ""),
                data_source_regex=deployment_data.get("data_source_regex", ""),
                latitude=deployment_data.get("latitude"),
                longitude=deployment_data.get("longitude"),
            )
            self.id_maps["deployment"][deployment_data["id"]] = deployment.pk
        self.stdout.write(f"  Imported {len(deployments_data)} deployments")

    def import_events(self, events_data, project):
        """Import events"""
        for event_data in events_data:
            deployment_id = self.id_maps["deployment"].get(event_data["deployment_id"])
            if not deployment_id:
                continue

            # Parse timestamps
            start = datetime.datetime.fromisoformat(event_data["start"]) if event_data.get("start") else None
            end = datetime.datetime.fromisoformat(event_data["end"]) if event_data.get("end") else None

            event = Event.objects.create(
                project=project,
                deployment_id=deployment_id,
                group_by=event_data["group_by"],
                start=start,
                end=end,
            )
            self.id_maps["event"][event_data["id"]] = event.pk
        self.stdout.write(f"  Imported {len(events_data)} events")

    def import_taxa(self, taxa_data, project):
        """Import taxa - handle in two passes to resolve parent relationships"""
        # First pass: create all taxa without parents
        taxa_by_old_id = {}
        for taxon_data in taxa_data:
            # Check if taxon already exists by name
            taxon = Taxon.objects.filter(name=taxon_data["name"]).first()
            if taxon:
                self.id_maps["taxon"][taxon_data["id"]] = taxon.pk
                taxa_by_old_id[taxon_data["id"]] = taxon
            else:
                taxon = Taxon.objects.create(
                    name=taxon_data["name"],
                    rank=taxon_data.get("rank", ""),
                    description=taxon_data.get("description", ""),
                    ordering=taxon_data.get("ordering", 0),
                )
                self.id_maps["taxon"][taxon_data["id"]] = taxon.pk
                taxa_by_old_id[taxon_data["id"]] = taxon

            # Add to project
            taxon.projects.add(project)

        # Second pass: set parent relationships
        for taxon_data in taxa_data:
            taxon = taxa_by_old_id[taxon_data["id"]]

            # Set parent if exists
            if taxon_data.get("parent_id"):
                parent_id = self.id_maps["taxon"].get(taxon_data["parent_id"])
                if parent_id:
                    taxon.parent_id = parent_id
                    taxon.save(update_calculated_fields=True)

            # Set synonym_of if exists
            if taxon_data.get("synonym_of_id"):
                synonym_id = self.id_maps["taxon"].get(taxon_data["synonym_of_id"])
                if synonym_id:
                    taxon.synonym_of_id = synonym_id
                    taxon.save()

        self.stdout.write(f"  Imported {len(taxa_data)} taxa")

    def import_taxa_lists(self, taxa_lists_data, project):
        """Import taxa lists"""
        for taxa_list_data in taxa_lists_data:
            taxa_list = TaxaList.objects.create(
                name=taxa_list_data["name"],
                description=taxa_list_data.get("description", ""),
            )
            taxa_list.projects.add(project)

            # Add taxa to list
            taxon_ids = [self.id_maps["taxon"].get(old_id) for old_id in taxa_list_data.get("taxa_ids", [])]
            taxon_ids = [tid for tid in taxon_ids if tid]  # Filter out None values
            if taxon_ids:
                taxa_list.taxa.set(taxon_ids)

            self.id_maps["taxa_list"][taxa_list_data["id"]] = taxa_list.pk
        self.stdout.write(f"  Imported {len(taxa_lists_data)} taxa lists")

    def import_tags(self, tags_data, project):
        """Import tags"""
        for tag_data in tags_data:
            tag = Tag.objects.create(
                project=project,
                name=tag_data["name"],
                description=tag_data.get("description", ""),
                color=tag_data.get("color", ""),
            )

            # Add taxa to tag
            taxon_ids = [self.id_maps["taxon"].get(old_id) for old_id in tag_data.get("taxa_ids", [])]
            taxon_ids = [tid for tid in taxon_ids if tid]  # Filter out None values
            if taxon_ids:
                tag.taxa.set(taxon_ids)

            self.id_maps["tag"][tag_data["id"]] = tag.pk
        self.stdout.write(f"  Imported {len(tags_data)} tags")

    def import_source_images(self, images_data, project):
        """Import source images"""
        for img_data in images_data:
            deployment_id = self.id_maps["deployment"].get(img_data["deployment_id"])
            event_id = self.id_maps["event"].get(img_data.get("event_id"))

            if not deployment_id:
                continue

            # Parse timestamp
            timestamp = datetime.datetime.fromisoformat(img_data["timestamp"]) if img_data.get("timestamp") else None

            img = SourceImage.objects.create(
                project=project,
                deployment_id=deployment_id,
                event_id=event_id,
                path=img_data["path"],
                timestamp=timestamp,
                width=img_data.get("width"),
                height=img_data.get("height"),
                size=img_data.get("size"),
                checksum=img_data.get("checksum"),
            )
            self.id_maps["source_image"][img_data["id"]] = img.pk
        self.stdout.write(f"  Imported {len(images_data)} source images")

    def import_collections(self, collections_data, project):
        """Import source image collections"""
        for collection_data in collections_data:
            collection = SourceImageCollection.objects.create(
                project=project,
                name=collection_data["name"],
                description=collection_data.get("description", ""),
                method=collection_data.get("method"),
                kwargs=collection_data.get("kwargs"),
            )

            # Add images to collection
            image_ids = [self.id_maps["source_image"].get(old_id) for old_id in collection_data.get("image_ids", [])]
            image_ids = [iid for iid in image_ids if iid]  # Filter out None values
            if image_ids:
                collection.images.set(image_ids)

            self.id_maps["collection"][collection_data["id"]] = collection.pk
        self.stdout.write(f"  Imported {len(collections_data)} collections")

    def import_algorithms(self, algorithms_data, project):
        """Import algorithms"""
        for algo_data in algorithms_data:
            # Check if algorithm already exists
            algorithm = Algorithm.objects.filter(key=algo_data["key"], version=algo_data["version"]).first()

            if not algorithm:
                # Create category map if needed
                category_map = None
                if algo_data.get("category_map_data"):
                    # Try to find existing category map
                    from ami.ml.models import AlgorithmCategoryMap

                    category_map = AlgorithmCategoryMap.objects.filter(
                        data=algo_data["category_map_data"]
                    ).first()

                    if not category_map:
                        category_map = AlgorithmCategoryMap.objects.create(
                            data=algo_data["category_map_data"],
                            labels=algo_data.get("category_map_labels", []),
                        )

                algorithm = Algorithm.objects.create(
                    name=algo_data["name"],
                    key=algo_data["key"],
                    version=algo_data["version"],
                    task_type=algo_data.get("task_type", ""),
                    category_map=category_map,
                )

            self.id_maps["algorithm"][algo_data["id"]] = algorithm.pk
        self.stdout.write(f"  Imported {len(algorithms_data)} algorithms")

    def import_occurrences(self, occurrences_data, project):
        """Import occurrences (without determination initially)"""
        for occ_data in occurrences_data:
            event_id = self.id_maps["event"].get(occ_data.get("event_id"))
            deployment_id = self.id_maps["deployment"].get(occ_data.get("deployment_id"))
            determination_id = self.id_maps["taxon"].get(occ_data.get("determination_id"))

            if not event_id or not deployment_id:
                continue

            occurrence = Occurrence.objects.create(
                project=project,
                event_id=event_id,
                deployment_id=deployment_id,
                determination_id=determination_id,
                determination_score=occ_data.get("determination_score"),
            )
            self.id_maps["occurrence"][occ_data["id"]] = occurrence.pk
        self.stdout.write(f"  Imported {len(occurrences_data)} occurrences")

    def import_detections(self, detections_data, project):
        """Import detections"""
        for det_data in detections_data:
            source_image_id = self.id_maps["source_image"].get(det_data["source_image_id"])
            occurrence_id = self.id_maps["occurrence"].get(det_data.get("occurrence_id"))
            algorithm_id = self.id_maps["algorithm"].get(det_data.get("detection_algorithm_id"))

            if not source_image_id:
                continue

            # Parse timestamp
            timestamp = datetime.datetime.fromisoformat(det_data["timestamp"]) if det_data.get("timestamp") else None

            detection = Detection.objects.create(
                source_image_id=source_image_id,
                occurrence_id=occurrence_id,
                detection_algorithm_id=algorithm_id,
                bbox=det_data.get("bbox"),
                path=det_data.get("path", ""),
                timestamp=timestamp,
                frame_num=det_data.get("frame_num"),
                detection_score=det_data.get("detection_score"),
            )
            self.id_maps["detection"][det_data["id"]] = detection.pk
        self.stdout.write(f"  Imported {len(detections_data)} detections")

    def import_classifications(self, classifications_data, project):
        """Import classifications"""
        for cls_data in classifications_data:
            detection_id = self.id_maps["detection"].get(cls_data["detection_id"])
            taxon_id = self.id_maps["taxon"].get(cls_data.get("taxon_id"))
            algorithm_id = self.id_maps["algorithm"].get(cls_data.get("algorithm_id"))

            if not detection_id:
                continue

            # Parse timestamp
            timestamp = datetime.datetime.fromisoformat(cls_data["timestamp"]) if cls_data.get("timestamp") else None

            classification = Classification.objects.create(
                detection_id=detection_id,
                taxon_id=taxon_id,
                algorithm_id=algorithm_id,
                score=cls_data.get("score"),
                timestamp=timestamp,
            )
            self.id_maps["classification"][cls_data["id"]] = classification.pk
        self.stdout.write(f"  Imported {len(classifications_data)} classifications")

    def import_identifications(self, identifications_data, project, user):
        """Import identifications (assign to importing user)"""
        for id_data in identifications_data:
            occurrence_id = self.id_maps["occurrence"].get(id_data["occurrence_id"])
            taxon_id = self.id_maps["taxon"].get(id_data.get("taxon_id"))
            agreed_with_identification_id = self.id_maps["identification"].get(
                id_data.get("agreed_with_identification_id")
            )
            agreed_with_prediction_id = self.id_maps["classification"].get(id_data.get("agreed_with_prediction_id"))

            if not occurrence_id:
                continue

            # Parse timestamp
            created_at = (
                datetime.datetime.fromisoformat(id_data["created_at"]) if id_data.get("created_at") else None
            )

            identification = Identification.objects.create(
                user=user,  # Assign to importing user
                occurrence_id=occurrence_id,
                taxon_id=taxon_id,
                agreed_with_identification_id=agreed_with_identification_id,
                agreed_with_prediction_id=agreed_with_prediction_id,
                withdrawn=id_data.get("withdrawn", False),
                remarks=id_data.get("remarks", ""),
                created_at=created_at,
            )
            self.id_maps["identification"][id_data["id"]] = identification.pk
        self.stdout.write(f"  Imported {len(identifications_data)} identifications")

    def import_processing_services(self, services_data, project):
        """Import processing services (if they don't already exist)"""
        for service_data in services_data:
            # Check if service already exists
            service = ProcessingService.objects.filter(endpoint_url=service_data["endpoint_url"]).first()

            if not service:
                service = ProcessingService.objects.create(
                    name=service_data["name"],
                    endpoint_url=service_data["endpoint_url"],
                )

            # Add project to service
            service.projects.add(project)
            self.id_maps["processing_service"][service_data["id"]] = service.pk
        self.stdout.write(f"  Imported {len(services_data)} processing services")

    def import_pipelines(self, pipelines_data, project):
        """Import pipelines (if they don't already exist)"""
        for pipeline_data in pipelines_data:
            # Check if pipeline already exists
            pipeline = Pipeline.objects.filter(
                name=pipeline_data["name"], version=pipeline_data["version"]
            ).first()

            if not pipeline:
                pipeline = Pipeline.objects.create(
                    name=pipeline_data["name"],
                    slug=pipeline_data.get("slug", ""),
                    version=pipeline_data["version"],
                    default_config=pipeline_data.get("default_config"),
                )

                # Add algorithms to pipeline
                algorithm_ids = [
                    self.id_maps["algorithm"].get(old_id) for old_id in pipeline_data.get("algorithm_ids", [])
                ]
                algorithm_ids = [aid for aid in algorithm_ids if aid]
                if algorithm_ids:
                    pipeline.algorithms.set(algorithm_ids)

            self.id_maps["pipeline"][pipeline_data["id"]] = pipeline.pk
        self.stdout.write(f"  Imported {len(pipelines_data)} pipelines")

    def import_pipeline_configs(self, configs_data, project):
        """Import pipeline configurations"""
        for config_data in configs_data:
            pipeline_id = self.id_maps["pipeline"].get(config_data["pipeline_id"])

            if not pipeline_id:
                continue

            # Check if config already exists
            config = ProjectPipelineConfig.objects.filter(project=project, pipeline_id=pipeline_id).first()

            if not config:
                config = ProjectPipelineConfig.objects.create(
                    project=project,
                    pipeline_id=pipeline_id,
                    enabled=config_data.get("enabled", False),
                    config=config_data.get("config"),
                )

            self.id_maps["pipeline_config"][config_data["id"]] = config.pk
        self.stdout.write(f"  Imported {len(configs_data)} pipeline configs")

    def update_occurrences(self, project):
        """Update occurrence determinations after all classifications are created"""
        self.stdout.write("  Updating occurrence determinations...")
        count = 0
        for occurrence in Occurrence.objects.filter(project=project):
            occurrence.save()  # This triggers determination recalculation
            count += 1
        self.stdout.write(f"  Updated {count} occurrences")

    def print_summary(self, export_data):
        """Print summary of imported data"""
        self.stdout.write("\nImport Summary:")
        self.stdout.write(f"  Project: {export_data['project']['name']}")
        self.stdout.write(f"  Sites: {len(self.id_maps['site'])}")
        self.stdout.write(f"  Devices: {len(self.id_maps['device'])}")
        self.stdout.write(f"  Storage Sources: {len(self.id_maps['storage_source'])}")
        self.stdout.write(f"  Deployments: {len(self.id_maps['deployment'])}")
        self.stdout.write(f"  Events: {len(self.id_maps['event'])}")
        self.stdout.write(f"  Source Images: {len(self.id_maps['source_image'])}")
        self.stdout.write(f"  Collections: {len(self.id_maps['collection'])}")
        self.stdout.write(f"  Taxa: {len(self.id_maps['taxon'])}")
        self.stdout.write(f"  Taxa Lists: {len(self.id_maps['taxa_list'])}")
        self.stdout.write(f"  Tags: {len(self.id_maps['tag'])}")
        self.stdout.write(f"  Detections: {len(self.id_maps['detection'])}")
        self.stdout.write(f"  Classifications: {len(self.id_maps['classification'])}")
        self.stdout.write(f"  Occurrences: {len(self.id_maps['occurrence'])}")
        self.stdout.write(f"  Identifications: {len(self.id_maps['identification'])}")
        self.stdout.write(f"  Algorithms: {len(self.id_maps['algorithm'])}")
        self.stdout.write(f"  Pipelines: {len(self.id_maps['pipeline'])}")
        self.stdout.write(f"  Processing Services: {len(self.id_maps['processing_service'])}")
        self.stdout.write(f"  Pipeline Configs: {len(self.id_maps['pipeline_config'])}")
