"""测试 cross_judge.py — 交叉评判矩阵（P1 简化版）

覆盖：
- _infer_provider / _same_provider（provider 推断 + 禁绝自评的判断基础）
- _heuristic_score（P1 启发式打分；P2 升级为结构化 rubric 时需更新）
- cross_judge_results（编排：禁绝自评 + 无 worktree 降级）
- _judge_one（正常 + 异常路径，含四维退化行为验证）
- calibrate_judge（reliable / marginal / unreliable 三档判定）

所有 LLM 调用都 mock 掉（patch agent_go.evaluator.evaluate_semantic），
参考 tests/test_eval.py:367 的范式。
"""

import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.cross_judge import (
    _infer_provider,
    _same_provider,
    _heuristic_score,
    cross_judge_results,
    _judge_one,
    calibrate_judge,
)


# ═══════════════════════════════════════════════════════════════
# _infer_provider
# ═══════════════════════════════════════════════════════════════

class TestInferProvider:
    """模型名 → provider 推断"""

    def test_anthropic_family(self):
        """claude/fable/haiku/sonnet/opus 都归 anthropic"""
        for m in ["claude-sonnet-4", "claude-opus-4-7", "claude-haiku-4-5",
                  "claude-fable-5", "sonnet[1m]"]:
            assert _infer_provider(m) == "anthropic", f"{m} 应为 anthropic"

    def test_openai_family(self):
        for m in ["gpt-5", "gpt-4o-mini", "o3", "o4-mini"]:
            assert _infer_provider(m) == "openai", f"{m} 应为 openai"

    def test_google(self):
        assert _infer_provider("gemini-2.5-pro") == "google"

    def test_deepseek(self):
        assert _infer_provider("deepseek-v4-flash") == "deepseek"

    def test_china_providers(self):
        """alibaba/volcengine/moonshot/zhipu"""
        assert _infer_provider("qwen-max") == "alibaba"
        assert _infer_provider("doubao-1.5-pro") == "volcengine"
        assert _infer_provider("kimi-k2") == "moonshot"
        assert _infer_provider("glm-5") == "zhipu"

    def test_unknown_falls_back_to_custom(self):
        assert _infer_provider("some-unknown-model") == "custom"
        assert _infer_provider("llama-3") == "custom"


# ═══════════════════════════════════════════════════════════════
# _same_provider
# ═══════════════════════════════════════════════════════════════

class TestSameProvider:
    """禁绝自评的判断基础：同 provider 即视为自评（比模型名严格）"""

    def test_same_provider_different_models_returns_true(self):
        """同 provider 不同模型也判为自评（实现比文档措辞更严）"""
        assert _same_provider("claude-sonnet-4", "claude-haiku-4") is True
        assert _same_provider("gpt-5", "gpt-4o-mini") is True

    def test_different_providers_returns_false(self):
        assert _same_provider("claude-sonnet-4", "gpt-5") is False
        assert _same_provider("deepseek-chat", "claude-sonnet-4") is False


# ═══════════════════════════════════════════════════════════════
# _heuristic_score（P1 简化版核心）
# ═══════════════════════════════════════════════════════════════

class TestHeuristicScore:
    """P1 启发式评分：从 reason 文本正则提取分数。

    注意：P2 升级为结构化 rubric 时，这些测试需相应更新
    （_heuristic_score 将被独立四维分取代）。
    """

    def test_high_quality_keywords_return_5(self):
        assert _heuristic_score("完全正确实现了 spec") == 5.0
        assert _heuristic_score("excellent work, fully correct") == 5.0
        assert _heuristic_score("完整实现所有要求") == 5.0

    def test_good_quality_keywords_return_4(self):
        assert _heuristic_score("基本正确，有小问题") == 4.0
        assert _heuristic_score("mostly correct with minor issue") == 4.0

    def test_incomplete_keywords_return_2(self):
        assert _heuristic_score("缺少边界条件处理") == 2.0
        assert _heuristic_score("implementation is incomplete") == 2.0

    def test_error_keywords_return_1(self):
        assert _heuristic_score("实现错误") == 1.0
        assert _heuristic_score("incorrect logic, test failed") == 1.0

    def test_empty_returns_2_5(self):
        assert _heuristic_score("") == 2.5
        assert _heuristic_score(None) == 2.5

    def test_default_returns_3(self):
        """无关键词命中时返回中等分"""
        assert _heuristic_score("一些中性的描述文字") == 3.0
        assert _heuristic_score("the code does things") == 3.0


