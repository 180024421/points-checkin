# 界面改版设计方案：M3 柔彩玻璃 · 明暗双主题

适用范围：`checkin_tool/ui/index.html`（pywebview / WebView2 端，2414 行）。
Tkinter 回退端（`checkin_tool/gui.py`）只做**同色系降级**，不做玻璃与动效，见 §12。

本文件是方案，不含代码改动。分三期落地，每期验收标准见 §11。

---

## 0. 结论先行

方向不用推翻：左侧导航 + 统计卡 + 表格的骨架是对的，问题全在**令牌没收住**和**只有一套白昼配色**。

实测到的四条硬伤（都在当前文件里，可核对）：

| 现状 | 数据 |
|---|---|
| 辅助文字对比度不达标 | `--muted: #7b8ea4`（:18）在页面底色 `#eef3fa` 上只有 **3.02:1**，在白底上 **3.36:1**，WCAG AA 要求正文 4.5:1 —— 而它被用在 11px/10.5px 的小字上 |
| 错误色刚好卡在红线上 | `--danger: #d24545`（:27）在白底 **4.49:1**，差 0.01 不达标 |
| 只有浅色一套 | 全文 `data-theme` / `prefers-color-scheme` **0 处**；晚窗固定 20:00 之后补签，全屏白光 |
| 令牌形同虚设 | `:root` 之外 **69 处硬编码颜色**、**39 处内联 `style`**；字号散成 **10 档**，圆角 **7 档**，而 `--radius: 14px` 只被用到 1 次 |

方案要做的事，一句话：**把这 100 多处漂移收回 6 档字号 + 4 档圆角 + 一整套可切换明暗的 M3 色调令牌，再谈组件和布局。**

---

## 1. 范围与硬约束

先划掉那些"看起来能做其实做不了"的：

- **没有 web font**。工具常驻离线桌面运行，不能引 Google Sans / Roboto / Material Icons 字体。中文栈保持 `"Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui`，英文数字靠字重与字号拉开层级。
- **没有图标字体**，当前文件里 `<svg>` **0 处**、emoji **0 处**，全部是纯文字按钮。改版需要图标，只能走**内联 SVG sprite**（一组 14 个，见 §6.9），不引外部请求。
- **窗口最小尺寸 980×640**（`webview_app.py:1251`）。响应式的真实下限是 980，不是手机；断点为 980 / 1180 / 1440 三档加一个 <900 的收窄侧栏。
- **WebView2 支持 `backdrop-filter`，但要算性能**：玻璃层数 ≤3，且不得互相嵌套（tile 内再嵌玻璃卡），hover 只做 `transform`，不做 `filter` 过渡。
- **不改后端契约**：所有新增视觉都吃 `public_account_view` / `today_board` 已有字段，包括刚落地的第三态 `未开放`。

---

## 2. 设计语言与三条纪律

M3（Material You）几何 + 柔彩玻璃层次 + 低饱和粉彩。三条纪律贯穿全站：

1. **用色调分层替代阴影**：层级靠 `surface-container` 的深浅递进表达，阴影只用于浮层（对话框、菜单、snackbar）。不再给每张卡片挂 `--shadow`。
2. **曲线是身份**：静态大圆角（卡 28px、控件 16px、chip 全圆），只在交互态收紧，不做直角。
3. **色彩只做灰调叙事，饱和度留给语义**：品牌蓝负责"可点"，绿/琥珀/红只负责状态，其余一律中性蓝灰。避免整屏高饱和。

主色沿用现有品牌蓝 `#0a6fc2` 作种子色，派生两套色调。

---

## 3. 设计令牌

### 3.1 浅色（默认）

