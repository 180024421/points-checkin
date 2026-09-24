# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .license_client import data_root
from .redact import mask_text
from .secure_storage import load_json, save_json
from . import server_client  # 代跑账号 / 签到记录 / 代挂权益

ACCOUNTS_FILE = data_root() / "accounts.json"
RUN_LOG_FILE = data_root() / "run_log.json"
LIVE_LOG_FILE = data_root() / "live_log.json"
CREDIT_HISTORY_FILE = data_root() / "credit_history.json"

# Constants for log and history limits
MAX_RUN_LOGS = 1000
MAX_LIVE_LOGS = 800
MAX_CREDIT_HISTORY_ITEMS = 2000

DEFAULT_RUN_LOGS_LIMIT = 200
DEFAULT_LIVE_LOGS_LIMIT = 300
DEFAULT_CREDIT_HISTORY_LIMIT = 200

# UI 线程与后台线程（自动采集 / 调度）都会读改写这些文件，
# 锁保证 read-modify-write 不丢数据（例如两个账号同时入库）。
_IO_LOCK = threading.RLock()

# 服务端代跑账号列表的进程内缓存：load_accounts() 被调用得非常频繁
# （每次入库、每块看板渲染都会调），每次都打一次 HTTP 会把 _IO_LOCK 占住。
_SERVER_TTL_SEC = 15.0
_server_cache: tuple[float, list[dict[str, Any]]] | None = None
_server_cache_lock = threading.Lock()

# 服务器聚合签到数据同样每块看板都要读一次，Tk 主线程 15 秒刷一次，
# 不打缓存会让界面在网络差的时候直接卡住。
_AGG_TTL_SEC = 15.0
_agg_cache: tuple[float, list[dict[str, Any]]] | None = None
_agg_cache_lock = threading.Lock()

# 代挂额度 / 联系邮箱绑定状态。更新频率极低，但界面每次刷新都要读，
# 而且额度校验发生在入库路径上，所以必须走缓存、且 HTTP 不能落在 _IO_LOCK 里。
_ENT_TTL_SEC = 60.0
_ent_cache: tuple[float, dict[str, Any]] | None = None
_ent_cache_lock = threading.Lock()
_ent_last_error: str | None = None


def _warn(message: str) -> None:
    """EXE 是无控制台窗口启动的，print 看不到；告警统一进应用日志。"""
    text = mask_text(message, 500)
    try:
        append_live_log(text)
    except Exception:  # noqa: BLE001 - 日志本身不能再抛
        pass
    print(text)


def invalidate_server_cache() -> None:
    """服务器侧数据（代跑账号 / 签到记录）变化后调用，下次读取重新拉取。"""
    global _server_cache, _agg_cache
    with _server_cache_lock:
        _server_cache = None
    with _agg_cache_lock:
        _agg_cache = None


def invalidate_entitlement_cache() -> None:
    global _ent_cache
    with _ent_cache_lock:
        _ent_cache = None


def refresh_entitlement(*, force: bool = False) -> dict[str, Any]:
    """拉取代挂权益（额度 + 邮箱绑定状态），带 TTL 缓存。

    必须在 ``_IO_LOCK`` 之外调用：里面是 HTTP 请求。失败时返回上次结果或 ``{}``，
    调用方按「未知」处理，不因为网络抖动阻断用户。
    """
    global _ent_cache, _ent_last_error
    now = time.monotonic()
    if not force:
        with _ent_cache_lock:
            if _ent_cache is not None and now - _ent_cache[0] < _ENT_TTL_SEC:
                return dict(_ent_cache[1])
    message = ""
    try:
        resp = server_client.entitlement_info()
        if resp.get("ok"):
            data = dict(resp.get("data") or {})
            with _ent_cache_lock:
                _ent_cache = (now, data)
            _ent_last_error = None
            return data
        message = str(resp.get("message") or "未知错误")
    except Exception as exc:  # noqa: BLE001 - 服务端不可达时退回缓存
        message = str(exc)
    # 失败也推进时间戳，否则断网时每次读都会重打一次 HTTP
    with _ent_cache_lock:
        _ent_cache = (now, dict(_ent_cache[1]) if _ent_cache else {})
        stale = dict(_ent_cache[1])
    if message != _ent_last_error:
        _ent_last_error = message
        _warn(f"获取代挂额度失败：{message}")
    return stale


def get_entitlement() -> dict[str, Any]:
    """只读缓存，不发请求（供持锁路径调用）。"""
    with _ent_cache_lock:
        return dict(_ent_cache[1]) if _ent_cache else {}


def contact_binding() -> dict[str, Any]:
    """联系邮箱绑定状态：{verified, email}。verified=None 表示未知（离线/未取到）。"""
    ent = get_entitlement()
    return {"verified": ent.get("contactVerified"), "email": ent.get("contactEmail") or ""}


