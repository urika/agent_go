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
