from django.urls import path
from src.blog import views

urlpatterns = [
    path("posts/", views.post_list, name="post-list"),
    path("posts/search/", views.post_search, name="post-search"),
    path("posts/trending/", views.trending_posts, name="post-trending"),
    path("posts/<int:pk>/", views.post_detail, name="post-detail"),
    path("posts/archive/<int:year>/<int:month>/", views.monthly_archive, name="monthly-archive"),
    path("categories/<int:category_id>/posts/", views.category_posts, name="category-posts"),
    path("categories/stats/", views.category_stats, name="category-stats"),
    path("authors/<int:author_id>/posts/", views.author_posts, name="author-posts"),
]