def contact_notice(*, force: bool = True) -> str | None:
    """「建议绑定联系邮箱」的一句话引导；None = 不必提示（已绑定 / 状态未知）。

    **这不是门槛**，任何调用方都不许因为它中断操作。邮箱只决定服务端在站内消息之外
    要不要再发一封邮件（``CheckinNotifier.notifyIssue`` 站内必发）；旧实现把
    ``verified is False`` 拦在挂载/上传之前，等于用一条提醒渠道堵死整条代跑链路 ——
    现网 4 条授权里 0 条绑过邮箱，服务端也因此删掉了同一句 400（run-jane c1f0d49）。

    服务端不可达时 ``verified`` 为 None，同样不提示：额度与绑定最终由服务端裁决，
    本机不该因为网络抖动反复骚扰用户。
    """
    refresh_entitlement(force=force)
    if contact_binding().get("verified") is False:
        return "建议绑定联系邮箱：账号签到异常时除站内提醒外还能收到邮件通知（设置页可绑，不绑定也能代跑）"
    return None


def _aggregated_runs_cached() -> list[dict[str, Any]]:
    global _agg_cache
    now = time.monotonic()
    with _agg_cache_lock:
        if _agg_cache is not None and now - _agg_cache[0] < _AGG_TTL_SEC:
            return list(_agg_cache[1])
    try:
        rows = server_client.fetch_aggregated_checkin_data()
    except Exception as exc:  # noqa: BLE001 - 服务器不可达时退回上次结果
        _warn(f"获取服务器签到聚合数据失败：{exc}")
        with _agg_cache_lock:
            return list(_agg_cache[1]) if _agg_cache else []
    with _agg_cache_lock:
        _agg_cache = (now, list(rows))
    return list(rows)


def _server_accounts_cached() -> list[dict[str, Any]]:
    global _server_cache
    now = time.monotonic()
    with _server_cache_lock:
        if _server_cache is not None and now - _server_cache[0] < _SERVER_TTL_SEC:
            return list(_server_cache[1])
    try:
        rows = server_client.list_server_accounts()
    except Exception as exc:  # noqa: BLE001 - 服务器不可达时用上次结果兜底
        _warn(f"获取服务器代跑账号失败：{exc}")
        with _server_cache_lock:
            return list(_server_cache[1]) if _server_cache else []
    with _server_cache_lock:
        _server_cache = (now, list(rows))
    return list(rows)


def _enabled_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("enabled", True))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def resolve_account_quota() -> dict[str, Any]:
    """坐席额度三态解析：``{limit, known, source}``。**只读缓存，不打 HTTP**（持锁路径可调）。

    - ``source="server"``：``/entitlement/info`` 的 ``quota``（对应
      ``checkin_entitlement.account_quota``，由服务端 ``reconcileCapacity`` 反写）。
      **0 是有效额度**（坐席自然到期 = 0 个），不能再当「不限」放行 ——
      现网 8 条权益行实测 quota 全 > 0（最小 1），所以把 0 认作已知额度不会误伤任何现有用户。
    - ``source="cache"``：服务端不可达时，退回授权状态里的 ``accountLimit``。
      它在服务端是按「设备座位数」校验的，语义不同，只能当离线兜底。
    - ``known=False``：两个来源都没有值。此时**拒绝新增挂载，但绝不停用/清空已有账号**
      —— 旧实现把「取不到」并进来当成「不限」，任何一次授权抖动都把额度闸门打开。
    """
    raw = get_entitlement().get("quota")
    if raw is not None:
        try:
            return {"limit": int(raw), "known": True, "source": "server"}
        except (TypeError, ValueError) as e:
            _warn(f"代挂额度不是数字：{raw}（{e}）")
    try:
        from .license_client import load_cache

        limit = _quota_number(load_cache().get("accountLimit"), "accountLimit")
        if limit is not None:
            return {"limit": limit, "known": True, "source": "cache"}
    except Exception as e:  # noqa: BLE001
        _warn(f"读取账号上限失败：{e}")
    return {"limit": None, "known": False, "source": ""}


def _quota_number(raw: Any, label: str) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        _warn(f"{label} 不是数字：{raw}")
        return None
    # 0 留给调用方判断（= 已知额度为 0）；负数才是「未配置/不限」
    return value if value >= 0 else None


