"""采集任务业务规则。

任务只维护自己的执行状态与运行类业务字段（调度周期、增量字段、起止水位等）；
“能否进入待调度 / 能否启动”由关联调度计划的审批结论决定，规则统一在
``app.lifecycle``，与调度计划侧共用同一份生命周期算法。
"""
from __future__ import annotations

from typing import Any

from app import lifecycle as lc
from app.store import store

MODULE = lc.TASK_MODULE
REQUIRED_FIELDS = ["任务编号", "采集源名称", "规则编号"]
# 兼容旧引用：状态序列与动作映射以 lifecycle 为唯一事实来源。
STATUS_ORDER = lc.TASK_STATUSES
ACTION_RULES = lc.TASK_ACTIONS
NEGATIVE_ACTIONS: list[str] = []


class TaskcrawlService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [lc.task_view(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            # 清单过滤与详情读取同一份最终状态，冲突时审批结论写在 blocked_reason。
            rows = [row for row in rows if row.get("effective_status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return lc.task_view(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 任务侧运行类业务字段，与计划侧的 cron/依赖/超时等字段各存各的。
        for field in ("调度周期", "增量字段", "批次大小", "起始水位", "截止水位"):
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = lc.TASK_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["status_log"] = ["登记采集任务：草稿"]
        rows.append(entry)
        return lc.task_view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        try:
            return lc.run_task_action(entry_id, action)
        except lc.LifecycleError as exc:
            return None, str(exc)
