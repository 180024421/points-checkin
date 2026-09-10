# 积分签到工具 CheckinTool

卡密门禁下，自动签到 **WorkBuddy** 与 **TraeWork** 积分；支持本机调度或授权 run-jane 代跑。

## 交付

- 对外产品形态：**Windows EXE**（`dist\CheckinTool.exe`）
- 界面：PyWebView 简约白底卡片（对齐 Cursor 工具交付形态）
- 备用：`python -m checkin_tool --native`（Tk）
- 图标：`assets/icon.ico`

```bat
pip install -r requirements.txt
playwright install chromium
build_gui.bat
```

详见 [docs/使用说明.md](docs/使用说明.md)。
