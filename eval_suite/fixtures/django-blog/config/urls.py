from django.urls import path, include

urlpatterns = [
    path("api/", include("src.blog.urls")),
    path("api/users/", include("src.users.urls")),
    path("api/analytics/", include("src.analytics.urls")),
]
