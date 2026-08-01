from django.urls import path
from src.analytics import views

urlpatterns = [
    path("dashboard/", views.dashboard, name="analytics-dashboard"),
]
