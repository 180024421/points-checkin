# -*- coding: utf-8 -*-
"""WorkBuddy（CodeBuddy）成长中心「每日做任务」适配器。

签到只拿每日积分，成长中心还有一批任务（召唤专家、用模板、聊天、抽奖、盲盒、
派猫猫旅行、连签兑换…）能拿积分和能量。本模块把这部分自动化：

    拉任务列表 → 接受未接受的任务 → 逐项补进度 → 给已完成的任务领奖

设计要点：
* **幂等**：每项任务先查进度，已完成/已领奖直接跳过，重复运行零副作用。
* **只需 accessToken**：和签到一样，本机从 WorkBuddy 桌面端登录态取，服务器代跑
  直接用上传的 token_blob。
* **纯 API**：不碰桌面端（参考脚本里的「桌面换血」两项任务需要真实客户端会话，
  这里不实现，会在结果里标注为待手动）。
* 进度上报走官方遥测接口 ``/v2/report``，事件结构参考 WorkBuddy 桌面端实现。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import date, timedelta
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://www.workbuddy.cn"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "WorkBuddy/5.5.4 Chrome/138.0.7204.251 Electron/37.10.3 Safari/537.36"
)
UA_SHORT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 WorkBuddy/5.5.4"

QQ_TPL = "cb_y5Dy46tPQGGWtueMxXbe"          # 企鹅教师助手模板
THEME_KEY = "theme-tkmw7j"                   # 和平精英主题
LIB_DOC_URL = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
EXPERT_MARKETPLACE_URL = (
    "https://acc-1258344699.cos.accelerate.myqcloud.com"
    "/workbuddy/expert-marketplace/expert_center.json"
)

TASK_NAME_CN = {
    "create_canvas": "设计创意模式",
    "playbook_prompt": "探索优秀灵感",
    "RichMeow_Chat": "桌面端对话",
    "Library_read": "体验资料库",
    "Expert_lighthouse": "腾讯轻量云专家",
    "Expert_Philanthropy": "公益专家",
    "Hp_Appearance": "和平精英主题",
    "Buddy_App": "发现应用",
    "Buddy_App_QQ": "企鹅教师助手",
    "Model_chat_GLM5.2": "GLM-5.2模型对话",
    "black_cat": "夜猫子活动",
    "Expert_team_use_3": "召唤3次专家团",
    "first_buddy": "领取Buddy",
    "chat_5": "和AI聊天5次",
    "skill_1": "尝鲜热门技能",
    "expert_5": "召唤5次专家",
    "template_5": "使用5个模板",
    "automation_1": "设置自动化任务",
    "workstation_expert": "工作台搭建师",
}

# 需要真实客户端的任务（本机自动做不了，交给用户在 WorkBuddy 里点一下）
DESKTOP_ONLY = {"RichMeow_Chat", "skill_1", "workstation_expert"}

LogFn = Callable[[str], None]


def task_cn(code: str) -> str:
    return TASK_NAME_CN.get(code, code or "?")


def jwt_field(token: str, field: str, default: str = "") -> str:
    """从 accessToken（JWT）里取字段，失败返回 default。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str(data.get(field) or default)
    except Exception:
        return default