| 角色 | 变量 | 值 | 用途 |
|---|---|---|---|
| Surface Dim | `--surface-dim` | `#EAF0F8` | 窗口外沿、页脚带 |
| Surface | `--surface` | `#F6F9FF` | 页面画布 |
| Container Lowest | `--surface-c-lowest` | `#FFFFFF` | 表格底、输入底 |
| Container Low | `--surface-c-low` | `#F2F7FE` | 卡片（默认） |
| Container | `--surface-c` | `#ECF3FB` | 分组块、hover 行 |
| Container High | `--surface-c-high` | `#E4EEFA` | 选中行、导航容器 |
| On Surface | `--on-surface` | `#0F1B26` | 主文字（16.5:1） |
| On Surface Variant | `--on-surface-var` | `#41525F` | 次文字（**7.7:1**，替掉现在的 3.02） |
| Outline / Outline Var | `--outline` / `--outline-var` | `#C3CCD6` / `#DCE4EE` | 描边、分隔线 |
| Primary | `--primary` | `#0B5FA5` | 主按钮、链接、焦点（白字 6.6:1） |
| On Primary | `--on-primary` | `#FFFFFF` | |
| Primary Container | `--primary-c` / `--on-primary-c` | `#D3E5FF` / `#00264A` | 选中导航、强调 chip |
| Secondary Container | `--secondary-c` / `--on-secondary-c` | `#E8EFF5` / `#3F5E75` | 中性信息块 |
| Tertiary | `--tertiary` / `--tertiary-c` / on | `#5B4A82` / `#EAE4F5` / `#1E1140` | **只标"服务器代跑"**，与本机操作区分 |
| Error | `--error` / `--on-error` / `--error-c` / on | `#B3261E` / `#FFFFFF` / `#FDE3E1` / `#6B0A06` | 失败（容器上 5.4:1） |
| Ok | `--ok` / `--ok-c` / on | `#0A6E4A` / `#E7F6EF` / `#053726` | 已签到 |
| Warn | `--warn` / `--warn-c` / on | `#7A5200` / `#FBF0DA` / `#3E2900` | 积分低于阈值、token 将过期 |
| Info | `--info` / `--info-c` / on | `#3F5E75` / `#E8EFF5` / `#1E3343` | **未开放**（新第三态专用） |

### 3.2 深色

| 角色 | 变量 | 值 | 实测对比 |
|---|---|---|---|
| Surface | `--surface` | `#0E1319` | 不用纯黑（M3 反模式） |
| Container Low / / High / Highest | | `#131A21` `#171F28` `#1C252F` `#212B36` | 色调递进即层级 |
| On Surface | `--on-surface` | `#E2E7ED` | 15.0:1 |
| On Surface Variant | `--on-surface-var` | `#B7C2CD` | 8.9:1 on `#1A222B` |
| Primary / On Primary | | `#8EC7FF` / `#00314F` | 7.6:1 |
| Primary Container / on | | `#004A77` / `#D3E5FF` | |
| Ok / container | | `#6FDBA5` / `#04241A` | 9.7:1 |
| Warn / container | | `#F0C05A` / `#2B1B00` | 9.8:1 |
| Error / container | | `#FFB4AB` / `#3B0A05`（填充按钮用 `#8C1D18`） | 10.0:1 |
| Info / container | | `#A9C3D6` / `#13212C` | 9.0:1 |
| Outline / Outline Var | | `#45484D` / `#353A41` | |
| Glass | `--glass-bg` / `--glass-brd` | `rgba(28,37,47,.55)` / `rgba(255,255,255,.09)` | |

### 3.3 玻璃、阴影、动效

```css
:root {
  /* 玻璃：最多 3 层，禁止嵌套 */
  --glass-blur: 18px;
  --glass-bg: rgba(255, 255, 255, .55);
  --glass-brd: rgba(255, 255, 255, .60);
  /* 阴影：只给浮层 */
  --elev-1: 0 1px 2px rgba(9, 40, 70, .10), 0 2px 6px rgba(9, 40, 70, .06);
  --elev-2: 0 8px 24px rgba(9, 40, 70, .16);
  --elev-3: 0 20px 50px rgba(9, 40, 70, .26);
  /* 动效 */
  --dur-1: 180ms; --dur-2: 320ms; --dur-3: 500ms;
  --ease-emph: cubic-bezier(.2, 0, 0, 1);
  --ease-exit: cubic-bezier(0, 0, .2, 1);
}
@media (prefers-reduced-motion: reduce) {
  :root { --dur-1: 1ms; --dur-2: 1ms; --dur-3: 1ms; }
  .tile:hover { transform: none; }
}
```

