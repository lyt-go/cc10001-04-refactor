"""调度计划 / 采集任务生命周期重构的回归测试。

只依赖标准库 unittest，直接在内存仓库上驱动真实 service / lifecycle，
覆盖：事务一致性、重复提交幂等、暂停联动、终态锁定、审批门禁、
统一最终状态视图以及计划/任务两侧字段职责划分。
"""
from __future__ import annotations

import copy
import unittest

from app import lifecycle as lc
from app.services.scheduler import SchedulerService
from app.services.taskcrawl import TaskcrawlService
from app.store import store


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从一份干净的种子数据开始，避免用例间互相污染。
        from app.seed import SEED_ROWS

        for module, rows in (
            (lc.PLAN_MODULE, SEED_ROWS[lc.PLAN_MODULE]),
            (lc.TASK_MODULE, SEED_ROWS[lc.TASK_MODULE]),
        ):
            table = store.rows(module)
            table.clear()
            table.extend(copy.deepcopy(rows))

        self.plans = SchedulerService()
        self.tasks = TaskcrawlService()

    # -- 辅助 --------------------------------------------------------------
    def _plan(self, code: str) -> dict:
        return next(p for p in store.rows(lc.PLAN_MODULE) if p["计划编号"] == code)

    def _task(self, code: str) -> dict:
        return next(t for t in store.rows(lc.TASK_MODULE) if t["任务编号"] == code)

    def _plan_id(self, code: str) -> int:
        return int(self._plan(code)["id"])

    def _task_id(self, code: str) -> int:
        return int(self._task(code)["id"])

    # -- 1. 审批状态决定任务能否进入待调度 ---------------------------------
    def test_plan_approval_gates_task_dispatch(self) -> None:
        # 新建草稿计划 + 草稿任务并关联
        plan, _ = self.plans.create_entry({
            "计划编号": "PLAN-T1", "关联任务": "TASK-T1", "cron表达式": "0 * * * * ?",
        })
        task, _ = self.tasks.create_entry({
            "任务编号": "TASK-T1", "采集源名称": "src", "规则编号": "PARS-1",
        })
        # 计划仍在草稿：任务提交调度被审批结论拦下
        entry, message = self.tasks.run_action(int(task["id"]), "提交调度")
        self.assertIsNone(entry)
        self.assertIn("确认排期", message)
        self.assertEqual(self._task("TASK-T1")["status"], lc.TASK_DRAFT)

        # 计划提交审批后仍不能调度
        self.plans.run_action(int(plan["id"]), "提交审批")
        entry, _ = self.tasks.run_action(int(task["id"]), "提交调度")
        self.assertIsNone(entry)
        self.assertEqual(self._task("TASK-T1")["status"], lc.TASK_DRAFT)

        # 确认排期后任务才允许进入待调度
        self.plans.run_action(int(plan["id"]), "确认排期")
        entry, message = self.tasks.run_action(int(task["id"]), "提交调度")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], lc.TASK_READY)

    def test_confirm_schedule_pulls_draft_tasks_to_ready(self) -> None:
        plan, _ = self.plans.create_entry({
            "计划编号": "PLAN-T2", "关联任务": "TASK-T2", "cron表达式": "0 * * * * ?",
        })
        self.tasks.create_entry({
            "任务编号": "TASK-T2", "采集源名称": "src", "规则编号": "PARS-2",
        })
        self.plans.run_action(int(plan["id"]), "提交审批")
        _, message = self.plans.run_action(int(plan["id"]), "确认排期")
        self.assertIn("确认排期", message)
        # 两侧共享同一份“进入待调度”结论
        self.assertEqual(self._task("TASK-T2")["status"], lc.TASK_READY)

    # -- 2. 暂停计划：未启动任务回待调度，执行中/终态不动 -------------------
    def test_pause_keeps_unstarted_ready_and_preserves_running(self) -> None:
        # PLAN-31 已暂停、TASK-1042 执行中
        self.assertEqual(self._plan("PLAN-31")["status"], lc.PLAN_PAUSED)
        self.assertEqual(self._task("TASK-1042")["status"], lc.TASK_RUNNING)

        # 再暂停一次（幂等），执行中任务状态保持
        _, message = self.plans.run_action(self._plan_id("PLAN-31"), "暂停计划")
        self.assertIn("重复提交", message)
        self.assertEqual(self._task("TASK-1042")["status"], lc.TASK_RUNNING)

        # 把一个草稿任务挂到已暂停计划上，暂停动作不应把它推进到待调度（入口已关）
        self._task("TASK-1043")["status"] = lc.TASK_DRAFT
        self._plan("PLAN-32")["关联任务"] = "TASK-1043"  # PLAN-32 已排期，先制造一个待调度任务
        plan31 = self._plan("PLAN-31")
        plan31["关联任务"] = "TASK-1042,TASK-1043"
        self.plans.run_action(self._plan_id("PLAN-31"), "确认排期")  # 恢复排期
        self.assertEqual(self._task("TASK-1043")["status"], lc.TASK_READY)
        # 再次暂停：待调度任务保留在待调度但不被拾取，执行中任务不变
        self.plans.run_action(self._plan_id("PLAN-31"), "暂停计划")
        self.assertEqual(self._task("TASK-1043")["status"], lc.TASK_READY)
        self.assertEqual(self._task("TASK-1042")["status"], lc.TASK_RUNNING)

    def test_terminated_task_terminal_state_never_rewritten(self) -> None:
        # TASK-1044 已终止，任何任务动作都被拒绝
        entry, message = self.tasks.run_action(self._task_id("TASK-1044"), "启动任务")
        self.assertIsNone(entry)
        self.assertIn("不允许执行", message)
        entry, message = self.tasks.run_action(self._task_id("TASK-1044"), "提交调度")
        self.assertIsNone(entry)
        self.assertEqual(self._task("TASK-1044")["status"], lc.TASK_TERMINATED)

        # 让一个已排期计划关联已终止任务，确认排期也不得改写其终态
        plan = self._plan("PLAN-32")
        plan["关联任务"] = "TASK-1044"
        with self.assertRaises(lc.LifecycleError):
            # PLAN-32 已处于已排期，重复确认走幂等；这里直接验证联动入口拒绝终态
            lc.admit_to_ready(self._task("TASK-1044"), reason_source="测试")
        self.assertEqual(self._task("TASK-1044")["status"], lc.TASK_TERMINATED)

    def test_terminated_task_cannot_restart_under_old_plan(self) -> None:
        # TASK-1042 执行中 -> 终止；PLAN-31 已暂停，终止后无法再启动
        entry, _ = self.tasks.run_action(self._task_id("TASK-1042"), "终止任务")
        self.assertEqual(entry["status"], lc.TASK_TERMINATED)
        # 终态锁定优先于审批门禁：已终止任务直接以“不允许执行启动”被拒，不会被旧计划拉起
        entry, message = self.tasks.run_action(self._task_id("TASK-1042"), "启动任务")
        self.assertIsNone(entry)
        self.assertIn("不允许执行", message)
        self.assertIn("启动任务", message)
        # 恢复计划排期：终态任务被跳过但不阻断计划恢复，历史终态保持
        plan_entry, plan_msg = self.plans.run_action(self._plan_id("PLAN-31"), "确认排期")
        self.assertIsNotNone(plan_entry)
        self.assertEqual(plan_entry["status"], lc.PLAN_SCHEDULED)
        self.assertEqual(self._task("TASK-1042")["status"], lc.TASK_TERMINATED)
        # 即使恢复计划排期，终态任务依旧不能启动
        entry, message = self.tasks.run_action(self._task_id("TASK-1042"), "启动任务")
        self.assertIsNone(entry)
        self.assertIn("不允许执行", message)
        self.assertEqual(self._task("TASK-1042")["status"], lc.TASK_TERMINATED)

    def test_reschedule_skips_terminal_task_without_blocking_plan(self) -> None:
        # PLAN-32 已排期；把已终止的 TASK-1044 关联进来，重复确认排期应成功且跳过终态任务
        self._plan("PLAN-32")["关联任务"] = "TASK-1043,TASK-1044"
        entry, message = self.plans.run_action(self._plan_id("PLAN-32"), "确认排期")
        self.assertIsNotNone(entry, message)
        self.assertEqual(self._task("TASK-1044")["status"], lc.TASK_TERMINATED)
        # 终态任务的日志里应留下“保留历史结果、跳过调度”的可追溯记录
        self.assertTrue(
            any("终态" in line and "跳过调度" in line for line in self._task("TASK-1044")["status_log"])
        )

    # -- 3. 重复提交只产生一次状态变化 -------------------------------------
    def test_duplicate_submission_is_idempotent(self) -> None:
        plan, _ = self.plans.create_entry({
            "计划编号": "PLAN-IDEM", "关联任务": "TASK-IDEM", "cron表达式": "0 * * * * ?",
        })
        self.tasks.create_entry({
            "任务编号": "TASK-IDEM", "采集源名称": "src", "规则编号": "PARS-X",
        })
        self.plans.run_action(int(plan["id"]), "提交审批")
        before = copy.deepcopy(dict(self._plan("PLAN-IDEM")))
        _, message = self.plans.run_action(int(plan["id"]), "提交审批")
        self.assertIn("重复提交", message)
        after = self._plan("PLAN-IDEM")
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before.get("status_log"), after.get("status_log"))

    # -- 4. 事务一致性：任一侧失败整体回滚 ---------------------------------
    def test_transaction_context_rolls_back_all_changes(self) -> None:
        plan = self._plan("PLAN-32")
        task = self._task("TASK-1043")
        plan_before = copy.deepcopy(dict(plan))
        task_before = copy.deepcopy(dict(task))

        with self.assertRaises(RuntimeError):
            with lc.transaction():
                plan["status"] = lc.PLAN_PAUSED          # 先真实改动计划侧
                task["status"] = lc.TASK_TERMINATED     # 再改动任务侧
                raise RuntimeError("模拟中途写入失败")    # 任意一步失败

        self.assertEqual(dict(self._plan("PLAN-32")), plan_before)
        self.assertEqual(dict(self._task("TASK-1043")), task_before)

    def test_service_action_rolls_back_both_sides_on_failure(self) -> None:
        plan, _ = self.plans.create_entry({
            "计划编号": "PLAN-TX", "关联任务": "TASK-TX", "cron表达式": "0 * * * * ?",
        })
        self.tasks.create_entry({
            "任务编号": "TASK-TX", "采集源名称": "src", "规则编号": "PARS-TX",
        })
        self.plans.run_action(int(plan["id"]), "提交审批")

        plan_snapshot = copy.deepcopy(dict(self._plan("PLAN-TX")))
        task_snapshot = copy.deepcopy(dict(self._task("TASK-TX")))

        # run_plan_action 内部以模块全局名调用 linked_tasks，patch 该名字让联动步骤抛业务异常。
        original = lc.linked_tasks

        def boom(plan_row):  # noqa: ANN001
            raise lc.LifecycleError("模拟联动校验失败")

        lc.linked_tasks = boom  # type: ignore[assignment]
        try:
            entry, message = self.plans.run_action(int(plan["id"]), "确认排期")
        finally:
            lc.linked_tasks = original  # type: ignore[assignment]

        self.assertIsNone(entry)
        self.assertIn("模拟联动校验失败", message)
        self.assertEqual(self._plan("PLAN-TX")["status"], plan_snapshot["status"])
        self.assertEqual(self._task("TASK-TX")["status"], task_snapshot["status"])
        self.assertEqual(
            self._plan("PLAN-TX").get("status_log"), plan_snapshot.get("status_log")
        )

    # -- 5. 统一最终状态视图，冲突时审批结论优先并给可操作原因 --------------
    def test_unified_view_conflict_prefers_plan_with_reason(self) -> None:
        view = self.tasks.get_entry(self._task_id("TASK-1042"))
        self.assertEqual(view["effective_status"], lc.TASK_RUNNING)
        self.assertFalse(view["dispatchable"])
        self.assertIn("PLAN-31", view["blocked_reason"])
        self.assertIn("暂停", view["blocked_reason"])
        # 清单与详情同一份口径
        listed, total = self.tasks.list_entries(keyword="TASK-1042")
        self.assertEqual(total, 1)
        self.assertEqual(listed[0]["status_reason"], view["status_reason"])

        pview = self.plans.get_entry(self._plan_id("PLAN-31"))
        self.assertIn("TASK-1042", pview["blocked_reason"])
        self.assertEqual(pview["effective_status"], lc.PLAN_PAUSED)

    def test_view_without_plan_gives_actionable_reason(self) -> None:
        self.tasks.create_entry({
            "任务编号": "TASK-ORPHAN", "采集源名称": "src", "规则编号": "PARS-O",
        })
        view = self.tasks.get_entry(
            next(t["id"] for t in store.rows(lc.TASK_MODULE) if t["任务编号"] == "TASK-ORPHAN")
        )
        self.assertIn("没有关联调度计划", view["blocked_reason"])
        self.assertFalse(view["dispatchable"])

    # -- 6. 两类记录各自保存自己的业务字段，互不串字段 ----------------------
    def test_plan_and_task_keep_own_business_fields(self) -> None:
        plan_view = self.plans.get_entry(self._plan_id("PLAN-31"))
        task_view = self.tasks.get_entry(self._task_id("TASK-1042"))

        # 计划侧字段
        for field in ("cron表达式", "依赖计划", "超时分钟", "优先级", "生效日期", "失效日期"):
            self.assertIn(field, plan_view)
        # 任务侧字段
        for field in ("调度周期", "增量字段", "起始水位", "截止水位"):
            self.assertIn(field, task_view)
        # 互不越界：计划不存运行水位，任务不存 cron
        self.assertNotIn("起始水位", plan_view)
        self.assertNotIn("cron表达式", task_view)
        # 任务视图能读到关联计划的审批结论，两份记录共享同一份排期/暂停结论
        self.assertEqual(plan_view["status"], lc.PLAN_PAUSED)
        self.assertEqual(task_view["plan_status"], lc.PLAN_PAUSED)
        self.assertEqual(task_view["plan_code"], "PLAN-31")

    # -- 7. 状态过滤基于最终状态 -------------------------------------------
    def test_status_filter_uses_effective_status(self) -> None:
        rows, total = self.tasks.list_entries(status=lc.TASK_RUNNING)
        self.assertTrue(all(r["effective_status"] == lc.TASK_RUNNING for r in rows))
        self.assertGreaterEqual(total, 1)
        self.assertIn("TASK-1042", [r["任务编号"] for r in rows])

    # -- 8. pending/abnormal 旧字段语义保持兼容，冲突标记只在视图派生 -------
    def test_legacy_pending_flag_keeps_original_meaning(self) -> None:
        # 已完成任务按旧口径仍算“待处理（pending=True）”，只有已终止才是终态
        task, _ = self.tasks.create_entry({
            "任务编号": "TASK-DONE", "采集源名称": "src", "规则编号": "PARS-D",
        })
        tid = int(task["id"])
        plan, _ = self.plans.create_entry({
            "计划编号": "PLAN-DONE", "关联任务": "TASK-DONE", "cron表达式": "0 * * * * ?",
        })
        pid = int(plan["id"])
        self.plans.run_action(pid, "提交审批")
        self.plans.run_action(pid, "确认排期")          # 任务进入待调度
        self.tasks.run_action(tid, "启动任务")          # 执行中：pending 仍为 True
        self.assertTrue(self._task("TASK-DONE")["pending"])
        self.tasks.run_action(tid, "终止任务")          # 已终止：唯一 pending=False 的任务态
        self.assertFalse(self._task("TASK-DONE")["pending"])

        # 计划侧：已排期 pending=True，已暂停才 pending=False
        self.assertTrue(self._plan("PLAN-DONE")["pending"])
        self.plans.run_action(pid, "暂停计划")
        self.assertFalse(self._plan("PLAN-DONE")["pending"])

    def test_conflict_is_derived_in_view_not_stored(self) -> None:
        view = self.tasks.get_entry(self._task_id("TASK-1042"))
        self.assertTrue(view["conflict"])
        # 落库记录本身不新增 conflict 字段，只保留原有字段
        self.assertNotIn("conflict", self._task("TASK-1042"))
        pview = self.plans.get_entry(self._plan_id("PLAN-31"))
        self.assertTrue(pview["conflict"])
        self.assertNotIn("conflict", self._plan("PLAN-31"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
