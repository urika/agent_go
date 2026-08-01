from django.db import models


class PageView(models.Model):
    path = models.CharField(max_length=500)
    user_id = models.IntegerField(null=True, blank=True)
    ip_address = models.GenericIPAddressField()
    user_agent = models.TextField(blank=True)
    session_key = models.CharField(max_length=100, blank=True)
    duration_sec = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["path"])]
