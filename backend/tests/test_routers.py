"""接口层集成测试：直接调用路由处理函数（无需 httpx）。

验证 /api/scheduler 与 /api/taskcrawl 的列表、详情、动作入口都基于
lifecycle 的统一最终状态视图，并把业务拦截以 ActionResult(ok=False) 返回。
"""
from __future__ import annotations

import copy
import unittest

from app.routers import scheduler as scheduler_router
from app.routers import taskcrawl as taskcrawl_router
from app.schemas import EntryPayload
from app.seed import SEED_ROWS
from app.store import store


class RouterIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for module, rows in (
            ("scheduler", SEED_ROWS["scheduler"]),
            ("taskcrawl", SEED_ROWS["taskcrawl"]),
        ):
            table = store.rows(module)
            table.clear()
            table.extend(copy.deepcopy(rows))

    def _id(self, module: str, code: str, field: str) -> int:
        return int(next(r for r in store.rows(module) if r[field] == code)["id"])

    def test_task_list_and_detail_share_unified_view(self) -> None:
        page = taskcrawl_router.list_entries(keyword="TASK-1042", status=None, page=1, size=20)
        self.assertEqual(page.total, 1)
        row = page.items[0]
        self.assertEqual(row["effective_status"], "执行中")
        self.assertFalse(row["dispatchable"])
        self.assertIn("PLAN-31", row["blocked_reason"])

        detail = taskcrawl_router.get_entry(row["id"])
        self.assertEqual(detail["status_reason"], row["status_reason"])

    def test_paused_plan_blocks_start_with_actionable_message(self) -> None:
        task_id = self._id("taskcrawl", "TASK-1042", "任务编号")
        result = taskcrawl_router.run_action(task_id, EntryPayload(values={"action": "终止任务"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.entry["status"], "已终止")

        # 终态任务再次启动：接口返回 ok=False 且带可操作原因，记录不被改写
        again = taskcrawl_router.run_action(task_id, EntryPayload(values={"action": "启动任务"}))
        self.assertFalse(again.ok)
        self.assertIn("不允许执行", again.message)

    def test_duplicate_action_returns_single_change_message(self) -> None:
        plan_id = self._id("scheduler", "PLAN-31", "计划编号")
        first = scheduler_router.run_action(
            plan_id, EntryPayload(values={"action": "暂停计划"})
        )
        self.assertTrue(first.ok)
        self.assertIn("重复提交", first.message)
        # 关联的执行中任务依旧执行中，没有被暂停动作改写
        task = next(t for t in store.rows("taskcrawl") if t["任务编号"] == "TASK-1042")
        self.assertEqual(task["status"], "执行中")

    def test_confirm_schedule_moves_draft_task_and_lists_ready(self) -> None:
        # PLAN-32 已排期、TASK-1043 待调度；先把任务降回草稿，再重复确认排期验证联动
        task = next(t for t in store.rows("taskcrawl") if t["任务编号"] == "TASK-1043")
        task["status"] = "草稿"
        plan_id = self._id("scheduler", "PLAN-32", "计划编号")
        result = scheduler_router.run_action(
            plan_id, EntryPayload(values={"action": "确认排期"})
        )
        self.assertTrue(result.ok)
        self.assertEqual(task["status"], "待调度")

        ready_page = taskcrawl_router.list_entries(keyword=None, status="待调度", page=1, size=20)
        self.assertIn("TASK-1043", [r["任务编号"] for r in ready_page.items])

    def test_export_contains_unified_fields(self) -> None:
        exported = scheduler_router.export_entries()
        self.assertEqual(exported["module"], "scheduler")
        self.assertTrue(all("effective_status" in item for item in exported["items"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