# ═══════════════════════════════════════════════════════════════
# cross_judge_results
# ═══════════════════════════════════════════════════════════════

class TestCrossJudgeResults:
    """编排器：禁绝自评 + 无 worktree 降级"""

    def _make_bench_result(self, tmp_path, task_id="task-1",
                           candidate_model="claude-sonnet-4",
                           with_worktree=True):
        """造一条 bench_result + 对应的 meta.json"""
        task_dir = tmp_path / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "wt" / task_id
        if with_worktree:
            worktree.mkdir(parents=True, exist_ok=True)
            results = [{
                "status": "completed",
                "worktree": str(worktree),
                "verification_results": [{"command": "pytest tests/"}],
            }]
        else:
            results = [{"status": "completed"}]  # 无 worktree 字段
        (task_dir / "meta.json").write_text(
            json.dumps({"results": results}), encoding="utf-8")
        return {
            "task_id": task_id,
            "model": candidate_model,
            "task_dir": str(task_dir),
        }

    def test_same_provider_blocked_as_self_eval(self, tmp_path):
        """candidate=claude, judge=claude(不同模型) → 标自评禁止"""
        br = self._make_bench_result(tmp_path, candidate_model="claude-sonnet-4")
        scores = cross_judge_results([br], ["claude-haiku-4"])
        assert len(scores) == 1
        assert scores[0]["error"] == "自评禁止（LLM-as-Judge 自偏）"
        assert scores[0]["semantic_score"] == -1
        assert scores[0]["candidate_model"] == "claude-sonnet-4"
        assert scores[0]["judge_model"] == "claude-haiku-4"

    def test_different_provider_runs_judge(self, tmp_path):
        """candidate=claude, judge=gpt → 正常评判（mock evaluate_semantic）"""
        br = self._make_bench_result(tmp_path, candidate_model="claude-sonnet-4")
        fake_feedback = {
            "passed": True, "reason": "完全正确实现",
            "suggestions": "", "cost_usd": 0.01, "latency_ms": 200,
        }
        with patch("agent_go.evaluator.evaluate_semantic",
                   return_value=fake_feedback):
            scores = cross_judge_results([br], ["gpt-5"])
        assert len(scores) == 1
        assert "error" not in scores[0]
        assert scores[0]["semantic_score"] == 5.0  # "完全正确" → 5.0

    def test_no_worktree_degrades_gracefully(self, tmp_path):
        """meta.json 里 subtask 无 worktree → 所有 judge 标无可用 worktree"""
        br = self._make_bench_result(tmp_path, with_worktree=False)
        scores = cross_judge_results([br], ["gpt-5", "deepseek-chat"])
        assert len(scores) == 2
        for s in scores:
            assert s["error"] == "无可用 worktree"
            assert s["semantic_score"] == -1


# ═══════════════════════════════════════════════════════════════
# _judge_one
# ═══════════════════════════════════════════════════════════════

