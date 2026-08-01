from django.db.models import Q, Count, Avg
from django.contrib.auth.models import User
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from src.blog.models import Post, Comment, Category
from src.blog.serializers import PostListSerializer, PostDetailSerializer


# Intentional anti-pattern 1: N+1 — no select_related/prefetch_related
@api_view(["GET"])
def post_list(request):
    posts = Post.objects.all()
    serializer = PostListSerializer(posts, many=True)
    return Response(serializer.data)


# Intentional anti-pattern 2: no pagination
@api_view(["GET"])
def post_search(request):
    q = request.GET.get("q", "")
    # Intentional anti-pattern: LIKE %keyword% — no full-text search index
    posts = Post.objects.filter(
        Q(title__icontains=q) | Q(body__icontains=q), status="published"
    )
    serializer = PostListSerializer(posts, many=True)
    return Response(serializer.data)


@api_view(["GET"])
def post_detail(request, pk):
    try:
        post = Post.objects.get(pk=pk)
        serializer = PostDetailSerializer(post)
        return Response(serializer.data)
    except Post.DoesNotExist:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)


# Intentional anti-pattern 3: unoptimized aggregation — no prefetch
@api_view(["GET"])
def category_posts(request, category_id):
    posts = Post.objects.filter(category_id=category_id, status="published")
    serializer = PostListSerializer(posts, many=True)
    return Response({"category": category_id, "posts": serializer.data})


# Intentional anti-pattern 4: no pagination on author posts
@api_view(["GET"])
def author_posts(request, author_id):
    try:
        author = User.objects.get(pk=author_id)
        posts = Post.objects.filter(author=author, status="published")
        serializer = PostListSerializer(posts, many=True)
        return Response({"author": author.username, "post_count": len(serializer.data),
                        "posts": serializer.data})
    except User.DoesNotExist:
        return Response({"error": "user not found"}, status=status.HTTP_404_NOT_FOUND)


# Intentional anti-pattern 5: unoptimized trending query
@api_view(["GET"])
def trending_posts(request):
    posts = Post.objects.filter(status="published").order_by("-view_count")[:20]
    serializer = PostListSerializer(posts, many=True)
    return Response(serializer.data)


# Intentional anti-pattern 6: counting without index
@api_view(["GET"])
def monthly_archive(request, year, month):
    posts = Post.objects.filter(
        status="published",
        created_at__year=year,
        created_at__month=month,
    ).order_by("-created_at")
    serializer = PostListSerializer(posts, many=True)
    return Response({"year": year, "month": month, "posts": serializer.data})


# Intentional anti-pattern 7: heavy aggregation without db-level optimization
@api_view(["GET"])
def category_stats(request):
    data = []
    for cat in Category.objects.all():
        count = Post.objects.filter(category=cat, status="published").count()
        avg_views = Post.objects.filter(category=cat).aggregate(avg=Avg("view_count"))
        data.append({
            "category": cat.name,
            "post_count": count,
            "avg_views": avg_views["avg"] or 0,
        })
    return Response(data)
