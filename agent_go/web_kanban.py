"""Web 看板（Kanban）切面：看板视图数据 + 看板写端点 mixin。

拆分自 web_server.py（ISSUE-55）：
  - api_kanban / _task_status_snapshot：/api/kanban 的卡片分组视图与任务状态
    快照缓存（meta 签名失效重建）；
  - WebKanbanMixin：/api/kanban/* 写端点实现（建卡/拆解/导入 spec/更新/流转/
    归档/删除/审批/派发/降级建议），由 web_handler.WebHandler 组合。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import kanban
from .kanban import KanbanError
from .status import task_status
from .task_runner import task_runner
from .web_data import (
    _RUNNING_STATES,
    _audit,
    _list_task_dirs,
    _root,
    _task_meta,
    _task_status_of,
)

logger = logging.getLogger(__name__)


# ── 任务状态快照（api_kanban 复用，meta 签名缓存避免每请求全量解析）──

_task_status_cache: dict[str, dict] = {}
_task_status_sig: str = ""
_task_status_lock = threading.Lock()


def _task_status_snapshot() -> dict[str, dict]:
    """task_id → {task_id, status, task} 快照，按各 meta.json 的 mtime:size 签名
    缓存；任务无变化时 /api/kanban 复用，避免每次请求全量 open+json.loads（O(tasks)）。
    任务目录/meta 变化（含新增/清理）都会使签名失效。
    """
    global _task_status_cache, _task_status_sig
    parts: list[str] = []
    dirs: list[Path] = []
    for td in _list_task_dirs():
        dirs.append(td)
        meta_p = td / "meta.json"
        try:
            parts.append(f"{td.name}:{meta_p.stat().st_mtime:.6f}:{meta_p.stat().st_size}")
        except OSError:
            continue
    sig = f"{_root().AGENT_GO_DIR}\n" + "|".join(parts)
    if sig == _task_status_sig:
        return _task_status_cache
    status_map: dict[str, dict] = {}
    for td in dirs:
        meta = _task_meta(td)
        if not meta:
            continue
        status_map[td.name] = {
            "task_id": td.name,
            "status": task_status(meta),
            "task": meta.get("task", ""),
        }
    with _task_status_lock:
        _task_status_cache = status_map
        _task_status_sig = sig
    return status_map


def api_kanban(include_archived: bool = False) -> dict:
    """看板数据（Kanban）：卡片按 stage 分组 + 关联任务实时状态派生。

    看板 stage 与执行状态正交：卡片只存 task_ids 软链接，latest_task 从
    meta.json 实时派生（复用 _list_task_dirs/task_status，mtime 签名缓存），
    不冗余在卡片上。archived 卡片默认不返回；include_archived=True 时按其
    stage 分组返回（卡片带 archived:true，供前端归档视图/取消归档）。
    """
    board = kanban.load_board()
    status_map = _task_status_snapshot()
    grouped: dict[str, list] = {key: [] for key, _ in kanban.STAGES}
    for card in board.get("cards", []):
        if card.get("archived") and not include_archived:
            continue
        stage = card.get("stage", "")
        if stage not in grouped:
            continue  # 历史脏数据（非法 stage）防御性跳过
        c = dict(card)
        tids = c.get("task_ids") or []
        latest = None
        for tid in reversed(tids):  # 最近一次派发优先
            if tid in status_map:
                latest = status_map[tid]
                break
        if latest is None and tids:
            # 关联任务已被清理 → 标记 unknown（不丢弃链接信息）
            latest = {"task_id": tids[-1], "status": "unknown", "task": ""}
        c["latest_task"] = latest
        grouped[stage].append(c)
    return {
        "stages": [{"key": k, "label": label} for k, label in kanban.STAGES],
        "card_types": kanban.CARD_TYPES,
        "cards": grouped,
        "total": sum(len(v) for v in grouped.values()),
    }


class WebKanbanMixin:
    """看板写端点 mixin（由 web_handler.WebHandler 组合进 HTTP handler）。

    方法体与原 web_server.WebHandler 实现逐行一致；仅依赖组合层提供的
    _reply_json（TYPE_CHECKING 声明供 mypy 静态检查）。
    """

    if TYPE_CHECKING:
        def _reply_json(self, code: int, payload: Any,
                        extra_headers: Optional[dict] = None) -> None: ...

    # ── 看板（Kanban）写操作 ─────────────────────────────────

    def _kanban_card_or_reply(self, card_id: str) -> Optional[dict]:
        """看板写端点共用前置：id 格式非法 → 400；不存在 → 404；通过返回卡片。"""
        try:
            card = kanban.get_card(card_id)
        except KanbanError as e:
            self._reply_json(400, {"error": str(e)})
            return None
        if card is None:
            self._reply_json(404, {"error": f"卡片不存在: {card_id}"})
            return None
        return card

    def _op_kanban_create(self, body: dict, token: str) -> None:
        title = str(body.get("title") or "").strip()
        ctype = str(body.get("type") or "")
        if not title:
            self._reply_json(400, {"error": "title 不能为空"})
            return
        if ctype not in kanban.CARD_TYPES:
            self._reply_json(400, {"error": f"type 须为 {'/'.join(kanban.CARD_TYPES)}"})
            return
        # 非法 stage / implementation 缺 repo → KanbanError → 422（except 链）
        card = kanban.create_card(
            title=title, type=ctype,
            stage=str(body.get("stage") or "brainstorm"),
            repo=str(body.get("repo") or "").strip(),
            description=str(body.get("description") or ""),
            cron=str(body.get("cron") or "").strip(),
            spec_path=str(body.get("spec_path") or "").strip())
        _audit("kanban.create", {"card_id": card["id"], "title": title, "type": ctype},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_decompose(self, body: dict, token: str) -> None:
        """需求拆解（decompose）：本地 LLM 按预设模板把复杂需求拆解为功能单元，
        每单元生成 Task Spec（7 章节 Markdown），可选自动建看板卡片。"""
        requirement = str(body.get("requirement") or "").strip()
        if not requirement:
            self._reply_json(400, {"error": "requirement 不能为空"})
            return
        auto_create = bool(body.get("auto_create", False))
        repo = str(body.get("repo") or "").strip()
        stage = str(body.get("stage") or "brainstorm")

        # ── 模板 1：需求拆解为功能单元（JSON 数组）──
        from .api import call_api
        from .config import load_config
        config = load_config()
        # 本地模型做需求拆解是长推理任务（35B 级可能超默认 120s timeout）→ 加大到 300s
        config = dict(config)
        config["plan_api"] = dict(config.get("plan_api", {}))
        config["plan_api"]["timeout_ms"] = int(config["plan_api"].get("timeout_ms", 120000)) * 3
        decompose_prompt = (
            "你是一个需求分析专家。把以下复杂业务需求拆解为可独立交付的功能单元。"
            "每个功能单元应是单一模块/功能点（可独立实现和验证，避免跨模块耦合）。\n\n"
            f"===== 复杂需求 =====\n{requirement}\n===== 结束 =====\n\n"
            "输出要求：只输出合法 JSON 数组（不要 markdown 包裹），每个元素一个功能单元，字段：\n"
            '{"title": "功能单元标题（动词短语）", "goal": "该单元的目标", '
            '"scope_hint": "范围提示（改什么/不动什么）", "task_type": "feature|bugfix|refactor|test|docs|infra", '
            '"priority": 1-5的数（1最高）}\n'
            "拆解为 3-8 个功能单元，按业务逻辑排序（基础/依赖先行）。"
        )
        messages = [
            {"role": "system", "content": "You are a requirements analyst. Output ONLY valid JSON array."},
            {"role": "user", "content": decompose_prompt},
        ]
        try:
            content = call_api(config, messages, logger)
        except Exception as e:
            _audit("kanban.decompose", {"requirement": requirement[:100]}, str(e), False, token)
            self._reply_json(422, {"error": f"LLM 拆解失败: {e}"})
            return

        # 解析功能单元 JSON（容错 markdown 包裹）
        import re as _re
        units: list = []
        try:
            text = content.strip()
            m = _re.search(r"\[\s*\{.*\}\s*\]", text, _re.S)
            if m:
                text = m.group(0)
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("units") or data.get("features") or [data]
            units = [u for u in data if isinstance(u, dict) and u.get("title")][:8]
        except (json.JSONDecodeError, TypeError) as e:
            _audit("kanban.decompose", {"requirement": requirement[:100]}, f"解析失败: {e}", False, token)
            self._reply_json(422, {"error": f"LLM 返回无法解析为功能单元 JSON: {e}"})
            return
        if not units:
            self._reply_json(422, {"error": "未拆解出功能单元"})
            return

        # ── 模板 2：每单元生成 Task Spec + 可选建卡 ──
        from .config import AGENT_GO_DIR
        from . import kanban
        specs_dir = AGENT_GO_DIR / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        specs_out = []
        cards_out = []
        for i, u in enumerate(units, 1):
            spec_prompt = (
                "为以下功能单元生成一份 Task Spec 需求文档（Markdown，7 章节）。\n\n"
                f"功能单元: {u.get('title')}\n目标: {u.get('goal', '')}\n"
                f"范围提示: {u.get('scope_hint', '')}\n类型: {u.get('task_type', 'feature')}\n\n"
                "输出 Markdown，章节：# Task Spec: <标题>\n## §1 目标\n## §2 动机\n## §3 范围\n"
                "## §4 约束\n## §5 验收标准（可执行命令/测试）\n## §6 参考资料\n## §7 已知风险\n"
                "顶部加元数据行: task_type: <类型>"
            )
            try:
                spec_md = call_api(config, [
                    {"role": "system", "content": "You write Task Spec documents. Output ONLY Markdown."},
                    {"role": "user", "content": spec_prompt},
                ], logger)
            except Exception as e:
                spec_md = f"# Task Spec: {u.get('title')}\n\n(生成失败: {e})"
            _slug = _re.sub(r"[^a-z0-9]+", "-", str(u.get("title", "")).lower()).strip("-")[:40]
            spec_file = specs_dir / f"unit-{i}-{_slug or f'unit-{i}'}.md"
            spec_file.write_text(spec_md, encoding="utf-8")
            specs_out.append({"unit": u.get("title"), "spec_path": str(spec_file)})

            if auto_create:
                try:
                    card = kanban.create_card(
                        title=str(u.get("title", ""))[:80], type="implementation",
                        stage=stage, repo=repo,
                        description=f"【目标】{u.get('goal', '')}\n\n【范围】{u.get('scope_hint', '')}",
                        spec_path=str(spec_file),
                    )
                    cards_out.append({"unit": u.get("title"), "card_id": card["id"],
                                      "automation": card.get("automation")})
                except Exception as e:
                    cards_out.append({"unit": u.get("title"), "error": str(e)[:80]})

        _audit("kanban.decompose", {"requirement": requirement[:100], "units": len(units),
                                    "auto_create": auto_create},
               {"units": len(units), "cards": len(cards_out)}, True, token)
        self._reply_json(200, {
            "ok": True, "units": units, "specs": specs_out, "cards": cards_out,
            "note": "人工 review spec 后可用 agent_go spec validate 准入审查；卡片已进入看板编排流" if auto_create else "",
        })

    def _op_kanban_import_spec(self, body: dict, token: str) -> None:
        """POST /api/kanban/import-spec：从 Task Spec 需求文档生成看板卡片。

        body: {spec_path, stage?, repo?, type?} → 解析 spec → 组装卡片 → 创建。
        """
        from .spec import parse_spec
        spec_path = str(body.get("spec_path") or "").strip()
        if not spec_path:
            self._reply_json(400, {"error": "spec_path 不能为空"})
            return
        spec = parse_spec(Path(spec_path))
        if spec is None:
            self._reply_json(422, {"error": f"Spec 解析失败或文件不存在: {spec_path}"})
            return
        stage = str(body.get("stage") or "brainstorm")
        repo = str(body.get("repo") or "").strip()
        ctype = str(body.get("type") or "implementation")
        title = spec.title or Path(spec_path).stem
        desc_parts = []
        if spec.goal:
            desc_parts.append(f"【目标】{spec.goal}")
        if spec.acceptance:
            desc_parts.append(f"【验收】{spec.acceptance}")
        if spec.scope:
            desc_parts.append(f"【范围】{spec.scope}")
        description = "\n\n".join(desc_parts)
        try:
            card = kanban.create_card(
                title=title, type=ctype, stage=stage, repo=repo,
                description=description, spec_path=spec_path,
            )
        except Exception as e:
            self._reply_json(422, {"error": f"创建卡片失败: {e}"})
            return
        _audit("kanban.import_spec", {"spec_path": spec_path, "card_id": card["id"]},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card,
                               "flow": f"{card['stage']} → design → implementation → operations"})

    def _op_kanban_update(self, card_id: str, body: dict, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        # null 安全（与 create 一致）：null 视为未传，跳过；title/repo/cron/spec_path strip，
        # description 保留原文（markdown 缩进/首尾空格可能是有意为之）。
        fields = {}
        for k in ("title", "description", "repo", "cron", "spec_path"):
            if k in body and body[k] is not None:
                v = body[k]
                fields[k] = str(v).strip() if k != "description" else str(v)
        if not fields:
            self._reply_json(400, {"error": "无可更新字段（title/description/repo/cron/spec_path）"})
            return
        card = kanban.update_card(card_id, **fields)
        _audit("kanban.update", {"card_id": card_id, "fields": sorted(fields)},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_move(self, card_id: str, body: dict, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        stage = str(body.get("stage") or "")
        if not stage:
            self._reply_json(400, {"error": "stage 不能为空"})
            return
        card = kanban.move_card(card_id, stage, note=str(body.get("note") or ""))
        _audit("kanban.move", {"card_id": card_id, "stage": stage}, card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_archive(self, card_id: str, body: dict, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        archived = body.get("archived", True)
        if not isinstance(archived, bool):
            self._reply_json(400, {"error": "archived 必须为布尔值（true/false）"})
            return
        card = kanban.archive_card(card_id, archived=archived)
        _audit("kanban.archive", {"card_id": card_id, "archived": archived},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_delete(self, card_id: str, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        # 已派发过任务的卡片 → KanbanError → 422（except 链）
        kanban.delete_card(card_id)
        _audit("kanban.delete", {"card_id": card_id}, {"deleted": card_id}, True, token)
        self._reply_json(200, {"ok": True, "deleted": card_id})

    def _op_kanban_review(self, card_id: str, body: dict, token: str) -> None:
        """operations 列审批（W3.3）：approve→approved；reject/changes-requested→rejected + 回退 implementation。"""
        if self._kanban_card_or_reply(card_id) is None:
            return
        decision = str(body.get("decision") or "")
        if decision not in ("approve", "reject", "changes-requested"):
            self._reply_json(400, {"error": "decision 必须是 approve/reject/changes-requested"})
            return
        comment = str(body.get("comment") or "")
        try:
            card = kanban.review_card(card_id, decision, comment)
        except kanban.KanbanError as e:
            self._reply_json(422, {"error": str(e)})
            return
        _audit("kanban.review", {"card_id": card_id, "decision": decision, "comment": comment},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card, "decision": decision})

    def _op_kanban_dispatch(self, card_id: str, body: dict, token: str) -> None:
        """派发执行：implementation/periodic 卡片 → task_runner.start_run，
        成功后原子 link_task + 自动流转到 implementation 列（dispatch_card 单锁）。"""
        card = self._kanban_card_or_reply(card_id)
        if card is None:
            return
        if card.get("type") not in ("implementation", "periodic"):
            self._reply_json(422, {"error": "仅 implementation/periodic 卡片可派发执行"})
            return
        repo = (card.get("repo") or "").strip()
        if not repo or not Path(repo).is_dir():
            self._reply_json(422, {"error": f"卡片 repo 为空或路径不存在: {repo or '<空>'}"})
            return
        try:
            parallel = int(body.get("parallel") or 1)
        except (TypeError, ValueError):
            self._reply_json(400, {"error": "parallel 必须为正整数"})
            return
        confirm_mode = str(body.get("confirm_mode") or "auto")
        if confirm_mode not in ("auto", "web"):
            self._reply_json(400, {"error": "confirm_mode 须为 auto/web"})
            return
        # W1.3 按列路由：automation=manual（架构/困难任务）→ 强制人工确认计划（web 模式），
        # 防止本地模型直接跑困难任务；auto/pending → 按请求 confirm_mode（默认 auto 本地队列）
        automation = card.get("automation", "pending")
        if automation == "manual" and confirm_mode == "auto":
            confirm_mode = "web"
            logger.info("[kanban] 卡片 %s automation=manual → 强制 confirm_mode=web（人工确认计划）", card_id)
        # 幂等防护：卡片已有运行中任务（EXECUTING/PLANNING 或托管句柄存活）→ 拒绝重复派发
        for tid in reversed(card.get("task_ids") or []):
            st = _task_status_of(tid)
            if st in _RUNNING_STATES or task_runner.is_running(tid):
                self._reply_json(409, {
                    "error": f"卡片已有运行中任务（{tid}，状态 {st}），不可重复派发。"
                             "可先取消或等待其结束。",
                    "task_id": tid,
                })
                return
        # 任务文本 = 标题 + 描述（截断防御，防超长 argv）
        task_text = card.get("title", "")
        if card.get("description"):
            task_text += "\n\n" + card["description"]
        task_text = task_text[:4000]
        # W2.1 异步派发：wait_for_id=False 立即返回（不阻塞等 task_id，本地模型 plan 生成
        # 可能 >30s 导致 HTTP 超时）；task_id 解析后经 on_task_id 回调 link_task + 自动流转。
        def _on_task_id(tid: str) -> None:
            try:
                # W3.1：design 列卡片（系统架构/困难任务）只 link 不流转——
                # 停留 design 列待计划确认（R5b web 确认）；确认后由 _op_confirm 流转。
                card_now = kanban.get_card(card_id)
                if card_now and card_now.get("stage") == "design":
                    kanban.link_task(card_id, tid)
                    logger.info("[kanban] 卡片 %s (design) 仅链接任务 %s，待计划确认后流转", card_id, tid)
                else:
                    kanban.dispatch_card(card_id, tid, note=f"派发任务 {tid}")
                    logger.info("[kanban] 卡片 %s 任务已派发: %s", card_id, tid)
            except Exception as e:
                logger.warning("[kanban] dispatch_card 回调失败: %s", e)
        # W2.2 状态回流：任务退出后读 meta.json，完成 → operations，失败 → blocked
        def _on_exit(tid: str, code: int) -> None:
            try:
                from .status import normalize_task_status
                import json as _json
                meta_path = _root().AGENT_GO_DIR / tid / "meta.json"
                if not meta_path.exists():
                    return
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                status = normalize_task_status(meta.get("status", ""), meta)
                new_stage = None
                if status in ("DELIVERY_READY", "ACCEPTED_DELIVERY"):
                    new_stage = "operations"
                # 失败（FAILED/BLOCKED/VERIFICATION_FAILED/CANCELLED）：停留 implementation
                # 列（看板无 blocked 列），不流转——卡片保留在执行列标失败态，通知带
                # 现场链接（worktree + inspect 命令），人工介入处理后手动流转。
                if new_stage:
                    kanban.move_card(card_id, new_stage, note=f"任务 {tid} 结束（{status}）")
                    logger.info("[kanban] 状态回流 卡片 %s → %s（task %s，status=%s）",
                                card_id, new_stage, tid, status)
                # W2.3 完成/失败通知（notify.py 多通道：desktop/webhook/command）
                try:
                    from .notify import notify_event
                    from .config import load_config as _load_cfg
                    _cfg = _load_cfg()
                    _evt = "on_complete" if status in ("DELIVERY_READY", "ACCEPTED_DELIVERY") else (
                        "on_blocked" if status in ("FAILED", "BLOCKED", "VERIFICATION_FAILED", "CANCELLED") else "")
                    if _evt:
                        _payload = {
                            "task_id": tid,
                            "task": meta.get("task", card.get("title", "")),
                            "status": status,
                            "result": new_stage,
                            "card_id": card_id,
                            "summary": (meta.get("summary") or "")[:200],
                            "failure_reason": (meta.get("failure_reason") or "")[:200],
                        }
                        # W3.2：blocked 时附现场链接（保留 worktree 路径 + inspect 提示）
                        if status in ("FAILED", "BLOCKED", "VERIFICATION_FAILED", "CANCELLED"):
                            _wts = [r.get("worktree", "") for r in meta.get("results", [])
                                    if r.get("status") in ("failed", "blocked") and r.get("worktree")]
                            if _wts:
                                _payload["worktrees"] = _wts
                            _payload["inspect_cmd"] = f"agent_go inspect {tid}"
                        notify_event(_evt, _payload, _cfg)
                except Exception as _ne:
                    logger.warning("[kanban] 通知发送失败（不影响回流）: %s", _ne)
            except Exception as e:
                logger.warning("[kanban] 状态回流失败: %s", e)
        task_runner.start_run(repo, task_text, parallel=parallel,
                              confirm_mode=confirm_mode, wait_for_id=False,
                              on_task_id=_on_task_id, on_exit=_on_exit)
        result = {"ok": True, "status": "starting",
                  "note": "任务后台派发中（task_id 解析后自动关联并流转到 implementation 列）"}
        _audit("kanban.dispatch", {"card_id": card_id, "repo": repo}, result, True, token)
        self._reply_json(200, result)

    def _op_kanban_suggest_degrade(self, card_id: str, token: str) -> None:
        """W4.3 自动降级建议：对失败卡片调用 insight 分析失败原因，生成降级/修复建议。"""
        import logging as _logging
        try:
            card = kanban.get_card(card_id)
        except kanban.KanbanError as e:
            self._reply_json(404, {"error": str(e)})
            return
        if card is None:
            self._reply_json(404, {"error": "卡片不存在"})
            return
        task_ids = card.get("task_ids") or []
        if not task_ids:
            self._reply_json(422, {"error": "卡片无关联任务，无法生成降级建议"})
            return
        task_id = task_ids[-1]
        # W4.3 任务级证据组装（构造 materialize_evidence 兼容结构）
        try:
            from .config import AGENT_GO_DIR, load_config
            from .eval import _insight_llm, _parse_insight_suggestions
            td = AGENT_GO_DIR / task_id
            meta = json.loads((td / "meta.json").read_text(encoding="utf-8")) if (td / "meta.json").exists() else {}
            results = meta.get("results", [])
            failed_records = [
                {"sub_id": r.get("subtask_id"), "status": r.get("status"),
                 "failure_reason": (r.get("failure_reason") or "")[:300],
                 "verify_ok": r.get("verify_ok"), "retries": r.get("retry_count")}
                for r in results if r.get("status") in ("failed", "blocked")
            ]
            failure_classes: dict = {}
            for r in failed_records:
                fc = (r.get("failure_reason") or "unknown").split(";")[0][:40]
                failure_classes[fc] = failure_classes.get(fc, 0) + 1
            cost = round(sum(float(r.get("cost_usd", 0) or 0) for r in results), 4)
            config = load_config()
            env_snapshot = {
                "plan_model": (config.get("plan_api") or {}).get("model", ""),
                "goal_policy": (config.get("goal") or {}).get("policy", "off"),
                "worker_models": config.get("worker_models", {}),
                "router_enabled": (config.get("router") or {}).get("enabled", False),
            }
            evidence = {
                "schema": "insight-evidence/1",
                "source_batch": task_id,
                "suite": "task",
                "manifest": {},
                "metrics": {
                    "task_count": len(results),
                    "valid_cost_usd": cost,
                    "pass_rate_diagnostic": round((len(results) - len(failed_records)) / len(results), 3) if results else 0,
                    "failure_class_counts": failure_classes,
                },
                "failure_modes": {
                    "by_failure_class": failure_classes,
                    "by_model": {},
                    "by_task": {r["sub_id"]: 1 for r in failed_records},
                    "failed_records": failed_records,
                },
                "per_task": {},
                "environment": env_snapshot,
                "problems_history": [],
                "record_count": len(results),
                "evidence_hash": "task-level",
            }
            goal = f"分析任务 {task_id}（{meta.get('task','')[:60]}）失败原因并给出降级/修复建议"
            content = _insight_llm(evidence, goal, "", config, _logging.getLogger(__name__))
            suggestions = _parse_insight_suggestions(content)
            if not suggestions:
                self._reply_json(422, {"error": "LLM 分析未产出有效建议"})
                return
            _audit("kanban.suggest_degrade", {"card_id": card_id, "task_id": task_id},
                   f"{len(suggestions)} 条建议", True, token)
            self._reply_json(200, {
                "task_id": task_id,
                "suggestions": suggestions[:3],
                "goal": goal,
            })
        except Exception as e:
            _logging.getLogger(__name__).exception("suggest_degrade failed")
            self._reply_json(500, {"error": f"降级建议生成失败: {e}"})
