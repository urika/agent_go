"""GitHub Actions workflow auto-generation."""

from pathlib import Path

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


def detect_language(repo):
    for lang, cfg in TEMPLATES.items():
        for f in cfg["detect"]:
            if (Path(repo) / f).exists():
                return lang
    return None


def generate_workflow(repo):
    lang = detect_language(repo)
    if lang is None:
        return None, None
    return lang, TEMPLATES[lang]["workflow"]


def cmd_ci():
    import sys
    dry_run = "--dry-run" in sys.argv
    repo = Path.cwd()
    if len(sys.argv) > 2 and not sys.argv[2].startswith("--"):
        repo = Path(sys.argv[2]).resolve()

    lang, content = generate_workflow(repo)
    if lang is None:
        print("未检测到已知项目语言。支持: python, go, node, rust, java")
        return

    wf_dir = repo / ".github" / "workflows"
    wf_file = wf_dir / "test.yml"

    if dry_run:
        print(f"[dry-run] 语言: {lang}, 目标: {wf_file}")
        print(content)
        return

    wf_dir.mkdir(parents=True, exist_ok=True)
    if wf_file.exists():
        print(f"已存在: {wf_file}")
        return
    wf_file.write_text(content, encoding="utf-8")
    print(f"已生成: {wf_file} ({lang})")
