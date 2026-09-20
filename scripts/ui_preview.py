# -*- coding: utf-8 -*-
"""生成 UI 预览页：把真实 index.html 复制一份，在主 <script> 前注入 mock pywebview.api。

只用于浏览器里看真实渲染效果，不参与打包。用法：python scripts/ui_preview.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent / "checkin_tool" / "ui" / "index.html"
OUT_DIR = pathlib.Path(tempfile.gettempdir()) / "checkin_ui_preview"

MOCK = r"""
<script>
(function () {
  function minsAgo(m) { const d = new Date(Date.now() - m * 60000); return d.toISOString().replace('T', ' ').slice(0, 19); }
  const PROVIDERS = ['traework', 'workbuddy'];
  const accounts = [];
  for (let i = 0; i < 13; i++) {
    const provider = PROVIDERS[i % 2];
    const runMode = i % 5 === 3 ? 'server' : 'local';
    const expired = i === 6;
    const failed = i === 9;
    accounts.push({
      id: (provider === 'traework' ? 'tw-' : 'wb-') + (1000 + i),
      server_account_id: runMode === 'server' ? 900 + i : null,
      source: i === 12 ? 'server' : 'local',
      provider: provider,
      label: (provider === 'traework' ? 'Trae号' : 'WB号') + (i + 1),
      run_mode: runMode,
      enabled: i !== 11,
      last_ok_at: minsAgo(60 + i * 37),
      last_error: failed ? '签到失败：HTTP 401 unauthorized（refresh_token 已失效）' : '',
      last_credits: provider === 'workbuddy' ? [40, 880, 1290, 60, 2450, 0, 310][i % 7] : null,
      last_streak: [12, 3, 45, 1, 8, 0, 21][i % 7],
      token_hint: 'a3f' + '…' + '9c' + i,
      token_expired: expired,
      today_status: failed ? '失败' : (i % 4 === 2 ? '未跑' : (i === 11 ? '未跑' : '已完成')),
      today_credits: failed ? null : 30 + i,
      today_message: failed ? '' : '签到成功 +' + (30 + i) + ' 积分',
      updatedAt: minsAgo(i * 11)
    });
  }
  const done = accounts.filter(a => a.today_status.startsWith('已')).length;
  const failedRows = accounts.filter(a => a.today_status === '失败');
  const pending = accounts.filter(a => a.today_status !== '失败' && !a.today_status.startsWith('已'));
  const settings = {
    license_base_url: 'http://124.220.147.6:9099',
    card_code: 'DEMO-CARD-CODE',
    autostart: true,
    auto_schedule: true,
    evening_schedule: true,
    traework_auto_capture: true,
    auto_sync: true,
    auto_sync_minutes: 5,
    schedule_hour: 8,
    schedule_minute: 0,
    evening_hour: 20,
    evening_minute: 30,
    run_gap_min_sec: 20,
    run_gap_max_sec: 60,
    credit_low_threshold: 100,
    traework_user_dir: 'C:/Users/demo/.trae',
    workbuddy_task_mode: 'local',
    workbuddy_chat_tasks: true
  };
  const data = {
    ok: true,
    version: '1.14.0',
    settings: settings,
    traework_watch: { running: true, captured: 4, last_at: minsAgo(9), last_message: '入库 Trae号7' },
    scheduler: { enabled: true, running: true, nextAt: '补漏 20:30' },
    license: { valid: true, license: { valid: true, planLabel: '年度 13 号席' }, message: '' },
    revoked: null,
    accountUsage: {
      used: 13, limit: 15, planLabel: '年度 15 号席', quotaKnown: true, quotaSource: 'server',
      expireAtIso: '2027-06-30T00:00:00', expireDaysLeft: 283, timeUnlimited: false, contactVerified: true
    },
    licenseGuard: { running: true, state: 'ok' },
    sync: { enabled: true, minutes: 5, at: minsAgo(2).slice(11, 16), failed: false, busy: false, message: '' },
    board: {
      day: '2026-09-20', done_count: done, pending_count: pending.length, failed_count: failedRows.length,
      total_enabled: accounts.filter(a => a.enabled).length,
      done: accounts.filter(a => a.today_status.startsWith('已')).slice(0, 6),
      pending: pending, failed: failedRows
    },
    accounts: accounts,
    logs: [
      { at: minsAgo(1), message: 'WB号3 签到成功 +32 积分（连签 45）' },
      { at: minsAgo(2), message: 'Trae号10 签到失败：HTTP 401 unauthorized' },
      { at: minsAgo(3), message: '自动同步完成：服务器 13 条 / 本机 7 条' }
    ],
    credentials: [
      { id: 'c-1', provider: 'workbuddy', username: 'demo@mail.com', last_login_ok: true, last_login_method: 'api', last_login_error: '' },
      { id: 'c-2', provider: 'traework', username: 'demo2@mail.com', last_login_ok: false, last_login_method: 'browser', last_login_error: '验证码超时' }
    ]
  };
  const creditRows = [];
  for (let i = 0; i < 24; i++) {
    creditRows.push({
      at: minsAgo(i * 47), provider: PROVIDERS[i % 2], credits: 30 + (i % 9) * 5,
      streak: 1 + (i % 40), message: i % 6 === 0 ? '签到失败：HTTP 401' : '签到成功', account_id: accounts[i % accounts.length].id
    });
  }
  const generic = { ok: true, message: '[mock] 操作完成' };
  const impl = {
    get_bootstrap: () => JSON.parse(JSON.stringify(data)),
    credit_history: () => ({ items: creditRows }),
    poll_logs: () => ({ lines: [] }),
    notices: () => ({ items: [] }),
    traework_watch_status: () => ({ watch: data.traework_watch }),
    save_settings: (payload) => { Object.assign(data.settings, payload || {}); console.log('[mock] save_settings', JSON.stringify(payload)); return { ok: true, message: '[mock] 设置已保存' }; }
  };
  window.pywebview = {
    api: new Proxy(impl, {
      get(target, name) {
        if (typeof name !== 'string') return undefined;
        if (name in target) return target[name];
        return function () { console.log('[mock call]', name, arguments); return Promise.resolve(JSON.parse(JSON.stringify(generic))); };
      },
      has() { return true; }
    })
  };
})();
</script>
"""


def build(tab: str = "") -> pathlib.Path:
    html = SRC.read_text(encoding="utf-8")
    # 必须在页面自身脚本之前注入：脚本末尾用 window.pywebview.api 判断是否 boot()
    injected = html.replace("  <script>\n", MOCK + "\n  <script>\n", 1)
    if "window.pywebview = {" not in injected:
        raise SystemExit("注入失败：没找到主 <script> 锚点")
    if tab:
        # 无头截图拍不到「点了哪个标签」，所以预览页自己点一下（不改真实文件）。
        # 立即点一次 + 400ms 再点一次：只靠定时器的话，--virtual-time-budget 下
        # 截图常常赶在回调之前，拍出来永远是概览页。
        injected = injected.replace(
            "</body>",
            '<script>function _pick(){var b=document.querySelector("[data-tab=%s]");if(b)b.click();}'
            "_pick();setTimeout(_pick,400);</script></body>" % tab,
            1,
        )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / ("index.html" if not tab else f"index-{tab}.html")
    out.write_text(injected, encoding="utf-8")
    # 图标顺带拷过去，免得 onerror 分支挡住真实布局
    for asset in ("icon.png",):
        src = SRC.parent / asset
        if src.exists():
            (OUT_DIR / asset).write_bytes(src.read_bytes())
    return out


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else ""))
    sys.exit(0)
