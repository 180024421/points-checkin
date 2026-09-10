# 积分签到工具 — 设计规格（实现对照）

日期：2026-09-10  
状态：已实现 P0  
代码仓：`D:\xiangmu\points-checkin`  
服务端：`D:\project\run-jane-script`（`app_key=points-checkin`）

## 决策摘要

| 项 | 选择 |
|---|---|
| 交付 | 对外 EXE + 卡密 |
| 账号 | 客户自有号（先登录再采集） |
| 模式 | 本机跑 / 服务器代跑 |
| 鉴权 | 卡密 + 设备指纹 |
| 产品 | WorkBuddy + TraeWork |

## 产物

- `CheckinTool.exe` / `python -m checkin_tool`
- run-jane：`V061__points_checkin.sql` + `/api/points-checkin/*` + 日调度 Worker

## 非目标

共享池领取、假会员、macOS、Sync Key 打进客户包。
