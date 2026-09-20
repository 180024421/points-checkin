"""授权守卫：定时在线校验卡密，一经失效即「踢出登录」。

策略（尽量避免误伤）：
- 只有**服务端明确拒绝**才踢：check_status 返回 valid=False，且
  （a）带 raw 业务响应体，或（b）ok=True（在线拿到了 200）；
- 纯网络异常（networkError）与「加密通道建不起来、已拒绝降级明文」（cryptoError）
  只记日志与失败计数，不踢——用户可能只是断网；本地票据仍在时 check_status 自己会返回 offline 有效；
- 踢出动作由宿主传入的 on_revoked 执行（停调度/清票据/弹回激活门禁），
  本模块只做判定与幂等触发；重新激活后宿主需调用 reset() 恢复校验。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

DEFAULT_INTERVAL_SEC = 300.0  # 5 分钟校验一次
FIRST_DELAY_SEC = 20.0  # 启动后先等界面/激活流程稳定


class LicenseGuard:
    def __init__(
        self,
        *,
        get_settings: Callable[[], dict[str, Any]],
        on_revoked: Callable[[str], None],
        log: Callable[[str], None] | None = None,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._get_settings = get_settings
        self._on_revoked = on_revoked
        self._log = log or (lambda _m: None)
        self.interval_sec = float(interval_sec or DEFAULT_INTERVAL_SEC)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._revoked = False
        self._net_fail = 0
        self._last: dict[str, Any] = {"at": None, "valid": None, "message": "未校验"}

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="license-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        """卡密重新激活成功后调用：清掉踢出标记并恢复后台校验。

        没有这一步，一旦触发过踢出，本进程内就再也不会做在线校验了。
        """
        with self._lock:
            self._revoked = False
        self._net_fail = 0
        self._last = {"at": _now(), "valid": None, "message": "已重新激活，等待下次校验"}
        self.start()

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "intervalSec": self.interval_sec,
            "revoked": self._revoked,
            "netFail": self._net_fail,
            "last": dict(self._last),
        }

    def get_account_limit(self) -> int | None:
        """获取当前生效的账户数量限制。"""
        if self._last and self._last.get("license"):
            return self._last["license"].get("accountLimit")
        return None

    # ------------------------------------------------------------ 判定
    def check_now(self) -> dict[str, Any]:
        """立即校验一次。返回 {valid, revoked, message, explicit}。"""
        from .license_client import check_status

        try:
            settings = self._get_settings() or {}
            res = check_status(settings, force_online=True)
        except Exception as exc:  # noqa: BLE001 - 任何异常都按"校验失败"处理，不踢
            self._net_fail += 1
            self._last = {"at": _now(), "valid": None, "message": f"校验异常：{exc}"}
            self._log(f"授权校验异常（第 {self._net_fail} 次，不踢出）：{exc}")
            return {"valid": None, "revoked": False, "message": str(exc), "explicit": False}

        if res.get("valid"):
            from .license_client import public_license_view

            self._net_fail = 0
            # 只留界面要用的字段：完整 cache 里有 ticket，宿主的 status() 会被送进渲染进程
            self._last = {
                "at": _now(),
                "valid": True,
                "message": res.get("message") or "授权有效",
                "license": public_license_view(res.get("license")),
            }
            return {"valid": True, "revoked": False, "message": self._last["message"], "explicit": False}

        # 无效：服务端明确拒绝 vs 网络异常 / 加密通道异常
        network_issue = bool(res.get("networkError")) or bool(res.get("cryptoError"))
        explicit = (bool(res.get("raw")) or res.get("ok") is True) and not network_issue
        message = str(res.get("message") or "授权已失效")
        if explicit:
            self._last = {"at": _now(), "valid": False, "message": message}
            self._revoke(message)
            return {"valid": False, "revoked": True, "message": message, "explicit": True}

        self._net_fail += 1
        self._last = {"at": _now(), "valid": None, "message": message}
        self._log(f"授权校验未通过（第 {self._net_fail} 次，{message}，暂不踢出）")
        return {"valid": None, "revoked": False, "message": message, "explicit": False}

    def _revoke(self, reason: str) -> None:
        with self._lock:
            if self._revoked:
                return
            self._revoked = True
        self._log(f"[!] 卡密已失效，已退出登录：{reason}")
        try:
            self._on_revoked(reason)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[!] 踢出处理异常：{exc}")

    # ------------------------------------------------------------ 循环
    def _loop(self) -> None:
        if self._stop.wait(min(FIRST_DELAY_SEC, self.interval_sec)):
            return
        while not self._stop.is_set():
            # 被踢出后不退出线程：等待 reset()（重新激活）或 stop()，
            # 期间跳过校验，避免无意义地反复打服务端。
            if self._revoked:
                if self._stop.wait(self.interval_sec):
                    return
                continue
            self.check_now()
            if self._stop.wait(self.interval_sec):
                return


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