def get_account_usage(*, accounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """供界面展示：{limit, quotaKnown, quotaSource, used, remain, planLabel,
    expireAt, expireAtIso, expireDaysLeft, timeUnlimited, contactVerified, contactEmail}。

    ``limit=None`` 只表示**额度未知**（不是「不限」），界面要用 ``quotaKnown`` 区分。

    会先刷一次代挂权益（HTTP 在此处、锁外发起），保证界面显示的是服务端额度。
    ``used`` 取本机「启用中的账号数」——这才是本地额度校验的口径；服务端 ``used``
    只统计它那边的代挂记录（不含纯本机账号），拿来显示会和拦截提示自相矛盾。

    ``accounts`` 让调用方把已经算好的合并视图传进来，一次界面刷新不必合并三遍。
    """
    refresh_entitlement()
    quota = resolve_account_quota()
    limit = quota["limit"]
    used = 0
    plan_label = ""
    expire_at = None
    ent = get_entitlement()
    try:
        from .license_client import load_cache

        cache = load_cache()
        plan_label = str(cache.get("accountPlanLabel") or cache.get("planLabel") or "")
        expire_at = ent.get("expireAt") or cache.get("expireAt")
    except Exception as e:  # noqa: BLE001
        _warn(f"读取授权缓存中的账号用量失败：{e}")

    try:
        rows = load_accounts() if accounts is None else accounts
        used = sum(1 for a in rows if a.get("enabled", True))
    except Exception as e:  # noqa: BLE001
        _warn(f"统计已用账号数失败：{e}")
        used = 0
    # 额度三态要能被界面区分：limit=None 有两种含义（未知 / 不限），
    # 光靠一个字段说不清，界面上「不限账号」和「离线取不到额度」是两回事。
    days_left: int | None = None
    expire_iso = ""
    if not ent.get("timeUnlimited"):
        # expireAt 线上是 epoch 毫秒、src 那代是 ISO 字符串，_parse_iso 两种都吃
        from .license_client import _parse_iso

        dt = _parse_iso(expire_at)
        if dt:
            expire_iso = dt.isoformat()
            days_left = max((dt - datetime.now(timezone.utc)).days, 0)
    return {
        "limit": limit,
        "quotaKnown": quota["known"],
        "quotaSource": quota["source"],
        "used": used,
        "remain": None if limit is None else max(limit - used, 0),
        "planLabel": plan_label,
        "expireAt": expire_at,
        "expireAtIso": expire_iso,
        "expireDaysLeft": days_left,
        "timeUnlimited": bool(ent.get("timeUnlimited")),
        "contactVerified": ent.get("contactVerified"),
        "contactEmail": ent.get("contactEmail") or "",
    }



def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _merge_rows(local_accounts: list[dict[str, Any]], server_accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本地账号 + 服务器代跑记录的合并视图（每个账号只出现一行，本机 id 为准）。

    旧实现是按 id 直接覆盖：服务器记录（自增整数 id）和本机记录（uuid）是两条，
    于是同一个代挂账号在列表里出现两次，而且服务器整行会被写进 accounts.json，
    服务端删号后就留下幽灵行。这里按 ``clientAccountId`` → provider+identity 对回
    同一条，只把服务端的运行状态覆盖进来；对不上的（例如别机上传的）单独列一行，
    并打上 ``source=server``，由 ``save_accounts`` 挡在本地文件之外。
    """
    server_ids = {str(r.get("server_account_id") or r.get("id") or "") for r in server_accounts}
    server_ids.discard("")
    rows = _drop_server_sourced(
        [r for r in local_accounts if str(r.get("id") or "") not in server_ids]
    )
    out: list[dict[str, Any]] = list(rows)
    by_client_id = {str(r.get("id")): r for r in out}
    by_identity = {}
    for row in out:
        key = account_identity_key(row)
        if key.split(":", 1)[-1]:
            by_identity.setdefault(key, row)
    for srv in server_accounts:
        twin = by_client_id.get(str(srv.get("client_account_id") or "")) or by_identity.get(account_identity_key(srv))
        if twin is None:
            orphan = dict(srv)
            orphan["id"] = f"srv:{srv.get('server_account_id') or srv.get('id')}"
            orphan["source"] = "server"  # 不依赖上游打标：合并视图里的纯服务器行一律不落盘
            orphan["run_mode"] = "server"
            out.append(orphan)
            continue
        for key in ("enabled", "last_ok_at", "last_error"):
            if srv.get(key) is not None:
                twin[key] = srv[key]
        twin["run_mode"] = "server"  # 服务器确实在代跑这个账号
        twin["server_account_id"] = srv.get("server_account_id") or srv.get("id")
        if not twin.get("label") and srv.get("label"):
            twin["label"] = srv["label"]
    return out


_purged_server_rows = 0

# 本机账号 id 一律是 uuid4（见 upsert_account），而 checkin_bound_account.id 是服务端自增整数。
# 因此「纯数字 id + run_mode=server」只可能是旧版本误落盘的代跑记录。
_SERVER_GHOST_ID_RE = re.compile(r"^\d+$")


def _is_server_sourced(row: dict[str, Any]) -> bool:
    if row.get("source") == "server":
        return True
    return bool(_SERVER_GHOST_ID_RE.match(str(row.get("id") or ""))) and str(row.get("run_mode") or "") == "server"


def _drop_server_sourced(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剔除历史版本误写进 accounts.json 的服务器代跑记录（本机不产生这类行）。"""
    global _purged_server_rows
    kept, purged = [], 0
    for row in rows:
        if _is_server_sourced(row):
            purged += 1
        else:
            kept.append(row)
    if purged:
        _purged_server_rows += purged
        _warn(f"清理了 {purged} 条被误存进本机的服务器代跑记录（累计 {_purged_server_rows} 条），下次保存不再写回")
    return kept


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    """公开写入口：写本地账号文件，自己持锁（调度器/界面都在锁外直接调用）。

    服务器代跑记录（``source=server``，或旧版本误落盘的服务端自增 id）不落盘，
    否则服务端删号后本机永远留着一条幽灵账号。
    """
    with _IO_LOCK:
        rows = [r for r in accounts if not _is_server_sourced(r)]
        save_json(ACCOUNTS_FILE, {"accounts": rows, "updatedAt": _now()})


def _load_local_accounts() -> list[dict[str, Any]]:
    with _IO_LOCK:
        data = load_json(ACCOUNTS_FILE, {"accounts": []})
        return list(data.get("accounts") or [])


def load_accounts(*, include_server: bool = True) -> list[dict[str, Any]]:
    """本地账号 +（默认）服务器代跑账号合并视图。

    网络请求放在 `_IO_LOCK` 之外，且带 TTL 缓存：以前每次读账号都在持锁状态下
    打一次 HTTP，入库 N 个账号 = 卡 N 次往返，调度线程与 UI 会互相阻塞。
    """
    local_accounts = _load_local_accounts()
    if not include_server:
        return local_accounts
    return _merge_rows(local_accounts, _server_accounts_cached())


def update_account(account_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any] | None]) -> bool:
    """在同一把 ``_IO_LOCK`` 里读-改-写一行本地账号，命中并改动返回 True。

    以前各处写法是 ``load_accounts()`` → 改一行 → ``save_accounts()`` 整表覆盖：
    读和写之间锁已经放开，自动同步写 ``last_credits`` 与签到写 ``token_blob`` 一交错，
    后写者就会拿旧快照把对方刚续期好的 token 覆盖回去。HTTP 依旧留在锁外，
    这里只读本地文件。``mutate`` 收到当前行，返回要合并的字段（None/空 = 不改）。
    """
    with _IO_LOCK:
        rows = _load_local_accounts()
        for row in rows:
            if str(row.get("id")) != str(account_id):
                continue
            patch = mutate(row)
            if not isinstance(patch, dict) or not patch:
                return False
            row.update(patch)
            save_accounts(rows)
            return True
        return False


