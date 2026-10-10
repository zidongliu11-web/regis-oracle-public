"""Manual-only bridge to the official TauricResearch/TradingAgents package.

The upstream dependency stays in its own virtual environment.  This keeps its
large LLM/data dependency graph out of the web process and means it is started
only after a user explicitly asks for a second opinion on one stock.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .deepseek import EXTERNAL_PAID_API_DISABLED


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_PATH = Path(__file__).with_name("tradingagents_worker.py")


class TradingAgentsError(RuntimeError):
    pass


def a_share_ticker(code: str) -> str:
    normalized = code.strip().split(".")[0]
    if len(normalized) != 6 or not normalized.isdigit():
        raise TradingAgentsError("TradingAgents只接受有效的6位A股代码")
    return f"{normalized}.SS" if normalized.startswith("6") else f"{normalized}.SZ"


def _isolated_python() -> Path:
    configured = os.getenv("TRADINGAGENTS_PYTHON", "").strip()
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / "third_party" / "runtime" / "tradingagents-venv" / "bin" / "python"


def _installed() -> bool:
    """Check deployment artifacts without making a slow request-path subprocess.

    Importing TradingAgents pulls a large LangChain graph; doing that on every
    dashboard refresh made the status card vulnerable to a false "not
    installed" result under load.  The installer performs the real import
    verification, while every manual run still starts the official interpreter
    and returns a contained error if that environment is damaged later.
    """
    python = _isolated_python()
    return python.is_file() and WORKER_PATH.is_file()


def tradingagents_status() -> dict[str, Any]:
    installed = _installed()
    return {
        "name": "TradingAgents",
        "repository": "https://github.com/TauricResearch/TradingAgents",
        "version": "0.3.1",
        "license": "Apache-2.0",
        "installed": installed,
        "configured": False,
        "mode": "disabled_local_only",
        "external_paid_api_disabled": EXTERNAL_PAID_API_DISABLED,
        "network_requests_allowed": False,
        "runtime": "isolated_python_environment",
        "a_share_mapping": "6xxxxx -> .SS；0/3xxxxx -> .SZ",
        "execution_allowed": False,
        "message": "外部付费 API 已禁用；TradingAgents 不会启动。",
        "limitations": [
            "上游项目使用LLM和外部数据，输出每次可能不同",
            "A股代码可映射到Yahoo Finance，但不能替代本系统的A股逐日数据审计",
            "输出只作为第二意见，不能解除本地历史研究门禁",
        ],
    }


def _review_request(code: str, as_of_date: str | None) -> dict[str, str]:
    return {
        "code": code,
        "ticker": a_share_ticker(code),
        "as_of_date": as_of_date or datetime.now().date().isoformat(),
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "backend_url": os.getenv("DEEPSEEK_BASE_URL", "").strip(),
        "runtime_dir": str(PROJECT_ROOT / "reports" / "tradingagents"),
    }


def _run_worker(payload: dict[str, str]) -> dict[str, Any]:
    python = _isolated_python()
    try:
        result = subprocess.run(
            [str(python), str(WORKER_PATH)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TradingAgentsError("官方多智能体研究超过10分钟仍未完成，已停止本次人工请求") from exc
    except OSError as exc:
        raise TradingAgentsError(f"无法启动官方TradingAgents隔离环境：{exc}") from exc
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout or "无可读错误输出").strip()[-500:]
        raise TradingAgentsError(f"官方TradingAgents返回格式异常：{detail}") from exc
    if result.returncode != 0 or not response.get("ok"):
        raise TradingAgentsError(response.get("error") or "官方TradingAgents运行失败")
    return response


def run_tradingagents_review(code: str, *, as_of_date: str | None = None) -> dict[str, Any]:
    """Run one opt-in upstream review; it never produces an executable order."""
    if EXTERNAL_PAID_API_DISABLED:
        raise TradingAgentsError("外部付费 API 已禁用；TradingAgents 不会启动")
    status = tradingagents_status()
    if not status["installed"]:
        raise TradingAgentsError("TradingAgents官方包尚未安装或安装校验失败")
    if not status["configured"]:
        raise TradingAgentsError("TradingAgents需要先在网站安全连接页验证DeepSeek密钥")
    response = _run_worker(_review_request(code, as_of_date))
    return {
        "code": code,
        "ticker": response["ticker"],
        "as_of_date": response["as_of_date"],
        "decision": str(response["decision"])[:20_000],
        "execution_allowed": False,
        "research_gate_changed": False,
        "safety": (
            "这是官方多智能体研究文本，不是回测结论、买卖指令或盈利承诺；"
            "本地门禁、人工确认和A股交易规则继续生效。"
        ),
    }
