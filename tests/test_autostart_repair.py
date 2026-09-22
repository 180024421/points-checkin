"""开机自启：默认打开 + 打包态启动时自愈注册表。

事故背景：`autostart` 一直是关的，机器每天 20 点后开机，早窗口必然错过；
而即使用户勾过自启，exe 重装/挪目录后 `HKCU\\...\\Run` 里留的是旧路径，
Windows 只会静默不启动，界面上的勾却还亮着。
"""
from __future__ import annotations

from checkin_tool import autostart, settings as settings_mod

OURS = r'"D:\xiangmu\points-checkin\dist\CheckinTool.exe"'
STALE = r'"D:\旧目录\CheckinTool.exe"'
SOMEONE_ELSE = r'"C:\Tools\OtherApp.exe"'


def plan(settings: dict, *, frozen=True, stored=OURS):
    merged = {**settings_mod.default_settings(), **settings}
    return autostart.plan_repair(merged, frozen=frozen, stored=stored, expected=OURS)


def test_new_default_turns_autostart_on():
    assert settings_mod.default_settings()["autostart"] is True


def test_new_default_turns_startup_catchup_on():
    assert settings_mod.default_settings()["catchup_on_start"] is True


def test_missing_entry_gets_written_when_setting_is_on():
    assert plan({"autostart": True}, stored=None) == "write"


def test_stale_path_self_heals():
    # exe 换过目录 → 注册表里还是旧路径，必须重写而不是「看起来已经开了」
    assert plan({"autostart": True}, stored=STALE) == "write"


def test_matching_entry_needs_no_write():
    assert plan({"autostart": True}, stored=OURS) is None


def test_setting_off_removes_our_own_entry():
    assert plan({"autostart": False}, stored=OURS) == "remove"


def test_setting_off_never_touches_someone_elses_value():
    assert plan({"autostart": False}, stored=SOMEONE_ELSE) is None


def test_source_run_never_touches_the_registry():
    # 源码跑的时候 _exe_command 会写成 python + run_gui.py，
    # 那会把已装好的 exe 顶掉，开机后用的是另一套 data/ 目录 → 账号"消失"
    assert plan({"autostart": True}, frozen=False, stored=None) is None
    assert plan({"autostart": False}, frozen=False, stored=STALE) is None


def test_expected_command_quotes_a_path_with_spaces(monkeypatch):
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        autostart.sys, "executable", r"D:\Program Files\签到工具\CheckinTool.exe", raising=False
    )
    command = autostart.expected_command()
    assert command.startswith('"') and command.endswith('"')
    assert "Program Files" in command


def test_is_ours_recognises_both_layouts():
    assert autostart.is_ours(OURS) is True
    assert autostart.is_ours(r'"C:\py\python.exe" "D:\x\run_gui.py"') is True
    assert autostart.is_ours(SOMEONE_ELSE) is False
    assert autostart.is_ours(None) is False


def test_repair_on_startup_reports_a_line_only_when_it_acted(monkeypatch):
    actions: list[str] = []

    def fake_write():
        actions.append("write")

    def fake_remove():
        actions.append("remove")

    monkeypatch.setattr(autostart, "stored_command", lambda: None)
    monkeypatch.setattr(autostart, "expected_command", lambda: OURS)
    monkeypatch.setattr(autostart, "_is_frozen", lambda: True)
    monkeypatch.setattr(autostart, "write_entry", fake_write)
    monkeypatch.setattr(autostart, "remove_entry", fake_remove)
    message = autostart.repair_on_startup({"autostart": True})
    assert actions == ["write"]
    assert "开机自启" in (message or "")

    monkeypatch.setattr(autostart, "stored_command", lambda: OURS)
    assert autostart.repair_on_startup({"autostart": True}) == ""
    assert actions == ["write"]  # 没再动过


def test_toggle_from_source_run_refuses_to_touch_the_registry(monkeypatch):
    # 源码态写的是 python.exe + run_gui.py，会把装好的 exe 顶掉
    def boom():
        raise AssertionError("源码态不许写注册表")

    monkeypatch.setattr(autostart, "_is_frozen", lambda: False)
    monkeypatch.setattr(autostart, "write_entry", boom)
    monkeypatch.setattr(autostart, "remove_entry", boom)
    message = autostart.apply_toggle(True)
    assert "源码" in message


def test_toggle_when_frozen_writes_and_reports(monkeypatch):
    actions: list[str] = []
    monkeypatch.setattr(autostart, "_is_frozen", lambda: True)
    monkeypatch.setattr(autostart, "write_entry", lambda: actions.append("write"))
    monkeypatch.setattr(autostart, "remove_entry", lambda: actions.append("remove"))
    assert "开启" in autostart.apply_toggle(True)
    assert "关闭" in autostart.apply_toggle(False)
    assert actions == ["write", "remove"]


def test_toggle_swallows_registry_errors_into_a_line(monkeypatch):
    def deny():
        raise OSError("拒绝访问")

    monkeypatch.setattr(autostart, "_is_frozen", lambda: True)
    monkeypatch.setattr(autostart, "write_entry", deny)
    assert "失败" in autostart.apply_toggle(True)
