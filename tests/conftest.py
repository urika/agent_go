"""pytest 共享 fixtures"""

import logging
import sys
import os
from pathlib import Path
from typing import Generator
import pytest

# 确保可以导入 agent_go 模块
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def logger():
    """返回一个只写 DEBUG 级别的内存 logger，不产生文件输出。"""
    log = logging.getLogger("test_logger")
    log.setLevel(logging.DEBUG)
    # 清除已有 handler
    for h in list(log.handlers):
        log.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    return log


@pytest.fixture
def temp_dir(tmp_path: Path) -> Generator[Path, None, None]:
    """返回一个临时目录，测试结束后自动清理。"""
    cwd = Path.cwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(cwd)


@pytest.fixture
def sample_plan():
    """返回一个典型的 Plan JSON 结构。"""
    return {
        "overview": "实现用户认证功能",
        "steps": [
            {
                "id": 1,
                "title": "后端 JWT 认证",
                "description": "实现 JWT 令牌签发和验证",
                "files": ["src/auth/jwt.py", "src/auth/middleware.py"],
                "verification": "pytest tests/test_auth.py",
                "risks": ["密钥管理"],
                "agent_prompt": "请在后端实现 JWT 认证流程，包括签发和验证中间件。"
            },
            {
                "id": 2,
                "title": "前端登录页面",
                "description": "实现用户登录表单和 token 存储",
                "files": ["src/pages/login.tsx"],
                "verification": "npm run test:login",
                "risks": ["Token 安全存储"],
                "agent_prompt": "请实现登录页面组件。"
            }
        ],
        "dependencies": {
            "2": [1]
        },
        "estimated_effort": "3 天",
        "shared_resources": {
            "git_remote": "https://github.com/user/repo.git",
            "git_branch": "main",
            "directories": ["src", "tests"],
            "config_files": ["package.json"],
            "env_vars": ["JWT_SECRET", "API_URL"]
        }
    }


@pytest.fixture
def minimal_plan():
    """返回最小 Plan（无 dependencies、shared_resources）。"""
    return {
        "overview": "简单任务",
        "steps": [
            {
                "id": 1,
                "title": "单步任务",
                "description": "描述"
            }
        ],
        "estimated_effort": "1 小时"
    }
