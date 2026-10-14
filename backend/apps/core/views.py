from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def health(request):
    """Public liveness check (load balancer, deploy smoke test): the database answers, and which release is live."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})
