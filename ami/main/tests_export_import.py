"""
Tests for project export/import management commands.

This module contains comprehensive tests for:
- export_project command
- import_project command
- create_demo_project command (with export/import options)

Test Classes:
- ExportProjectCommandTest: Tests export functionality
- ImportProjectCommandTest: Tests import functionality
- CreateDemoProjectCommandTest: Tests enhanced demo project creation
- ExportImportIntegrationTest: End-to-end integration tests

Run with:
    docker compose run --rm django python manage.py test ami.main.tests_export_import
    docker compose run --rm django python manage.py test ami.main.tests_export_import -v 2
"""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from ami.main.models import (
    Classification,
    Deployment,
    Detection,
    Device,
    Event,
    Occurrence,
    Project,
    S3StorageSource,
    Site,
    SourceImage,
    SourceImageCollection,
    Tag,
    TaxaList,
    Taxon,
)
from ami.ml.models import Algorithm, AlgorithmCategoryMap, Pipeline, ProcessingService, ProjectPipelineConfig
from ami.tests.fixtures.main import (
    create_captures_from_files,
    create_occurrences_from_frame_data,
    create_taxa_from_csv,
    setup_test_project,
)
from ami.users.models import User


class ExportProjectCommandTest(TransactionTestCase):
    """Test the export_project management command"""

    def setUp(self):
        """Create a test project with data"""
        self.project, self.deployment = setup_test_project(reuse=False)
        self.taxa_list = create_taxa_from_csv(self.project)

        # Create some source images and occurrences
        frame_data = create_captures_from_files(self.deployment, skip_existing=False)
        self.occurrences = create_occurrences_from_frame_data(frame_data, taxa_list=self.taxa_list)

        self.export_file = None

    def tearDown(self):
        """Clean up export file"""
        if self.export_file and Path(self.export_file).exists():
            Path(self.export_file).unlink()

    def test_export_project_basic(self):
        """Test basic export functionality"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            self.export_file = f.name

        out = StringIO()
        call_command(
            "export_project",
            self.project.name,
            "--output",
            self.export_file,
            stdout=out,
        )

        # Check that file was created
        self.assertTrue(Path(self.export_file).exists())

        # Load and verify the export data
        with open(self.export_file, "r") as f:
            export_data = json.load(f)

        # Verify structure
        self.assertEqual(export_data["export_version"], "1.0")
        self.assertIn("project", export_data)
        self.assertIn("sites", export_data)
        self.assertIn("devices", export_data)
        self.assertIn("deployments", export_data)
        self.assertIn("events", export_data)
        self.assertIn("source_images", export_data)
        self.assertIn("taxa", export_data)
        self.assertIn("detections", export_data)
        self.assertIn("classifications", export_data)
        self.assertIn("occurrences", export_data)

        # Verify project data
        self.assertEqual(export_data["project"]["name"], self.project.name)
        self.assertEqual(export_data["project"]["id"], self.project.pk)

        # Verify counts match
        self.assertEqual(len(export_data["deployments"]), self.project.deployments.count())
        self.assertEqual(len(export_data["events"]), Event.objects.filter(project=self.project).count())
        self.assertEqual(len(export_data["source_images"]), SourceImage.objects.filter(project=self.project).count())
        self.assertEqual(len(export_data["occurrences"]), Occurrence.objects.filter(project=self.project).count())

    def test_export_project_nonexistent(self):
        """Test exporting a project that doesn't exist"""
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as cm:
            call_command("export_project", "Nonexistent Project")

        self.assertIn("does not exist", str(cm.exception))

    def test_export_includes_relationships(self):
        """Test that export includes all relationships"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            self.export_file = f.name

        call_command(
            "export_project",
            self.project.name,
            "--output",
            self.export_file,
            stdout=StringIO(),
        )

        with open(self.export_file, "r") as f:
            export_data = json.load(f)

        # Check that deployments have site and device references
        if export_data["deployments"]:
            deployment_data = export_data["deployments"][0]
            self.assertIn("research_site_id", deployment_data)
            self.assertIn("device_id", deployment_data)
            self.assertIn("data_source_id", deployment_data)

        # Check that source images have deployment and event references
        if export_data["source_images"]:
            image_data = export_data["source_images"][0]
            self.assertIn("deployment_id", image_data)
            self.assertIn("event_id", image_data)

        # Check that detections have source_image and occurrence references
        if export_data["detections"]:
            detection_data = export_data["detections"][0]
            self.assertIn("source_image_id", detection_data)
            self.assertIn("occurrence_id", detection_data)

        # Check that taxa have parent references
        if export_data["taxa"]:
            # Find a taxon with a parent
            for taxon_data in export_data["taxa"]:
                if taxon_data.get("parent_id"):
                    self.assertIn("parent_name", taxon_data)
                    break


class ImportProjectCommandTest(TransactionTestCase):
    """Test the import_project management command"""

    def setUp(self):
        """Create export file from a test project"""
        # Create original project
        self.original_project, self.deployment = setup_test_project(reuse=False)
        self.taxa_list = create_taxa_from_csv(self.original_project)

        # Create some source images and occurrences
        frame_data = create_captures_from_files(self.deployment, skip_existing=False)
        self.occurrences = create_occurrences_from_frame_data(frame_data, taxa_list=self.taxa_list)

        # Export the project
        self.export_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False).name
        call_command(
            "export_project",
            self.original_project.name,
            "--output",
            self.export_file,
            stdout=StringIO(),
        )

        # Create a user for importing
        self.import_user = User.objects.create_user(
            email="importer@test.com",
            password="testpass123",
        )

    def tearDown(self):
        """Clean up export file"""
        if Path(self.export_file).exists():
            Path(self.export_file).unlink()

    def test_import_project_basic(self):
        """Test basic import functionality"""
        out = StringIO()
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            stdout=out,
        )

        # Find the imported project (should have same name or with suffix)
        imported_projects = Project.objects.filter(name__startswith=self.original_project.name).exclude(
            pk=self.original_project.pk
        )
        self.assertEqual(imported_projects.count(), 1)
        imported_project = imported_projects.first()

        # Verify owner is the import user
        self.assertEqual(imported_project.owner, self.import_user)

        # Verify counts match
        original_deployment_count = self.original_project.deployments.count()
        imported_deployment_count = imported_project.deployments.count()
        self.assertEqual(imported_deployment_count, original_deployment_count)

        original_event_count = Event.objects.filter(project=self.original_project).count()
        imported_event_count = Event.objects.filter(project=imported_project).count()
        self.assertEqual(imported_event_count, original_event_count)

        original_image_count = SourceImage.objects.filter(project=self.original_project).count()
        imported_image_count = SourceImage.objects.filter(project=imported_project).count()
        self.assertEqual(imported_image_count, original_image_count)

        original_occurrence_count = Occurrence.objects.filter(project=self.original_project).count()
        imported_occurrence_count = Occurrence.objects.filter(project=imported_project).count()
        self.assertEqual(imported_occurrence_count, original_occurrence_count)

    def test_import_project_with_custom_name(self):
        """Test importing with a custom project name"""
        custom_name = "Custom Import Name"
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            "--project-name",
            custom_name,
            stdout=StringIO(),
        )

        # Find the imported project
        imported_project = Project.objects.get(name=custom_name)
        self.assertIsNotNone(imported_project)
        self.assertEqual(imported_project.owner, self.import_user)

    def test_import_project_duplicate_name_handling(self):
        """Test that duplicate project names are handled"""
        # Import once
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            stdout=StringIO(),
        )

        # Import again - should create with different name
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            stdout=StringIO(),
        )

        # Should have 2 imported projects with similar names
        imported_projects = Project.objects.filter(name__startswith=self.original_project.name).exclude(
            pk=self.original_project.pk
        )
        self.assertGreaterEqual(imported_projects.count(), 2)

    def test_import_preserves_relationships(self):
        """Test that import preserves all relationships"""
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            stdout=StringIO(),
        )

        imported_project = (
            Project.objects.filter(name__startswith=self.original_project.name)
            .exclude(pk=self.original_project.pk)
            .first()
        )

        # Check deployment relationships
        deployment = imported_project.deployments.first()
        self.assertIsNotNone(deployment.research_site)
        self.assertIsNotNone(deployment.device)
        self.assertEqual(deployment.research_site.project, imported_project)
        self.assertEqual(deployment.device.project, imported_project)

        # Check event relationships
        event = Event.objects.filter(project=imported_project).first()
        if event:
            self.assertIsNotNone(event.deployment)
            self.assertEqual(event.deployment.project, imported_project)

        # Check source image relationships
        source_image = SourceImage.objects.filter(project=imported_project).first()
        if source_image:
            self.assertIsNotNone(source_image.deployment)
            self.assertIsNotNone(source_image.event)
            self.assertEqual(source_image.deployment.project, imported_project)

        # Check detection relationships
        detection = Detection.objects.filter(source_image__project=imported_project).first()
        if detection:
            self.assertIsNotNone(detection.source_image)
            self.assertEqual(detection.source_image.project, imported_project)
            if detection.occurrence:
                self.assertEqual(detection.occurrence.project, imported_project)

        # Check occurrence relationships
        occurrence = Occurrence.objects.filter(project=imported_project).first()
        if occurrence:
            self.assertIsNotNone(occurrence.event)
            self.assertIsNotNone(occurrence.deployment)
            self.assertEqual(occurrence.event.project, imported_project)
            self.assertEqual(occurrence.deployment.project, imported_project)

    def test_import_skip_images(self):
        """Test importing without source images"""
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            "--skip-images",
            stdout=StringIO(),
        )

        imported_project = (
            Project.objects.filter(name__startswith=self.original_project.name)
            .exclude(pk=self.original_project.pk)
            .first()
        )

        # Should have project structure but no images
        self.assertGreater(imported_project.deployments.count(), 0)
        self.assertEqual(SourceImage.objects.filter(project=imported_project).count(), 0)

    def test_import_skip_ml_data(self):
        """Test importing without ML data"""
        call_command(
            "import_project",
            self.export_file,
            "--user",
            self.import_user.email,
            "--skip-ml-data",
            stdout=StringIO(),
        )

        imported_project = (
            Project.objects.filter(name__startswith=self.original_project.name)
            .exclude(pk=self.original_project.pk)
            .first()
        )

        # Should have images but no ML data
        self.assertGreater(SourceImage.objects.filter(project=imported_project).count(), 0)
        self.assertEqual(Occurrence.objects.filter(project=imported_project).count(), 0)
        self.assertEqual(Detection.objects.filter(source_image__project=imported_project).count(), 0)

    def test_import_nonexistent_file(self):
        """Test importing from a file that doesn't exist"""
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as cm:
            call_command(
                "import_project",
                "/tmp/nonexistent_file.json",
                "--user",
                self.import_user.email,
            )

        self.assertIn("does not exist", str(cm.exception))

    def test_import_nonexistent_user(self):
        """Test importing with a user that doesn't exist"""
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as cm:
            call_command(
                "import_project",
                self.export_file,
                "--user",
                "nonexistent@test.com",
            )

        self.assertIn("does not exist", str(cm.exception))


