web: gunicorn app:app --workers 4 --bind 0.0.0.0:$PORT

If you integrate Celery for background tasks, you would add a worker process like this:
worker: celery -A app.celery_app worker --loglevel=info
