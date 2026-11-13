"""调度计划业务规则。

状态流转、计划/任务联动与最终状态口径统一收敛在 ``app.lifecycle``，
本服务只负责列表筛选、登记入参与视图组装，避免与采集任务各写一遍状态机。
"""
from __future__ import annotations

from typing import Any

from app import lifecycle as lc
from app.store import store

MODULE = lc.PLAN_MODULE
REQUIRED_FIELDS = ["计划编号", "关联任务", "cron表达式"]
# 兼容旧引用：状态序列与动作映射以 lifecycle 为唯一事实来源。
STATUS_ORDER = lc.PLAN_STATUSES
ACTION_RULES = lc.PLAN_ACTIONS
NEGATIVE_ACTIONS: list[str] = []

_PENDING_STATUSES = frozenset({lc.PLAN_DRAFT, lc.PLAN_REVIEWING})


class SchedulerService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [lc.plan_view(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            # 过滤口径同样以最终状态为准，保证清单与详情读到同一份结论。
            rows = [row for row in rows if row.get("effective_status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return lc.plan_view(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 登记时顺带保存计划侧其余业务字段（cron、依赖、超时、优先级、生效/失效日期等）。
        for field in ("生效日期", "失效日期", "优先级", "超时分钟", "依赖计划"):
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = lc.PLAN_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["status_log"] = ["登记调度计划：草稿"]
        rows.append(entry)
        return lc.plan_view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        try:
            return lc.run_plan_action(entry_id, action)
        except lc.LifecycleError as exc:
            # 规则拦截属于业务可读错误，保持与旧接口一致的 (None, message) 返回形态。
            return None, str(exc)