class TestJudgeOne:
    """单条评判：正常路径（含四维退化验证）+ 异常路径"""

    def test_normal_path_four_dims_degenerate_to_same_score(self, tmp_path):
        """P1 简化：四维（correctness/completeness/code_quality）退化为同一 semantic_score。

        验证退化行为本身（这是当前实现的契约）。
        P2 升级结构化 rubric 后此测试需更新为断言四维独立。
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        fake_feedback = {
            "passed": True, "reason": "完全正确",
            "suggestions": "", "cost_usd": 0.01, "latency_ms": 200,
        }
        with patch("agent_go.evaluator.evaluate_semantic",
                   return_value=fake_feedback):
            result = _judge_one(
                candidate_model="claude-sonnet-4",
                judge_model="gpt-5",
                task_id="task-1",
                task_dir=tmp_path,
                worktree=worktree,
                git_diff="diff --git a/f.py",
                verification="pytest tests/",
            )
        # P1 退化契约：四维同值
        assert result["correctness"] == result["semantic_score"]
        assert result["completeness"] == result["semantic_score"]
        assert result["code_quality"] == result["semantic_score"]
        assert result["semantic_score"] == 5.0  # "完全正确"
        assert result["false_positive"] is False  # passed=True → not True
        assert result["cost_usd"] == 0.01
        assert result["candidate_model"] == "claude-sonnet-4"
        assert result["judge_model"] == "gpt-5"

    def test_failed_pass_means_false_positive(self, tmp_path):
        """passed=False → false_positive=True（验证假阳性检测降级为二元）"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        fake_feedback = {
            "passed": False, "reason": "实现错误",
            "suggestions": "修复 X", "cost_usd": 0.02, "latency_ms": 150,
        }
        with patch("agent_go.evaluator.evaluate_semantic",
                   return_value=fake_feedback):
            result = _judge_one(
                candidate_model="deepseek-chat",
                judge_model="gpt-5",
                task_id="task-1",
                task_dir=tmp_path,
                worktree=worktree,
                git_diff="",
                verification="pytest tests/",
            )
        assert result["false_positive"] is True
        assert result["semantic_score"] == 1.0  # "错误"

    def test_exception_returns_error_with_minus_one(self, tmp_path):
        """evaluate_semantic 抛异常 → 填 error + semantic_score=-1"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with patch("agent_go.evaluator.evaluate_semantic",
                   side_effect=RuntimeError("connection refused")):
            result = _judge_one(
                candidate_model="claude-sonnet-4",
                judge_model="gpt-5",
                task_id="task-1",
                task_dir=tmp_path,
                worktree=worktree,
                git_diff="",
                verification="pytest tests/",
            )
        assert "error" in result
        assert "评判失败" in result["error"]
        assert "connection refused" in result["error"]
        assert result["semantic_score"] == -1
        assert result["false_positive"] is None


# ═══════════════════════════════════════════════════════════════
# calibrate_judge
# ═══════════════════════════════════════════════════════════════

class TestCalibrateJudge:
    """人工校准：reliable / marginal / unreliable 三档"""

    def _write_llm_scores(self, path, scores):
        """造 LLM 评分 JSONL（每行一条 judge 结果）"""
        with open(path, "w", encoding="utf-8") as f:
            for s in scores:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

    def _write_human_csv(self, path, rows):
        """造人工评分 CSV"""
        with open(path, "w", encoding="utf-8") as f:
            f.write("task_id,candidate_model,correctness,completeness,code_quality,false_positive\n")
            for r in rows:
                f.write(f"{r['task_id']},{r['candidate_model']},"
                        f"{r['correctness']},{r['completeness']},"
                        f"{r['code_quality']},{r['false_positive']}\n")

    def test_reliable_when_divergence_low(self, tmp_path):
        """LLM 与人工分歧 ≤1.0 → ✓ reliable"""
        llm_path = tmp_path / "llm.jsonl"
        human_path = tmp_path / "human.csv"
        # LLM 给 4 分，人工三维平均 (5+4+3)/3=4，分歧 0 → reliable
        self._write_llm_scores(llm_path, [{
            "task_id": "t1", "candidate_model": "claude-sonnet-4",
            "judge_model": "gpt-5", "semantic_score": 4,
        }])
        self._write_human_csv(human_path, [{
            "task_id": "t1", "candidate_model": "claude-sonnet-4",
            "correctness": 5, "completeness": 4, "code_quality": 3,
            "false_positive": False,
        }])
        result = calibrate_judge(llm_path, human_path)
        assert "error" not in result
        assert result["total_matches"] == 1
        gpt5 = result["judges"]["gpt-5"]
        assert gpt5["avg_divergence"] == 0.0
        assert "reliable" in gpt5["verdict"]

    def test_unreliable_when_divergence_high(self, tmp_path):
        """LLM 与人工分歧 >1.5 → ✗ unreliable"""
        llm_path = tmp_path / "llm.jsonl"
        human_path = tmp_path / "human.csv"
        # LLM 给 5 分，人工三维平均 (1+1+2)/3=1.33，分歧 3.67 → unreliable
        self._write_llm_scores(llm_path, [{
            "task_id": "t1", "candidate_model": "claude-sonnet-4",
            "judge_model": "gpt-5", "semantic_score": 5,
        }])
        self._write_human_csv(human_path, [{
            "task_id": "t1", "candidate_model": "claude-sonnet-4",
            "correctness": 1, "completeness": 1, "code_quality": 2,
            "false_positive": True,
        }])
        result = calibrate_judge(llm_path, human_path)
        gpt5 = result["judges"]["gpt-5"]
        assert gpt5["avg_divergence"] > 1.5
        assert "unreliable" in gpt5["verdict"]

    def test_empty_data_returns_error(self, tmp_path):
        """无数据时返回 error（不崩溃）"""
        empty_llm = tmp_path / "empty.jsonl"
        empty_csv = tmp_path / "empty.csv"
        empty_llm.write_text("", encoding="utf-8")
        empty_csv.write_text(
            "task_id,candidate_model,correctness,completeness,code_quality,false_positive\n",
            encoding="utf-8")
        result = calibrate_judge(empty_llm, empty_csv)
        assert "error" in result
