"""M1.2 交付命令测试：cmd_pr head/base 与 cmd_merge 显式交付。"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent_go.cli import cmd_merge, cmd_pr


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), capture_output=True)
    (path / "file.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)
    return path


def _make_task(tmp_path, repo):
    task_dir = tmp_path / ".agent_go" / "task-t1"
    task_dir.mkdir(parents=True)
    # 创建 delivery branch 并提交
    subprocess.run(["git", "checkout", "-b", "agent_go/task-t1/delivery"], cwd=str(repo), capture_output=True)
    (repo / "delivery.txt").write_text("delivery content", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "delivery commit"], cwd=str(repo), capture_output=True)
    base = subprocess.run(["git", "rev-parse", "main"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    meta = {
        "task_id": "task-t1", "task": "test task", "repo": str(repo),
        "status": "DELIVERY_READY", "status_schema_version": 1,
        "base_commit": base, "base_branch": "main", "target_branch": "main",
        "delivery_branch": "agent_go/task-t1/delivery",
        "results": [{"subtask_id": "sub-1", "status": "completed", "verify_ok": True,
                     "commit_hash": subprocess.run(
                         ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()}],
    }
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return task_dir, meta


class Args:
    def __init__(self, task_id, offline=False, push=False, remote="origin"):
        self.task_id = task_id
        self.offline = offline
        self.push = push
        self.remote = remote


def test_cmd_pr_offline_writes_pr_md(tmp_path):
    """offline 模式生成 PR.md，不推送不创建 PR。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir, meta = _make_task(tmp_path, repo)
    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"):
        cmd_pr(Args("task-t1", offline=True))
    pr_md = task_dir / "PR.md"
    assert pr_md.exists()
    assert "test task" in pr_md.read_text(encoding="utf-8")


def test_cmd_pr_push_uses_delivery_branch(tmp_path):
    """--push 必须推送 delivery branch（delivery:delivery），禁止推 HEAD 到 base。"""
    repo = _init_repo(tmp_path / "repo")
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "origin.git")], cwd=str(repo), capture_output=True)
    task_dir, meta = _make_task(tmp_path, repo)
    # 恢复主分支状态
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

    push_cmds = []

    def fake_run(cmd, **kwargs):
        if cmd and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "push":
            push_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd and len(cmd) >= 2 and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="https://example.test/pr/9", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.shutil.which", return_value="/usr/local/bin/gh"), \
         patch("agent_go.cli.subprocess.run", side_effect=fake_run):
        cmd_pr(Args("task-t1", offline=False, push=True))

    assert push_cmds, "应当有 git push 调用"
    for c in push_cmds:
        assert "HEAD" not in c, f"禁止推 HEAD: {c}"
        assert "delivery" in c[3], f"应推 delivery branch: {c}"
        assert "main:main" not in c[3], f"禁止推 main:main: {c}"

    # PR 成功后应写 pr_url / pr_head / pr_base 到 meta
    updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    assert updated["pr_url"] == "https://example.test/pr/9"
    assert updated["pr_head"] == "agent_go/task-t1/delivery"
    assert updated["pr_base"] == "main"
    assert updated["status"] == "ACCEPTED_DELIVERY"


def test_cmd_pr_reuses_existing_pr(tmp_path):
    """gh 报 "already exists"（该 delivery branch 已有 PR）→ 复用已有 PR，不误判交付失败。

    真实远端场景：PR 创建后再次运行 cmd_pr，gh 返回 "a pull request for branch X into
    branch Y already exists: <url>"。修复前 cmd_pr 标记 DELIVERY_FAILED；修复后从错误
    信息提取已有 PR URL，标记 ACCEPTED_DELIVERY。
    """
    repo = _init_repo(tmp_path / "repo")
    task_dir, meta = _make_task(tmp_path, repo)
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

    def fake_run(cmd, **kwargs):
        if cmd and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "push":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd and len(cmd) >= 2 and cmd[0] == "gh":
            err = ('a pull request for branch "agent_go/task-t1/delivery" into branch "main" '
                   'already exists:\nhttps://github.com/example/repo/pull/42')
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=err)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.shutil.which", return_value="/usr/local/bin/gh"), \
         patch("agent_go.cli.subprocess.run", side_effect=fake_run):
        cmd_pr(Args("task-t1", offline=False, push=True))

    updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    assert updated["pr_url"] == "https://github.com/example/repo/pull/42"
    assert updated["pr_head"] == "agent_go/task-t1/delivery"
    assert updated["pr_base"] == "main"
    assert updated["delivery_failed"] is False
    assert updated["accepted_delivery"] is True
    assert updated["status"] == "ACCEPTED_DELIVERY"


