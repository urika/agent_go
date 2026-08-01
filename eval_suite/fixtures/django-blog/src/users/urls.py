from django.urls import path
from src.users import views

urlpatterns = [
    path("<int:pk>/", views.user_detail, name="user-detail"),
]