---

## 4. 排版、圆角、间距的收敛表

现状 10 档字号 → 6 档，7 档圆角 → 4 档。左边是要被替换掉的值，右边是唯一保留的令牌：

| 现状 `font-size` | 次数 | → 新令牌 | 值 |
|---|---|---|---|
| 10.5px | 2 | `--fs-100` | 12px |
| 11px / 11.5px | 4 / 11 | `--fs-100` | 12px |
| 12px / 12.5px | 4 / 9 | `--fs-200` | 13px |
| 13px | 4 | `--fs-300` | 14px |
| 15.5px | 1 | `--fs-400` | 16px |
| 17px | 1 | `--fs-400` | 16px |
| 20px | 1 | `--fs-500` | 20px |
| 26px | 1 | `--fs-600` | 26px（仅统计数字） |

**12px 是全站地板**：低于它的小字在 100% 缩放的桌面上读不动，这也是当前 `--muted` 对比度不达标最伤的地方。

| 现状 `border-radius` | → 新令牌 |
|---|---|
| 5px / 8px | `--r-sm: 12px`（输入框、小 chip） |
| 10px / 11px / 13px / 14px(`--radius`) | `--r-md: 16px`（按钮、控件、行） |
| 16px | `--r-md` 或 `--r-lg`（按容器尺寸就近） |
| — | `--r-lg: 28px`（卡片、对话框、统计 tile） |
| 99px | `--r-full`（状态点、头像、segment 滑块） |

间距统一 4 倍数：`--sp-1..6 = 4 / 8 / 12 / 16 / 24 / 32`。卡片内边距 24，卡片间 16，页面左右 24（≥1440 时 32）。

---

## 5. 状态语义：`today_status` → 色档映射

后端 `account_store._process_run_log_entry` 现在会产出 5 种状态字符串，界面必须一一有归属（"未开放"是新档，目前会掉进灰）：

| `today_status` | 色档 | 视觉 | 计入统计卡 |
|---|---|---|---|
| `已跑成功` | ok | 绿容器 chip + ✓ | 已跑 |
| `已签(之前)` | ok（弱化） | 绿描边、无底色 | 已跑 |
| `未开放` | info | 蓝灰容器 chip + 圆点 | **不计失败**（中性，另列"未领"） |
| `失败` | error | 红容器 chip + ! | 异常 |
| `未跑` / 空 | neutral | `--on-surface-var` 文字，无底色 | 待办 |
| （行附加）`token_expired` | warn | 琥珀描边 + 时钟图标 | 异常 |
| （行附加）积分 < 阈值 | warn | 数字琥珀 + `low` 标记 | — |
| `enabled=false` | disabled | 整行 38% 透明 + 左侧灰条 | 不计 |

统计卡从"已跑 / 未跑 / 失败"三档扩成四档：**已跑 · 未领(未开放) · 待办 · 异常**。第四档必须独立，否则改版后所有"活动没开"的号会被用户误读成坏了。

---

## 6. 组件规范

### 6.1 导航 rail（侧栏）

- 宽 232px（<1180 收成 72px 图标栏，文字消失、tooltip 兜底）。
- 玻璃容器 `--glass-bg` + `blur(18px)`，右 1px `--outline-var`。
- 选中项：`--primary-c` 容器 + 左侧 4×28px 圆角指示条（`--r-full`），文字 `--on-primary-c`；hover 只换 `--surface-c`，不动位移。
- 分组标题 `导航 / 配置` 用 `--fs-100` + 字重 600 + `--on-surface-var`，**不再用内联 padding**（现 :1283）。
- 底部状态区（:1290-1295 调度灯 / 下次签到 / 今日已签 / 版本）保留，改成 `--surface-c-high` 容器 + 12px 栅格；调度灯三态：绿=就绪、琥珀=正在跑、灰=暂停/未激活。

### 6.2 页面头

每页统一：`页面标题(--fs-500)` + 一行说明（`--fs-200` / `--on-surface-var`）+ 右侧主操作按钮。当前各页标题层级混用，收敛成一个 `.page-head`。

