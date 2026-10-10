"""Process-owned research projection; HTTP reads never perform its computation.

No persisted account cache and no order/alert writes. Input revisions and age
are checked again by readers, including while the one producer is busy.
"""
from copy import deepcopy
from datetime import datetime, timezone
import logging
import threading
import time


class ResearchRouteSnapshot:
    MAX_AGE = 60
    REFRESH_AFTER = 30

    def __init__(self, build, identity):
        self.build, self.identity = build, identity
        self.lock = threading.Lock()
        self.producer = threading.Lock()
        self.payload = None
        self.revision = None
        self.started = 0.0
        self.published_at = None
        self.next_try = 0.0
        self.running = False
        self.stopped = False
        self.stop_event = None
        self.reason = "pending"

    def refresh(self):
        if not self.producer.acquire(blocking=False):
            return
        try:
            revision = self.identity()
            if revision is None:
                raise ValueError("input_revision_unavailable")
            now = time.monotonic()
            with self.lock:
                if self.stopped or (self.stop_event and self.stop_event.is_set()) or now < self.next_try:
                    return
                if self.payload is not None and revision == self.revision and 0 <= now-self.started < self.REFRESH_AFTER:
                    return
                self.running = True
            payload = self.build()
            current = self.identity()
            with self.lock:
                self.payload = None
                if self.stopped or (self.stop_event and self.stop_event.is_set()):
                    return
                if current != revision:
                    self.reason = "inputs_changed"
                elif time.monotonic()-now >= self.MAX_AGE:
                    self.reason = "calculation_expired"
                elif not isinstance(payload, dict):
                    self.reason = "failed"
                else:
                    self.payload = deepcopy(payload)
                    self.revision, self.started = current, now
                    self.published_at = datetime.now(timezone.utc).isoformat()
                    self.reason = "current"
                self.next_try = time.monotonic()+5
        except Exception as exc:
            # Do not expose account data or source exception text via an API.
            logging.getLogger(__name__).warning("research route snapshot failed: %s", type(exc).__name__)
            with self.lock:
                self.payload = None
                self.reason = "failed"
                self.next_try = time.monotonic()+15
        finally:
            with self.lock:
                self.running = False
            self.producer.release()

    def run(self, stop_event):
        self.stop_event = stop_event
        try:
            while not stop_event.is_set():
                self.refresh()
                if stop_event.wait(5):
                    break
        finally:
            with self.lock:
                self.stopped = True
                self.payload = None

    def read(self):
        try:
            revision = self.identity()
        except Exception:
            revision = None
        with self.lock:
            age = time.monotonic()-self.started
            stopped = self.stopped or bool(self.stop_event and self.stop_event.is_set())
            current = (not stopped and revision is not None and self.payload is not None
                       and revision == self.revision and 0 <= age < self.MAX_AGE)
            reason = ("current" if current else "stopped" if stopped else "inputs_unavailable" if revision is None
                      else "inputs_changed" if self.payload is not None and revision != self.revision
                      else "stale" if self.payload is not None else self.reason)
            messages = {
                "current": "已读取当前研究快照；计算完成不等于策略准入或可执行买单。",
                "pending": "联合研究后台尚未提供当前快照；不是市场没有机会，暂不显示计划与股数。",
                "inputs_changed": "账户、行情或研究原件已变化，等待后台重新核验；旧计划与股数已清除。",
                "stale": "联合研究快照已过期，等待后台刷新；旧计划与股数已清除。",
                "calculation_expired": "联合研究计算超过时效预算；不把较早输入当成当前计划。",
                "failed": "联合研究后台计算失败，稍后自动重试；不能据此判断没有机会。",
                "inputs_unavailable": "当前输入版本不可核验；旧计划与股数已清除。",
                "stopped": "联合研究后台任务已停止，当前快照不可用。",
            }
            metadata = {"schema": "research_route_snapshot.v1", "state": reason,
                        "current": current, "running": self.running, "summary": messages[reason],
                        "published_at": self.published_at, "age_seconds": round(age, 3) if self.published_at else None,
                        "maximum_age_seconds": self.MAX_AGE}
            result = deepcopy(self.payload) if current else {
                "status": "research_snapshot_pending", "market_state": "unknown",
                "next_action": messages[reason], "missing_inputs": ["current_research_snapshot"],
                "authority": {key: False for key in ("changes_trade_count", "changes_position_or_quantity",
                    "generates_buy_or_sell_alert", "production_strategy", "automatic_trading")},
            }
            result["research_snapshot"] = metadata
            return result
