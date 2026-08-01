from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    bio = models.TextField(blank=True)
    avatar_url = models.URLField(blank=True)
    website = models.URLField(blank=True)
    github_handle = models.CharField(max_length=100, blank=True)
    reputation = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
