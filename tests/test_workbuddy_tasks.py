"""WorkBuddy 成长任务适配器的离线逻辑测试（不联网，全部用假响应）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkin_tool.adapters import workbuddy_tasks as wt  # noqa: E402


class FakeRunner(wt.WorkBuddyTaskRunner):
    """拦截所有 HTTP：按 tasks 列表返回假数据，并统计调用次数。"""

    def __init__(self, tasks=None, **kwargs):
        super().__init__("fake.token.value", **kwargs)
        self.tasks = list(tasks or [])
        self.calls = {"report": 0, "claim": 0, "chat": 0}

    def _req(self, method, path, body=None, *, timeout=None, sse=False):
        key = path
        self.calls[key] = self.calls.get(key, 0) + 1
        if path == "/v2/activity/growth/tasks":
            return 200, {"code": 0, "data": {"tasks": self.tasks}}
        if path.endswith("/claim"):
            self.calls["claim"] += 1
            return 200, {"code": 0, "data": {"credit": 10, "energy": 5}}
        if path == "/v2/report":
            self.calls["report"] += 1
            return 200, {"code": 0}
        return 200, {"code": 0, "data": {}}

    def webchat(self, prompt, name="chat", model="glm-5.2", meta=None):
        self.calls["chat"] += 1
        return "conv-1", "你好呀"


def _task(code, status, cur=0, target=1):
    return {"task_code": code, "accept_status": status, "progress": {"current": cur, "target": target}}


def test_runs_and_claims_completed_only():
    tasks = [
        _task("chat_5", "accepted", 0, 5),
        _task("template_5", "completed", 5, 5),
        _task("RichMeow_Chat", "not_accepted", 0, 1),
    ]
    r = FakeRunner(tasks, chat_tasks=True)
    res = r.run()
    assert res["ok"]
    assert res["total"] == 3
    assert r.calls["claim"] == 1, "只给 completed 的任务领奖"
    assert r.calls["report"] > 0, "未完成的任务要上报进度"
    assert r.calls["chat"] == 5, "chat_5 需要 5 次对话"
    # 需要真实客户端的任务要标注出来，不能假装完成
    assert "桌面端对话" in res["rest"]
    assert "桌面端对话" in res["manual"]


def test_idempotent_when_all_claimed():
    tasks = [
        _task("chat_5", "claimed", 5, 5),
        _task("template_5", "claimed", 5, 5),
    ]
    r = FakeRunner(tasks, chat_tasks=True)
    res = r.run()
    assert r.calls["chat"] == 0, "已完成就不该再发起对话"
    assert r.calls["report"] == 0, "已完成就不该再上报"
    assert res["done"] == 2 and res["rest"] == []


def test_missing_task_is_skipped():
    """活动换期后任务码消失：必须跳过，不能当成未完成去白跑 AI 对话。"""
    r = FakeRunner([_task("chat_5", "claimed", 5, 5)], chat_tasks=True)
    res = r.run()
    assert r.calls["chat"] == 0


def test_chat_tasks_can_be_disabled():
    r = FakeRunner([_task("chat_5", "accepted", 0, 5)], chat_tasks=False)
    r.run()
    assert r.calls["chat"] == 0


def test_network_failure_is_isolated():
    class Boom(FakeRunner):
        def _req(self, *args, **kwargs):
            raise RuntimeError("boom")

    res = Boom([]).run()
    assert res["ok"], "单任务失败不应让整体崩掉"
    assert res["failed"], "失败项要记录下来"


def test_requires_token():
    out = wt.run_daily_tasks({})
    assert not out["ok"]
    assert "access_token" in out["message"]


def test_task_names_are_translated():
    assert wt.task_cn("chat_5") == "和AI聊天5次"
    assert wt.task_cn("no_such_code") == "no_such_code"