### 6.3 统计 tile

```css
.tile { border-radius: var(--r-lg); background: var(--glass-bg);
        backdrop-filter: blur(var(--glass-blur)); border: 1px solid var(--glass-brd);
        padding: var(--sp-6); transition: border-radius var(--dur-2) var(--ease-emph),
        transform var(--dur-2) var(--ease-emph); }
.tile:hover { border-radius: var(--r-md); transform: translateY(-4px); }
.tile .v { font-size: var(--fs-600); font-weight: 700; letter-spacing: -.01em; }
```
数字用 `--fs-600`，标签 `--fs-100`，副说明 `--fs-200`。四个 tile 一行（≥1180），窄于 1180 变 2×2。

### 6.4 表格

- 表头 sticky，`--surface-c-high` 底，文字 `--fs-100` + 600。
- 行高 48px（≥44 目标），hover `--surface-c`，选中 `--primary-c` + 左侧指示条，与导航同一语言。
- 单元格文字 `--fs-200`；数字列 `--mono` + 右对齐。
- 行内操作收纳：主操作常显（1 个），其余进 `⋯` 菜单（现在每行 3-4 个按钮平铺是窄窗口挤爆的主因）。
- `<th>` 可点排序，`aria-sort` 标注。
- 长错误文本：单行省略 + `title`，**不撑高行**（现在 `last_error` 长句会把行拉成三行）。

### 6.5 按钮四种

| 型 | 用法 | 规格 |
|---|---|---|
| Filled | 页面唯一主操作（本机立即签到、激活卡密） | `--primary` / 白字，`--r-md`，高 40，`--elev-1` |
| Tonal | 次要（刷新积分、同步） | `--secondary-c` / `--on-secondary-c` |
| Outlined | 第三级（清理日志、导出） | 1px `--outline`，透明底 |
| Text | 行内、对话框右下 | 无边框，hover 底色 `--surface-c` |

状态：`:focus-visible` 统一 2px `--primary` 外环 + 2px offset（现在全站没有 focus 样式）；disabled 38% 透明 + `cursor: not-allowed`；进行中在按钮左侧转 12px 环，**文字不变**（防抖动）。

### 6.6 分段控件 / chip

`.seg` 用于平台筛选、任务模式开关：容器 `--surface-c-high` + `--r-md`，滑块 `--surface-c-lowest` + `--elev-1`，切换走 `--dur-2`。
chip 一律 `--r-full`、`--fs-100`、容器色按 §5 映射。

### 6.7 对话框 / 抽屉

`--surface-c-high` + `--r-lg` + `--elev-3`，遮罩 `rgba(15,27,38,.42)`（深色 `rgba(0,0,0,.6)`）。进入 `scale(.96)→1` + `--dur-2 --ease-emph`。键盘：Esc 关闭、Tab 锁定在内部、焦点回触发元素。
当前激活门禁、绑定邮箱、删除确认三处弹窗共用这套，不再各写各的 inline style。

### 6.8 反馈

- Snackbar：底部居中，`--on-surface` 底 + `--surface` 字，`--r-md`，4 秒；"让路/已有任务在跑"这类 `busy` 提示走它（对齐 §P2-7 后端返回的 `busy` 字段），不弹模态。
- 实时日志区：`--mono` `--fs-200`，行首 6px 状态点按级别着色，自动跟随最新一条（用户手动上滚即停跟随）。

### 6.9 图标

内联 SVG sprite，14 个起步：签到(✓)、刷新(↻)、服务器、本机、设置、说明、告警、时钟、chevron、⋯菜单、导出、关闭、绑定、同步。统一 20px 盒、1.6 描边、`currentColor`，随文字色走，不需要第二套配色。

### 6.10 空 / 加载 / 错误三态

每页都要有，不能只有一片白：
- 空：插画位（可用纯几何）+ 一句"为什么空" + 一个主操作（如"导入账号"）。
- 加载：骨架行（表格 4 行、tile 3 块），不用转圈遮全页。
- 错误：说明 + 「重试」+ 让路类原因直显后端 `message`（已脱敏，可安全展示）。

