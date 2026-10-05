"""规则集管线（rule-set pipeline）P0：受限 DSL ＋ 清单读写 ＋ 候选导入 ＋ 离线复算。

概念设计：``docs/design/rule-set-pipeline-design-20261005.md``（v0.2）；
需求登记：``docs/design/jev-review-triage-pilot-requirements-20261005.md`` §18／O-14。

P0 边界（**零 runtime 接入**）：
- 只读写本地文件（默认 ``~/.agent_go/rules/``）；不接 pipeline／executor／任何 runtime 路径；
- 零网络、仅标准库；
- ``shadow_evaluate`` 为 P1 影子接入准备的纯函数，P0 不调用任何执行路径。

四道闸（与需求文档 §9.4 一一对应，机械可查）：
① 标签源闸：候选生成器签名只有 ``states ＋ 人工标签``——**结构上拿不到 jev 结果**；
② 验证闸：``promote → active`` 强制 ``metrics.holdout_n >= 100``、``holdout_sha`` 非空、
   ``regression_ok is True``（不足即拒，无 force 开关）；
③ 落地闸：规则＝受限 DSL＋测试＋``rules.jsonl``（``frozen_sha256`` 执行前校验）；
④ 反哺计量闸：``replay_report`` 输出规则侧覆盖/有效性，供"扩张后按同一判据重度量"。

CLI（P0 形态；``agent_go rules`` 子命令随 P1 接入 cli.py）：

    python3 -m agent_go.rule_set list|show|validate|import-candidates|generate|replay|promote|retire
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import AGENT_GO_DIR

RULES_DIRNAME = "rules"
RULES_FILENAME = "rules.jsonl"

STATUSES = ("candidate", "shadow", "active", "retired")
STAGES = ("plan", "verify", "review")
OPS = ("==", "!=", ">=", "<=", ">", "<")
OPERAND_OPS = (">=", "<=", ">", "<")

HOLDOUT_MIN_N = 100
LABEL_POSITIVE = "content_fix"
LABEL_NEGATIVE = "infra_or_process"
LABEL_UNDECIDABLE = "undecidable"

_RULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class RuleSyntaxError(ValueError):
    """受限 DSL 语法错误（携带位置，便于人读）。"""


# ---------------------------------------------------------------------------
# 受限 DSL：tokenize / parse / eval（AST，全程禁 eval）
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def _tokenize(text: str) -> List[Tuple[str, Any, int]]:
    tokens: List[Tuple[str, Any, int]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LP", ch, i))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RP", ch, i))
            i += 1
            continue
        if ch in "'\"":
            j = text.find(ch, i + 1)
            if j < 0:
                raise RuleSyntaxError("unterminated string at %d" % i)
            tokens.append(("STR", text[i + 1:j], i))
            i = j + 1
            continue
        matched = False
        for op in ("==", "!=", ">=", "<="):
            if text.startswith(op, i):
                tokens.append(("OP", op, i))
                i += 2
                matched = True
                break
        if matched:
            continue
        if ch in "><":
            tokens.append(("OP", ch, i))
            i += 1
            continue
        m = _NUM_RE.match(text, i)
        if m:
            raw = m.group()
            tokens.append(("NUM", float(raw) if "." in raw else int(raw), i))
            i = m.end()
            continue
        m = _IDENT_RE.match(text, i)
        if m:
            word = m.group()
            low = word.lower()
            if low in ("and", "or", "not"):
                tokens.append(("KW", low, i))
            elif low == "true":
                tokens.append(("BOOL", True, i))
            elif low == "false":
                tokens.append(("BOOL", False, i))
            elif low == "null":
                tokens.append(("NULL", None, i))
            else:
                tokens.append(("IDENT", word, i))
            i = m.end()
            continue
        raise RuleSyntaxError("unexpected character %r at %d" % (ch, i))
    tokens.append(("EOF", None, n))
    return tokens


class _Parser:
    def __init__(self, tokens: Sequence[Tuple[str, Any, int]]) -> None:
        self.tokens = list(tokens)
        self.pos = 0

    def _peek(self) -> Tuple[str, Any, int]:
        return self.tokens[self.pos]

    def _next(self) -> Tuple[str, Any, int]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> Any:
        node = self._or_expr()
        kind, _, at = self._peek()
        if kind != "EOF":
            raise RuleSyntaxError("unexpected token at %d" % at)
        return node

    def _or_expr(self) -> Any:
        children = [self._and_expr()]
        while self._peek()[0] == "KW" and self._peek()[1] == "or":
            self._next()
            children.append(self._and_expr())
        return children[0] if len(children) == 1 else ("or", children)

    def _and_expr(self) -> Any:
        children = [self._not_expr()]
        while self._peek()[0] == "KW" and self._peek()[1] == "and":
            self._next()
            children.append(self._not_expr())
        return children[0] if len(children) == 1 else ("and", children)

    def _not_expr(self) -> Any:
        if self._peek()[0] == "KW" and self._peek()[1] == "not":
            self._next()
            return ("not", self._not_expr())
        return self._primary()

    def _primary(self) -> Any:
        kind, value, at = self._peek()
        if kind == "LP":
            self._next()
            node = self._or_expr()
            if self._peek()[0] != "RP":
                raise RuleSyntaxError("missing ')' for '(' at %d" % at)
            self._next()
            return node
        if kind != "IDENT":
            raise RuleSyntaxError("expected field name at %d" % at)
        _, path, _ = self._next()
        kind, op, at = self._next()
        if kind != "OP":
            raise RuleSyntaxError("expected comparison operator after %r at %d" % (path, at))
        kind, value, at = self._next()
        if kind == "NUM" or kind == "STR" or kind == "BOOL":
            literal = value
        elif kind == "NULL":
            literal = None
        else:
            raise RuleSyntaxError("expected literal at %d" % at)
        return ("cmp", path, op, literal)


def parse_condition(condition: str) -> Any:
    """把受限 DSL 文本解析为 AST（元组嵌套；``json`` 序列化后即为规范形）。"""
    if not isinstance(condition, str) or not condition.strip():
        raise RuleSyntaxError("condition must be a non-empty string")
    return _Parser(_tokenize(condition)).parse()


def _resolve(state: Any, path: str) -> Tuple[bool, Any]:
    cur = state
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _compare(op: str, value: Any, literal: Any) -> Tuple[Optional[bool], str]:
    bool_value, bool_literal = isinstance(value, bool), isinstance(literal, bool)
    if bool_value or bool_literal:
        if not (bool_value and bool_literal) or op not in ("==", "!="):
            return None, "type mismatch"
        return (value == literal) if op == "==" else (value != literal), ""
    num_value = isinstance(value, (int, float)) and not bool_value
    num_literal = isinstance(literal, (int, float)) and not bool_literal
    if num_value and num_literal:
        if op == "==":
            return value == literal, ""
        if op == "!=":
            return value != literal, ""
        if op == ">=":
            return value >= literal, ""
        if op == "<=":
            return value <= literal, ""
        if op == ">":
            return value > literal, ""
        return value < literal, ""
    str_value, str_literal = isinstance(value, str), isinstance(literal, str)
    if str_value and str_literal:
        if op == "==":
            return value == literal, ""
        if op == "!=":
            return value != literal, ""
        return None, "ordering on strings is not supported"
    if value is None or literal is None:
        if op == "==":
            return (value is None and literal is None), ""
        if op == "!=":
            return (value is None) != (literal is None), ""
        return None, "ordering with null is not supported"
    return None, "type mismatch"


def eval_condition(condition: Any, state: Any) -> Tuple[Optional[bool], str]:
    """三值求值：``True``／``False``／``None``（判不了，reason 给出原因）。

    fail-open：任何异常一律归为 ``(None, reason)``——规则不可用时不误伤调用方。
    """
    try:
        ast = parse_condition(condition) if isinstance(condition, str) else condition
    except RuleSyntaxError as exc:
        return None, "syntax error: %s" % exc
    if not isinstance(state, dict):
        return None, "state is not a dict"
    try:
        return _eval_node(ast, state)
    except Exception as exc:  # noqa: BLE001 - fail-open 是设计约束
        return None, "eval error: %s" % exc


def _eval_node(node: Any, state: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    kind = node[0]
    if kind == "cmp":
        _, path, op, literal = node
        found, value = _resolve(state, path)
        if not found:
            return None, "missing field: %s" % path
        return _compare(op, value, literal)
    if kind == "not":
        result, reason = _eval_node(node[1], state)
        if result is None:
            return None, reason
        return (not result), ""
    if kind in ("and", "or"):
        unknown_reason = ""
        for child in node[1]:
            result, reason = _eval_node(child, state)
            if kind == "and":
                if result is False:
                    return False, ""
                if result is None and not unknown_reason:
                    unknown_reason = reason
            else:
                if result is True:
                    return True, ""
                if result is None and not unknown_reason:
                    unknown_reason = reason
        return (None, unknown_reason) if unknown_reason else ((True, "") if kind == "and" else (False, ""))
    return None, "bad AST node: %r" % (kind,)


def rule_fires(rule: Dict[str, Any], state: Any) -> Tuple[bool, str]:
    """``True`` 仅当求值结果恰为 True；其余（False/未知）按"未命中"处理并附原因。"""
    result, reason = eval_condition(rule.get("condition", ""), state)
    if result is True:
        return True, ""
    return False, reason or "condition evaluated false"


def condition_sha(condition: str) -> str:
    return hashlib.sha256(condition.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 清单（rules.jsonl）：读写 / 校验 / 冻结校验 / 状态流转
# ---------------------------------------------------------------------------

def default_rules_path() -> Path:
    return AGENT_GO_DIR / RULES_DIRNAME / RULES_FILENAME


def rule_sha(rule: Dict[str, Any]) -> str:
    payload = {k: rule.get(k) for k in ("rule_id", "version", "stage", "condition", "cover")}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_rule(rule: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(rule, dict):
        return ["rule is not an object"]
    rid = rule.get("rule_id")
    if not isinstance(rid, str) or not _RULE_ID_RE.match(rid or ""):
        problems.append("rule_id 缺失或格式不合法（^[a-z0-9][a-z0-9._-]{2,63}$）")
    if not isinstance(rule.get("version"), int) or rule.get("version", 0) < 1:
        problems.append("version 必须是 ≥1 的整数")
    if rule.get("status") not in STATUSES:
        problems.append("status 必须是 %s" % (STATUSES,))
    if rule.get("stage") not in STAGES:
        problems.append("stage 必须是 %s" % (STAGES,))
    try:
        parse_condition(rule.get("condition", ""))
    except RuleSyntaxError as exc:
        problems.append("condition 解析失败：%s" % exc)
    for key in ("cover", "evidence", "metrics"):
        if key in rule and not isinstance(rule[key], dict):
            problems.append("%s 必须是对象" % key)
    return problems


def check_fields(rule: Dict[str, Any], states: Iterable[Dict[str, Any]]) -> List[str]:
    """返回规则引用但从未在任何 state 中出现过的字段（机械核验"字段可达"）。"""
    seen: set = set()
    for state in states:
        for part in _walk_paths(state):
            seen.add(part)
    unknown: List[str] = []
    for path in sorted(_condition_paths(rule.get("condition", ""))):
        if path not in seen:
            unknown.append(path)
    return unknown


def _condition_paths(condition: str) -> set:
    try:
        ast = parse_condition(condition)
    except RuleSyntaxError:
        return set()
    found: set = set()

    def walk(node: Any) -> None:
        if not isinstance(node, tuple):
            return
        if node[0] == "cmp":
            found.add(node[1])
        elif node[0] == "not":
            walk(node[1])
        elif node[0] in ("and", "or"):
            for child in node[1]:
                walk(child)

    walk(ast)
    return found


def _walk_paths(state: Any, prefix: str = "", depth: int = 0) -> Iterable[str]:
    if depth > 3 or not isinstance(state, dict):
        return
    for key, value in state.items():
        path = "%s.%s" % (prefix, key) if prefix else str(key)
        if isinstance(value, dict):
            for sub in _walk_paths(value, path, depth + 1):
                yield sub
        elif isinstance(value, (int, float, bool, str)) or value is None:
            yield path


def load_rules(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = Path(path) if path else default_rules_path()
    if not p.is_file():
        return []
    rules: List[Dict[str, Any]] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rules.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError("%s:%d JSON 解析失败：%s" % (p, lineno, exc))
    return rules


def save_rules(rules: Sequence[Dict[str, Any]], path: Optional[Path] = None) -> Path:
    p = Path(path) if path else default_rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rules, key=lambda r: (str(r.get("rule_id")), int(r.get("version") or 0)))
    body = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in ordered)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
    os.replace(str(tmp), str(p))
    return p


def verify_frozen(rule: Dict[str, Any]) -> bool:
    return bool(rule.get("frozen_sha256")) and rule.get("frozen_sha256") == rule_sha(rule)


def make_rule(*, rule_id: str, condition: str, stage: str = "review", status: str = "candidate",
              cover: Optional[Dict[str, Any]] = None, evidence: Optional[Dict[str, Any]] = None,
              metrics: Optional[Dict[str, Any]] = None, created_by: str = "human",
              created_at: Optional[str] = None) -> Dict[str, Any]:
    rule: Dict[str, Any] = {
        "rule_id": rule_id,
        "version": 1,
        "status": status,
        "stage": stage,
        "condition": condition,
        "cover": cover or {},
        "evidence": evidence or {"source": "human", "refs": []},
        "metrics": metrics or {"holdout_n": 0, "holdout_sha": "", "regression_ok": False,
                               "precision": None, "recall": None},
        "created_by": created_by,
        "reviewed_by": "",
        "created_at": created_at or _now(),
        "updated_at": created_at or _now(),
    }
    rule["frozen_sha256"] = rule_sha(rule)
    return rule


def upsert_rule(rules: List[Dict[str, Any]], rule: Dict[str, Any]) -> str:
    """按 rule_id 合并：同条件去重 / 换条件升版（回到 candidate，需重新过验证闸）。"""
    for idx, existing in enumerate(rules):
        if existing.get("rule_id") != rule.get("rule_id"):
            continue
        if existing.get("condition") == rule.get("condition"):
            return "unchanged"
        rule["version"] = int(existing.get("version") or 1) + 1
        rule["status"] = "candidate"
        rule["created_at"] = existing.get("created_at") or rule.get("created_at")
        rule["frozen_sha256"] = rule_sha(rule)
        rules[idx] = rule
        return "updated"
    rules.append(rule)
    return "added"


def promote_rule(rules: List[Dict[str, Any]], rule_id: str, to_status: str) -> Tuple[bool, str]:
    """状态流转；``→ active`` 强制过验证闸（②），无 force 逃逸。"""
    if to_status not in ("shadow", "active"):
        return False, "promote 只支持 → shadow / active"
    for rule in rules:
        if rule.get("rule_id") != rule_id:
            continue
        if to_status == "active":
            metrics = rule.get("metrics") or {}
            n = metrics.get("holdout_n") or 0
            if n < HOLDOUT_MIN_N:
                return False, "验证闸未过：holdout_n=%s < %d" % (n, HOLDOUT_MIN_N)
            if not metrics.get("holdout_sha"):
                return False, "验证闸未过：缺 holdout_sha"
            if metrics.get("regression_ok") is not True:
                return False, "验证闸未过：regression_ok != True"
        rule["status"] = to_status
        rule["updated_at"] = _now()
        rule["frozen_sha256"] = rule_sha(rule)
        return True, "ok"
    return False, "rule_id 不存在：%s" % rule_id


def retire_rule(rules: List[Dict[str, Any]], rule_id: str, reason: str = "") -> Tuple[bool, str]:
    for rule in rules:
        if rule.get("rule_id") != rule_id:
            continue
        rule["status"] = "retired"
        rule["updated_at"] = _now()
        if reason:
            evidence = rule.setdefault("evidence", {})
            evidence["retire_reason"] = reason
        rule["frozen_sha256"] = rule_sha(rule)
        return True, "ok"
    return False, "rule_id 不存在：%s" % rule_id


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# 候选（rule_candidates.jsonl → 规则清单）
# ---------------------------------------------------------------------------

def candidate_to_rule(cand: Dict[str, Any]) -> Dict[str, Any]:
    condition = cand.get("condition")
    if not condition:
        feature, op, threshold = cand.get("feature"), cand.get("op"), cand.get("threshold")
        if not isinstance(feature, str) or op not in OPS:
            raise ValueError("候选缺 condition，且 feature/op 不完整：%r" % (cand,))
        condition = "%s %s %s" % (feature, op, _literal_text(threshold))
    parse_condition(condition)
    rule_id = cand.get("rule_id") or ("cand-" + condition_sha(condition)[:10])
    evidence = {
        "source": cand.get("source") or "generator",
        "refs": list(cand.get("evidence_refs") or []),
        "cover_n": cand.get("cover_n"),
        "direction": cand.get("direction"),
    }
    metrics = {
        "holdout_n": int(cand.get("holdout_n") or 0),
        "holdout_sha": "",
        "regression_ok": False,
        "precision": cand.get("precision"),
        "recall": cand.get("recall"),
    }
    return make_rule(rule_id=rule_id, condition=condition,
                     stage=str(cand.get("stage") or "review"), status="candidate",
                     cover=cand.get("cover") or {}, evidence=evidence, metrics=metrics,
                     created_by=str(cand.get("created_by") or "generator"))


def _literal_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "'%s'" % value
    return repr(value)


def import_candidates(candidates_path: Path, rules: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"added": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    if not Path(candidates_path).is_file():
        raise FileNotFoundError(str(candidates_path))
    for lineno, line in enumerate(Path(candidates_path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cand = json.loads(line)
            rule = candidate_to_rule(cand)
        except (json.JSONDecodeError, ValueError, RuleSyntaxError):
            summary["rejected"] += 1
            continue
        summary[upsert_rule(rules, rule)] += 1
    return summary


# ---------------------------------------------------------------------------
# 历史样本离线复算（规则侧覆盖/有效性）
# ---------------------------------------------------------------------------

def load_labeled_states(states_dir: Path,
                        labels_path: Path) -> List[Tuple[str, Dict[str, Any], str]]:
    labels: Dict[str, str] = {}
    if Path(labels_path).is_file():
        for line in Path(labels_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("origin", "human") == "human":
                labels[str(row.get("run_ref"))] = str(row.get("label"))
    rows: List[Tuple[str, Dict[str, Any], str]] = []
    for state_file in sorted(Path(states_dir).glob("*.json")):
        run_ref = state_file.stem
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append((run_ref, state, labels.get(run_ref, "")))
    return rows


def _binary_stats(fired: Sequence[bool], labels: Sequence[str]) -> Dict[str, Any]:
    pairs = list(zip(fired, labels))
    tp = sum(1 for hit, label in pairs if hit and label == LABEL_POSITIVE)
    fn = sum(1 for hit, label in pairs if not hit and label == LABEL_POSITIVE)
    fp = sum(1 for hit, label in pairs if hit and label == LABEL_NEGATIVE)
    tn = sum(1 for hit, label in pairs if not hit and label == LABEL_NEGATIVE)
    precision = (tp / (tp + fp)) if (tp + fp) else None
    recall = (tp / (tp + fn)) if (tp + fn) else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall,
            "labeled": tp + fp + fn + tn}


def replay_report(rules: Sequence[Dict[str, Any]],
                  labeled: Sequence[Tuple[str, Dict[str, Any], str]],
                  *, statuses: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """对历史样本复算每条规则的覆盖/有效性，并给出联合覆盖（∪）——确定性输出。"""
    selected = [r for r in rules if statuses is None or r.get("status") in statuses]
    total = len(labeled)
    dist: Dict[str, int] = {}
    for _, _, lb in labeled:
        key = lb or "unlabeled"
        dist[key] = dist.get(key, 0) + 1

    fired_matrix: List[List[bool]] = []
    per_rule: List[Dict[str, Any]] = []
    unknown_reasons: Dict[str, int] = {}
    for rule in selected:
        fired: List[bool] = []
        for _, state, _lb in labeled:
            hit, reason = rule_fires(rule, state)
            fired.append(hit)
            if not hit and reason:
                unknown_reasons[reason.split(":")[0]] = unknown_reasons.get(reason.split(":")[0], 0) + 1
        fired_matrix.append(fired)
        labeled_fired = [f for f, (_r, _s, lb) in zip(fired, labeled)
                         if lb in (LABEL_POSITIVE, LABEL_NEGATIVE)]
        labeled_labels = [lb for _r, _s, lb in labeled if lb in (LABEL_POSITIVE, LABEL_NEGATIVE)]
        stats = _binary_stats(labeled_fired, labeled_labels)
        per_rule.append({
            "rule_id": rule.get("rule_id"),
            "version": rule.get("version"),
            "status": rule.get("status"),
            "frozen_ok": verify_frozen(rule),
            "fired": sum(1 for f in fired if f),
            "fire_rate": (sum(1 for f in fired if f) / total) if total else None,
            **stats,
        })

    union_stats: Dict[str, Any] = {}
    if fired_matrix:
        union_fired = [any(matrix[i] for matrix in fired_matrix) for i in range(total)]
        labeled_union = [f for f, (_r, _s, lb) in zip(union_fired, labeled)
                         if lb in (LABEL_POSITIVE, LABEL_NEGATIVE)]
        labeled_labels = [lb for _r, _s, lb in labeled if lb in (LABEL_POSITIVE, LABEL_NEGATIVE)]
        union_stats = {"fired": sum(1 for f in union_fired if f),
                       "fire_rate": (sum(1 for f in union_fired if f) / total) if total else None,
                       **_binary_stats(labeled_union, labeled_labels)}

    return {
        "schema": 1,
        "generated_at": _now(),
        "samples": total,
        "labels": dist,
        "rules": per_rule,
        "union": union_stats,
        "unknown_field_hits": dict(sorted(unknown_reasons.items())),
    }


# ---------------------------------------------------------------------------
# 候选生成（P0 最小版：单特征阈值扫描；签名只收 states＋人工标签＝标签源闸①）
# ---------------------------------------------------------------------------

def generate_candidates(labeled: Sequence[Tuple[str, Dict[str, Any], str]],
                        *, min_cover: int = 3, min_precision: float = 0.7,
                        max_candidates: int = 20) -> List[Dict[str, Any]]:
    """从"state＋人工标签"生成候选（正类＝content_fix）。

    只读 states 与人工标签——**结构上不接触 jev 输出**（标签源闸①）。
    """
    rows = [(st, lb) for _ref, st, lb in labeled if lb in (LABEL_POSITIVE, LABEL_NEGATIVE)]
    if not rows:
        return []
    numeric_paths: Dict[str, set] = {}
    bool_paths: set = set()
    str_values: Dict[str, set] = {}
    for state, _label in rows:
        for field_path in _walk_paths(state):
            found, value = _resolve(state, field_path)
            if not found:
                continue
            if isinstance(value, bool):
                bool_paths.add(field_path)
            elif isinstance(value, (int, float)):
                numeric_paths.setdefault(field_path, set()).add(value)
            elif isinstance(value, str):
                str_values.setdefault(field_path, set()).add(value)

    conditions: List[str] = []
    for num_path in sorted(numeric_paths):
        for threshold in sorted(numeric_paths[num_path]):
            conditions.append("%s >= %s" % (num_path, _literal_text(threshold)))
            conditions.append("%s <= %s" % (num_path, _literal_text(threshold)))
            conditions.append("%s == %s" % (num_path, _literal_text(threshold)))
    for bool_path in sorted(bool_paths):
        conditions.append("%s == true" % bool_path)
        conditions.append("%s == false" % bool_path)
    for str_path in sorted(str_values):
        if len(str_values[str_path]) > 8:
            continue
        for value in sorted(str_values[str_path]):
            conditions.append("%s == '%s'" % (str_path, value))

    results: List[Dict[str, Any]] = []
    for condition in conditions:
        fired = [rule_fires({"condition": condition}, state)[0] for state, _lb in rows]
        labels = [lb for _st, lb in rows]
        stats = _binary_stats(fired, labels)
        if stats["tp"] < min_cover:
            continue
        if stats["precision"] is None or stats["precision"] < min_precision:
            continue
        results.append({
            "condition": condition,
            "source": "generator",
            "cover_n": sum(1 for f in fired if f),
            "precision": round(stats["precision"], 4),
            "recall": round(stats["recall"], 4) if stats["recall"] is not None else None,
            "tp": stats["tp"], "fp": stats["fp"], "fn": stats["fn"], "tn": stats["tn"],
            "created_by": "generator",
        })
    results.sort(key=lambda c: (-(c["precision"] or 0.0), -(c["cover_n"] or 0), c["condition"]))
    return results[:max_candidates]


# ---------------------------------------------------------------------------
# 影子执行（P1 接入点；P0 只提供纯函数，不调用）
# ---------------------------------------------------------------------------

def shadow_evaluate(rules: Sequence[Dict[str, Any]], state: Dict[str, Any], stage: str,
                    *, now: Optional[str] = None) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    for rule in rules:
        if rule.get("stage") != stage:
            continue
        if rule.get("status") not in ("shadow", "active"):
            continue
        hit, reason = rule_fires(rule, state)
        decisions.append({"rule_id": rule.get("rule_id"), "version": rule.get("version"),
                          "stage": stage, "result": hit if hit else None,
                          "reason": reason, "ts": now or _now()})
    return decisions


def append_decisions(task_dir: Path, decisions: Sequence[Dict[str, Any]]) -> Optional[Path]:
    if not decisions:
        return None
    path = Path(task_dir) / "rule_decisions.jsonl"
    try:
        with path.open("a", encoding="utf-8") as fh:
            for row in decisions:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return path
    except OSError:
        return None  # fail-open：影子记录失败绝不阻断主链路


# ---------------------------------------------------------------------------
# CLI（P0 形态：python3 -m agent_go.rule_set ...）
# ---------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    if args.json:
        print(json.dumps(rules, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for rule in rules:
        print("%-28s v%-3s %-9s %-7s %s" % (rule.get("rule_id"), rule.get("version"),
                                            rule.get("status"), rule.get("stage"),
                                            rule.get("condition")))
    print("共 %d 条" % len(rules))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    for rule in load_rules(args.rules):
        if rule.get("rule_id") == args.rule_id:
            print(json.dumps(rule, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
    print("rule_id 不存在：%s" % args.rule_id, file=sys.stderr)
    return 1


def _cmd_validate(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    problems: List[str] = []
    for rule in rules:
        for problem in validate_rule(rule):
            problems.append("%s: %s" % (rule.get("rule_id"), problem))
        if not verify_frozen(rule):
            problems.append("%s: frozen_sha256 校验失败（被改动或缺失）" % rule.get("rule_id"))
    if args.states_dir:
        states = [json.loads(p.read_text(encoding="utf-8"))
                  for p in sorted(Path(args.states_dir).glob("*.json"))]
        for rule in rules:
            unknown = check_fields(rule, states)
            if unknown:
                problems.append("%s: 字段在样本中不可达 %s" % (rule.get("rule_id"), unknown))
    for problem in problems:
        print("[validate][FAIL] %s" % problem)
    if problems:
        return 1
    print("[validate] 通过：%d 条规则；DSL/冻结校验%s" %
          (len(rules), "＋字段可达性" if args.states_dir else ""))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    summary = import_candidates(Path(args.candidates), rules)
    save_rules(rules, args.rules)
    print("[import] %s" % json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    labeled = load_labeled_states(Path(args.states_dir), Path(args.labels))
    candidates = generate_candidates(labeled, min_cover=args.min_cover,
                                     min_precision=args.min_precision)
    out = Path(args.out) if args.out else None
    body = "\n".join(json.dumps(c, ensure_ascii=False, sort_keys=True) for c in candidates)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body + ("\n" if body else ""), encoding="utf-8")
        print("[generate] %d 条候选 → %s" % (len(candidates), out))
    else:
        print(body)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    labeled = load_labeled_states(Path(args.states_dir), Path(args.labels))
    report = replay_report(rules, labeled, statuses=args.statuses)
    body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print("[replay] → %s（样本 %d；规则 %d）" % (args.out, report["samples"], len(report["rules"])))
    else:
        print(body)
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    ok, message = promote_rule(rules, args.rule_id, args.to)
    if not ok:
        print("[promote][FAIL] %s" % message, file=sys.stderr)
        return 1
    save_rules(rules, args.rules)
    print("[promote] %s → %s" % (args.rule_id, args.to))
    return 0


def _cmd_retire(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    ok, message = retire_rule(rules, args.rule_id, args.reason or "")
    if not ok:
        print("[retire][FAIL] %s" % message, file=sys.stderr)
        return 1
    save_rules(rules, args.rules)
    print("[retire] %s" % args.rule_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m agent_go.rule_set",
                                description="规则集管线 P0（离线；零 runtime 接入）")
    p.add_argument("--rules", type=Path, default=None, help="rules.jsonl 路径（默认 ~/.agent_go/rules/）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="列出规则")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_list)

    sp = sub.add_parser("show", help="显示单条规则")
    sp.add_argument("rule_id")
    sp.set_defaults(func=_cmd_show)

    sp = sub.add_parser("validate", help="校验 DSL/清单/冻结哈希（可选字段可达性）")
    sp.add_argument("--states-dir")
    sp.set_defaults(func=_cmd_validate)

    sp = sub.add_parser("import-candidates", help="导入 rule_candidates.jsonl")
    sp.add_argument("--candidates", required=True)
    sp.set_defaults(func=_cmd_import)

    sp = sub.add_parser("generate", help="从历史样本＋人工标签生成候选（标签源闸①）")
    sp.add_argument("--states-dir", required=True)
    sp.add_argument("--labels", required=True)
    sp.add_argument("--out")
    sp.add_argument("--min-cover", type=int, default=3)
    sp.add_argument("--min-precision", type=float, default=0.7)
    sp.set_defaults(func=_cmd_generate)

    sp = sub.add_parser("replay", help="历史样本离线复算（覆盖/有效性报告）")
    sp.add_argument("--states-dir", required=True)
    sp.add_argument("--labels", required=True)
    sp.add_argument("--statuses", nargs="*", default=None)
    sp.add_argument("--out")
    sp.set_defaults(func=_cmd_replay)

    sp = sub.add_parser("promote", help="状态流转（→ active 强制验证闸②）")
    sp.add_argument("rule_id")
    sp.add_argument("--to", choices=["shadow", "active"], required=True)
    sp.set_defaults(func=_cmd_promote)

    sp = sub.add_parser("retire", help="退役规则")
    sp.add_argument("rule_id")
    sp.add_argument("--reason", default="")
    sp.set_defaults(func=_cmd_retire)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print("[usage] 文件不存在：%s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