def test_cmd_pr_gh_real_failure_marks_delivery_failed(tmp_path):
    """gh pr create 真实失败（非 already exists）→ 标记 DELIVERY_FAILED。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir, meta = _make_task(tmp_path, repo)
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

    def fake_run(cmd, **kwargs):
        if cmd and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "push":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd and len(cmd) >= 2 and cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="authentication failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.shutil.which", return_value="/usr/local/bin/gh"), \
         patch("agent_go.cli.subprocess.run", side_effect=fake_run):
        cmd_pr(Args("task-t1", offline=False, push=True))

    updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    assert updated["delivery_failed"] is True
    assert updated["accepted_delivery"] is False
    assert updated["status"] == "DELIVERY_FAILED"


def test_cmd_merge_success_advances_target_branch(tmp_path):
    """cmd_merge 成功后将 target branch 推进到 merge commit 并记录 explicit_merge_commit。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir, meta = _make_task(tmp_path, repo)
    # 恢复主分支状态（_make_task 已 checkout delivery）
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)
    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"):
        cmd_merge(Args("task-t1"))
    updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    assert updated["explicit_merge_commit"]
    assert updated["status"] == "ACCEPTED_DELIVERY"
    assert updated["accepted_delivery"] is True
    assert updated["delivery_failed"] is False
    # main 分支应包含 delivery 内容
    check = subprocess.run(["git", "show", "main:delivery.txt"], cwd=str(repo), capture_output=True, text=True)
    assert check.returncode == 0 and check.stdout.strip() == "delivery content"


def test_cmd_merge_missing_delivery_branch(tmp_path):
    """没有 delivery_branch → 报错退出，不修改 meta。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir = tmp_path / ".agent_go" / "task-t2"
    task_dir.mkdir(parents=True)
    meta = {"task_id": "task-t2", "repo": str(repo), "delivery_branch": "",
            "target_branch": "main", "status_schema_version": 1}
    (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.sys.exit", side_effect=SystemExit) as exit_mock:
        try:
            cmd_merge(Args("task-t2"))
        except SystemExit:
            pass
    exit_mock.assert_called_once_with(1)


def test_cmd_pr_blocks_after_explicit_merge(tmp_path):
    """P1 互斥：任务已显式 merge 交付后，cmd_pr 必须阻断（禁止双交付）。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir = tmp_path / ".agent_go" / "task-t3"
    task_dir.mkdir(parents=True)
    meta = {"task_id": "task-t3", "repo": str(repo), "delivery_branch": "agent_go/task-t3/delivery",
            "target_branch": "main", "status_schema_version": 1,
            "explicit_merge_commit": "a" * 40}
    (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.sys.exit", side_effect=SystemExit) as exit_mock:
        try:
            cmd_pr(Args("task-t3", offline=False, push=True))
        except SystemExit:
            pass
    exit_mock.assert_called_once_with(1)


def test_cmd_pr_offline_allowed_after_explicit_merge(tmp_path):
    """P1 互斥：offline 模式（仅生成 PR.md，不创建真实 PR）不受互斥阻断。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir = tmp_path / ".agent_go" / "task-t4"
    task_dir.mkdir(parents=True)
    meta = {"task_id": "task-t4", "repo": str(repo), "delivery_branch": "agent_go/task-t4/delivery",
            "target_branch": "main", "status_schema_version": 1,
            "explicit_merge_commit": "b" * 40}
    (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"):
        cmd_pr(Args("task-t4", offline=True))
    assert (task_dir / "PR.md").exists()


def test_cmd_merge_syncs_commit_when_pr_already_merged(tmp_path):
    """P1 互斥+对齐：任务已有 pr_url 且对应 PR 已在 GitHub 合并时，
    cmd_merge 直接同步 mergeCommit 完成交付，不再执行本地 merge。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir, meta = _make_task(tmp_path, repo)
    meta["pr_url"] = "https://github.com/urika/agent_go/pull/999"
    meta["delivery_branch"] = ""
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.shutil.which", return_value="/usr/local/bin/gh"), \
         patch("agent_go.cli.subprocess.run", side_effect=lambda cmd, **kw: (
             subprocess.CompletedProcess(
                 cmd, 0,
                 stdout=json.dumps({"state": "MERGED",
                                    "mergeCommit": {"oid": "9" * 40},
                                    "mergedAt": "2026-08-09T14:09:14Z"}),
                 stderr="") if cmd and cmd[:2] == ["gh", "pr"] else
             subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))):
        cmd_merge(Args("task-t1"))

    updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    assert updated["explicit_merge_commit"] == "9" * 40
    assert updated["status"] == "ACCEPTED_DELIVERY"
    assert updated["accepted_delivery"] is True


def test_cmd_merge_blocks_when_pr_open(tmp_path):
    """P1 互斥：任务已有 pr_url 但 PR 未合并时，cmd_merge 阻断。"""
    repo = _init_repo(tmp_path / "repo")
    task_dir, meta = _make_task(tmp_path, repo)
    meta["pr_url"] = "https://github.com/urika/agent_go/pull/998"
    meta["delivery_branch"] = ""
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

    with patch("agent_go.cli.AGENT_GO_DIR", tmp_path / ".agent_go"), \
         patch("agent_go.cli.shutil.which", return_value="/usr/local/bin/gh"), \
         patch("agent_go.cli.subprocess.run", side_effect=lambda cmd, **kw: (
             subprocess.CompletedProcess(
                 cmd, 0,
                 stdout=json.dumps({"state": "OPEN", "mergeCommit": None}),
                 stderr="") if cmd and cmd[:2] == ["gh", "pr"] else
             subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))), \
         patch("agent_go.cli.sys.exit", side_effect=SystemExit) as exit_mock:
        try:
            cmd_merge(Args("task-t1"))
        except SystemExit:
            pass
    exit_mock.assert_called_once_with(1)
    updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    assert "explicit_merge_commit" not in updated
