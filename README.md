# 积分签到工具 CheckinTool

卡密门禁下，自动签到 **WorkBuddy** 与 **TraeWork** 积分；支持本机调度或授权 run-jane 代跑。

## 交付

- 对外产品形态：**Windows EXE**（`dist\CheckinTool.exe`）
- 界面：参考 Cursor 工具原生 Tk 简约布局（标题 + Tab + 底部日志）

```bat
pip install -r requirements.txt
playwright install chromium
build_gui.bat
```

详见 [docs/使用说明.md](docs/使用说明.md)。
