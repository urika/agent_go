from django.db.models import Count, Sum, Avg
from django.contrib.auth.models import User
from rest_framework.decorators import api_view
from rest_framework.response import Response

from src.analytics.models import PageView


# Intentional anti-pattern: full table scan + no pagination
@api_view(["GET"])
def dashboard(request):
    total_users = User.objects.count()
    total_views = PageView.objects.count()
    unique_paths = len(set(PageView.objects.values_list("path", flat=True)))
    avg_duration = PageView.objects.aggregate(avg=Avg("duration_sec"))

    # Heavy GROUP BY without materialized view
    path_stats = (
        PageView.objects
        .values("path")
        .annotate(count=Count("id"), total_duration=Sum("duration_sec"))
        .order_by("-count")[:100]
    )

    return Response({
        "total_users": total_users,
        "total_views": total_views,
        "unique_paths": unique_paths,
        "avg_duration_sec": avg_duration["duration_sec__avg"] or 0,
        "top_paths": list(path_stats),
    })
