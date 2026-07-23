"""GitHub Actions workflow auto-generation."""

from pathlib import Path
from typing import Optional

from .console import _LazyConsole

console = _LazyConsole()

__all__ = ["cmd_ci"]

TEMPLATES = {
    "python": {
        "detect": ["requirements.txt", "setup.py", "pyproject.toml", "setup.cfg"],
        "workflow": "name: Test\n\non: [push, pull_request]\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.11'\n      - run: pip install pytest\n      - run: pytest tests/ -v\n",
    },
    "go": {
        "detect": ["go.mod"],
        "workflow": "name: Test\n\non: [push, pull_request]\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-go@v5\n        with:\n          go-version: '1.22'\n      - run: go test ./... -v\n",
    },
    "node": {
        "detect": ["package.json", "yarn.lock", "pnpm-lock.yaml"],
        "workflow": "name: Test\n\non: [push, pull_request]\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n        with:\n          node-version: '20'\n      - run: npm ci\n      - run: npm test\n",
    },
    "rust": {
        "detect": ["Cargo.toml"],
        "workflow": "name: Test\n\non: [push, pull_request]\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: cargo test --verbose\n",
    },
    "java": {
        "detect": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "workflow": "name: Test\n\non: [push, pull_request]\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-java@v4\n        with:\n          java-version: '17'\n          distribution: 'temurin'\n      - run: mvn test\n",
    },
}


def detect_language(repo: Path) -> Optional[str]:
    for lang, cfg in TEMPLATES.items():
        for f in cfg["detect"]:
            if (Path(repo) / f).exists():
                return lang
    return None


def generate_workflow(repo: Path) -> tuple[Optional[str], Optional[str]]:
    lang = detect_language(repo)
    if lang is None:
        return None, None
    return lang, TEMPLATES[lang]["workflow"]


def cmd_ci(args=None) -> None:
    if args and hasattr(args, 'dry_run'):
        dry_run = args.dry_run
        repo = Path(getattr(args, 'repo', None) or Path.cwd()).resolve()
    else:
        import sys
        dry_run = "--dry-run" in sys.argv
        repo = Path.cwd()
        if len(sys.argv) > 2 and not sys.argv[2].startswith("--"):
            repo = Path(sys.argv[2]).resolve()

    lang, content = generate_workflow(repo)
    if lang is None:
        console.print("未检测到已知项目语言。支持: python, go, node, rust, java")
        return

    wf_dir = repo / ".github" / "workflows"
    wf_file = wf_dir / "test.yml"

    if dry_run:
        console.print(f"[dry-run] 语言: {lang}, 目标: {wf_file}")
        console.print(content)
        return

    wf_dir.mkdir(parents=True, exist_ok=True)
    if wf_file.exists():
        console.print(f"已存在: {wf_file}")
        return
    wf_file.write_text(content, encoding="utf-8")
    console.print(f"已生成: {wf_file} ({lang})")