class CreateDemoProjectCommandTest(TransactionTestCase):
    """Test the enhanced create_demo_project management command"""

    def test_create_demo_from_scratch(self):
        """Test creating demo project from scratch"""
        out = StringIO()
        call_command("create_demo_project", stdout=out)

        # Should have created a project
        projects = Project.objects.all()
        self.assertGreater(projects.count(), 0)

        # Should have created source images
        self.assertGreater(SourceImage.objects.count(), 0)

    def test_create_demo_with_export(self):
        """Test creating and exporting a demo project"""
        export_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False).name

        try:
            call_command(
                "create_demo_project",
                "--export",
                export_file,
                stdout=StringIO(),
            )

            # Export file should exist
            self.assertTrue(Path(export_file).exists())

            # Should be valid JSON
            with open(export_file, "r") as f:
                export_data = json.load(f)

            self.assertEqual(export_data["export_version"], "1.0")
            self.assertIn("project", export_data)

        finally:
            if Path(export_file).exists():
                Path(export_file).unlink()

    def test_create_demo_from_export(self):
        """Test creating demo project from an export file"""
        # First create and export
        export_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False).name

        try:
            call_command(
                "create_demo_project",
                "--export",
                export_file,
                stdout=StringIO(),
            )

            original_project_count = Project.objects.count()

            # Now import from the export
            call_command(
                "create_demo_project",
                "--from-export",
                export_file,
                stdout=StringIO(),
            )

            # Should have created another project
            self.assertGreater(Project.objects.count(), original_project_count)

        finally:
            if Path(export_file).exists():
                Path(export_file).unlink()


