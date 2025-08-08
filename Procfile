web: gunicorn app:app --workers 4 --bind 0.0.0.0:$PORT
worker: celery -A app.celery_app worker --loglevel=info