---

## 7. 页面线框与交互

### 7.1 概览（home）

```
┌ page-head  概览 · 下一次 09:10 左右            [本机立即签到] [刷新积分] ┐
│ ┌ 已跑 ┐ ┌ 未领 ┐ ┌ 待办 ┐ ┌ 异常 ┐  ← 四档 tile，点击= 跳到账号页并带筛选
│ ├────────────────────────────────────────────────┤
│ │ 今日节奏   早窗 09:10 ▓▓▓▓░░ 12/15   晚窗 20:00 ░░ 未跑    │
│ ├────────────────────────────────────────────────┤
│ │ 需要你处理（3）  · 2 个号 token 将过期  · 1 个号积分低于阈值  │
│ └────────────────────────────────────────────────┘
```
改动要点：现有"快捷操作 + 入池方式"两张静态卡下移；**首屏优先回答"今天到底签了没、还有什么是坏的"**，异常为 0 时整块折叠成一行绿字。

### 7.2 账号管理（accounts）

```
page-head  账号管理 · 15 个（TRAE 9 / WB 6）        [导入] [上传代跑] [＋]
seg: 全部 | TRAE | WB        search: [____]      filter: 异常▾ 未跑▾ 代跑▾
┌ 表格：状态 chip | 名称 | 平台 | 模式 | 积分 | 连签 | 上次成功 | ⋯ ┐
```
**平台筛选从侧栏移到这一页的 page-head 右侧**（现 :1282-1289 挂在全局导航区，却只作用这一页 —— IA 错位）。侧栏那个位置腾给"账号额度"进度条（`accountLimit` / `account_quota` 两个不同概念，之前只在顶部标签挤着）。

### 7.3 今日签到（today）

时间轴（早窗 / 晚窗两段）+ 账号结果列表。每行显示 `today_status` chip 与 `today_message`；`未开放` 归到"未领"分组而非"失败"。

### 7.4 签到记录（credits）

上半：积分折线（纯 SVG，单色 `--primary`，容器底色分层）；下半：可筛选记录表。加"导出 CSV"（outlined 按钮，走已有前端导出路径）。

### 7.5 签到设置 / 系统设置

分组卡片（栅格 2 列，≥1440 三列），每组：标题 + 说明 + 控件行。
- 开关统一 M3 switch（48×24 轨道、20 手柄、`--dur-2`）。
- 数字输入带范围提示（错峰间隔 0-600、同步间隔 1-240），非法值即时标红，不等到保存。
- **新增「主题」三选：跟随系统 / 浅色 / 深色**（放在系统设置顶部，见 §8）。

### 7.6 使用说明（help）

文档正文渲染，`--fs-300` / 行高 1.75，最大宽度 72ch，右侧粘性小目录。

---

## 8. 主题切换的落地方式

- CSS：`html[data-theme="light"]` / `html[data-theme="dark"]` 两组变量覆盖；`data-theme` 缺省时由 `@media (prefers-color-scheme: dark)` 提供初始值。
- 后端：`settings.py` 增 `theme`（`auto|light|dark`，默认 `auto`），随现有 `load_settings` 下发；界面启动时写入 `data-theme`，并监听 `matchMedia('(prefers-color-scheme: dark)')` 变化实时跟随。
- 切换不加过渡遮罩（避免整屏闪），只让颜色本身走 `--dur-2`。
- Tk 端：同一份 `theme` 字段驱动 `ttk.Style` 主题名与调色板（见 §12）。
- 验收：两套主题下重跑对比度复测，任何前景/背景组合 < 4.5 视为不通过。

---

## 9. 现状漂移 → 令牌 对照（第一期要清账的清单）

