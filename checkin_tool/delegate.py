# -*- coding: utf-8 -*-
"""代跑（服务器托管）相关的写操作 —— 两个前端共用一份实现。

原来这段逻辑只长在 ``webview_app.CheckinApi`` 里：tkinter 回退端要么复制一份
（两条前端各自演化，服务器删了本地没回滚这类坑修一遍要修两次），要么干脆没有
这个入口。抽出来后两边都只剩「取参数 → 起后台线程 → 调这里 → 刷界面」。
"""

from __future__ import annotations

from typing import Any, Callable

from . import account_store, server_client

LogFn = Callable[[str], None]


def _has_token(account: dict[str, Any]) -> bool:
    blob = account.get("token_blob") or {}
    return bool(blob.get("token") or blob.get("access_token"))


def upload_delegate_accounts(
    accounts: list[dict[str, Any]],
    *,
    task_enabled: bool,
    log: LogFn | None = None,
) -> int:
    """把一批账号标记为服务器代跑并上传 token，返回真正推上去的个数。

    一条失败只跳过这一条：批量路径里异常冒出去会中断整批，前面已经改成
    ``run_mode=server`` 的账号就停在半完成状态没人收尾。
    """
    uploaded = 0
    for account in accounts:
        if not _has_token(account):
            if log:
                log(f"跳过无 token 账号 {account.get('id')}")
            continue
        account["run_mode"] = "server"
        if str(account.get("provider")) == "workbuddy":
            # 告诉服务器：这个账号代跑时要不要顺带做成长任务
            account["task_enabled"] = task_enabled
        stored = account_store.try_upsert_account(account)
        if not stored.get("ok"):
            if log:
                log(f"账号 {account.get('id')} 未能标记为代跑：{stored.get('message')}")
            continue
        result = server_client.sync_server_blob(account, log=log, force=True)
        if not result:
            if log:
                log(f"上传代跑 {account.get('provider')}: 无可用凭证，已跳过")
            continue
        uploaded += 1
    return uploaded


def _back_to_local(account: dict[str, Any], log: LogFn | None) -> None:
    """上传失败后把本机这行退回本机模式。

    否则界面上会留一个「显示在代跑、其实服务器没有」的分裂状态。
    """
    account["run_mode"] = "local"
    account_store.try_upsert_account(account)


def replace_server_account(
    old_account_id: str, new_account_id: str, *, log: LogFn | None = None
) -> dict[str, Any]:
    """用一个本机账号换掉服务器上的代跑记录（额度坐席不重复占用）。"""
    if log:
        log(f"尝试更换服务器代跑账号：旧账号ID={old_account_id}, 新账号ID={new_account_id}")

    all_accounts = account_store.load_accounts()
    old_account = next(
        (a for a in all_accounts if str(a.get("id")) == str(old_account_id)), None
    )
    if not old_account:
        return {"ok": False, "message": f"未找到旧账号 {old_account_id}"}
    if old_account.get("run_mode") != "server":
        return {"ok": False, "message": f"旧账号 {old_account_id} 不是服务器代跑模式，无法更换"}

    new_account = next(
        (a for a in all_accounts if str(a.get("id")) == str(new_account_id)), None
    )
    if not new_account:
        return {"ok": False, "message": f"未找到新账号 {new_account_id}"}
    if new_account.get("run_mode") == "server":
        return {
            "ok": False,
            "message": f"新账号 {new_account_id} 已是服务器代跑模式，请选择本机账号进行更换",
        }

    old_server_id = old_account.get("server_account_id") or (
        str(old_account_id).removeprefix("srv:") if str(old_account_id).startswith("srv:") else ""
    )
    if not old_server_id:
        # 没有服务端 id 就绝不能拿本机 uuid 去删：服务端按自增主键查，只会误报或漏删
        return {"ok": False, "message": f"旧账号 {old_account_id} 没有服务器代跑记录 id，无法更换"}

    try:
        if log:
            log(f"正在删除服务器上的旧代跑记录: {old_server_id}")
        server_del = server_client.delete_server_account(old_server_id)
        if not server_del.get("ok"):
            return {"ok": False, "message": f"删除服务器旧账号失败: {server_del.get('message')}"}
        # 旧记录已经不代跑了：本机那一行改回本机模式，否则合并视图会立刻把它标回 server
        if old_account.get("source") != "server":
            old_account["run_mode"] = "local"
            old_account.pop("server_account_id", None)
            # 纯状态回写：写不进只记日志，不能因为本地存储失败就中断更换流程
            # ——服务器上的旧记录此刻已经删掉了。
            rollback = account_store.try_upsert_account(old_account)
            if not rollback.get("ok") and log:
                log(f"旧账号 {old_account_id} 本地状态回写失败：{rollback.get('message')}")
    except Exception as exc:  # noqa: BLE001 - 前端只关心能不能继续
        return {"ok": False, "message": f"调用服务器删除旧账号接口异常: {exc}"}

    try:
        if log:
            log(f"正在上传新账号 {new_account_id} 到服务器")
        # 先落本机：本机没落成 server 就不要往服务器推，否则会出现
        # 「服务器在代跑、本机界面看不到」的分裂状态。
        new_account["run_mode"] = "server"
        switched = account_store.try_upsert_account(new_account)
        if not switched.get("ok"):
            _back_to_local(new_account, log)
            return {"ok": False, **switched}

        synced = server_client.sync_server_blob(new_account, log=log, force=True)
        if synced is None:
            _back_to_local(new_account, log)
            return {
                "ok": False,
                "message": f"新账号 {new_account_id} 没有可用 token，无法上传代跑",
            }
        if not synced.get("ok"):
            _back_to_local(new_account, log)
            reason = synced.get("message") or "未知错误"
            if log:
                log(f"新账号 {new_account_id} 上传服务器失败: {reason}")
            return {"ok": False, "message": f"新账号上传服务器失败: {reason}"}

        if log:
            log(f"账号 {old_account_id} 已成功更换为 {new_account_id} 并上传至服务器。")
        return {"ok": True, "message": f"账号 {old_account_id} 已成功更换为 {new_account_id}"}
    except Exception as exc:  # noqa: BLE001
        _back_to_local(new_account, log)
        return {"ok": False, "message": f"上传新账号到服务器异常: {exc}"}