def account_identity_key(account: dict[str, Any]) -> str:
    provider = str(account.get("provider") or "").strip()
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    uid = str(
        account.get("identity")
        or blob.get("uid")
        or blob.get("user_id")
        or blob.get("nickname")
        or account.get("label")
        or account.get("id")
        or ""
    ).strip()
    return f"{provider}:{uid}"


class QuotaBlocked(ValueError):
    """额度不足 / 额度未知导致的入库拒绝。

    继承 ``ValueError`` 是有意的：现存 13 个调用点里有若干 ``except ValueError``
    与批量入库的兜底逻辑，换成全新基类会让它们静默失效。
    界面要的是能直接渲染的结构，不是让人去 parse 中文句子 —— 所以带上 code/seats/used。
    """

    def __init__(self, code: str, message: str, *, seats: int | None = None, used: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.seats = seats
        self.used = used

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": str(self),
            "seats": self.seats,
            "used": self.used,
        }


def try_upsert_account(account: dict[str, Any]) -> dict[str, Any]:
    """给界面与批量路径用的入库：把额度拦截转成结构化结果，**不抛异常**。

    ``upsert_account`` 返回的是账号本身，被额度拦住时抛 :class:`QuotaBlocked`；
    调用点分散在 13 处（含批量采集循环），逐个 try/except 既啰嗦又容易漏 ——
    漏掉的那处会让整个批量任务中途 abort，留下「一半入库」的脏状态。
    这里统一成 ``{ok, account | code, message, seats, used}``，批量循环逐条收集即可。
    """
    try:
        return {"ok": True, "message": "", "account": upsert_account(account)}
    except QuotaBlocked as exc:
        return exc.as_result()
    except Exception as exc:  # noqa: BLE001 - 存储层异常同样转成结果，不能冒到 JS 侧
        _warn(f"账号入库失败：{exc}")
        return {"ok": False, "code": "STORE_FAILED", "message": f"账号入库失败：{exc}"}


def upsert_account(account: dict[str, Any]) -> dict[str, Any]:
    # 合并视图和代挂额度都要打 HTTP，必须在锁外先取好；
    # 持锁期间只做本地文件的读写，否则一次入库会把所有界面/调度线程一起挂住。
    server_rows = _server_accounts_cached()
    refresh_entitlement()
    with _IO_LOCK:
        return _upsert_account(account, server_rows)


