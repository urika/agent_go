#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "=== Starting PostgreSQL ==="
docker compose up -d db
echo "Waiting for PostgreSQL..."
until docker compose exec -T db pg_isready -U blog_user -d django_blog 2>/dev/null; do
  sleep 1
done

echo "=== Installing dependencies ==="
pip install -e .

echo "=== Running migrations ==="
python -m django migrate --settings=config.settings

echo "=== Seeding 181k+ rows ==="
python scripts/seed_data.py

echo "=== Running baseline tests ==="
python -m pytest tests/ -v --tb=short

echo "=== Done ==="
echo "PostgreSQL running on 127.0.0.1:15432"
echo "DJANGO_SETTINGS_MODULE=config.settings"
