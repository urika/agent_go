from rest_framework import serializers
from django.contrib.auth.models import User
from src.blog.models import Post, Comment, Category, Tag
from src.users.models import UserProfile


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = "__all__"


class CommentSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)

    class Meta:
        model = Comment
        fields = ["id", "author", "body", "is_approved", "created_at"]


# Intentional anti-pattern: N+1 — accesses author.profile per post
class PostListSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    tag_names = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = ["id", "title", "slug", "author_name", "category_name",
                   "comment_count", "tag_names", "view_count", "created_at"]

    def get_author_name(self, obj):
        # N+1: each call queries UserProfile separately
        # Intentional anti-pattern: no select_leading, so each access triggers a query
        try:
            return obj.author.profile.github_handle or obj.author.username
        except UserProfile.DoesNotExist:
            return obj.author.username
        except User.DoesNotExist:
            return "deleted_user"

    def get_category_name(self, obj):
        return obj.category.name if obj.category else ""

    def get_comment_count(self, obj):
        # N+1: each call does COUNT query
        return Comment.objects.filter(post=obj).count()

    def get_tag_names(self, obj):
        # N+1: each call queries M2M table
        return list(obj.tags.values_list("name", flat=True))


class PostDetailSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)
    category = serializers.StringRelatedField()
    tags = serializers.StringRelatedField(many=True)
    comments = CommentSerializer(many=True, read_only=True)

    class Meta:
        model = Post
        fields = "__all__"
