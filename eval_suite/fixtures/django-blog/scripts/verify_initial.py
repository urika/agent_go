#!/usr/bin/env python3
"""Verify the fixture is in its clean initial state.

Checks:
  1. Git status is clean
  2. Current commit matches the v0.1-initial tag
  3. Baseline tests pass
  4. Baseline query counts match expected N+1 pattern

Usage:
    python scripts/verify_initial.py

Exit codes:
    0 = fixture is in clean initial state
    1 = fixture has been modified
    2 = tests fail
    3 = query counts don't match baseline
"""
import os
import sys
import subprocess
import json

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_TAG = "v0.1-initial"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)


def check_git() -> bool:
    """Verify git status is clean and at v0.1-initial tag."""
    os.chdir(REPO)
    status = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True)
    if status.stdout.strip():
        print(f"❌ Git status not clean:\n{status.stdout}")
        return False

    tag_out = subprocess.run(
        ["git", "describe", "--exact-match", "--tags", "HEAD"],
        capture_output=True, text=True
    )
    current_tag = tag_out.stdout.strip()
    if current_tag != EXPECTED_TAG:
        print(f"❌ HEAD not at {EXPECTED_TAG} ({current_tag or 'no tag'})")
        return False

    print(f"✅ Git clean at {EXPECTED_TAG}")
    return True


def check_tests() -> bool:
    """Verify baseline tests pass."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"],
        capture_output=True, text=True, cwd=REPO
    )
    if result.returncode != 0:
        print(f"❌ Tests failed:\n{result.stdout}")
        print(result.stderr[:500])
        return False
    print(f"✅ {result.stdout.strip().split(chr(10))[-2] if chr(10) in result.stdout else 'All'} tests passed")
    return True


def check_query_baseline() -> bool:
    """Verify N+1 anti-pattern produces expected query counts.

    Uses a direct capture of Django query counts on post list endpoint.
    Requires PostgreSQL running.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"
    import django
    django.setup()
    from django.test import Client
    from django.test.utils import CaptureQueriesContext
    from django.db import connection
    from django.contrib.auth.models import User
    from src.blog.models import Post, Category, Tag, Comment

    client = Client()
    u = User.objects.create_user("baseline_user")
    cat = Category.objects.create(name="Base", slug="base")
    t = Tag.objects.create(name="base", slug="base")
    for i in range(10):
        p = Post.objects.create(title=f"B{i}", slug=f"b{i}", body="body",
                                 author=u, category=cat, status="published")
        p.tags.add(t)
        Comment.objects.create(post=p, author=u, body="nice!")

    with CaptureQueriesContext(connection) as ctx:
        resp = client.get("/api/posts/")
        query_count = len(ctx.captured_queries)

    if resp.status_code != 200:
        print(f"❌ Post list returned {resp.status_code}")
        return False
    if query_count < 20:
        print(f"❌ N+1 baseline too low: {query_count} queries (expected >20)")
        return False
    print(f"✅ Baseline query count: {query_count} (N+1 confirmed)")
    return True


def main():
    os.chdir(REPO)
    print(f"Verifying initial state of django-blog fixture...")
    print(f"  Repo: {REPO}")

    checks = [
        ("Git status", check_git()),
        ("Baseline tests", check_tests()),
        ("N+1 query baseline", check_query_baseline()),
    ]

    print(f"\n{'─'*50}")
    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"{'─'*50}")
    print(f"  Result: {'PASS' if not failures else f'FAIL ({len(failures)} check(s))'}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
