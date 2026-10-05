"""本地模型生命周期管理——P0 只读管理面。

设计：docs/design/local-model-management-design.md §3/§4（P0＝`model status/list/current/diagnose`）。

边界（P0，硬性）：
- **只读**：只做 HTTP GET（/api/status、/metrics、/v1/models）与文件读取（configs/*.conf、
  pidfile）；不启停任何进程、不执行 manage.sh、不写任何文件——本模块不 import subprocess。
- **默认关**：``local_model_manager.enabled=false`` 时命令明确报错并给出开启指引，run 流程零影响。
- **fail-open**：任一探测失败只降级为对应字段/诊断级别，不抛异常（与 diag.fetch_json 同语义）。
- P1（start/stop/switch）、P2（repair＋pipeline 集成）、P3（web 监控）不在本模块的 P0 范围。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import diag

# 诊断级别（设计 §3.4）：healthy / starting / backend_down / proxy_down / model_drift / down
LEVEL_HEALTHY = "healthy"
LEVEL_STARTING = "starting"
LEVEL_BACKEND_DOWN = "backend_down"
LEVEL_PROXY_DOWN = "proxy_down"
LEVEL_MODEL_DRIFT = "model_drift"
LEVEL_DOWN = "down"

LEVELS = (LEVEL_HEALTHY, LEVEL_STARTING, LEVEL_BACKEND_DOWN,
          LEVEL_PROXY_DOWN, LEVEL_MODEL_DRIFT, LEVEL_DOWN)

PROBE_TIMEOUT = 2.0

# conf 变量提取（shell 风格 KEY="value" / KEY=value；只读正则，不 eval）
_CONF_ASSIGN_RE = re.compile(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$', re.M)


def _strip_trailing_comment(value: str) -> str:
    """剥离未加引号的行尾注释（``KEY=value  # 注释``）；引号内的 # 保留。"""
    quote = ""
    for idx, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (idx == 0 or value[idx - 1].isspace()):
            return value[:idx].rstrip()
    return value


def _conf_value(body: str, *names: str) -> str:
    """取 conf 中首个命中的变量值（去引号、剥行尾注释；未命中返回 ""）。"""
    values: Dict[str, str] = {}
    for key, raw in _CONF_ASSIGN_RE.findall(body):
        value = _strip_trailing_comment(raw.strip()).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values.setdefault(key, value)
    for name in names:
        if values.get(name):
            return values[name]
    return ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:  # 进程存在但当前用户无信号权限
        return True
    except OSError:
        return False
    return True


