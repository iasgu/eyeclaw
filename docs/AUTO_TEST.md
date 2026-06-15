# EyeClaw 自动测试说明

这套脚本用于验证别人拿到 EyeClaw 后，能不能安装、启动、打开页面、保存监听事件、保存截图和上传录屏文件。它不会清空历史记录，也不依赖真实业务网站。

## 入口

在 PowerShell 里执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
D:\Codex\liangzhu\scripts\auto_test_windows.ps1
```

默认运行 `integration` 模式。也可以指定模式：

```powershell
D:\Codex\liangzhu\scripts\auto_test_windows.ps1 -Mode smoke
D:\Codex\liangzhu\scripts\auto_test_windows.ps1 -Mode integration
D:\Codex\liangzhu\scripts\auto_test_windows.ps1 -Mode full
D:\Codex\liangzhu\scripts\auto_test_windows.ps1 -Mode extension
```

如果服务已经由 `scripts\run_windows.ps1` 启动，可以复用现有服务：

```powershell
D:\Codex\liangzhu\scripts\auto_test_windows.ps1 -UseExistingServer
```

## 测试模式

| 模式 | 适用场景 | 主要检查 |
| --- | --- | --- |
| `smoke` | 最快确认环境和服务能不能启动 | Python、`.venv`、依赖、项目文件、扩展 manifest、后端启动、`/api/status`、基础 API、前端 HTML |
| `integration` | 默认推荐，确认核心链路能保存数据 | `smoke` 内容，加监听事件写入、截图保存、模拟录屏上传、录屏列表查询 |
| `full` | 交付前自测 | `integration` 内容，加 headless Edge 打开前端、关键 DOM 检查、扩展录屏代码静态诊断、真实扩展录屏手工提示 |
| `extension` | 专项排查扩展录屏链路 | 扩展权限、offscreen/tabCapture/MediaRecorder 代码标记、后端录屏上传接口、真实录屏手工提示 |

## 报告位置

每次运行都会生成一个独立目录：

```text
D:\Codex\liangzhu\artifacts\auto_tests\<时间戳>\
```

里面至少包含：

```text
report.json
report.md
server.out.log
server.err.log
frontend-browser.png
```

其中 `frontend-browser.png` 只会在 `full` 模式且 UI 浏览器检查实际运行时生成。

## 结果含义

- `PASS`：该项通过。
- `WARN`：不阻断基础使用，但需要人工确认，例如 `.env` 未填写模型 key、没有跑真实扩展录屏。
- `FAIL`：该项失败，脚本会以非 0 退出码结束。

报告底部的 `Recommendations` 会给出定位建议，例如：

- 依赖缺失：重新运行 `scripts\install_windows.ps1`。
- 端口占用：换端口或停止已有服务。
- 后端未启动：查看 `server.out.log` 和 `server.err.log`。
- 扩展录屏失败：确认 Edge 已加载 `browser_listener_extension`，并在普通 http/https 页面测试“开始演示 / 停止演示”。

## 真实扩展录屏专项验证

自动脚本可以验证扩展文件、权限、后端上传接口，但不能完全替代浏览器权限弹窗和真实 tabCapture 录屏。交付前建议再做一次手工辅助测试：

1. 运行 `D:\Codex\liangzhu\scripts\run_windows.ps1`。
2. Edge 打开后确认扩展已启用。
3. 在 EyeClaw 页面点击“开始演示”。
4. 切换到一个普通网站页面，完成几步真实操作。
5. 回到 EyeClaw 页面点击“停止演示”。
6. 确认“可继续分析的录制视频”里出现新录屏，并且报告里能看到事件数和截图数。

如果这一步失败，重点看：

- Edge 是否禁止扩展录屏权限。
- service worker 是否在扩展管理页报错。
- `offscreen.html` / `offscreen.js` 是否能创建 `MediaRecorder`。
- 后端是否收到 `POST /api/browser-listener/session-recording`。
- `artifacts\session_recordings` 是否出现对应 `.webm` 文件。
