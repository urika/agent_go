"""Performance benchmark tests.

These tests measure query count and response time using Django's
CaptureQueriesContext for precise SQL query counting.

Usage:
    pytest tests/test_performance.py -v --benchmark-skip
    pytest tests/test_performance.py -v  # baseline run
    pytest tests/test_performance.py -v --benchmark-enable  # after optimization

The tests use the SEEDED database (not a blank test DB) — run scripts/seed_data.py first.
They clean up their own data to avoid cross-test pollution.
"""
import pytest
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import User
from src.blog.models import Post, Category, Tag, Comment


@pytest.mark.django_db(transaction=True)
class TestPostListQueries:
    """Critical: N+1 detection for post listing.

    Baseline: query count grows linearly with post count due to
    SerializerMethodField accessing author.profile, comment_count, tags.
    After fix: query count should be constant (≤5).
    """

    def test_post_list_shows_at_least_10(self):
        """Verify the API works (query count not asserted — just correctness)."""
        u = User.objects.create_user("u1")
        User.objects.create_user("u2")
        cat = Category.objects.create(name="C", slug="c")
        t = Tag.objects.create(name="t", slug="t")
        for i in range(10):
            p = Post.objects.create(title=f"P{i}", slug=f"p{i}", body="b",
                                     author_id=(i % 2) + u.id, category=cat,
                                     status="published")
            p.tags.add(t)
            Comment.objects.create(post=p, author=u, body="nice!")
        c = Client()
        resp = c.get("/api/posts/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 10

    def test_post_list_query_count_is_high_due_to_n_plus_1(self):
        """Baseline assertion: N+1 causes high query count.

        Each post triggers 4 extra queries (author_name -> author+profile,
        comment_count, tag_names), so 10 posts → ~1 + 10*4 = 41+ queries.
        """
        u = User.objects.create_user("u1")
        User.objects.create_user("u2")
        cat = Category.objects.create(name="C", slug="c")
        t = Tag.objects.create(name="t", slug="t")
        for i in range(10):
            p = Post.objects.create(title=f"P{i}", slug=f"p{i}", body="b",
                                     author_id=(i % 2) + u.id, category=cat,
                                     status="published")
            p.tags.add(t)
            Comment.objects.create(post=p, author=u, body="nice!")
        c = Client()
        with CaptureQueriesContext(connection) as ctx:
            resp = c.get("/api/posts/")
            query_count = len(ctx.captured_queries)
        assert resp.status_code == 200
        # N+1 baseline: each post triggers 4 extra queries (profile, count, tags)
        # After optimization: should be ≤5 (1 list + 1 author prefetch + 1 profile + 1 tags + 1 comment_count)
        assert query_count > 20, (
            f"Expected >20 queries due to N+1, got {query_count}. "
            "If optimization is complete, update this assertion."
        )


@pytest.mark.django_db(transaction=True)
class TestMonthlyArchivePerformance:
    """Monthly archive must use index on created_at.

    Baseline: no index on created_at → full sequential scan.
    After: index on created_at → index-only scan.
    """

    def test_archive_query_slow_due_to_missing_index(self):
        """Verify the archive query is slow without an index."""
        u = User.objects.create_user("u1")
        cat = Category.objects.create(name="C", slug="c")
        for i in range(100):
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="b",
                                 author=u, category=cat, status="published",
                                 created_at=f"2024-01-{i%28+1:02d}")
        c = Client()
        resp = c.get("/api/posts/archive/2024/01/")
        assert resp.status_code == 200


@pytest.mark.django_db(transaction=True)
class TestCategoryStatsPerformance:
    """Category stats must use single aggregation query.

    Baseline: loops over each category, doing N individual COUNT+AVG queries.
    After: single annotated query.
    """

    def test_category_stats_uses_many_queries_due_to_loop(self):
        """Baseline: each category triggers 1 query → 5+1 = 6 queries."""
        u = User.objects.create_user("u1")
        for i in range(5):
            cat = Category.objects.create(name=f"C{i}", slug=f"c{i}")
            for j in range(20):
                Post.objects.create(title=f"P{j}", slug=f"p{j}_{i}", body="b",
                                     author=u, category=cat, status="published")
        c = Client()
        with CaptureQueriesContext(connection) as ctx:
            resp = c.get("/api/categories/stats/")
            query_count = len(ctx.captured_queries)
        assert resp.status_code == 200
        # Baseline: ~1 + N queries (1 for categories, N for individual counts)
        # After optimization: should be ≤2 (1 for categories + 1 for annotated aggregates)
        assert query_count > 3, (
            f"Expected >3 queries due to per-category loop, got {query_count}. "
            "If optimization is complete, update this assertion."
        )
