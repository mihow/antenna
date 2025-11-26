# Project Export/Import Guide

This guide explains how to export and import complete Antenna projects between environments using the new management commands.

## Overview

The export/import system allows you to:
- **Export** all data from a project to a JSON file
- **Import** projects into different environments
- **Create demo projects** from exported data
- **Migrate projects** between deployments

## Commands

### 1. Export a Project

Export all project data to a JSON file:

```bash
docker compose run --rm django python manage.py export_project "Project Name"
```

**Options:**
- `--output`, `-o`: Specify output file path (default: auto-generated)
- `--indent`: JSON indentation level (default: 2, use 0 for compact)
- `--include-images`: Include image file data (not implemented yet)

**Example:**
```bash
# Export with auto-generated filename
docker compose run --rm django python manage.py export_project "My Project"

# Export to specific file
docker compose run --rm django python manage.py export_project "My Project" -o my_project_backup.json

# Export with compact JSON
docker compose run --rm django python manage.py export_project "My Project" --indent 0
```

### 2. Import a Project

Import a project from a JSON export file:

```bash
docker compose run --rm django python manage.py import_project export.json --user user@example.com
```

**Options:**
- `--user`, `-u`: Email of user who will own the imported project (required)
- `--project-name`, `-n`: Override the project name
- `--skip-images`: Skip importing source images
- `--skip-ml-data`: Skip importing ML data (detections, classifications, occurrences)

**Examples:**
```bash
# Basic import
docker compose run --rm django python manage.py import_project my_export.json --user admin@example.com

# Import with custom project name
docker compose run --rm django python manage.py import_project my_export.json --user admin@example.com --project-name "Imported Project"

# Import structure only (no images or ML data)
docker compose run --rm django python manage.py import_project my_export.json --user admin@example.com --skip-images --skip-ml-data
```

### 3. Create Demo Projects

The `create_demo_project` command now supports both creating from scratch and importing from export files:

```bash
# Create from scratch (default)
docker compose run --rm django python manage.py create_demo_project

# Create from export file
docker compose run --rm django python manage.py create_demo_project --from-export demo_export.json

# Create and export for reuse
docker compose run --rm django python manage.py create_demo_project --export demo_export.json

# Delete existing data first
docker compose run --rm django python manage.py create_demo_project --delete
```

## What Gets Exported

The export includes all project-related data:

### Infrastructure
- Sites (monitoring locations)
- Devices (hardware configurations)
- Deployments (device + site combinations)
- S3 Storage Sources (with credentials)

### Data Collection
- Events (temporal groupings)
- Source Images (image metadata and paths)
- Source Image Collections

### Taxonomy
- Taxa (species and taxonomy hierarchy)
- Taxa Lists
- Tags

### ML Results
- Algorithms (used by the project)
- Detections (bounding boxes)
- Classifications (species predictions)
- Occurrences (validated observations)
- Identifications (human review)

### ML Configuration
- Pipelines (ML workflows)
- Processing Services (ML backends)
- Pipeline Configurations (project-specific settings)

## What Does NOT Get Exported

- **User credentials**: Passwords and authentication tokens are never exported
- **User accounts**: Only the project owner reference (reassigned on import)
- **Image files**: Only metadata and paths (actual image files remain in S3/MinIO)
- **Jobs**: Celery job history is not exported

## Import Behavior

### User Assignment
All user references (owner, identifications, etc.) are assigned to the importing user specified with `--user`.

### ID Mapping
Old database IDs are mapped to new IDs automatically. Relationships are preserved.

### Duplicate Handling

