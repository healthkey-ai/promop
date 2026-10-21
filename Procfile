web: python manage.py migrate >&2 && gunicorn ctomop.wsgi:application --bind 0.0.0.0:$PORT --error-logfile /dev/stderr
release: python manage.py migrate >&2