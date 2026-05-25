#!/bin/bash
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Creating/resetting admin user..."
python manage.py setup_admin

echo "Starting gunicorn..."
exec gunicorn ctomop.wsgi:application
