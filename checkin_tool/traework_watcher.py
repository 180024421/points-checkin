# -*- coding: utf-8 -*-
"""Trae CN / TRAE SOLO 登录态实时捕获（免手动点采集）。

为什么要"实时"：
    Trae 桌面端只在本机保留**一个**当前登录态，固定写在
    ``storage.json`` 的 ``iCubeAuthInfo://icube.cloudide``；每次登录 / 切号
    都会把整条键覆盖掉，旧账号的 accessToken、refreshToken 随即从磁盘消失
    （只剩 ``iCubeAuthInfo://usertag`` 里的 userId 记录，没有 token）。

    所以历史账号事后无法补采，只有在"登录发生的那一刻"抓下来才拿得到。

本模块轮询 storage.json / state.vscdb 的指纹（mtime + size），一旦发现变化，
立刻解密并回调给上层落库；入库的 blob 自带 refreshToken + 该账号的设备私钥，
之后由适配器自动续期，无需再次登录。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .adapters import traework

LogFn = Callable[[str], None]
AuthFn = Callable[[list[dict[str, Any]], str], None]


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class TraeWorkAutoCapture:
    """后台线程：监听 Trae 登录态文件，变化即提取。"""

    def __init__(
        self,
        on_auths: AuthFn,
        log: LogFn | None = None,
        *,
        interval: float = 3.0,
        rescan_every: float = 60.0,
        get_user_dir: Callable[[], str | None] | None = None,
    ) -> None:
        self._on_auths = on_auths
        self._log = log or (lambda _m: None)
        self._get_user_dir = get_user_dir or (lambda: None)
        self._interval = max(1.0, float(interval))
        self._rescan_every = max(self._interval, float(rescan_every))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # uid -> token：只回调"新账号 / token 变了"的，避免刷屏
        self._known: dict[str, str] = {}
        self._last_sig: dict[str, tuple[int, int]] = {}
        self._last_full_scan = 0.0
        self.captured = 0
        self.last_at = ""
        self.last_message = ""

    # ---------------- 对外接口 ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="traework-autocapture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "captured": self.captured,
            "known": len(self._known),
            "last_at": self.last_at,
            "last_message": self.last_message,
        }

    def scan(self, reason: str = "手动") -> list[dict[str, Any]]:
        """立即提取一次，返回较上次有变化的登录态列表。"""
        user_dir = self._get_user_dir() or None
        auths, err = traework.load_all_local_auths(user_dir)
        fresh = [a for a in auths if a.get("token")]
        changed: list[dict[str, Any]] = []
        with self._lock:
            for auth in fresh:
                uid = str(auth.get("user_id") or "").strip() or f"anon:{auth.get('auth_key')}"
                token = str(auth.get("token") or "")
                if self._known.get(uid) == token:
                    continue
                self._known[uid] = token
                changed.append(auth)
        if err and not fresh:
            self.last_message = err
        if changed:
            self.captured += len(changed)
            self.last_at = _now_str()
            labels = ", ".join(str(a.get("user_id") or a.get("nickname") or "未知") for a in changed)
            self.last_message = labels
            try:
                self._on_auths(changed, reason)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[Trae自动采集] 入库失败: {exc}")
        return changed

    # ---------------- 内部 ----------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                user_dir = self._get_user_dir() or None
                sig = traework.storage_signature(user_dir)
                now = time.time()
                if sig != self._last_sig:
                    self._last_sig = sig
                    self._last_full_scan = now
                    self.scan("检测到 Trae 登录态变化")
                elif now - self._last_full_scan >= self._rescan_every:
                    self._last_full_scan = now
                    self.scan("定时巡检")
            except Exception as exc:  # noqa: BLE001
                self._log(f"[Trae自动采集] 异常: {exc}")
            self._stop.wait(self._interval)
