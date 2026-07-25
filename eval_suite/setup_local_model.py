"""本地模型接入验证 + 配置辅助。

用法：
  python eval_suite/setup_local_model.py check       # 检查本地模型是否可用
  python eval_suite/setup_local_model.py configure   # 配置 agent_go router
  python eval_suite/setup_local_model.py test        # 用简单提示词跑一次验证

依赖：本地运行 ollama / vLLM / llama.cpp 服务，支持 OpenAI 兼容 API。
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

LOCAL_URL = "http://localhost:8000/v1/chat/completions"
LOCAL_MODEL = "qwen3.6-27b"
LOCAL_PROVIDER = "custom"
HOURLY_COST_USD = 1.50  # RTX 4090 折旧估算


def cmd_check():
    """检查本地模型服务是否可达。"""
    print(f"🔍 检查本地模型服务: {LOCAL_URL}")
    try:
        req = urllib.request.Request(
            LOCAL_URL,
            data=json.dumps({
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": "Say 'OK' in one word."}],
                "max_tokens": 10,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            print(f"  ✅ 服务可达，模型响应: {content[:50]}")
            return True
    except Exception as e:
        print(f"  ❌ 无法连接: {e}")
        print(f"  → 请确认本地模型服务已启动（ollama / vLLM / llama.cpp）")
        print(f"  → 如用 ollama: ollama serve && ollama pull {LOCAL_MODEL}")
        return False


def cmd_configure():
    """生成 agent_go router 配置，让 worker 可以路由到本地模型。"""
    print(f"""📝 将以下配置合并到 ~/.agent_go/config.json:

{{
  "router": {{
    "enabled": true,
    "roles": {{
      "worker": {{
        "provider": "{LOCAL_PROVIDER}",
        "model": "{LOCAL_MODEL}",
        "base_url": "{LOCAL_URL}"
      }}
    }}
  }},
  "worker_models": {{
    "easy": "{LOCAL_MODEL}",
    "medium": "{LOCAL_MODEL}",
    "hard": "claude-opus-4-8"
  }},
  "local_model_hourly_cost": {HOURLY_COST_USD},
  "_note": "local_model_hourly_cost 用于本地推理成本估算（折旧+电费）。RTX 4090 ≈ $1.5/h，M3 Max ≈ $0.5/h。"
}}

或者用 CLI 设置:
  agent_go router set-role worker --provider custom --model {LOCAL_MODEL} --base-url {LOCAL_URL}
""")


def cmd_test():
    """用简单提示词测试本地模型推理能力。"""
    print(f"🧪 测试本地模型简单编码任务...")
    prompt = "Write a Python function `fibonacci(n: int) -> list[int]` that returns the first n Fibonacci numbers. Include a docstring."
    try:
        req = urllib.request.Request(
            LOCAL_URL,
            data=json.dumps({
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = __import__("time").time()
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        elapsed = __import__("time").time() - start
        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("completion_tokens", 0)
        tok_s = round(tokens / max(elapsed, 0.1))
        print(f"  ✅ 响应 ({tokens} tokens, {elapsed:.1f}s, ~{tok_s} tok/s):")
        for line in content.split("\n")[:12]:
            print(f"    {line}")
    except Exception as e:
        print(f"  ❌ 测试失败: {e}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": cmd_check, "configure": cmd_configure, "test": cmd_test}[cmd]()