**Unique Objects (created if don't exist):**
- Taxa (matched by name)
- Algorithms (matched by key + version)
- Pipelines (matched by name + version)
- Processing Services (matched by endpoint URL)
- Category Maps (matched by data content)

**Project-Specific Objects (always created):**
- Sites, Devices, Deployments
- Events, Source Images
- Detections, Classifications, Occurrences

**Name Conflicts:**
If a project with the same name exists, a suffix is added: "Project Name (1)", "Project Name (2)", etc.

## Use Cases

### 1. Backup and Restore

```bash
# Backup
docker compose run --rm django python manage.py export_project "Production Project" -o backup.json

# Restore (to different environment)
docker compose run --rm django python manage.py import_project backup.json --user admin@newenv.com
```

### 2. Create Reusable Demo Data

```bash
# Create and export a demo project
docker compose run --rm django python manage.py create_demo_project --export demo_data.json

# Later, import the demo in a new environment
docker compose run --rm django python manage.py create_demo_project --from-export demo_data.json
```

### 3. Migrate Between Environments

```bash
# On source environment
docker compose run --rm django python manage.py export_project "Research Project" -o project_export.json

# Transfer file to destination environment
scp project_export.json user@destination:/path/

# On destination environment
docker compose run --rm django python manage.py import_project /path/project_export.json --user newowner@example.com
```

### 4. Share Project Templates

```bash
# Export without ML data for faster sharing
docker compose run --rm django python manage.py export_project "Template Project" -o template.json

# Import as template (skip ML data)
docker compose run --rm django python manage.py import_project template.json --user user@example.com --skip-ml-data --project-name "New Project from Template"
```

## Export File Format

The export is a JSON file with this structure:

```json
{
  "export_version": "1.0",
  "exported_at": "2025-01-15T10:00:00Z",
  "export_tool": "export_project management command",
  "project": { "name": "...", "description": "...", ... },
  "sites": [...],
  "devices": [...],
  "storage_sources": [...],
  "deployments": [...],
  "events": [...],
  "source_images": [...],
  "collections": [...],
  "taxa": [...],
  "taxa_lists": [...],
  "tags": [...],
  "detections": [...],
  "classifications": [...],
  "occurrences": [...],
  "identifications": [...],
  "algorithms": [...],
  "pipelines": [...],
  "processing_services": [...],
  "pipeline_configs": [...]
}
```

## Testing

### Running Automated Tests

Comprehensive automated tests are available in `ami/main/tests_export_import.py`:

```bash
# Run all export/import tests
docker compose run --rm django python manage.py test ami.main.tests_export_import

# Run specific test class
docker compose run --rm django python manage.py test ami.main.tests_export_import.ExportProjectCommandTest

# Run with verbose output
docker compose run --rm django python manage.py test ami.main.tests_export_import -v 2
```

### Manual Testing Script

A comprehensive test script is available at `test_export_import.sh`:

```bash
# Run the complete test suite (creates demo, exports, imports, verifies)
docker compose run --rm django bash test_export_import.sh
```

This script:
1. Runs automated tests
2. Creates a demo project
3. Exports the project to JSON
4. Verifies the export file
5. Imports the project
6. Verifies the import matches the original
7. Tests create_demo_project with export/import options

### Test Coverage

The automated tests cover:
- ✅ Basic export functionality
- ✅ Export of all data types (sites, devices, images, taxa, etc.)
- ✅ Export relationship preservation
- ✅ Basic import functionality
- ✅ Import with custom project name
- ✅ Duplicate name handling
- ✅ Relationship preservation after import
- ✅ Skip images option
- ✅ Skip ML data option
- ✅ Error handling (nonexistent files, users, projects)
- ✅ Full export/import cycle integrity
- ✅ Taxonomic hierarchy preservation
- ✅ create_demo_project enhancements

## Troubleshooting

### Import Fails with Missing User

**Error:** `User with email "..." does not exist`

**Solution:** Create the user first or use an existing user's email:
```bash
docker compose run --rm django python manage.py createsuperuser
```

### Import Fails with Name Conflict

The import automatically handles name conflicts by appending a number. No action needed.

### Missing Image Files After Import

**Important:** The export/import only transfers metadata, not the actual image files. You must:

1. **Transfer image files separately** using S3 sync or similar tools
2. **Update S3 Storage Source settings** in the imported project to point to the new location
3. Or **set up S3 replication** between environments

### Large Export Files

For projects with many images, the export file can be large. Options:

1. Use `--indent 0` for more compact JSON
2. Use `--skip-images` to export structure only
3. Compress the export file: `gzip project_export.json`

## Security Considerations

### Credentials in Exports

S3 storage source credentials (access keys and secret keys) ARE included in exports.

**Best Practices:**
- Keep export files secure (treat like credentials)
- Use environment-specific storage sources when possible
- Rotate credentials if export files are compromised
- Consider removing credentials from export files before sharing

### User Privacy

User passwords and authentication tokens are never exported. Only basic user references (converted to the importing user).

## Performance

### Export Performance

- Small projects (<1000 images): < 1 second
- Medium projects (1000-10,000 images): 1-10 seconds
- Large projects (>10,000 images): 10+ seconds

### Import Performance

Import is slower due to database writes:

- Small projects: 5-30 seconds
- Medium projects: 30-120 seconds
- Large projects: 2-10 minutes

All imports run in a single database transaction for data consistency.

## Future Enhancements

Potential improvements for future versions:

- [ ] Include actual image files in export (optional)
- [ ] Support for incremental exports (only new data)
- [ ] Export compression built-in
- [ ] Exclude specific data types from export
- [ ] Import validation and dry-run mode
- [ ] Export versioning and compatibility checking
- [ ] Export encryption for sensitive data
