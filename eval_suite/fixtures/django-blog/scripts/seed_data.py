"""Seed 100k+ rows of blog data into the database for performance testing.

Usage:
    python scripts/seed_data.py

Requires PostgreSQL running (docker-compose up -d) and migrations applied.
"""
import os
import sys
import random
import string
from datetime import datetime, timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django
django.setup()

from django.contrib.auth.models import User
from src.blog.models import Category, Tag, Post, Comment, Rating
from src.users.models import UserProfile
from src.analytics.models import PageView

BATCH_SIZE = 1000


def random_text(min_len=10, max_len=200):
    n = random.randint(min_len, max_len)
    return " ".join(
        "".join(random.choices(string.ascii_lowercase, k=random.randint(3, 10)))
        for _ in range(n)
    )


def seed_users(n=1000):
    users = []
    for i in range(n):
        username = f"user_{i}_{random_text(4, 8)}"
        u = User(username=username[:150], email=f"{username[:50]}@blog.com")
        u.set_password("password123")
        users.append(u)
    User.objects.bulk_create(users, batch_size=BATCH_SIZE)
    print(f"  Created {n} users")


def seed_profiles():
    users = list(User.objects.all())
    profiles = [
        UserProfile(user=u, bio=random_text(20, 100),
                    github_handle=f"gh_{u.username[:20]}")
        for u in users
    ]
    UserProfile.objects.bulk_create(profiles, batch_size=BATCH_SIZE)
    print(f"  Created {len(profiles)} profiles")


def seed_categories(n=15):
    cats = [Category(name=f"Category_{i}", slug=f"cat-{i}",
                     description=random_text(30, 80)) for i in range(n)]
    Category.objects.bulk_create(cats)
    print(f"  Created {n} categories")


def seed_tags(n=50):
    tags = [Tag(name=f"tag_{i}", slug=f"tag-{i}") for i in range(n)]
    Tag.objects.bulk_create(tags)
    print(f"  Created {n} tags")


def seed_posts(n=10000):
    users = list(User.objects.all())
    categories = list(Category.objects.all())
    tags = list(Tag.objects.all())
    posts = []
    for i in range(n):
        days_ago = random.randint(0, 365)
        author = random.choice(users)
        cat = random.choice(categories)
        posts.append(Post(
            title=f"Post {i}: {random_text(5, 15)}",
            slug=f"post-{i}-{random.randint(1000, 9999)}",
            body=random_text(200, 2000),
            excerpt=random_text(20, 100),
            status=random.choices(["published", "draft", "archived"], weights=[85, 10, 5])[0],
            author=author,
            category=cat,
            view_count=random.randint(0, 50000),
            created_at=datetime.now() - timedelta(days=days_ago),
        ))
        if len(posts) >= BATCH_SIZE:
            Post.objects.bulk_create(posts)
            posts = []
    if posts:
        Post.objects.bulk_create(posts)
    print(f"  Created {n} posts")


def seed_post_tags():
    posts = list(Post.objects.all())
    tags = list(Tag.objects.all())
    through = Post.tags.through
    rows = []
    for post in posts:
        for tag in random.sample(tags, random.randint(1, 4)):
            rows.append(through(post_id=post.id, tag_id=tag.id))
    through.objects.bulk_create(rows, batch_size=BATCH_SIZE * 5)
    print(f"  Created {len(rows)} post-tag relations")


def seed_comments(n=50000):
    posts = list(Post.objects.filter(status="published"))
    users = list(User.objects.all())
    comments = []
    for i in range(n):
        comments.append(Comment(
            post=random.choice(posts),
            author=random.choice(users),
            body=random_text(10, 300),
            is_approved=random.random() > 0.1,
            created_at=datetime.now() - timedelta(days=random.randint(0, 365)),
        ))
        if len(comments) >= BATCH_SIZE:
            Comment.objects.bulk_create(comments)
            comments = []
    if comments:
        Comment.objects.bulk_create(comments)
    print(f"  Created {n} comments")


def seed_ratings(n=20000):
    posts = list(Post.objects.filter(status="published"))
    users = list(User.objects.all())
    ratings = []
    seen = set()
    for i in range(n):
        post = random.choice(posts)
        user = random.choice(users)
        key = (post.id, user.id)
        if key in seen:
            continue
        seen.add(key)
        ratings.append(Rating(post=post, user=user, score=random.randint(1, 5)))
        if len(ratings) >= BATCH_SIZE:
            Rating.objects.bulk_create(ratings)
            ratings = []
    if ratings:
        Rating.objects.bulk_create(ratings)
    print(f"  Created {len(ratings)} ratings")


def seed_pageviews(n=100000):
    paths = ["/", "/api/posts/", "/api/posts/trending/"]
    paths += [f"/api/posts/{i}/" for i in range(1, 200)]
    paths += [f"/api/categories/{i}/posts/" for i in range(1, 16)]
    views = []
    for i in range(n):
        views.append(PageView(
            path=random.choice(paths),
            user_id=random.randint(1, 1000),
            ip_address=f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(0,255)}",
            duration_sec=random.randint(1, 300),
            created_at=datetime.now() - timedelta(days=random.randint(0, 90)),
        ))
        if len(views) >= BATCH_SIZE:
            PageView.objects.bulk_create(views)
            views = []
    if views:
        PageView.objects.bulk_create(views)
    print(f"  Created {n} page views")


if __name__ == "__main__":
    import time
    start = time.time()
    print("Seeding database...")
    seed_users(1000)
    seed_profiles()
    seed_categories()
    seed_tags()
    seed_posts(10000)
    seed_post_tags()
    seed_comments(50000)
    seed_ratings(20000)
    seed_pageviews(100000)
    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s")
