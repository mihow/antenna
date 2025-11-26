#!/bin/bash
# Script to test export/import commands
# Run with: docker compose run --rm django bash test_export_import.sh

set -e  # Exit on error

echo "========================================="
echo "Testing Export/Import Commands"
echo "========================================="
echo ""

# Run the tests
echo "Running automated tests..."
python manage.py test ami.main.tests_export_import -v 2

echo ""
echo "========================================="
echo "Manual Command Tests"
echo "========================================="
echo ""

# Create a demo project
echo "1. Creating demo project..."
python manage.py create_demo_project

# Get the project name (most recent)
PROJECT_NAME=$(python manage.py shell -c "from ami.main.models import Project; print(Project.objects.order_by('-created_at').first().name)")

# Export it
echo "2. Exporting project: $PROJECT_NAME"
python manage.py export_project "$PROJECT_NAME" -o /tmp/test_export.json

# Verify export file exists and is valid JSON
echo "3. Verifying export file..."
if [ ! -f /tmp/test_export.json ]; then
    echo "ERROR: Export file not created!"
    exit 1
fi

# Check JSON is valid
python -m json.tool /tmp/test_export.json > /dev/null
echo "   ✓ Export file is valid JSON"

# Get file size
EXPORT_SIZE=$(du -h /tmp/test_export.json | cut -f1)
echo "   ✓ Export file size: $EXPORT_SIZE"

# Create a new user for import
echo "4. Creating import user..."
python manage.py shell -c "from ami.users.models import User; User.objects.get_or_create(email='importtest@example.com', defaults={'password': 'test123'})"

# Import the project
echo "5. Importing project..."
python manage.py import_project /tmp/test_export.json --user importtest@example.com --project-name "Imported Test Project"

# Verify import
echo "6. Verifying import..."
python manage.py shell -c "
from ami.main.models import Project
p = Project.objects.get(name='Imported Test Project')
print(f'   ✓ Imported project: {p.name}')
print(f'   ✓ Deployments: {p.deployments.count()}')
print(f'   ✓ Sites: {p.sites.count()}')
print(f'   ✓ Devices: {p.devices.count()}')
"

# Test create_demo_project with export
echo "7. Testing create_demo_project with export..."
python manage.py create_demo_project --export /tmp/demo_export.json

# Test create_demo_project from export
echo "8. Testing create_demo_project from export..."
python manage.py create_demo_project --from-export /tmp/demo_export.json --name "Demo from Export"

# Clean up
echo ""
echo "9. Cleaning up test files..."
rm -f /tmp/test_export.json /tmp/demo_export.json

echo ""
echo "========================================="
echo "✓ All tests passed!"
echo "========================================="
