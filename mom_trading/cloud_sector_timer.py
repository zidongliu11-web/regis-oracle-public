"""A clock-driven entry point for the existing, immutable sector collector.

The OS timer owns cadence and overlap prevention; research jobs own no clock.
No catch-up observations, strategy admissions, account reads or alert writes.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import traceback

from .cloud_research_worker import ROOT, publish, read_json
from .research_release_guard import source_lease

SCHEMA = "cloud_sector_timer.v1"
IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def run_once(*, root=ROOT):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "独立板块定时器不在开发电脑写入留档。"}
    with source_lease() as descriptors:
        if descriptors is None:
            result = {"status": "publication_wait", "summary": "等待版本安全切换；不补写错过的板块观察。"}
            publish(root / "reports/cloud_sector_timer_status.json", {
                "schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256,
                "checked_at": datetime.now(timezone.utc).isoformat(), "result": result,
            })
            return result
        return collect_once(root)


def collect_once(root):
    from .cloud_sector_observations import run
    from .cloud_sector_constituents import run as collect_constituents
    from .cloud_sector_close import run as collect_close
    from .cloud_sector_close_members import run as collect_close_members
    from .sector_mainline_origin import capture as capture_mainline_origin
    from .sector_proxy_forecast import capture as capture_proxy_forecast

    try:
        result = run(root=root)
    except Exception:
        traceback.print_exc()
        result = {"status": "failed", "summary": "独立板块采集失败；保留既有原件，下一次仅检查当时的观察桶。"}
    try:
        result["constituents"] = collect_constituents(root=root)
    except Exception:
        traceback.print_exc()
        result["constituents"] = {"status": "failed", "summary": "成分采集失败，不影响已留档板块；不能确认龙头。"}
    try:
        result["after_close"] = collect_close(root=root)
    except Exception:
        traceback.print_exc()
        result["after_close"] = {"status": "failed", "summary": "盘后采集失败，不沿用旧板块名单。"}
    try:
        result["after_close_members"] = collect_close_members(root=root)
    except Exception:
        traceback.print_exc()
        result["after_close_members"] = {"status": "failed", "summary": "盘后成分未完成核验；板块原件保留，不推算核心股。"}
    try:
        result["mainline_origin"] = capture_mainline_origin(root=root)
    except Exception:
        traceback.print_exc()
        result["mainline_origin"] = {"status": "failed", "summary": "盘后研究起点首写检查失败；不补写旧日起点。"}
    try:
        result["proxy_forecast"] = capture_proxy_forecast(root=root)
    except Exception:
        traceback.print_exc()
        result["proxy_forecast"] = {"status": "failed", "summary": "盘后代理预测首写失败；不沿用历史概率。"}
    publish(root / "reports/cloud_sector_timer_status.json", {
        "schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256,
        "checked_at": datetime.now(timezone.utc).isoformat(), "result": result,
    })
    return result


def current_status(path, *, now):
    packet = read_json(path)
    try:
        age = (now - datetime.fromisoformat(packet["checked_at"])).total_seconds()
        if (packet.get("schema") != SCHEMA or packet.get("implementation_sha256") != IMPLEMENTATION_SHA256
                or not 0 <= age <= 210 or not isinstance(packet.get("result"), dict)):
            raise ValueError("timer_receipt_not_current")
        return {**packet["result"], "checked_at": packet["checked_at"],
                "schedule": "独立定时采集，不等待耗时研究任务"}
    except (KeyError, TypeError, ValueError):
        return {"status": "unverified", "summary": "独立板块定时器尚无当前执行回执；不能判断市场是否存在主线。"}


if __name__ == "__main__":
    raise SystemExit(1 if run_once().get("status") == "failed" else 0)
