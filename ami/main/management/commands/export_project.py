"""
Export all data from a project to a JSON file for migration or backup.

This command exports a complete project including all related data:
- Sites, Devices, Deployments, Storage Sources
- Events, SourceImages, Collections
- Taxa, TaxaLists, Tags
- Detections, Classifications, Occurrences
- Identifications (without user passwords)
- Pipeline configurations, Algorithms

Users and passwords are NOT exported. On import, all user references
will be assigned to the importing user.
"""

import datetime
import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Prefetch

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
from ami.ml.models import Algorithm, Pipeline, ProcessingService, ProjectPipelineConfig

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Export all project data to a JSON file"

    def add_arguments(self, parser):
        parser.add_argument("project_name", type=str, help="Name of the project to export")
        parser.add_argument(
            "--output",
            "-o",
            type=str,
            help="Output file path (default: <project_slug>_export_<timestamp>.json)",
        )
        parser.add_argument(
            "--indent",
            type=int,
            default=2,
            help="JSON indentation level (default: 2, use 0 for compact)",
        )
        parser.add_argument(
            "--include-images",
            action="store_true",
            help="Include image file data (not just references)",
        )

    def handle(self, *args, **options):
        from ami.main.models import Project

        project_name = options["project_name"]

        try:
            project = Project.objects.get(name=project_name)
        except Project.DoesNotExist:
            raise CommandError(f'Project "{project_name}" does not exist')

        self.stdout.write(f"Exporting project: {project.name} (ID: {project.pk})")

        # Build export data structure
        export_data = self.build_export_data(project, options)

        # Generate output filename if not provided
        output_path = options.get("output")
        if not output_path:
            from django.utils.text import slugify

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"{slugify(project.name)}_export_{timestamp}.json"

        # Write to file
        output_path = Path(output_path)
        indent = options["indent"] if options["indent"] > 0 else None

        with open(output_path, "w") as f:
            json.dump(export_data, f, indent=indent, cls=DjangoJSONEncoder)

        # Print summary
        self.stdout.write(self.style.SUCCESS(f"\nExport completed: {output_path}"))
        self.stdout.write(f"File size: {output_path.stat().st_size / 1024:.1f} KB")
        self.print_summary(export_data)

    def build_export_data(self, project, options):
        """Build the complete export data structure"""
        self.stdout.write("Building export data...")

        export_data = {
            "export_version": "1.0",
            "exported_at": datetime.datetime.now().isoformat(),
            "export_tool": "export_project management command",
            "project": self.export_project(project),
            "sites": self.export_sites(project),
            "devices": self.export_devices(project),
            "storage_sources": self.export_storage_sources(project),
            "deployments": self.export_deployments(project),
            "events": self.export_events(project),
            "source_images": self.export_source_images(project),
            "collections": self.export_collections(project),
            "taxa": self.export_taxa(project),
            "taxa_lists": self.export_taxa_lists(project),
            "tags": self.export_tags(project),
            "detections": self.export_detections(project),
            "classifications": self.export_classifications(project),
            "occurrences": self.export_occurrences(project),
            "identifications": self.export_identifications(project),
            "algorithms": self.export_algorithms(project),
            "pipelines": self.export_pipelines(project),
            "processing_services": self.export_processing_services(project),
            "pipeline_configs": self.export_pipeline_configs(project),
        }

        return export_data

    def export_project(self, project):
        """Export project metadata (excluding owner)"""
        return {
            "id": project.pk,
            "name": project.name,
            "description": project.description,
            "is_draft": project.is_draft,
            "priority": project.priority,
            "image_base_url": project.image_base_url,
            "default_event_method": project.default_event_method,
            "default_event_time_threshold": project.default_event_time_threshold,
            "summary": project.summary,
            "details": project.details,
            "default_filters": project.default_filters,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        }

    def export_sites(self, project):
        """Export all sites for the project"""
        sites = []
        for site in Site.objects.filter(project=project):
            sites.append(
                {
                    "id": site.pk,
                    "name": site.name,
                    "description": site.description,
                    "latitude": site.latitude,
                    "longitude": site.longitude,
                    "elevation": site.elevation,
                }
            )
        self.stdout.write(f"  Exported {len(sites)} sites")
        return sites

    def export_devices(self, project):
        """Export all devices for the project"""
        devices = []
        for device in Device.objects.filter(project=project):
            devices.append(
                {
                    "id": device.pk,
                    "name": device.name,
                    "description": device.description,
                    "hardware_version": device.hardware_version,
                    "software_version": device.software_version,
                }
            )
        self.stdout.write(f"  Exported {len(devices)} devices")
        return devices

    def export_storage_sources(self, project):
        """Export all S3 storage sources for the project"""
        sources = []
        for source in S3StorageSource.objects.filter(project=project):
            sources.append(
                {
                    "id": source.pk,
                    "name": source.name,
                    "endpoint_url": source.endpoint_url,
                    "bucket": source.bucket,
                    "prefix": source.prefix,
                    "access_key": source.access_key,
                    "secret_key": source.secret_key,
                    "public_base_url": source.public_base_url,
                }
            )
        self.stdout.write(f"  Exported {len(sources)} storage sources")
        return sources

    def export_deployments(self, project):
        """Export all deployments for the project"""
        deployments = []
        for deployment in Deployment.objects.filter(project=project).select_related("research_site", "device", "data_source"):
            deployments.append(
                {
                    "id": deployment.pk,
                    "name": deployment.name,
                    "description": deployment.description,
                    "research_site_id": deployment.research_site_id,
                    "research_site_name": deployment.research_site.name if deployment.research_site else None,
                    "device_id": deployment.device_id,
                    "device_name": deployment.device.name if deployment.device else None,
                    "data_source_id": deployment.data_source_id,
                    "data_source_name": deployment.data_source.name if deployment.data_source else None,
                    "data_source_subdir": deployment.data_source_subdir,
                    "data_source_regex": deployment.data_source_regex,
                    "latitude": deployment.latitude,
                    "longitude": deployment.longitude,
                }
            )
        self.stdout.write(f"  Exported {len(deployments)} deployments")
        return deployments

    def export_events(self, project):
        """Export all events for the project"""
        events = []
        for event in Event.objects.filter(project=project).select_related("deployment"):
            events.append(
                {
                    "id": event.pk,
                    "deployment_id": event.deployment_id,
                    "group_by": event.group_by,
                    "start": event.start.isoformat() if event.start else None,
                    "end": event.end.isoformat() if event.end else None,
                }
            )
        self.stdout.write(f"  Exported {len(events)} events")
        return events

    def export_source_images(self, project):
        """Export all source images for the project"""
        images = []
        for img in SourceImage.objects.filter(project=project).select_related("deployment", "event"):
            images.append(
                {
                    "id": img.pk,
                    "deployment_id": img.deployment_id,
                    "event_id": img.event_id,
                    "path": img.path,
                    "timestamp": img.timestamp.isoformat() if img.timestamp else None,
                    "width": img.width,
                    "height": img.height,
                    "size": img.size,
                    "checksum": img.checksum,
                }
            )
        self.stdout.write(f"  Exported {len(images)} source images")
        return images

    def export_collections(self, project):
        """Export all source image collections for the project"""
        collections = []
        for collection in SourceImageCollection.objects.filter(project=project).prefetch_related("images"):
            collections.append(
                {
                    "id": collection.pk,
                    "name": collection.name,
                    "description": collection.description,
                    "method": collection.method,
                    "kwargs": collection.kwargs,
                    "image_ids": list(collection.images.values_list("id", flat=True)),
                }
            )
        self.stdout.write(f"  Exported {len(collections)} collections")
        return collections

    def export_taxa(self, project):
        """Export all taxa associated with the project"""
        taxa = []
        # Get all taxa linked to this project
        for taxon in project.taxa.all().select_related("parent", "synonym_of"):
            taxa.append(
                {
                    "id": taxon.pk,
                    "name": taxon.name,
                    "rank": taxon.rank,
                    "parent_id": taxon.parent_id,
                    "parent_name": taxon.parent.name if taxon.parent else None,
                    "synonym_of_id": taxon.synonym_of_id,
                    "synonym_of_name": taxon.synonym_of.name if taxon.synonym_of else None,
                    "description": taxon.description,
                    "parents_json": taxon.parents_json,
                    "ordering": taxon.ordering,
                }
            )
        self.stdout.write(f"  Exported {len(taxa)} taxa")
        return taxa

    def export_taxa_lists(self, project):
        """Export all taxa lists for the project"""
        taxa_lists = []
        for taxa_list in TaxaList.objects.filter(projects=project).prefetch_related("taxa"):
            taxa_lists.append(
                {
                    "id": taxa_list.pk,
                    "name": taxa_list.name,
                    "description": taxa_list.description,
                    "taxa_ids": list(taxa_list.taxa.values_list("id", flat=True)),
                }
            )
        self.stdout.write(f"  Exported {len(taxa_lists)} taxa lists")
        return taxa_lists

    def export_tags(self, project):
        """Export all tags for the project"""
        tags = []
        for tag in Tag.objects.filter(project=project).prefetch_related("taxa"):
            tags.append(
                {
                    "id": tag.pk,
                    "name": tag.name,
                    "description": tag.description,
                    "color": tag.color,
                    "taxa_ids": list(tag.taxa.values_list("id", flat=True)),
                }
            )
        self.stdout.write(f"  Exported {len(tags)} tags")
        return tags

    def export_detections(self, project):
        """Export all detections for the project"""
        detections = []
        for detection in Detection.objects.filter(source_image__project=project).select_related(
            "source_image", "occurrence", "detection_algorithm"
        ):
            detections.append(
                {
                    "id": detection.pk,
                    "source_image_id": detection.source_image_id,
                    "occurrence_id": detection.occurrence_id,
                    "detection_algorithm_id": detection.detection_algorithm_id,
                    "detection_algorithm_name": (
                        detection.detection_algorithm.name if detection.detection_algorithm else None
                    ),
                    "bbox": detection.bbox,
                    "path": detection.path,
                    "timestamp": detection.timestamp.isoformat() if detection.timestamp else None,
                    "frame_num": detection.frame_num,
                    "detection_score": detection.detection_score,
                }
            )
        self.stdout.write(f"  Exported {len(detections)} detections")
        return detections

    def export_classifications(self, project):
        """Export all classifications for the project"""
        classifications = []
        for classification in (
            Classification.objects.filter(detection__source_image__project=project)
            .select_related("detection", "taxon", "algorithm", "category_map")
        ):
            classifications.append(
                {
                    "id": classification.pk,
                    "detection_id": classification.detection_id,
                    "taxon_id": classification.taxon_id,
                    "taxon_name": classification.taxon.name if classification.taxon else None,
                    "algorithm_id": classification.algorithm_id,
                    "algorithm_name": classification.algorithm.name if classification.algorithm else None,
                    "category_map_id": classification.category_map_id,
                    "score": classification.score,
                    "timestamp": classification.timestamp.isoformat() if classification.timestamp else None,
                }
            )
        self.stdout.write(f"  Exported {len(classifications)} classifications")
        return classifications

    def export_occurrences(self, project):
        """Export all occurrences for the project"""
        occurrences = []
        for occurrence in Occurrence.objects.filter(project=project).select_related(
            "determination", "event", "deployment"
        ):
            occurrences.append(
                {
                    "id": occurrence.pk,
                    "determination_id": occurrence.determination_id,
                    "determination_name": occurrence.determination.name if occurrence.determination else None,
                    "event_id": occurrence.event_id,
                    "deployment_id": occurrence.deployment_id,
                    "determination_score": occurrence.determination_score,
                }
            )
        self.stdout.write(f"  Exported {len(occurrences)} occurrences")
        return occurrences

    def export_identifications(self, project):
        """Export all identifications for the project (without user info)"""
        identifications = []
        for identification in Identification.objects.filter(occurrence__project=project).select_related(
            "taxon", "occurrence", "agreed_with_identification", "agreed_with_prediction"
        ):
            identifications.append(
                {
                    "id": identification.pk,
                    "occurrence_id": identification.occurrence_id,
                    "taxon_id": identification.taxon_id,
                    "taxon_name": identification.taxon.name if identification.taxon else None,
                    "agreed_with_identification_id": identification.agreed_with_identification_id,
                    "agreed_with_prediction_id": identification.agreed_with_prediction_id,
                    "withdrawn": identification.withdrawn,
                    "remarks": identification.remarks,
                    "created_at": identification.created_at.isoformat() if identification.created_at else None,
                }
            )
        self.stdout.write(f"  Exported {len(identifications)} identifications")
        return identifications

    def export_algorithms(self, project):
        """Export all algorithms used by the project"""
        algorithms = []
        # Get algorithms from detections and classifications in this project
        algorithm_ids = set()

        # From detections
        for det in Detection.objects.filter(source_image__project=project).values_list(
            "detection_algorithm_id", flat=True
        ).distinct():
            if det:
                algorithm_ids.add(det)

        # From classifications
        for cls in (
            Classification.objects.filter(detection__source_image__project=project)
            .values_list("algorithm_id", flat=True)
            .distinct()
        ):
            if cls:
                algorithm_ids.add(cls)

        for algorithm in Algorithm.objects.filter(pk__in=algorithm_ids).select_related("category_map"):
            algorithms.append(
                {
                    "id": algorithm.pk,
                    "name": algorithm.name,
                    "key": algorithm.key,
                    "version": algorithm.version,
                    "task_type": algorithm.task_type,
                    "category_map_id": algorithm.category_map_id,
                    "category_map_data": algorithm.category_map.data if algorithm.category_map else None,
                    "category_map_labels": algorithm.category_map.labels if algorithm.category_map else None,
                }
            )
        self.stdout.write(f"  Exported {len(algorithms)} algorithms")
        return algorithms

    def export_pipelines(self, project):
        """Export pipelines enabled for the project"""
        pipelines = []
        for config in ProjectPipelineConfig.objects.filter(project=project).select_related("pipeline"):
            pipeline = config.pipeline
            pipelines.append(
                {
                    "id": pipeline.pk,
                    "name": pipeline.name,
                    "slug": pipeline.slug,
                    "version": pipeline.version,
                    "default_config": pipeline.default_config,
                    "algorithm_ids": list(pipeline.algorithms.values_list("id", flat=True)),
                }
            )
        self.stdout.write(f"  Exported {len(pipelines)} pipelines")
        return pipelines

    def export_processing_services(self, project):
        """Export processing services used by the project"""
        services = []
        for service in ProcessingService.objects.filter(projects=project):
            services.append(
                {
                    "id": service.pk,
                    "name": service.name,
                    "endpoint_url": service.endpoint_url,
                }
            )
        self.stdout.write(f"  Exported {len(services)} processing services")
        return services

    def export_pipeline_configs(self, project):
        """Export pipeline configurations for the project"""
        configs = []
        for config in ProjectPipelineConfig.objects.filter(project=project).select_related("pipeline"):
            configs.append(
                {
                    "id": config.pk,
                    "pipeline_id": config.pipeline_id,
                    "pipeline_name": config.pipeline.name,
                    "enabled": config.enabled,
                    "config": config.config,
                }
            )
        self.stdout.write(f"  Exported {len(configs)} pipeline configs")
        return configs

    def print_summary(self, export_data):
        """Print summary of exported data"""
        self.stdout.write("\nExport Summary:")
        self.stdout.write(f"  Project: {export_data['project']['name']}")
        self.stdout.write(f"  Sites: {len(export_data['sites'])}")
        self.stdout.write(f"  Devices: {len(export_data['devices'])}")
        self.stdout.write(f"  Storage Sources: {len(export_data['storage_sources'])}")
        self.stdout.write(f"  Deployments: {len(export_data['deployments'])}")
        self.stdout.write(f"  Events: {len(export_data['events'])}")
        self.stdout.write(f"  Source Images: {len(export_data['source_images'])}")
        self.stdout.write(f"  Collections: {len(export_data['collections'])}")
        self.stdout.write(f"  Taxa: {len(export_data['taxa'])}")
        self.stdout.write(f"  Taxa Lists: {len(export_data['taxa_lists'])}")
        self.stdout.write(f"  Tags: {len(export_data['tags'])}")
        self.stdout.write(f"  Detections: {len(export_data['detections'])}")
        self.stdout.write(f"  Classifications: {len(export_data['classifications'])}")
        self.stdout.write(f"  Occurrences: {len(export_data['occurrences'])}")
        self.stdout.write(f"  Identifications: {len(export_data['identifications'])}")
        self.stdout.write(f"  Algorithms: {len(export_data['algorithms'])}")
        self.stdout.write(f"  Pipelines: {len(export_data['pipelines'])}")
        self.stdout.write(f"  Processing Services: {len(export_data['processing_services'])}")
        self.stdout.write(f"  Pipeline Configs: {len(export_data['pipeline_configs'])}")