| 位置 | 现状 | 收进 |
|---|---|---|
| :73, :80 | `#c9d7e6` / `#aec3d8` 滚动条 | `--outline` / `--outline-var` |
| :100, :181, :309, :592 | `#fff` 硬写 | `--surface-c-lowest` / `--on-primary` |
| :105, :310 | `rgba(8,74,134,.22)` / `rgba(10,111,194,.24)` 阴影 | `--elev-1` / `--elev-2` |
| :122-123, :148-149, :180-182, :206 | 一堆 `rgba(255,255,255,.14~.28)` | `--glass-bg` / `--glass-brd` |
| :196-202 | 状态点 `rgba(20,180,120,.28)` / `rgba(235,170,40,.28)` | `--ok` / `--warn` 的容器档 |
| :275, :604 | `#cfe6f9` / `#c2d3e4` 边框 | `--outline-var` |
| :18, :27 | `--muted` 3.02:1、`--danger` 4.49:1 | `--on-surface-var #41525F`、`--error #B3261E` |
| :1162, :1185, :1222-1227, :1283, :1379, :1427, :1431 | 39 处内联 `style`（布局属性写死在 HTML） | 全部改语义 class；仅保留 JS 动态绑定的显隐 |
| :517, :527 | 只有 900 / 820 两个断点 | 900 / 1180 / 1440 三档（+ 侧栏收起） |

---

## 10. 可访问性核对（已实算）

浅色：body 16.5、次文字 7.7、主按钮白字 6.6、ok 5.6、warn 6.1、danger 5.4、info 5.9。
深色：body 15.0、次文字 7.9、on-primary 7.6、ok 9.7、warn 9.8、danger 10.0、info 9.0。全部 ≥4.5，其中 ≥7 的达 12/20 组（AA→AAA）。

其余必做项：
- `:focus-visible` 外环（当前无）；Tab 顺序与视觉顺序一致，模态锁焦点。
- 所有可点目标 ≥40×40px（表格行内小按钮目前明显偏小）。
- 状态不只靠颜色：chip 同时给文字与图标（色盲可用）。
- 表格用语义 `<table>` + `<caption>`/`scope`，导航 `<nav>` + `aria-current="page"`，状态灯 `aria-live="polite"`。
- `prefers-reduced-motion` 下关掉 tile 位移与形变。

---

## 11. 分期落地与验收

**第一期 · 令牌收敛（视觉基本不变）**
清 §9 全部条目：69 处硬编码色、39 处内联 style、字号 10→6、圆角 7→4、断点 2→3。
验收：`pytest` → `ruff check --select F,E9` → 重新打包 → 启动实跑逐页走查；对比"改版前/后"截图，除文字对比度与圆角外不应有可感知差异。

**第二期 · 明暗双主题 + 跟随系统**
`:root` 拆两套、`settings.theme` 三选、Tk 端配色同步、更新 `tests/test_native_gui_parity.py` 的令牌对齐断言。
验收：两套主题下对比度复测全过 + 打包实跑切换无闪烁。

**第三期 · 组件与 IA 重排**
tile 形变、表格收纳 `⋯` 菜单与 sticky 头、筛选下沉账号页、概览四档重排、三态补全、SVG 图标接入、focus 环。
验收：980×640 / 1280×800 / 1920×1080 三档无横向滚动、行高不被长错误文本撑破；键盘走查一遍全站点。

三期都各自过一次完整验收链条，不要攒到最后一次改。

---

## 12. 取舍与风险（先说清楚）

- **Tkinter 回退端拿不到同等效果**。没有 `backdrop-filter`、没有形变过渡。方案是"同色系降级"：吃同一份 `theme` 与色值表，圆角用直角近似。若你要求两端严格一致，那玻璃质感得整体放弃 —— 这条决定影响全站，需要你先点头。
- **大圆角 + 玻璃在数据表上容易变糊**。表格主体保持 `--surface-c-lowest` 实底，玻璃只用于侧栏、tile、对话框三类容器。
- **tile 的 hover 形变（28→16px）是这套语言的表情，但也是最贵的**。`prefers-reduced-motion` 下关闭，且只对可点 tile 生效。
- **状态从三档扩到四档会改变用户的读数习惯**（"未领"以前算在"已跑"里）。第一期不动，第三期一起换，并在 `docs/使用说明.md` 同步说明。
- **字号地板 12px 会让部分密集表格看起来更挤**，同时行高要提到 48px。窄窗口下用 `⋯` 收纳与列优先级隐藏来换空间，不靠缩字。