class ExportImportIntegrationTest(TransactionTestCase):
    """Integration tests for the complete export/import cycle"""

    def test_full_export_import_cycle(self):
        """Test complete export and import cycle preserves all data"""
        # Create a comprehensive project
        original_project, deployment = setup_test_project(reuse=False)
        taxa_list = create_taxa_from_csv(original_project)
        frame_data = create_captures_from_files(deployment, skip_existing=False)
        create_occurrences_from_frame_data(frame_data, taxa_list=taxa_list)

        # Add some tags
        tag = Tag.objects.create(project=original_project, name="Test Tag", color="#FF0000")
        tag.taxa.add(taxa_list.taxa.first())

        # Get original counts
        original_counts = {
            "deployments": original_project.deployments.count(),
            "events": Event.objects.filter(project=original_project).count(),
            "images": SourceImage.objects.filter(project=original_project).count(),
            "taxa": original_project.taxa.count(),
            "tags": Tag.objects.filter(project=original_project).count(),
            "detections": Detection.objects.filter(source_image__project=original_project).count(),
            "classifications": Classification.objects.filter(detection__source_image__project=original_project).count(),
            "occurrences": Occurrence.objects.filter(project=original_project).count(),
        }

        # Export
        export_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False).name

        try:
            call_command(
                "export_project",
                original_project.name,
                "--output",
                export_file,
                stdout=StringIO(),
            )

            # Create import user
            import_user = User.objects.create_user(email="import@test.com", password="test123")

            # Import
            call_command(
                "import_project",
                export_file,
                "--user",
                import_user.email,
                "--project-name",
                "Imported Test Project",
                stdout=StringIO(),
            )

            # Find imported project
            imported_project = Project.objects.get(name="Imported Test Project")

            # Verify all counts match
            self.assertEqual(imported_project.deployments.count(), original_counts["deployments"])
            self.assertEqual(Event.objects.filter(project=imported_project).count(), original_counts["events"])
            self.assertEqual(SourceImage.objects.filter(project=imported_project).count(), original_counts["images"])
            self.assertEqual(imported_project.taxa.count(), original_counts["taxa"])
            self.assertEqual(Tag.objects.filter(project=imported_project).count(), original_counts["tags"])
            self.assertEqual(
                Detection.objects.filter(source_image__project=imported_project).count(), original_counts["detections"]
            )
            self.assertEqual(
                Classification.objects.filter(detection__source_image__project=imported_project).count(),
                original_counts["classifications"],
            )
            self.assertEqual(Occurrence.objects.filter(project=imported_project).count(), original_counts["occurrences"])

            # Verify tag relationships
            imported_tag = Tag.objects.filter(project=imported_project, name="Test Tag").first()
            self.assertIsNotNone(imported_tag)
            self.assertGreater(imported_tag.taxa.count(), 0)

        finally:
            if Path(export_file).exists():
                Path(export_file).unlink()

    def test_taxa_hierarchy_preserved(self):
        """Test that taxonomic hierarchy is preserved during export/import"""
        # Create project with taxa
        project, _ = setup_test_project(reuse=False)
        taxa_list = create_taxa_from_csv(project)

        # Get a taxon with parent
        child_taxon = Taxon.objects.filter(parent__isnull=False).first()
        self.assertIsNotNone(child_taxon)
        original_parent = child_taxon.parent

        # Export and import
        export_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False).name

        try:
            call_command("export_project", project.name, "--output", export_file, stdout=StringIO())

            import_user = User.objects.create_user(email="taxtest@test.com", password="test123")
            call_command(
                "import_project",
                export_file,
                "--user",
                import_user.email,
                "--project-name",
                "Taxa Test Import",
                stdout=StringIO(),
            )

            imported_project = Project.objects.get(name="Taxa Test Import")

            # Find the imported child taxon
            imported_child = Taxon.objects.filter(name=child_taxon.name).exclude(pk=child_taxon.pk).first()
            self.assertIsNotNone(imported_child)
            self.assertIsNotNone(imported_child.parent)
            self.assertEqual(imported_child.parent.name, original_parent.name)

        finally:
            if Path(export_file).exists():
                Path(export_file).unlink()