def _upsert_account(account: dict[str, Any], server_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    accounts = _merge_rows(_load_local_accounts(), server_rows or [])
    identity = account_identity_key(account)
    account["identity"] = identity.split(":", 1)[-1] if ":" in identity else identity
    account["updatedAt"] = _now()
    if "run_mode" not in account:
        account["run_mode"] = "local"
    if "createdAt" not in account:
        account["createdAt"] = _now()

    # prefer match by explicit id, else by provider+identity (多号采集不互相覆盖错误对象)
    account_id = str(account.get("id") or "").strip()
    idx = -1
    if account_id:
        for i, row in enumerate(accounts):
            if str(row.get("id")) == account_id:
                idx = i
                break
    if idx < 0:
        for i, row in enumerate(accounts):
            if account_identity_key(row) == identity and identity.endswith(":") is False and identity:
                # empty identity after provider: skip
                if identity.split(":", 1)[-1]:
                    idx = i
                    account_id = str(row.get("id"))
                    break
    if not account_id:
        account_id = str(uuid.uuid4())
    account["id"] = account_id

    quota = resolve_account_quota()
    account_limit = quota["limit"] if quota["known"] else None
    if idx >= 0:
        was_enabled = bool(accounts[idx].get("enabled", True))
        merged = {**accounts[idx], **account}
        accounts[idx] = merged
        account = merged
        # 从禁用切回启用也要占用额度（额度按「启用中的账号数」计）
        # 额度未知时**不拦**：这是一条已经存在的记录，用户只是把它重新打开
        if not was_enabled and bool(merged.get("enabled", True)) and account_limit is not None:
            enabled_now = _enabled_count(accounts)
            if enabled_now > account_limit:
                raise QuotaBlocked(
                    "QUOTA_EXCEEDED",
                    f"超出账号数量上限：启用后将有 {enabled_now} 个，"
                    f"当前套餐仅支持 {account_limit} 个（升级套餐可增加）",
                    seats=account_limit,
                    used=enabled_now,
                )
    else:
        # 停用账号不占额度（额度按「启用中的账号数」计），否则额度满时连备份都存不进
        if bool(account.get("enabled", True)):
            if not quota["known"]:
                # 拿不到额度 ≠ 不限：拒绝新增，但上面那条「已有记录」的路径照常放行
                raise QuotaBlocked(
                    "QUOTA_UNKNOWN",
                    "暂时取不到套餐额度（离线或授权服务未响应），无法确认还能挂几个号。"
                    "已有账号不受影响；请联网后重试，或在设置页重新激活卡密。",
                    seats=None,
                    used=_enabled_count(accounts),
                )
            enabled_now = _enabled_count(accounts)
            if account_limit is not None and enabled_now >= account_limit:
                raise QuotaBlocked(
                    "QUOTA_EXCEEDED",
                    f"超出账号数量上限：当前已启用 {enabled_now} 个，"
                    f"套餐上限 {account_limit} 个（升级套餐或先停用部分账号）",
                    seats=account_limit,
                    used=enabled_now,
                )
        accounts.append(account)
    
    # Ensure tags is a list
    if "tags" not in account or not isinstance(account["tags"], list):
        account["tags"] = []

    # Handle user_tag from token_blob if present
    token_blob = account.get("token_blob")
    if isinstance(token_blob, dict):
        user_tag_from_blob = token_blob.get("user_tag")
        if user_tag_from_blob and isinstance(user_tag_from_blob, str) and user_tag_from_blob not in account["tags"]:
            account["tags"].append(user_tag_from_blob)
    
    save_accounts(accounts)
    return account


def delete_account(account_id: str) -> bool:
    server_rows = _server_accounts_cached()  # 锁外取，避免持锁打 HTTP
    with _IO_LOCK:
        accounts = _merge_rows(_load_local_accounts(), server_rows)
        new_rows = [a for a in accounts if str(a.get("id")) != account_id]
        if len(new_rows) == len(accounts):
            return False
        save_accounts(new_rows)
    return True


def delete_account_with_server(
    account_id: str, *, log: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """删账号：本地行 + 它在服务器上的代跑记录一起删。

    只删本地会留下幽灵：服务器记录还在代跑，下一次合并视图又会以 ``srv:<id>`` 冒出来。
    服务端自增 id 从 ``server_account_id`` 取，拿本机 uuid 去删只会误报。
    """
    row = next(
        (r for r in load_accounts() if str(r.get("id")) == str(account_id)), None
    )
    if row is None:
        return {"ok": False, "message": "未找到账号"}

    server_id = row.get("server_account_id") or (
        str(account_id).removeprefix("srv:")
        if str(account_id).startswith("srv:")
        else ""
    )
    delegates = row.get("source") == "server" or str(row.get("run_mode") or "") == "server"

    if row.get("source") != "server" and not delete_account(str(account_id)):
        return {"ok": False, "message": "删除本地账号失败"}
    if not delegates:
        return {"ok": True, "message": "已删除"}
    if not server_id:
        message = "本地记录已删除，但该账号没有服务器代跑记录 id，服务端可能仍在代跑"
        if log:
            log(message)
        return {"ok": True, "message": message}
    try:
        result = server_client.delete_server_account(server_id)
    except Exception as exc:  # noqa: BLE001 - 服务端异常不能让本地删除回滚
        if log:
            log(f"调用服务器删除接口异常: {exc}")
        return {"ok": False, "message": f"本地记录已删除，但调用服务器删除接口异常: {exc}"}
    if not result.get("ok"):
        reason = result.get("message") or "未知错误"
        if log:
            log(f"删除服务器代跑记录 {server_id} 失败: {reason}")
        return {"ok": False, "message": f"本地记录已删除，但删除服务器代跑记录失败: {reason}"}
    if log:
        log(f"服务器代跑记录 {server_id} 已删除。")
    return {"ok": True, "message": "已删除（含服务器代跑记录）"}


def set_run_mode(account_id: str, mode: str) -> dict[str, Any]:
    """切换本机/代跑模式（两套界面共用，避免出现两种语义）。

    注意：改回 local 只是本机不再跑，服务器上的代跑记录仍在——不删就会继续代跑，
    这里必须如实告知，不能让界面显示「已设为 local」却还在被服务器跑。
    """
    mode = str(mode or "").strip()
    if mode not in ("local", "server"):
        return {"ok": False, "message": "模式只能是 local 或 server"}
    merged = load_accounts()
    row = next((r for r in merged if str(r.get("id")) == str(account_id)), None)
    if row is None:
        return {"ok": False, "message": "未找到账号"}
    if row.get("source") == "server":
        # 只在服务器存在的记录（别机上传的）：本机没有对应行，改模式改不到它身上
        return {"ok": False, "message": "这条记录只存在于服务器，请直接删除以停止代跑"}
    # ``server_account_id`` 是合并视图从服务器记录里带进来的，本地行上没有；
    # 只写要改的那个字段，别把整行视图塞回 accounts.json（那会把服务端的 last_error 等存成历史）
    if not update_account(str(account_id), lambda _row: {"run_mode": mode}):
        return {"ok": False, "message": "未找到账号"}
    if mode == "local" and row.get("server_account_id"):
        return {"ok": True, "message": "已改回本机签到；服务器代跑记录仍在，要停止请删除该账号"}
    return {"ok": True, "message": f"已设为 {mode}"}


def public_account_view(account: dict[str, Any], today_map: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    today_map = today_map or {}
    # 代挂账号的跑批记录可能按服务端 id 回传，两种 id 都查一次
    today = today_map.get(str(account.get("id"))) or today_map.get(str(account.get("server_account_id") or "")) or {}
    expires_at = blob.get("expires_at") or blob.get("expiresAt")
    expired = False
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        expired = expires_at < (datetime.now().timestamp() * 1000 + 5 * 60 * 1000)
    return {
        "id": account.get("id"),
        "server_account_id": account.get("server_account_id"),
        "source": account.get("source") or "local",
        "provider": account.get("provider"),
        "label": account.get("label") or blob.get("nickname") or blob.get("user_id") or blob.get("uid") or account.get("id"),
        "run_mode": account.get("run_mode") or "local",
        "enabled": bool(account.get("enabled", True)),
        "last_ok_at": account.get("last_ok_at"),
        "last_error": account.get("last_error"),
        "last_credits": account.get("last_credits"),
        "last_streak": account.get("last_streak"),
        "token_hint": blob.get("token_hint") or ("(仅服务器)" if not blob and account.get("source") == "server" else "(已保存)"),
        "token_expired": expired,
        "today_status": today.get("status") or "未跑",
        "today_credits": today.get("credits"),
        "today_message": today.get("message") or "",
        "updatedAt": account.get("updatedAt"),
    }


def append_run_log(entry: dict[str, Any]) -> None:
    with _IO_LOCK:
        data = load_json(RUN_LOG_FILE, {"runs": []})
        runs = data.get("runs") if isinstance(data, dict) else []
        if not isinstance(runs, list):
            runs = []
        entry = {
            **entry,
            "at": entry.get("at") or _now(),
            "day": entry.get("day") or _today_local(),
        }
        runs.insert(0, entry)
        runs = runs[:MAX_RUN_LOGS]
        save_json(RUN_LOG_FILE, {"runs": runs})


def load_run_logs(account_id: str | None = None, limit: int = DEFAULT_RUN_LOGS_LIMIT) -> list[dict[str, Any]]:
    with _IO_LOCK:
        data = load_json(RUN_LOG_FILE, {"runs": []})
        runs = data.get("runs", [])
        if account_id:
            runs = [x for x in runs if str(x.get("account_id")) == str(account_id)]
        return runs[:limit]


def clear_run_logs() -> None:
    save_json(RUN_LOG_FILE, {"runs": [], "clearedAt": _now()})


# 实时日志是热路径：跑批时一秒能写十几条，而文件是整份 DPAPI 加密的 JSON，
# 每写一行都「解密 800 行 + 重加密 + 落盘」会把 _IO_LOCK 变成界面瓶颈。
# 写走内存环形缓冲，最多 LIVE_FLUSH_SEC 秒落盘一次；退出时调 flush_live_logs()。
LIVE_FLUSH_SEC = 2.0
_live_buf: list[dict[str, Any]] | None = None
_live_dirty = False
_live_flushed_at = 0.0


def _live_lines_locked() -> list[dict[str, Any]]:
    """取内存缓冲，首次调用时从盘上加载。调用方必须持 ``_IO_LOCK``。"""
    global _live_buf
    if _live_buf is None:
        data = load_json(LIVE_LOG_FILE, {"lines": []})
        lines = data.get("lines") if isinstance(data, dict) else []
        _live_buf = lines if isinstance(lines, list) else []
    return _live_buf


def _flush_live_locked() -> None:
    global _live_dirty, _live_flushed_at
    if not _live_dirty:
        return
    save_json(LIVE_LOG_FILE, {"lines": _live_lines_locked()[:MAX_LIVE_LOGS]})
    _live_dirty = False
    _live_flushed_at = time.monotonic()


def _flush_if_due_locked() -> None:
    """到一个落盘周期才真的写盘。读写两条路都调它：只有写入触发落盘的话，
    一小段集中打完的日志尾巴会一直卡在内存里，直到进程退出才下去。"""
    if time.monotonic() - _live_flushed_at >= LIVE_FLUSH_SEC:
        _flush_live_locked()


def flush_live_logs() -> None:
    """把内存里未落盘的日志写下去（退出前调，缓冲区最多丢 LIVE_FLUSH_SEC 秒）。"""
    with _IO_LOCK:
        _flush_live_locked()


def append_live_log(message: str) -> None:
    global _live_dirty
    with _IO_LOCK:
        lines = _live_lines_locked()
        lines.insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "message": message})
        del lines[MAX_LIVE_LOGS:]
        _live_dirty = True
        _flush_if_due_locked()


def load_live_logs(limit: int = DEFAULT_LIVE_LOGS_LIMIT) -> list[dict[str, Any]]:
    with _IO_LOCK:
        snapshot = list(_live_lines_locked()[:limit])
        _flush_if_due_locked()
        return snapshot


def clear_live_logs() -> None:
    global _live_buf, _live_dirty
    with _IO_LOCK:
        _live_buf = []
        _live_dirty = False
        save_json(LIVE_LOG_FILE, {"lines": [], "clearedAt": _now()})


def append_credit_history(entry: dict[str, Any]) -> None:
    with _IO_LOCK:
        data = load_json(CREDIT_HISTORY_FILE, {"items": []})
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        items.insert(0, {**entry, "at": entry.get("at") or _now(), "day": entry.get("day") or _today_local()})
        items = items[:MAX_CREDIT_HISTORY_ITEMS]
        save_json(CREDIT_HISTORY_FILE, {"items": items})


def load_credit_history(account_id: str | None = None, limit: int = DEFAULT_CREDIT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    with _IO_LOCK:
        data = load_json(CREDIT_HISTORY_FILE, {"items": []})
        items = data.get("items", [])
        if account_id:
            items = [x for x in items if str(x.get("account_id")) == str(account_id)]
        return items[:limit]


def clear_credit_history() -> None:
    save_json(CREDIT_HISTORY_FILE, {"items": [], "clearedAt": _now()})


def today_run_map() -> dict[str, dict[str, Any]]:
    """account_id -> {status, credits, message, ok} for local day."""
    day = _today_local()
    out: dict[str, dict[str, Any]] = {}

    # 加载本地运行日志
    for row in load_run_logs(limit=MAX_RUN_LOGS):
        row_day = str(row.get("day") or "")
        if not row_day:
            at = str(row.get("at") or "")
            row_day = at[:10] if len(at) >= 10 else ""
        if row_day != day:
            continue
        aid = str(row.get("account_id") or "")
        if not aid or aid in out:
            continue
        out[aid] = _process_run_log_entry(row)
    
    # 加载服务器端运行日志并合并，服务器端数据优先
    for row in _aggregated_runs_cached():
        row_day = str(row.get("day") or "")
        if not row_day:
            at = str(row.get("at") or "")
            row_day = at[:10] if len(at) >= 10 else ""
        if row_day != day:
            continue
        # 服务器按它自己的 bound_account_id 回填，同时认 clientAccountId（= 本机 uuid），
        # 否则代挂账号的跑批记录挂不到本机这一行上
        keys = [str(row.get(k) or "") for k in ("account_id", "clientAccountId", "client_account_id")]
        entry = _process_run_log_entry(row)
        for key in {k for k in keys if k}:
            out[key] = entry

    return out

def _process_run_log_entry(row: dict[str, Any]) -> dict[str, Any]:
    ok = bool(row.get("ok"))
    already = bool(row.get("already"))
    if ok and already:
        status = "已签(之前)"
    elif ok:
        status = "已跑成功"
    elif row.get("skipped"):
        # 「没有活动 / 签到未开放」既没领到积分也不算出错，单独一档，别混进失败统计
        status = "未开放"
    else:
        status = "失败"
    return {
        "status": status,
        "credits": row.get("credits"),
        "message": row.get("message") or "",
        "ok": ok,
        "already": already,
        "at": row.get("at"),
        "run_mode": row.get("run_mode") or "local", # 记录运行模式
    }


def today_board(
    *,
    accounts: list[dict[str, Any]] | None = None,
    today: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """今日看板。``accounts`` / ``today`` 可由调用方传入已算好的结果，
    界面一次刷新只合并一次账号和跑批记录。"""
    rows = load_accounts() if accounts is None else accounts
    run_map = today_run_map() if today is None else today
    done: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for acc in rows:
        if not acc.get("enabled", True):
            continue
        view = public_account_view(acc, run_map)
        st = view.get("today_status") or "未跑"
        if st.startswith("已"):
            done.append(view)
        elif st == "失败":
            failed.append(view)
        else:
            pending.append(view)
    return {
        "day": _today_local(),
        "done": done,
        "pending": pending,
        "failed": failed,
        "done_count": len(done),
        "pending_count": len(pending),
        "failed_count": len(failed),
        "total_enabled": len(done) + len(pending) + len(failed),
    }


def _row_day(row: dict[str, Any]) -> str:
    day = str(row.get("day") or "")
    if not day:
        at = str(row.get("at") or "")
        day = at[:10] if len(at) >= 10 else ""
    return day


def _run_category(entry: dict[str, Any]) -> str:
    """把 ``_process_run_log_entry`` 的状态文案归到四档之一。"""
    st = entry.get("status") or ""
    if st == "失败":
        return "failed"
    if st == "未开放":
        return "notopen"
    if st == "已签(之前)":
        return "already"
    return "done"


def _all_run_rows() -> list[dict[str, Any]]:
    """本机 + 服务器代跑的全部跑批记录（新在前）。"""
    rows = list(load_run_logs(limit=MAX_RUN_LOGS))
    try:
        rows += list(_aggregated_runs_cached())
    except Exception:  # noqa: BLE001 - 服务器不可达时只用本机数据
        pass
    return rows


def history_stats(days: int = 14) -> dict[str, Any]:
    """跨天跑批趋势：近 ``days`` 天每日成功率 + 期间各账号的失败情况。

    每个 (天, 账号) 只取当天最新一条（跑批记录新在前，先到先算），
    与今日看板口径一致；「未开放」独立一档、不进分母，避免把没活动算成没签上。
    """
    try:
        span = max(1, min(90, int(days)))
    except (TypeError, ValueError):
        span = 14
    day_keys = {(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)}

    latest: dict[tuple[str, str], str] = {}
    for row in _all_run_rows():
        row_day = _row_day(row)
        if row_day not in day_keys:
            continue
        keys = [str(row.get(k) or "") for k in ("account_id", "clientAccountId", "client_account_id")]
        aid = next((k for k in keys if k), "")
        if not aid:
            continue
        # 同一天同一账号只认最新一条（跑批记录新在前，先到先算）
        latest.setdefault((row_day, aid), _run_category(_process_run_log_entry(row)))

    per_day: dict[str, dict[str, int]] = {}
    for (day_key, _aid), cat in latest.items():
        d = per_day.setdefault(day_key, {"done": 0, "already": 0, "failed": 0, "notopen": 0})
        d[cat] += 1

    days_out: list[dict[str, Any]] = []
    for day in sorted(per_day.keys(), reverse=True):
        counts = per_day[day]
        signed = counts["done"] + counts["already"]
        denom = signed + counts["failed"]  # 未开放不计入成功率分母
        days_out.append(
            {
                "day": day,
                **counts,
                "signed": signed,
                "attempted": denom,
                "success_rate": round(signed / denom, 4) if denom else None,
            }
        )

    per_account: dict[str, dict[str, Any]] = {}
    for (_day_key, aid), cat in latest.items():
        rec = per_account.setdefault(
            aid, {"account_id": aid, "runs": 0, "failed": 0, "notopen": 0, "last_status": "", "last_day": ""}
        )
        rec["runs"] += 1
        if cat == "failed":
            rec["failed"] += 1
        elif cat == "notopen":
            rec["notopen"] += 1
    # 附标签/服务商：尽量从合并视图里取，取不到就留空
    try:
        labels = {str(a.get("id")): a for a in load_accounts()}
        for aid, rec in per_account.items():
            acc = labels.get(aid) or {}
            rec["label"] = acc.get("label") or acc.get("id") or aid
            rec["provider"] = acc.get("provider") or ""
    except Exception:  # noqa: BLE001
        for rec in per_account.values():
            rec.setdefault("label", rec["account_id"])
            rec.setdefault("provider", "")
    top_failing = sorted(
        (r for r in per_account.values() if r["failed"] > 0),
        key=lambda r: (-r["failed"], r["runs"]),
    )[:20]

    return {
        "window_days": span,
        "days": days_out,
        "problem_accounts": top_failing,
        "totals": {
            "signed": sum(d["signed"] for d in days_out),
            "failed": sum(d["failed"] for d in days_out),
            "notopen": sum(d["notopen"] for d in days_out),
            "attempted": sum(d["attempted"] for d in days_out),
        },
    }

