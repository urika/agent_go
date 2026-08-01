"""Baseline functional tests for the blog API.

These tests verify correctness — they should pass BEFORE and AFTER
performance optimization. Any regression means the optimization broke something.

Uses transaction=True for PostgreSQL isolation (each test runs in its own
transaction, rolled back after).
"""
import pytest
from django.test import Client
from django.contrib.auth.models import User
from src.blog.models import Post, Category


@pytest.mark.django_db(transaction=True)
class TestPostList:
    def test_empty_list_returns_ok(self):
        c = Client()
        resp = c.get("/api/posts/")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_all_posts(self):
        u = User.objects.create_user("author1")
        cat = Category.objects.create(name="Test", slug="test")
        Post.objects.create(title="P1", slug="p1", body="body", author=u,
                            category=cat, status="published")
        Post.objects.create(title="P2", slug="p2", body="body", author=u,
                            category=cat, status="draft")
        c = Client()
        resp = c.get("/api/posts/")
        data = resp.json()
        assert len(data) == 2


@pytest.mark.django_db(transaction=True)
class TestPostSearch:
    def test_search_by_title(self):
        u = User.objects.create_user("author1")
        cat = Category.objects.create(name="Test", slug="test")
        Post.objects.create(title="Django Performance Guide", slug="dpg",
                            body="How to optimize", author=u, category=cat,
                            status="published")
        Post.objects.create(title="Python Basics", slug="pb",
                            body="Learn python", author=u, category=cat,
                            status="published")
        c = Client()
        resp = c.get("/api/posts/search/?q=Django")
        data = resp.json()
        assert len(data) == 1
        assert "Django" in data[0]["title"]


@pytest.mark.django_db(transaction=True)
class TestPostDetail:
    def test_detail_returns_full_post(self):
        u = User.objects.create_user("author1")
        cat = Category.objects.create(name="Test", slug="test")
        post = Post.objects.create(title="Detail Test", slug="dt", body="Full body",
                                   author=u, category=cat, status="published")
        c = Client()
        resp = c.get(f"/api/posts/{post.id}/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Detail Test"
        assert data["body"] == "Full body"

    def test_detail_not_found(self):
        c = Client()
        resp = c.get("/api/posts/99999/")
        assert resp.status_code == 404


@pytest.mark.django_db(transaction=True)
class TestCategoryPosts:
    def test_category_filter(self):
        u = User.objects.create_user("author1")
        cat1 = Category.objects.create(name="A", slug="a")
        cat2 = Category.objects.create(name="B", slug="b")
        Post.objects.create(title="In A", slug="ia", body="body", author=u,
                            category=cat1, status="published")
        Post.objects.create(title="In B", slug="ib", body="body", author=u,
                            category=cat2, status="published")
        c = Client()
        resp = c.get(f"/api/categories/{cat1.id}/posts/")
        data = resp.json()
        assert len(data["posts"]) == 1
        assert data["posts"][0]["title"] == "In A"


@pytest.mark.django_db(transaction=True)
class TestMonthlyArchive:
    def test_archive_filter(self):
        u = User.objects.create_user("author1")
        cat = Category.objects.create(name="Test", slug="test")
        Post.objects.create(title="Recent", slug="rec", body="body",
                            author=u, category=cat, status="published")
        c = Client()
        from datetime import datetime
        now = datetime.now()
        resp = c.get(f"/api/posts/archive/{now.year}/{now.month:02d}/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["posts"]) == 1
