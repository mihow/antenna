import os
import time
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand

from ami.main.models import Deployment, Detection, Device, Event, Occurrence, Project, SourceImage, TaxaList, Taxon
from ami.ml.models import Algorithm, Pipeline
from ami.tests.fixtures.main import create_complete_test_project, create_local_admin_user


class Command(BaseCommand):
    r"""Create example data needed for development and tests.

    This command can create a demo project in two ways:
    1. From scratch using synthetic data (default)
    2. By importing from an export file (--from-export)

    You can also export the created project (--export) to create reusable demo data.
    """

    help = "Create example data needed for development and tests"

    def add_arguments(self, parser):
        # Add option to delete existing data
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete existing data before creating new demo project",
        )
        # Add option to import from export file
        parser.add_argument(
            "--from-export",
            type=str,
            help="Import demo project from an export file instead of creating from scratch",
        )
        # Add option to export after creation
        parser.add_argument(
            "--export",
            type=str,
            help="Export the created project to the specified file path",
        )
        # Add option to specify project name
        parser.add_argument(
            "--name",
            type=str,
            help="Name for the demo project (only used with --from-export)",
        )

    def handle(self, *args, **options):
        if options["delete"]:
            self.stdout.write(self.style.WARNING("! Deleting existing data !"))
            time.sleep(2)
            for model in [
                Project,
                Device,
                Deployment,
                TaxaList,
                Taxon,
                Event,
                SourceImage,
                Detection,
                Occurrence,
                Algorithm,
                Pipeline,
            ]:
                self.stdout.write(f"Deleting all {model._meta.verbose_name_plural} and related objects")
                model.objects.all().delete()

        # Check if we should import from export file
        if options["from_export"]:
            self.import_from_export(options)
        else:
            self.create_from_scratch(options)

        # Export if requested
        if options["export"]:
            self.export_project(options)

    def create_from_scratch(self, options):
        """Create demo project from scratch using synthetic data"""
        self.stdout.write("Creating demo project from scratch...")
        create_local_admin_user()
        create_complete_test_project()
        self.stdout.write(self.style.SUCCESS("Demo project created successfully!"))

    def import_from_export(self, options):
        """Import demo project from an export file"""
        export_file = options["from_export"]

        if not Path(export_file).exists():
            self.stdout.write(self.style.ERROR(f"Export file not found: {export_file}"))
            return

        # Ensure admin user exists
        create_local_admin_user()

        # Get the admin user email
        admin_email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "antenna@insectai.org")

        # Prepare import command arguments
        import_args = [export_file, "--user", admin_email]

        # Add project name if specified
        if options.get("name"):
            import_args.extend(["--project-name", options["name"]])

        self.stdout.write(f"Importing demo project from: {export_file}")
        call_command("import_project", *import_args)

    def export_project(self, options):
        """Export the demo project to a file"""
        export_path = options["export"]

        # Find the demo project (assume it's the most recently created)
        project = Project.objects.order_by("-created_at").first()

        if not project:
            self.stdout.write(self.style.ERROR("No project found to export"))
            return

        self.stdout.write(f"Exporting project: {project.name}")
        call_command("export_project", project.name, "--output", export_path)
        self.stdout.write(self.style.SUCCESS(f"Project exported to: {export_path}"))
