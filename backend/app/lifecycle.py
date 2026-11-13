"""调度计划 / 采集任务共用的生命周期算法。

两类记录的职责在这里被明确划分：

* 调度计划的「审批状态」决定任务能不能进入待调度（能不能被调度器拾取）；
* 采集任务的「执行状态」只记录任务自己的真实运行结果（执行中 / 已完成 / 已终止）。

因此“计划已暂停”和“任务执行中”不再是互相对立的结论：暂停的是调度入口，
任务的实际终态（已终止、已完成）永远不会被计划侧的动作改写。

模块只依赖标准库与内存仓库，所有跨记录写入都通过 :func:`transaction` 包裹，
任意一条记录写入失败都会整体回滚到操作前的状态。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.store import store

# --------------------------------------------------------------------------- 模块与字段
PLAN_MODULE = "scheduler"
TASK_MODULE = "taskcrawl"

PLAN_LINK_FIELD = "关联任务"   # 计划上指向任务编号的字段
TASK_CODE_FIELD = "任务编号"   # 任务自身的业务编号字段

# --------------------------------------------------------------------------- 计划审批状态
PLAN_DRAFT = "草稿"
PLAN_REVIEWING = "计划审批中"
PLAN_SCHEDULED = "已排期"
PLAN_PAUSED = "已暂停"

PLAN_STATUSES = [PLAN_DRAFT, PLAN_REVIEWING, PLAN_SCHEDULED, PLAN_PAUSED]

PLAN_ACTIONS: dict[str, str] = {
    "提交审批": PLAN_REVIEWING,
    "确认排期": PLAN_SCHEDULED,
    "暂停计划": PLAN_PAUSED,
}

# 每个动作允许从哪些审批状态发起；动作天然幂等，目标状态本身也算合法来源。
PLAN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "提交审批": (PLAN_DRAFT, PLAN_REVIEWING),
    "确认排期": (PLAN_REVIEWING, PLAN_PAUSED, PLAN_SCHEDULED),
    "暂停计划": (PLAN_SCHEDULED, PLAN_PAUSED),
}

# --------------------------------------------------------------------------- 任务执行状态
TASK_DRAFT = "草稿"
TASK_READY = "待调度"
TASK_RUNNING = "执行中"
TASK_DONE = "已完成"
TASK_TERMINATED = "已终止"

TASK_STATUSES = [TASK_DRAFT, TASK_READY, TASK_RUNNING, TASK_DONE, TASK_TERMINATED]

TASK_ACTIONS: dict[str, str] = {
    "提交调度": TASK_READY,
    "启动任务": TASK_RUNNING,
    "终止任务": TASK_TERMINATED,
}

TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "提交调度": (TASK_DRAFT, TASK_READY),
    "启动任务": (TASK_READY, TASK_RUNNING),
    "终止任务": (TASK_RUNNING, TASK_TERMINATED),
}

# 任务的实际终态：任何计划侧动作都不得改写这些状态。
TASK_TERMINAL_STATUSES = frozenset({TASK_DONE, TASK_TERMINATED})


class LifecycleError(Exception):
    """生命周期规则不满足时抛出，消息直接面向用户、可操作。"""


# 事务上下文中暂存的深拷贝快照；嵌套时复用最外层事务。
_TX_SNAPSHOT: ContextVar[dict[str, list[dict[str, Any]]] | None] = ContextVar(
    "lifecycle_snapshot", default=None
)


@contextmanager
def transaction() -> Iterator[None]:
    """跨记录写入的事务边界。

    进入时快照当前仓库里两张表的全部记录，期间任何一步抛错都会把两张表
    整体还原到快照，保证“计划 + 任务”要么一起成功、要么一起回到原状态。
    """
    existing = _TX_SNAPSHOT.get()
    token = None
    if existing is None:
        snapshot = {
            module: [dict(row) for row in store.rows(module)]
            for module in (PLAN_MODULE, TASK_MODULE)
        }
        token = _TX_SNAPSHOT.set(snapshot)
    try:
        yield
    except Exception:
        if token is not None:
            for module, rows in snapshot.items():
                table = store.rows(module)
                table.clear()
                table.extend(dict(row) for row in rows)
        raise
    finally:
        if token is not None:
            _TX_SNAPSHOT.reset(token)


# --------------------------------------------------------------------------- 基础状态机
def _allowed_statuses(action: str, transitions: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    if action not in transitions:
        raise LifecycleError(f"动作「{action}」不在允许的生命周期动作范围内")
    return transitions[action]


def plan_transition(plan: dict[str, Any], action: str) -> bool:
    """推进一条调度计划的审批状态，返回是否真正发生了状态变化。

    重复提交同一动作时保持原状态、返回 ``False``，保证一次操作只产生一次状态变化。
    """
    allowed = _allowed_statuses(action, PLAN_TRANSITIONS)
    current = str(plan.get("status") or "")
    target = PLAN_ACTIONS[action]
    if current not in allowed:
        raise LifecycleError(
            f"调度计划当前为「{current}」，不允许执行「{action}」；"
            f"可在「{'、'.join(allowed)}」状态下重试"
        )
    if current == target:
        return False
    plan["status"] = target
    return True


def task_transition(task: dict[str, Any], action: str) -> bool:
    """推进一条采集任务的执行状态，返回是否真正发生了状态变化。"""
    _ensure_task_action_allowed(str(task.get("status") or ""), action)
    target = TASK_ACTIONS[action]
    if str(task.get("status") or "") == target:
        return False
    task["status"] = target
    return True


def _ensure_task_action_allowed(current: str, action: str) -> None:
    """纯校验：判断当前执行状态能否发起该动作，不修改任何记录。

    终态（已完成 / 已终止）在这里被优先拦下，保证历史实际终态的锁定
    先于调度计划的审批门禁生效，消息直接说明不允许的动作。
    """
    allowed = _allowed_statuses(action, TASK_TRANSITIONS)
    if current not in allowed:
        raise LifecycleError(
            f"采集任务当前为「{current}」，不允许执行「{action}」；"
            f"可在「{'、'.join(allowed)}」状态下重试"
        )


# --------------------------------------------------------------------------- 计划 / 任务关联
def find_plan(plan_id: int) -> dict[str, Any] | None:
    return store.find(PLAN_MODULE, plan_id)


def find_task(task_id: int) -> dict[str, Any] | None:
    return store.find(TASK_MODULE, task_id)


def linked_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """按计划的“关联任务”编号找到全部关联采集任务（允许一个计划关联多个任务编号）。"""
    raw = str(plan.get(PLAN_LINK_FIELD) or "").strip()
    if not raw:
        return []
    codes = {part.strip() for part in raw.replace("，", ",").split(",") if part.strip()}
    if not codes:
        return []
    return [
        row for row in store.rows(TASK_MODULE)
        if str(row.get(TASK_CODE_FIELD) or "").strip() in codes
    ]


def plan_of_task(task: dict[str, Any]) -> dict[str, Any] | None:
    """找到任务当前挂靠的调度计划；存在多份时取审批结论最强的一份。

    审批优先级：已排期 > 计划审批中 > 已暂停 > 草稿，保证冲突时读到同一份结论。
    """
    code = str(task.get(TASK_CODE_FIELD) or "").strip()
    if not code:
        return None
    matches = [
        row for row in store.rows(PLAN_MODULE)
        if code in {
            part.strip()
            for part in str(row.get(PLAN_LINK_FIELD) or "").replace("，", ",").split(",")
            if part.strip()
        }
    ]
    if not matches:
        return None
    rank = {PLAN_SCHEDULED: 0, PLAN_REVIEWING: 1, PLAN_PAUSED: 2, PLAN_DRAFT: 3}
    return min(matches, key=lambda row: rank.get(str(row.get("status")), 99))


def plan_admits_dispatch(plan: dict[str, Any] | None) -> bool:
    """调度审批结论：只有“已排期”的计划才允许任务进入待调度。"""
    return plan is not None and str(plan.get("status")) == PLAN_SCHEDULED


# --------------------------------------------------------------------------- 进入待调度的统一口径
def admit_to_ready(task: dict[str, Any], *, reason_source: str) -> bool:
    """让一条任务进入“待调度”的唯一入口，计划与任务两侧共用。

    * 终态任务（已完成 / 已终止）直接拒绝，历史实际终态不被改写；
    * 已经在“待调度 / 执行中”的任务保持现状（幂等）；
    * 只有草稿任务会被推进到待调度。
    """
    current = str(task.get("status") or "")
    if current in TASK_TERMINAL_STATUSES:
        raise LifecycleError(
            f"任务 {task.get(TASK_CODE_FIELD)} 已处于「{current}」终态，"
            "历史执行结果不允许再被调度改写"
        )
    if current in (TASK_READY, TASK_RUNNING):
        return False
    if current != TASK_DRAFT:
        raise LifecycleError(f"任务当前为「{current}」，无法进入待调度")
    task["status"] = TASK_READY
    _append_log(task, f"{reason_source}：进入待调度")
    return True


def _append_log(row: dict[str, Any], message: str) -> None:
    row.setdefault("status_log", [])
    row["status_log"].append(message)


def _sync_flags(row: dict[str, Any], *, pending_statuses: frozenset[str]) -> None:
    """根据最新状态回写 pending/abnormal 两个既有看板字段。

    严格保持旧字段含义：pending 取“非终态即待处理”，动作执行后 abnormal 复位为 False
    （与旧实现 NEGATIVE_ACTIONS 为空时的结果一致）；冲突标记不在此落库，改由读视图派生。
    """
    status = str(row.get("status") or "")
    row["pending"] = status in pending_statuses
    row["abnormal"] = False


# 旧口径：状态序列最后一个为终态，其余一律算待处理，这里显式列出避免散落魔法值。
PLAN_PENDING_STATUSES = frozenset({PLAN_DRAFT, PLAN_REVIEWING, PLAN_SCHEDULED})
TASK_PENDING_STATUSES = frozenset({TASK_DRAFT, TASK_READY, TASK_RUNNING, TASK_DONE})


# --------------------------------------------------------------------------- 计划动作（含任务侧联动，事务内执行）
def run_plan_action(plan_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
    plan = find_plan(plan_id)
    if plan is None:
        return None, f"调度计划 {plan_id} 不存在或已归档"
    if action not in PLAN_ACTIONS:
        return None, f"动作「{action}」不属于调度计划可执行范围"

    with transaction():
        changed = plan_transition(plan, action)
        plan_code = plan.get("计划编号", plan_id)

        if action == "确认排期":
            # 计划审批通过 → 关联的草稿任务共享同一份“进入待调度”的结论。
            # 终态任务（已完成/已终止）只跳过、不改写历史结果，也不阻断计划恢复排期；
            # 它们“不得被旧计划再次启动”由启动入口的终态锁保证。
            moved = 0
            skipped_terminal = 0
            for task in linked_tasks(plan):
                if str(task.get("status")) in TASK_TERMINAL_STATUSES:
                    skipped_terminal += 1
                    _append_log(
                        task,
                        f"计划 {plan_code} 重新排期：任务已处于「{task.get('status')}」终态，保留历史结果、跳过调度",
                    )
                elif admit_to_ready(task, reason_source=f"计划 {plan_code} 确认排期"):
                    moved += 1
                _sync_flags(task, pending_statuses=TASK_PENDING_STATUSES)
            if changed:
                note = f"确认排期：审批通过，{moved} 条任务进入待调度"
                if skipped_terminal:
                    note += f"，{skipped_terminal} 条终态任务保留历史结果未被调度"
                _append_log(plan, note)
        elif action == "暂停计划":
            # 暂停的是调度入口：尚未启动（待调度）的任务回到/留在待调度且不再被拾取；
            # 执行中与终态任务的实际执行状态一律不动。
            affected = 0
            for task in linked_tasks(plan):
                status = str(task.get("status"))
                if status == TASK_READY:
                    _append_log(task, f"计划 {plan_code} 已暂停：保留待调度，暂停期间不会被调度拾取")
                    affected += 1
                elif status == TASK_DRAFT:
                    _append_log(task, f"计划 {plan_code} 已暂停：调度入口关闭，任务维持草稿")
                _sync_flags(task, pending_statuses=TASK_PENDING_STATUSES)
            if changed:
                _append_log(plan, f"暂停计划：{affected} 条未启动任务回到待调度等待，执行中/终态任务保持不变")
        elif changed:
            _append_log(plan, f"{action}：审批状态推进到{plan['status']}")

        _sync_flags(plan, pending_statuses=PLAN_PENDING_STATUSES)
        result = dict(plan)

    return result, _compose_plan_message(action, changed)


def _compose_plan_message(action: str, changed: bool) -> str:
    if not changed:
        return f"调度计划已是「{PLAN_ACTIONS[action]}」，重复提交未产生新的状态变化"
    return f"调度计划已{action}"


# --------------------------------------------------------------------------- 任务动作（审批结论为准，事务内执行）
def run_task_action(task_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
    task = find_task(task_id)
    if task is None:
        return None, f"采集任务 {task_id} 不存在或已归档"
    if action not in TASK_ACTIONS:
        return None, f"动作「{action}」不属于采集任务可执行范围"

    with transaction():
        current = str(task.get("status") or "")

        if action == "提交调度":
            # 已经在待调度：重复提交幂等返回，不再触发审批门禁（门禁只拦“新进入”）。
            if current == TASK_READY:
                changed = False
            else:
                # 先校验执行状态本身是否允许该动作（终态在此被优先锁定）。
                _ensure_task_action_allowed(current, action)
                plan = plan_of_task(task)
                if not plan_admits_dispatch(plan):
                    raise LifecycleError(
                        _block_reason(task, plan)
                        or "关联调度计划未处于“已排期”，任务暂不能进入待调度"
                    )
                changed = admit_to_ready(task, reason_source="任务提交调度")
        elif action == "启动任务":
            # 终态锁定优先于审批门禁：已终止/已完成任务直接拒绝，消息指向非法动作。
            _ensure_task_action_allowed(current, action)
            plan = plan_of_task(task)
            if not plan_admits_dispatch(plan):
                # 审批结论优先：计划未排期/已暂停时，任务即使停在待调度也不能启动。
                raise LifecycleError(
                    _block_reason(task, plan)
                    or "关联调度计划未处于“已排期”，任务暂不能启动"
                )
            changed = task_transition(task, action)
            if changed:
                _append_log(task, "启动任务：进入执行中")
        else:  # 终止任务
            changed = task_transition(task, action)
            if changed:
                _append_log(task, "终止任务：执行结束，终态锁定，旧计划不得再次启动")

        _sync_flags(task, pending_statuses=TASK_PENDING_STATUSES)
        result = dict(task)

    if not changed:
        return result, f"采集任务已是「{TASK_ACTIONS[action]}」，重复提交未产生新的状态变化"
    return result, f"采集任务已{action}"


# --------------------------------------------------------------------------- 统一最终状态视图
def _block_reason(task: dict[str, Any], plan: dict[str, Any] | None) -> str:
    """审批结论与任务执行状态冲突时，给出可操作的原因。"""
    status = str(task.get("status") or "")
    code = task.get(TASK_CODE_FIELD, "该任务")
    if plan is None:
        if status in (TASK_DRAFT, TASK_READY):
            return f"任务 {code} 没有关联调度计划，请先登记并排期计划后再提交调度"
        return ""
    plan_status = str(plan.get("status"))
    plan_code = plan.get("计划编号", "关联计划")
    if plan_status == PLAN_PAUSED:
        if status == TASK_RUNNING:
            return (
                f"计划 {plan_code} 已暂停（调度入口关闭），任务 {code} 仍在执行中；"
                "本次运行不受影响，终止后需先恢复计划排期才能再次启动"
            )
        if status == TASK_READY:
            return f"计划 {plan_code} 已暂停，任务 {code} 停在待调度且不会被拾取；请在计划页“确认排期”恢复后再启动"
        if status == TASK_DRAFT:
            return f"计划 {plan_code} 已暂停，任务 {code} 无法提交调度；请先恢复计划排期"
    if plan_status in (PLAN_DRAFT, PLAN_REVIEWING):
        if status in (TASK_DRAFT, TASK_READY):
            return f"计划 {plan_code} 仍处于「{plan_status}」，审批通过（确认排期）后任务 {code} 才能进入待调度"
    return ""


def task_view(task: dict[str, Any]) -> dict[str, Any]:
    """任务详情页与任务清单共用的最终状态视图。

    输出在不改动原始业务字段的前提下补充：

    * ``effective_status``：用户应看到的最终状态（任务实际执行状态不被计划改写）；
    * ``dispatchable``：按调度审批结论，任务此刻能否进入待调度 / 被启动；
    * ``blocked_reason``：冲突或被拦截时的可操作原因，无冲突为空字符串。
    """
    view = dict(task)
    plan = plan_of_task(task)
    status = str(task.get("status") or "")
    reason = _block_reason(task, plan)

    view["plan_status"] = plan.get("status") if plan else None
    view["plan_code"] = plan.get("计划编号") if plan else None
    view["effective_status"] = status
    view["dispatchable"] = plan_admits_dispatch(plan) and status in (TASK_DRAFT, TASK_READY)
    # 精确标记“计划已暂停、任务却在执行中”这类互相冲突的结论，供前端高亮；不落库。
    view["conflict"] = bool(
        plan is not None
        and plan.get("status") == PLAN_PAUSED
        and status == TASK_RUNNING
    )
    view["blocked_reason"] = reason
    # 列表里直接给一句“状态 + 原因”的合并结论，冲突时审批结论优先说明。
    view["status_reason"] = f"{status}（{reason}）" if reason else status
    return view


def plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    """计划详情页与计划清单共用的最终状态视图。"""
    view = dict(plan)
    tasks = linked_tasks(plan)
    view["effective_status"] = str(plan.get("status") or "")
    view["linked_task_count"] = len(tasks)
    running = [str(t.get(TASK_CODE_FIELD)) for t in tasks if str(t.get("status")) == TASK_RUNNING]
    view["running_task_codes"] = running
    conflict = plan.get("status") == PLAN_PAUSED and bool(running)
    view["conflict"] = conflict
    if conflict:
        view["blocked_reason"] = (
            f"计划已暂停，但仍有 {len(running)} 条任务执行中（{'、'.join(running)}）；"
            "调度入口已关闭，执行中任务终止后不会再被本计划启动"
        )
    else:
        view["blocked_reason"] = ""
    view["status_reason"] = (
        f"{view['effective_status']}（{view['blocked_reason']}）"
        if view["blocked_reason"]
        else view["effective_status"]
    )
    return view