class LocalModelManager:
    """llama-defender（本机代理＋本地后端）的只读管理面。

    Args:
        config: 完整 agent_go 配置（读 ``local_model_manager`` 段）
        console: 可选输出对象（仅 CLI 层用；本模块不直接打印）
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, *, console: Any = None,
                 proxy_url: Optional[str] = None, manage_script: Optional[Path] = None) -> None:
        cfg = ((config or {}).get("local_model_manager") or {})
        self.console = console
        self.enabled = bool(cfg.get("enabled"))
        script = manage_script if manage_script is not None else cfg.get("manage_script") or ""
        self.manage_script = Path(str(script)).expanduser() if script else Path("")
        url = proxy_url if proxy_url is not None else cfg.get("proxy_url") or ""
        self.proxy_url = str(url).rstrip("/")
        self.wait_ready_timeout = int(cfg.get("wait_ready_timeout") or 120)

    # ── 路径 ────────────────────────────────────────────────────────────
    @property
    def defender_dir(self) -> Path:
        return self.manage_script.parent if str(self.manage_script) else Path("")

    @property
    def profiles_dir(self) -> Path:
        return self.defender_dir / "configs"

    @property
    def pidfile(self) -> Path:
        return self.defender_dir / "anthropic_proxy.pid"

    def availability(self) -> Tuple[bool, str]:
        """P0 可用性（fail-open 前置）：未启用/未配置时给出明确原因。"""
        if not self.enabled:
            return False, ("本地模型管理未启用：config.local_model_manager.enabled=true 后可用"
                           "（设计：docs/design/local-model-management-design.md）")
        if not self.proxy_url:
            return False, "local_model_manager.proxy_url 未配置（如 http://127.0.0.1:4000）"
        if not str(self.manage_script) or self.manage_script.name in ("", "."):
            return False, "local_model_manager.manage_script 未配置（llama-defender manage.sh 路径）"
        if not self.manage_script.is_file():
            return False, f"manage.sh 不存在：{self.manage_script}"
        return True, ""

    # ── HTTP 探测（只读；测试可逐方法替换）─────────────────────────────
    def _api_status(self) -> Optional[Dict[str, Any]]:
        data = diag.fetch_json(self.proxy_url, "/api/status", PROBE_TIMEOUT)
        return data if isinstance(data, dict) else None

    def _metrics(self) -> Optional[Dict[str, Any]]:
        data = diag.fetch_json(self.proxy_url, "/metrics", PROBE_TIMEOUT)
        return data if isinstance(data, dict) else None

    def _models_ok(self) -> bool:
        data = diag.fetch_json(self.proxy_url, "/v1/models", PROBE_TIMEOUT)
        return isinstance(data, dict) and isinstance(data.get("data"), list)

    # ── 进程/文件面（只读）─────────────────────────────────────────────
    def proxy_pid(self) -> int:
        try:
            return int(self.pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def proxy_process_alive(self) -> bool:
        return _pid_alive(self.proxy_pid())

    def current_profile(self) -> str:
        """文件视角的激活档：configs/active.conf 软链目标 stem。

        坏链（目标不存在）同样返回目标 stem——"配置指向"是事实，存在性由
        ``list_profiles``/``diagnose`` 另行呈现；active.conf 缺失或非软链 → ""。
        """
        link = self.profiles_dir / "active.conf"
        try:
            if not link.is_symlink():
                return link.stem if link.is_file() else ""
            target = os.readlink(str(link))
        except OSError:
            return ""
        return Path(target).stem

    def list_profiles(self) -> List[Dict[str, Any]]:
        """configs/*.conf 档位清单（CONFIG_NAME/CONFIG_DESC/CONFIG_MEMORY＋模型/端口）。"""
        active = self.current_profile()
        rows: List[Dict[str, Any]] = []
        if not self.profiles_dir.is_dir():
            return rows
        for path in sorted(self.profiles_dir.glob("*.conf")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rows.append({
                "profile": path.stem,
                "name": _conf_value(body, "CONFIG_NAME") or path.stem,
                "desc": _conf_value(body, "CONFIG_DESC"),
                "memory": _conf_value(body, "CONFIG_MEMORY", "MEMORY"),
                "model": _conf_value(body, "MODEL_NAME", "LLAMA_MODEL"),
                "port": _conf_value(body, "LLAMA_PORT"),
                "backend": _conf_value(body, "LLAMA_BACKEND"),
                "active": path.stem == active,
            })
        return rows

    # ── 聚合面 ──────────────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        """只读状态汇总（不抛错；不可达字段降级为 None/False/""）。"""
        api = self._api_status() or {}
        backend = api.get("backend") if isinstance(api.get("backend"), dict) else {}
        proxy_info = api.get("proxy") if isinstance(api.get("proxy"), dict) else {}
        metrics = self._metrics() or {}
        loaded = str((backend or {}).get("model_name") or "")
        if not loaded:
            loaded = self._loaded_model_fallback()
        engines = api.get("local_engines") if isinstance(api.get("local_engines"), list) else []
        return {
            "proxy_url": self.proxy_url,
            "manage_script": str(self.manage_script),
            "proxy_reachable": bool(api) or self._models_ok(),
            "proxy_alive": bool(proxy_info.get("alive")),
            "proxy_pid": proxy_info.get("pid") or self.proxy_pid(),
            "proxy_uptime_sec": proxy_info.get("uptime_sec"),
            "api_state": str(api.get("state") or ""),
            "api_version": str(api.get("api_version") or ""),
            "ready": api.get("ready"),
            "backend_alive": bool((backend or {}).get("alive")),
            "backend_pid": (backend or {}).get("pid"),
            "backend_type": str((backend or {}).get("backend_type") or ""),
            "backend_uptime_sec": (backend or {}).get("uptime_sec"),
            "loaded_model": loaded,
            "active_profile": str(api.get("active_profile") or ""),
            "configured_profile": self.current_profile(),
            "engines": engines,
            "metrics": {
                "total": metrics.get("total"),
                "status_codes": metrics.get("status"),
                "ttft": metrics.get("ttft"),
                "quality_flags": metrics.get("quality_flags"),
                "schema": metrics.get("schema"),
            } if metrics else None,
        }

    def _loaded_model_fallback(self) -> str:
        """/api/status 无模型名时回退 executor 探测（HTML /status 兼容路径，带 TTL 缓存）。"""
        try:
            from .executor import _probe_local_model
            return _probe_local_model(self.proxy_url, PROBE_TIMEOUT) or ""
        except Exception:  # fail-open：探测链任何异常不得影响诊断输出
            return ""

    def diagnose(self) -> Dict[str, Any]:
        """分级诊断（设计 §3.4）＋建议命令；level ∈ ``LEVELS``。"""
        api = self._api_status()
        proxy_reachable = api is not None or self._models_ok()
        reasons: List[str] = []
        advice: List[str] = []
        script = str(self.manage_script)

        if not proxy_reachable:
            if self.proxy_process_alive():
                level = LEVEL_STARTING
                reasons.append(f"代理进程存活（pid={self.proxy_pid()}）但 {self.proxy_url} 接口不可达"
                               f"（启动/重载中；就绪上限 {self.wait_ready_timeout}s）")
                advice.append("等待就绪：轮询 GET /v1/models")
            elif self.manage_script.is_file():
                level = LEVEL_PROXY_DOWN
                reasons.append(f"代理不可达且无存活进程（pidfile：{self.pidfile}）")
                advice.append(f"{script} start")
            else:
                level = LEVEL_DOWN
                reasons.append("代理不可达、进程不存在、manage.sh 缺失——llama-defender 未部署或路径未配置")
                advice.append("人工介入：核对 local_model_manager.manage_script 与 llama-defender 部署")
            return {"level": level, "reasons": reasons, "advice": advice,
                    "proxy_url": self.proxy_url, "manage_script": script}

        backend = api.get("backend") if isinstance(api, dict) and isinstance(api.get("backend"), dict) else {}
        if api is None:  # /api/status 缺失但 /v1/models 可达：旧代理契约，只判"可达"
            level = LEVEL_HEALTHY if self._loaded_model_fallback() else LEVEL_BACKEND_DOWN
            reasons.append("代理 /api/status（契约 api_version≥2）不可用：回退 /v1/models 判定（旧代理兼容）")
        elif not (backend or {}).get("alive"):
            level = LEVEL_BACKEND_DOWN
            reasons.append(f"代理存活（pid={self.proxy_pid() or (api.get('proxy') or {}).get('pid')}）"
                           f"但本地后端未就绪（api_state={api.get('state')!r}）")
            advice.append(f"{script} start-backend（幂等）")
        elif not str((backend or {}).get("model_name") or ""):
            level = LEVEL_STARTING
            reasons.append("后端进程存活但未加载出模型名（加载中）")
            advice.append("等待就绪：轮询 GET /v1/models")
        else:
            configured = self.current_profile()
            running = str(api.get("active_profile") or "")
            if configured and running and configured != running:
                level = LEVEL_MODEL_DRIFT
                reasons.append(f"配置档与运行档不一致：configs/active.conf→{configured}，"
                               f"代理 active_profile={running}")
                advice.append(f"{script} reload（SIGHUP 热重载，~0.5s，在途请求不受影响）")
                advice.append("核对 configs/active.conf 指向（人工确认后再切换）")
            else:
                level = LEVEL_HEALTHY
                reasons.append(f"代理与后端均就绪；运行档={running or configured or '未知'}；"
                               f"加载模型={backend.get('model_name')}")

        return {"level": level, "reasons": reasons, "advice": advice,
                "proxy_url": self.proxy_url, "manage_script": script}


def manager_from_config(config: Optional[Dict[str, Any]] = None, *, console: Any = None) -> LocalModelManager:
    """从配置构造 manager（CLI 入口用；不做任何探测）。"""
    return LocalModelManager(config, console=console)


def format_status_text(status: Dict[str, Any]) -> List[str]:
    """人读状态行（CLI 层）；不含任何写操作。"""
    metrics = status.get("metrics") or {}
    ttft = (metrics.get("ttft") or {}) if isinstance(metrics.get("ttft"), dict) else {}
    codes = metrics.get("status_codes") if isinstance(metrics.get("status_codes"), dict) else {}
    lines = [
        f"代理     : {status.get('proxy_url')}  "
        f"{'可达' if status.get('proxy_reachable') else '不可达'}"
        f"（alive={status.get('proxy_alive')} pid={status.get('proxy_pid')} "
        f"uptime={_fmt_duration(status.get('proxy_uptime_sec'))}）",
        f"后端     : {'就绪' if status.get('backend_alive') else '未就绪'}"
        f"（pid={status.get('backend_pid')} type={status.get('backend_type') or '?'} "
        f"uptime={_fmt_duration(status.get('backend_uptime_sec'))}）",
        f"模型     : {status.get('loaded_model') or '（未探测到）'}",
        f"档位     : 运行={status.get('active_profile') or '?'}／"
        f"配置={status.get('configured_profile') or '?'}"
        f"{'（不一致）' if _drifted(status) else ''}",
        f"api_state: {status.get('api_state') or '?'}（api_version={status.get('api_version') or '?'}"
        f" ready={status.get('ready')}）",
    ]
    if metrics:
        lines.append(f"metrics  : total={metrics.get('total')} status={codes or {}} "
                     f"ttft p50={ttft.get('p50_ms')}ms p95={ttft.get('p95_ms')}ms")
    engines = status.get("engines") or []
    if engines:
        detail = "；".join(f"{e.get('provider')}({'✔' if e.get('alive') else '✘'}"
                           f" {e.get('model_name') or '-'})" for e in engines if isinstance(e, dict))
        lines.append(f"engines  : {detail}")
    return lines


def _drifted(status: Dict[str, Any]) -> bool:
    configured, running = status.get("configured_profile") or "", status.get("active_profile") or ""
    return bool(configured and running and configured != running)


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if total < 0:
        return "?"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