class WorkBuddyTaskRunner:
    """单账号任务执行器。所有方法都吞掉异常，保证一项失败不影响后面的任务。"""

    def __init__(
        self,
        token: str,
        *,
        uid: str = "",
        nickname: str = "",
        log: LogFn | None = None,
        timeout: float = 20.0,
        chat_tasks: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = str(token or "").strip()
        self.uid = uid or jwt_field(self.token, "sub")
        self.nickname = nickname or jwt_field(self.token, "nickname", "用户")
        self.log = log
        self.timeout = timeout
        self.chat_tasks = chat_tasks
        self._sleep = sleep
        self.lines: list[str] = []
        self.done: list[str] = []
        self.failed: list[str] = []
        self.rest: list[str] = []

    # ------------------------------------------------------------------ 基础

    def _say(self, msg: str) -> None:
        self.lines.append(msg)
        if self.log:
            self.log(msg)

    def _headers(self, *, sse: bool = False) -> dict[str, str]:
        h = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": BASE + "/profile/growth-center",
            "User-Agent": UA,
        }
        if sse:
            h["Accept"] = "text/event-stream"
        return h

    def _req(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: float | None = None,
        sse: bool = False,
    ) -> tuple[int, Any]:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(
            BASE + path,
            data=data,
            method=method,
            headers=self._headers(sse=sse),
        )
        try:
            with urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, _safe_json(resp.read())
        except HTTPError as exc:
            return exc.code, _safe_json(_read(exc))
        except URLError as exc:
            return -1, {"msg": f"网络失败: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return -1, {"msg": str(exc)}

    # ------------------------------------------------------------------ 任务读写

    def list_tasks(self) -> list[dict[str, Any]]:
        try:
            status, resp = self._req("GET", "/v2/activity/growth/tasks", timeout=25)
        except Exception:  # noqa: BLE001
            return []
        tasks = resp.get("data", {}).get("tasks") if isinstance(resp, dict) else None
        return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []

    def prog(self, code: str) -> tuple[str, Any, Any]:
        """返回 (accept_status, current, target)。"""
        for t in self.list_tasks():
            if t.get("task_code") == code:
                pr = t.get("progress") or {}
                return str(t.get("accept_status") or ""), pr.get("current"), pr.get("target")
        return "", None, None

    def _finished(self, code: str) -> bool:
        """是否无需再处理：已完成/已领奖/进度已满/当前活动没有这项任务。

        注意最后一种：活动换期后任务码会消失，此时绝不能当「未完成」去补进度，
        否则会白白发起真实 AI 对话、浪费账号额度。
        """
        st, cur, tgt = self.prog(code)
        if not st:
            return True
        if st in ("completed", "claimed"):
            return True
        return bool(tgt and isinstance(cur, (int, float)) and cur >= tgt)

    def accept_all(self) -> None:
        todo = [t.get("task_code") for t in self.list_tasks() if t.get("accept_status") == "not_accepted"]
        todo = [c for c in todo if c]
        if not todo:
            return
        self._req("POST", "/v2/activity/growth/tasks/accept", {"task_codes": todo})
        self._say(f"   已接受任务 {len(todo)} 项：{', '.join(task_cn(c) for c in todo)}")

    def claim(self, code: str) -> None:
        status, resp = self._req("POST", f"/v2/activity/growth/tasks/{code}/claim", {})
        data = resp.get("data") if isinstance(resp, dict) else {}
        if isinstance(data, dict) and data.get("already_claimed"):
            return
        if isinstance(data, dict) and (data.get("credit") or data.get("energy")):
            self._say(
                f"   领奖[{task_cn(code)}]: +{data.get('credit')}积分 +{data.get('energy')}能量"
            )

    def report(self, events: list[dict[str, Any]]) -> int:
        """上报遥测事件（任务进度的主要来源）。"""
        envelope = []
        for e in events:
            item = {
                "timestamp": int(time.time() * 1000),
                "reportDelay": 0,
                "userId": self.uid,
                "userNickname": self.nickname,
                "ideName": "web-Agents",
                "ideType": "web-Agents",
                "machineId": str(uuid.uuid4()),
                "mode": "CLOUD",
                "userAgent": UA_SHORT,
                "os": "Win32",
                "timezone": "Asia/Shanghai",
            }
            item.update(e)
            envelope.append(item)
        status, _ = self._req("POST", "/v2/report", envelope, timeout=15)
        return status

    # ------------------------------------------------------------------ 对话类

    def webchat(self, prompt: str, name: str = "chat", model: str = "glm-5.2",
                meta: dict[str, Any] | None = None) -> tuple[str, str]:
        """发起一次云上对话，返回 (conversationId, 回复文本)。"""
        conv_name = f"{name}-{uuid.uuid4().hex[:8]}"
        status, conv = self._req(
            "POST", "/console/webchat/conversations",
            {"name": conv_name}, timeout=20,
        )
        conv_id = conv.get("data", {}).get("conversationId", "") if isinstance(conv, dict) else ""
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "model": model,
            "stream": True,
            "conversationId": conv_id,
        }
        if meta:
            payload["_meta"] = meta
        text = ""
        try:
            req = Request(
                BASE + "/console/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers=self._headers(sse=True),
            )
            with urlopen(req, timeout=120) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:].strip()
                    if chunk in ("[DONE]", "[完成]", "[✅完成]"):
                        break
                    try:
                        node = json.loads(chunk)
                        for choice in node.get("choices", []):
                            text += (choice.get("delta") or {}).get("content", "") or ""
                    except Exception:
                        continue
        except Exception:
            pass
        return conv_id, text

    def _chat_events(self, conv_id: str, prompt: str, text: str) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        rid = "cmb-" + str(uuid.uuid4())
        common = {"conversationId": conv_id, "requestId": rid,
                  "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2"}
        return [
            {"eventCode": "chat_request_send", "timestamp": now, **common,
             "inputLength": len(prompt), "customAgentName": ""},
            {"eventCode": "chat_request_response", "timestamp": now + 100, **common,
             "toolCallCount": 0, "inputToken": max(1, len(prompt) // 4),
             "outputToken": max(1, len(text) // 4),
             "totalToken": max(2, (len(prompt) + len(text)) // 4)},
            {"eventCode": "chat_message_send", "timestamp": now + 50, **common,
             "messageId": "cmb-" + str(uuid.uuid4()), "historyCount": 1,
             "isContextTruncated": False, "currentStepCount": 1, "traceId": rid,
             "rootRequestId": rid, "parentConversationId": conv_id,
             "agentName": "cli", "agentType": "main"},
        ]

    def chat_n(self, code: str, n: int, prompts: list[str]) -> None:
        """通用聊天任务：chat_5 / Model_chat_GLM5.2。"""
        if not self.chat_tasks:
            return
        for i in range(n):
            if self._finished(code):
                break
            prompt = prompts[i % len(prompts)]
            conv_id, text = self.webchat(prompt, code)
            if text:
                self.report(self._chat_events(conv_id, prompt, text))
            self._sleep(4)
        st, cur, tgt = self.prog(code)
        self._mark(code, st, cur, tgt)

    # ------------------------------------------------------------------ 具体任务

    def _mark(self, code: str, st: str, cur: Any, tgt: Any) -> None:
        name = task_cn(code)
        if st in ("completed", "claimed"):
            self.done.append(name)
            self._say(f"   {name}: 已完成")
        elif st:
            self.rest.append(name)
            self._say(f"   {name}: {st} {cur}/{tgt}")
        else:
            self._say(f"   {name}: 无此任务")

    def t_sign(self) -> None:
        status, resp = self._req("POST", "/v2/billing/meter/daily-checkin", {})
        if isinstance(resp, dict) and resp.get("code") in (0, 200):
            d = resp.get("data") or {}
            self._say(f"   每日签到: +{d.get('credit', '?')}积分 连签{d.get('streak_days', '?')}天")
        else:
            self._say(f"   每日签到: {(resp or {}).get('msg', '已签到')}")

    def t_buddy_apps(self) -> None:
        for code in ("Buddy_App", "Buddy_App_QQ"):
            if self._finished(code):
                continue
            now = int(time.time() * 1000)
            self.report([
                {"eventCode": "buddyapp_discover_click", "timestamp": now},
                {"eventCode": "buddyapp_enter_click", "timestamp": now + 60,
                 "elementId": QQ_TPL, "elementName": "企鹅教师助手",
                 "position": "sidebar-switcher-trigger", "isFirstPage": "1"},
                {"eventCode": "buddyapp_show", "timestamp": now + 120,
                 "elementId": QQ_TPL, "elementName": "企鹅教师助手"},
            ])
            self._sleep(6)
        for code in ("Buddy_App", "Buddy_App_QQ"):
            st, cur, tgt = self.prog(code)
            if st:
                self._mark(code, st, cur, tgt)

    def t_theme(self) -> None:
        if self._finished("Hp_Appearance"):
            return
        status, resp = self._req(
            "POST", "/portal/user-asset/appearance/set",
            {"kind": "theme", "resource_key": THEME_KEY},
        )
        if isinstance(resp, dict) and resp.get("code") == 0:
            self._sleep(2)
            self.report([{
                "eventCode": "appearance_skin_apply", "action": "apply",
                "source": "settings_close", "id": THEME_KEY, "vipLevel": "free",
                "series": "craft", "type": "personal",
            }])
            self._sleep(6)
        self._mark("Hp_Appearance", *self.prog("Hp_Appearance"))

    def t_library(self) -> None:
        if self._finished("Library_read"):
            return
        self.report([{
            "eventCode": "web_element_click", "pageURL": LIB_DOC_URL,
            "elementId": "library_doc_intro_click", "elementName": "WorkBuddy资料库介绍",
            "enterpriseId": "",
        }])
        self._sleep(6)
        self._mark("Library_read", *self.prog("Library_read"))

    def t_canvas_automation(self) -> None:
        if not self._finished("create_canvas"):
            self.report([
                {"eventCode": "agent_task_created", "source": "CLOUD", "name": "",
                 "mode": "craft", "requestModelId": "default", "task_mode": "design"},
                {"eventCode": "wbx_design_canvas_task_create"},
            ])
            self._sleep(3)
            self._mark("create_canvas", *self.prog("create_canvas"))
        if not self._finished("automation_1"):
            self.report([
                {"eventCode": "agent_task_created", "source": "CLOUD", "name": "",
                 "mode": "craft", "requestModelId": "default", "task_mode": "automation",
                 "isAutomationBackground": True},
                {"eventCode": "automated_task_create_suc", "action": "create"},
                {"eventCode": "automated_task_execute", "action": "execute"},
            ])
            self._sleep(3)
            self._mark("automation_1", *self.prog("automation_1"))
        if not self._finished("playbook_prompt"):
            self.report([{
                "eventCode": "playbook_prompt_send", "ext1": str(uuid.uuid4()),
                "requestId": str(uuid.uuid4()), "id": "01-ProductDesign",
                "name": "产品设计", "type": "other", "promptLength": 30,
                "isOfficial": 1, "source": "growth-center",
            }])
            self._sleep(3)
            self._mark("playbook_prompt", *self.prog("playbook_prompt"))

    def _experts(self, kind: str) -> list[dict[str, Any]]:
        """拉专家市场（失败用内置兜底）。"""
        try:
            req = Request(EXPERT_MARKETPLACE_URL, method="GET", headers={"User-Agent": UA})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            node = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
            items = (node or {}).get(kind) or []
            out = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                out.append({
                    "id": str(it.get("id") or it.get("expertId") or uuid.uuid4().hex[:8]),
                    "name": str(it.get("name") or it.get("title") or "专家"),
                    "profession": str(it.get("profession") or it.get("expertTitle") or ""),
                    "industryId": str(it.get("industryId") or ""),
                })
            if out:
                return out
        except Exception:
            pass
        return [{"id": "expert-" + uuid.uuid4().hex[:8], "name": "Expert",
                 "profession": "", "industryId": ""}]

    def t_expert_5(self) -> None:
        if self._finished("expert_5"):
            return
        experts = self._experts("experts") or self._experts("normalExperts")
        for i in range(5):
            if self._finished("expert_5"):
                break
            e = experts[i % len(experts)]
            self.report([
                {"eventCode": "expert_summoned", "id": e["id"], "name": e["name"],
                 "type": "agent", "expertTitle": e.get("profession", ""),
                 "expertType": "agent"},
                {"eventCode": "expert_actual_use", "id": e["id"], "name": e["name"],
                 "type": e.get("industryId", "") or "", "expertType": "agent",
                 "source": "builtin", "version": "", "cost": 5, "characterCount": 30,
                 "requestId": str(uuid.uuid4()), "messageId": "cmb-" + str(uuid.uuid4()),
                 "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2"},
            ])
            self._sleep(3)
        self._mark("expert_5", *self.prog("expert_5"))

    def t_team_3(self) -> None:
        if self._finished("Expert_team_use_3"):
            return
        teams = self._experts("teams") or self._experts("teamExperts")
        for i in range(3):
            if self._finished("Expert_team_use_3"):
                break
            team = teams[i % len(teams)]
            prompt = "你好，请简单介绍一下你们团队能帮我做什么，回答OK即可"
            req_id, msg_id = str(uuid.uuid4()), "cmb-" + str(uuid.uuid4())
            conv_id, text = "", ""
            if self.chat_tasks:
                meta = {"codebuddy.ai": {
                    "growthEvent": json.dumps([{
                        "eventCode": "ExpertActualUse", "id": team["id"],
                        "extra": {"name": team["name"], "expertTitle": team.get("profession", ""),
                                  "type": team.get("industryId", "") or "", "expertType": "team",
                                  "source": "builtin", "version": "", "cost": 8,
                                  "characterCount": len(prompt), "requestId": req_id,
                                  "messageId": msg_id, "requestModelId": "glm-5.2",
                                  "requestModelName": "GLM-5.2"},
                        "expertType": "team"}], ensure_ascii=False),
                    "promptRequestId": req_id, "clientSendTime": int(time.time() * 1000),
                    "userId": self.uid, "mode": "craft", "model": "glm-5.2",
                    "expertId": team["id"],
                    "expert": {"id": team["id"], "name": team["name"],
                               "profession": team.get("profession", ""), "prompt": prompt[:50]},
                    "tags": ["expert:" + team["id"]]}}
                conv_id, text = self.webchat(prompt, "team", meta=meta)
            self.report([{
                "eventCode": "expert_actual_use", "id": team["id"], "name": team["name"],
                "expertTitle": team.get("profession", ""),
                "type": team.get("industryId", "") or "", "expertType": "team",
                "source": "builtin", "version": "", "cost": 8,
                "characterCount": len(prompt), "conversationId": conv_id,
                "requestId": req_id, "messageId": msg_id,
                "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2",
            }])
            self._sleep(5)
        self._mark("Expert_team_use_3", *self.prog("Expert_team_use_3"))

    def t_template_5(self) -> None:
        scenes = [
            {"id": "01-ProductDesign", "name": "产品设计"},
            {"id": "02-Marketing", "name": "营销文案"},
            {"id": "03-DataAnalysis", "name": "数据分析"},
            {"id": "04-CodeReview", "name": "代码审查"},
            {"id": "05-Report", "name": "报告撰写"},
        ]
        for i, sc in enumerate(scenes):
            if self._finished("template_5"):
                break
            tid = sc["id"]
            self.report([
                {"eventCode": "agent_task_created", "source": "CLOUD", "name": "", "mode": "craft",
                 "requestModelId": "default", "action": tid, "has_template": True,
                 "template_id": tid, "template_name": sc["name"]},
                {"eventCode": "agent_task_created_with_template", "templateId": tid,
                 "templateName": sc["name"], "isCustomModel": True, "id": tid, "name": sc["name"]},
                {"eventCode": "playbook_prompt_send", "ext1": str(uuid.uuid4()),
                 "requestId": str(uuid.uuid4()), "id": tid, "name": sc["name"], "type": "other",
                 "promptLength": 30, "isOfficial": 1, "source": "growth-center"},
            ])
            self._sleep(2)
        self._mark("template_5", *self.prog("template_5"))

    def t_black_cat(self) -> None:
        if self._finished("black_cat"):
            return
        hour = time.localtime().tm_hour
        if not (hour >= 23 or hour < 8):
            self._say(f"   夜猫子活动: 仅 23:00-08:00 计数，当前 {hour} 点，跳过")
            return
        st, cur, tgt = self.prog("black_cat")
        need = max(0, int(tgt or 3) - int(cur or 0))
        if not self.chat_tasks:
            return
        for i in range(need):
            conv_id, text = self.webchat(
                ["今天天气怎么样？", "1+1等于几？", "讲个笑话"][i % 3], "night")
            if text:
                self.report(self._chat_events(conv_id, "聊天", text))
            self._sleep(5)
            if self._finished("black_cat"):
                break
        self._mark("black_cat", *self.prog("black_cat"))

    # ------------------------------------------------------------------ 互动玩法

    def t_lottery(self) -> None:
        try:
            status, resp = self._req("GET", "/v2/activity/growth/lottery/chances")
            d = resp.get("data") or {} if isinstance(resp, dict) else {}
            chances = int(d.get("balance") or d.get("chances") or d.get("remaining") or 0)
            if chances <= 0:
                self._say("   抽奖: 无次数")
                return
            won = []
            for i in range(min(chances, 20)):
                if i:
                    self._sleep(2)
                _, rr = self._req("POST", "/v2/activity/growth/lottery/draw",
                                  {"client_token": "draw-" + str(uuid.uuid4())})
                if isinstance(rr, dict) and rr.get("code") == 0:
                    pd = rr.get("data") or {}
                    won.append(str(pd.get("prize_name") or pd.get("name") or "?"))
                else:
                    break
            self._say(f"   抽奖: {'、'.join(won) if won else '无结果'}")
        except Exception as exc:  # noqa: BLE001
            self._say(f"   抽奖异常: {str(exc)[:50]}")

    def t_blindbox(self) -> None:
        try:
            _, q = self._req("GET", "/v2/activity/growth/buddy/quota")
            qd = q.get("data") or {} if isinstance(q, dict) else {}
            affordable = int(qd.get("affordable") or 0)
            if affordable <= 0:
                self._say(f"   盲盒: 能量不足 ({qd.get('balance', '?')}/10)")
                return
            got = []
            for _ in range(min(affordable, 5)):
                _, rr = self._req("POST", "/v2/activity/growth/buddy/open", {"count": 1})
                if not (isinstance(rr, dict) and rr.get("code") == 0):
                    break
                for it in (rr.get("data") or {}).get("results", []):
                    ins = it.get("instance") or {}
                    tpl = it.get("template") or {}
                    got.append(f"{ins.get('name') or tpl.get('name') or '?'}"
                               f"({ins.get('rarity') or tpl.get('rarity') or ''})")
                self._sleep(1.5)
            self._say(f"   盲盒: {'、'.join(got) if got else '开启失败'}")
        except Exception as exc:  # noqa: BLE001
            self._say(f"   盲盒异常: {str(exc)[:50]}")

    def t_buddy_info(self) -> None:
        _, r = self._req("GET", "/v2/activity/growth/buddy/info")
        if isinstance(r, dict) and r.get("code") == 0:
            b = (r.get("data") or {}).get("buddy") or r.get("data") or {}
            self._say(f"   Buddy: {b.get('name', '?')} ({b.get('rarity', '')})")

    def t_travel(self) -> None:
        try:
            _, vis = self._req("GET", "/v2/activity/growth/buddy/visible")
            vd = vis.get("data") or {} if isinstance(vis, dict) else {}
            if isinstance(vd, dict) and vd and (not vd.get("buddy_visible", True) or not vd.get("has_buddy", True)):
                self._say("   派猫猫旅行: 无 Buddy，跳过")
                return
            _, st = self._req("GET", "/v2/activity/growth/buddy/travel/status")
            if not (isinstance(st, dict) and st.get("code") == 0):
                self._say("   派猫猫旅行: 状态获取失败")
                return
            sd = st.get("data") or {}
            state = sd.get("state", "idle")
            if state == "arrived":
                _, rr = self._req("POST", "/v2/activity/growth/buddy/travel/claim", {})
                if isinstance(rr, dict) and rr.get("code") == 0:
                    self._say(f"   派猫猫旅行: 领取礼物 +{(rr.get('data') or {}).get('reward_credit', 0)}积分")
                else:
                    self._say(f"   派猫猫旅行: 领取失败 {(rr or {}).get('msg', '')}")
                return
            if state == "traveling":
                remain = max(0, (int(sd.get("arrive_at") or 0) - int(sd.get("server_now") or 0)) // 60)
                self._say(f"   派猫猫旅行: 旅行中，约 {remain} 分钟后到达")
                return
            if sd.get("daily_limit_reached"):
                self._say("   派猫猫旅行: 今日次数已用尽")
                return
            _, cfg = self._req("GET", "/v2/activity/growth/buddy/travel/config")
            locs = (cfg.get("data") or {}).get("locations", []) if isinstance(cfg, dict) else []
            if not locs:
                self._say("   派猫猫旅行: 无目的地")
                return
            _, rr = self._req("POST", "/v2/activity/growth/buddy/travel/depart",
                              {"location_id": locs[0].get("id")})
            if isinstance(rr, dict) and rr.get("code") == 0:
                rd = rr.get("data") or {}
                hours = max(0, (int(rd.get("arrive_at") or 0) - int(rd.get("server_now") or 0)) // 3600)
                self._say(f"   派猫猫旅行: 已出发，约 {hours} 小时后到达（下次自动领取）")
            else:
                self._say(f"   派猫猫旅行: 出发失败 {(rr or {}).get('msg', '')}")
        except Exception as exc:  # noqa: BLE001
            self._say(f"   派猫猫旅行异常: {str(exc)[:50]}")

    def t_redeem(self, streak_days: int | None = None) -> None:
        for tier, need, label in (("7d", 7, "入门"), ("14d", 14, "进阶"), ("28d", 28, "巅峰")):
            if isinstance(streak_days, int) and streak_days < need:
                continue
            _, rr = self._req("POST", "/v2/activity/growth/redeem",
                              {"tier": tier, "client_token": f"redeem-{tier}-{uuid.uuid4()}"})
            if isinstance(rr, dict) and rr.get("code") == 0:
                d = rr.get("data") or {}
                self._say(f"   连签兑换{label}档: +{d.get('credit_granted', 0)}积分 "
                          f"+{d.get('energy_granted', 0)}能量 +{d.get('chances_granted', 0)}抽奖")
            elif isinstance(rr, dict) and rr.get("code") == 409:
                self._say(f"   连签兑换{label}档: 已兑换过")

    def t_makeup(self) -> None:
        """昨天漏签且有补签卡 → 自动补签，保住连签。"""
        try:
            _, hm = self._req("GET", "/v2/activity/growth/heatmap", timeout=20)
            cells = (hm.get("data") or {}).get("cells", []) if isinstance(hm, dict) else []
            _, sr = self._req("GET", "/v2/activity/growth/streak")
            bal = ((sr.get("data") or {}).get("makeup_cards") or {}).get("balance", 0) if isinstance(sr, dict) else 0
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            missed = None
            for c in cells:
                if not isinstance(c, dict):
                    continue
                if str(c.get("date", ""))[:10] == yesterday and not c.get("score", 0):
                    missed = yesterday
                    break
            if not missed:
                self._say("   补签: 无漏签，无需补签")
                return
            if not bal:
                self._say(f"   补签: 昨日({missed})漏签但没有补签卡")
                return
            _, rr = self._req("POST", "/v2/activity/growth/makeup-cards/use", {"target_date": missed})
            self._say(f"   补签{missed}: {'成功，连签保住' if isinstance(rr, dict) and rr.get('code') == 0 else str((rr or {}).get('msg', ''))[:40]}")
        except Exception as exc:  # noqa: BLE001
            self._say(f"   补签检查异常: {str(exc)[:50]}")

    def t_gift(self) -> None:
        for path, name in (("/billing/meter/claim-gift", "新手礼包"),
                           ("/billing/meter/claim-compensation", "补偿领取")):
            _, r = self._req("POST", path, {}, timeout=15)
            if isinstance(r, dict) and r.get("code") == 0:
                self._say(f"   {name}: +{(r.get('data') or {}).get('credit', '?')}积分")

    def t_first_buddy(self) -> None:
        if self._finished("first_buddy"):
            return
        _, r = self._req("POST", "/v2/activity/growth/buddy/first", {})
        self._say(f"   首只Buddy: {'成功' if isinstance(r, dict) and r.get('code') == 0 else str((r or {}).get('msg', ''))[:40]}")

    def t_badges(self) -> None:
        _, r = self._req("GET", "/v2/activity/growth/badges")
        if isinstance(r, dict) and r.get("code") == 0:
            badges = (r.get("data") or {}).get("badges") or (r.get("data") or {}).get("list") or []
            self._say(f"   徽章: {sum(1 for b in badges if isinstance(b, dict) and b.get('earned'))} 个")

    def t_unknown(self) -> None:
        known = set(TASK_NAME_CN)
        for t in self.list_tasks():
            code = t.get("task_code", "")
            if code in known or t.get("accept_status") in ("claimed", "completed"):
                continue
            desc = str(t.get("task_desc", ""))[:40]
            self._say(f"   未覆盖的新任务: {code} {t.get('title', '')} {desc}（可在 WorkBuddy 里手动完成）")

    # ------------------------------------------------------------------ 编排

    def run(self) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "message": "缺少 accessToken", "lines": self.lines}
        if not self.uid:
            self.uid = jwt_field(self.token, "sub")

        # 成长概览 + 连签天数（兑换档位要用）
        streak_days = None
        try:
            _, prof = self._req("GET", "/v2/activity/growth/profile", timeout=25)
            _, sr = self._req("GET", "/v2/activity/growth/streak", timeout=25)
            _, en = self._req("GET", "/v2/activity/growth/energy", timeout=25)
            streak_days = ((sr.get("data") or {}).get("streak") or {}).get("days") if isinstance(sr, dict) else None
            self._say(
                f"成长: 等级{(prof.get('data') or {}).get('level', '?')} "
                f"连签{streak_days}天 能量{(en.get('data') or {}).get('balance', '?')}"
            )
        except Exception:
            pass

        steps: list[tuple[str, Callable[[], None]]] = [
            ("accept", self.accept_all),
            ("sign", self.t_sign),
            ("team_3", self.t_team_3),
            ("buddy_apps", self.t_buddy_apps),
            ("theme", self.t_theme),
            ("library", self.t_library),
            ("canvas", self.t_canvas_automation),
            ("expert_5", self.t_expert_5),
            ("template_5", self.t_template_5),
            ("chat", lambda: (
                self.chat_n("Model_chat_GLM5.2", 1, ["你好，请介绍一下你自己"]),
                self.chat_n("chat_5", 5, ["你好", "今天天气怎么样？", "1+1等于几？", "Python是什么？", "推荐一本好书"]),
            )),
            ("black_cat", self.t_black_cat),
            ("badges", self.t_badges),
            ("lottery", self.t_lottery),
            ("blindbox", self.t_blindbox),
            ("buddy_info", self.t_buddy_info),
            ("travel", self.t_travel),
            ("redeem", lambda: self.t_redeem(streak_days)),
            ("gift", self.t_gift),
            ("first_buddy", self.t_first_buddy),
            ("makeup", self.t_makeup),
            ("unknown", self.t_unknown),
        ]
        for name, fn in steps:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.failed.append(name)
                self._say(f"   [{name}] 执行异常: {str(exc)[:80]}")

        # 领奖：所有已完成的都领一遍
        self._say("   ── 领奖 ──")
        claimed = 0
        for t in self.list_tasks():
            if isinstance(t, dict) and t.get("accept_status") == "completed":
                code = str(t.get("task_code") or "")
                if code:
                    try:
                        self.claim(code)
                        claimed += 1
                    except Exception:
                        pass
                self._sleep(0.8)
        if not claimed:
            self._say("   无待领奖励")

        tasks = self.list_tasks()
        total = len(tasks)
        done = sum(1 for t in tasks if t.get("accept_status") in ("claimed", "completed"))
        self.rest = [task_cn(t.get("task_code", "")) for t in tasks
                     if t.get("accept_status") not in ("claimed", "completed")]
        manual = sorted({task_cn(c) for c in DESKTOP_ONLY if c in {t.get("task_code") for t in tasks}}
                        & set(self.rest))
        self._say(f"完成 {done}/{total} 项" + (f"；剩余：{', '.join(self.rest)}" if self.rest else "；全部完成"))
        if manual:
            self._say(f"其中需在本机 WorkBuddy 客户端手动完成：{', '.join(manual)}")
        return {
            "ok": True,
            "provider": "workbuddy",
            "done": done,
            "total": total,
            "rest": self.rest,
            "manual": manual,
            "failed": self.failed,
            "lines": self.lines,
            "message": f"成长任务完成 {done}/{total}" + (f"，剩余 {len(self.rest)} 项" if self.rest else ""),
        }


def run_daily_tasks(
    token_blob: dict[str, Any],
    *,
    log: LogFn | None = None,
    chat_tasks: bool = True,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """从账号的 token_blob 直接跑一轮成长任务。"""
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    if not token:
        return {"ok": False, "message": "token_blob 缺少 access_token"}
    runner = WorkBuddyTaskRunner(
        token,
        uid=str(token_blob.get("uid") or ""),
        nickname=str(token_blob.get("nickname") or ""),
        log=log,
        chat_tasks=chat_tasks,
        timeout=timeout,
    )
    return runner.run()


def _safe_json(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return {"msg": str(raw)[:200]}


def _read(exc: HTTPError) -> bytes:
    try:
        return exc.read()
    except Exception:
        return b""
