# Use Python 3.12 as base image
FROM python:3.12-slim

# Install system dependencies and Node.js
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    gcc \
    postgresql-client \
    libpq-dev \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Verify installations
RUN python --version && node --version && npm --version

# Set working directory
WORKDIR /app

# Copy Python requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy frontend package files and install Node dependencies
COPY frontend/package*.json ./frontend/
WORKDIR /app/frontend
RUN npm install --legacy-peer-deps
WORKDIR /app

# Copy ALL application files
COPY . .

# Verify patient_portal directory exists
RUN ls -la /app/patient_portal/

# Build frontend
RUN cd frontend && npm run build && cd ..

# Collect Django static files
RUN python manage.py collectstatic --noinput --clear || true

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:${PORT:-8000}/api/health/ || exit 1

# Run migrations and start gunicorn
CMD python manage.py migrate && \
    gunicorn ctomop.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --workers 4 \
    --threads 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info