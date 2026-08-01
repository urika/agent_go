import random
from datetime import datetime

from django.db import models
from django.contrib.auth.models import User


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name


class Post(models.Model):
    STATUS_CHOICES = [("draft", "Draft"), ("published", "Published"), ("archived", "Archived")]

    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    body = models.TextField()
    excerpt = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name="posts")
    tags = models.ManyToManyField(Tag, related_name="posts")
    view_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Intentional anti-pattern: no db_index on created_at or author
    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def comment_count(self):
        return self.comments.count()

    @property
    def average_rating(self):
        ratings = self.ratings.all()
        if ratings:
            return sum(r.score for r in ratings) / len(ratings)
        return 0.0


class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    is_approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Intentional anti-pattern: no composite index on (post_id, created_at)


class Rating(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="ratings")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    score = models.IntegerField(choices=[(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("post", "user")]
