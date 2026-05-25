# Eyeclaw

## 中文说明

### 1. 项目定位

Eyeclaw 是一个本地原型系统，用来学习任意网页上的操作流程，并把这些流程转成：

- 可阅读的操作手册
- 可回放的步骤计划
- 可进一步沉淀成技能或智能体的结构化产物

它的核心思路是：

1. 先记录真实操作
2. 再结合录屏、事件时间轴和多模态分析理解流程
3. 最后生成 SOP、步骤计划和后续可复用资产

### 2. 当前产品目标

Eyeclaw 不再限定某一个目标网站。

当前目标是支持：

- 任意网站
- 任意业务流程
- 任意需要人工参与的登录或验证步骤

对于扫码登录、短信验证码、图片验证码、二次确认等操作，当前默认策略是：

- 允许用户在流程中手动接管
- 系统记录这些人工接管点
- 智能体暂时不承诺自动完成这些认证步骤

### 3. 核心能力

- 监听浏览器中的真实点击、输入、切换、滚动和页面跳转
- 记录一次完整会话的关键事件时间轴
- 同步录制浏览器标签页内容
- 根据监听器时间轴引导关键帧抽取，而不是只做均匀抽帧
- 用多模态模型理解关键帧和操作语义
- 输出 SOP、回放步骤和后续技能/智能体素材

### 4. 系统工作方式

一个完整会话的理想流程是：

1. 点击“开始监听”
2. 系统同步开始：
   - 浏览器事件监听
   - 当前标签页录制
   - 会话时间轴记录
3. 用户按真实流程操作页面
4. 点击“停止监听”
5. 系统得到：
   - 一份监听事件流
   - 一段录屏
   - 一组关键截图
6. 分析阶段结合：
   - 监听时间轴
   - 录屏时间轴
   - 多模态关键帧理解
7. 输出：
   - 操作手册
   - 回放计划
   - 后续技能/智能体定义基础

### 5. 本地访问地址

默认情况下，前端地址是：

`http://127.0.0.1:8018`

如果软件运行在别人的电脑上，并且前端服务也是在那台电脑本机启动的，那么地址通常仍然是：

`http://127.0.0.1:8018`

这里的 `127.0.0.1` 表示“当前这台电脑本机”，不是某个固定用户的电脑。

需要注意：

- 前端服务必须已经启动
- `8018` 端口不能被其他程序占用
- 如果未来做自动端口切换，实际端口可能会变化

### 6. 浏览器扩展加载方式

当前浏览器监听器扩展位于：

`browser_listener_extension/`

当前有两种使用方式：

#### 方式 A：手动加载

1. 打开 `edge://extensions`
2. 打开“开发人员模式”
3. 点击“加载解压缩的扩展”
4. 选择 `browser_listener_extension` 目录

#### 方式 B：自动拉起专用 Edge 配置

为了减少手动安装步骤，可以通过命令行启动一个专用 Edge 配置，并自动带上本地扩展。

这种方式适合：

- 原型验证
- 内部演示
- 本地个人使用

需要明确的是：

- 当前可以做到“自动加载扩展并启动专用 Edge”
- 但浏览器安全策略下，未必能对用户自己的主 Edge 配置做完全静默、永久安装

### 7. 监听器当前约束

监听器默认应当处于关闭状态，只有在用户明确点击“开始监听”之后才开始采集。

目标行为是：

- 点击“开始监听”后才开始记录事件
- 同步开始录屏
- 点击“停止监听”后停止事件采集和录屏
- 每次开始监听自动创建新的 `session_id`

### 8. 当前技术方向

目前技术方向以本地原型为主：

- Python 后端
- Edge / Chrome 扩展
- 浏览器事件监听
- 标签页录制
- 多模态关键帧理解
- 结构化 SOP / 回放计划生成

### 9. 本地启动

#### 9.1 创建虚拟环境

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

#### 9.2 安装依赖

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

#### 9.3 安装 Playwright 浏览器支持

```powershell
.venv\Scripts\python -m playwright install msedge
```

#### 9.4 从本地模型配置生成 `.env`

```powershell
.venv\Scripts\python scripts\import_model_txt.py
```

#### 9.5 启动 HTML 控制台

```powershell
.venv\Scripts\python -m uvicorn app_web:app --host 127.0.0.1 --port 8018
```

然后打开：

`http://127.0.0.1:8018`

### 10. 安全说明

- 不要提交 `.env`
- 不要提交 `model.txt`
- 不要在截图或录屏中暴露密钥
- 如果密钥泄露，请立即轮换

### 11. 当前状态说明

当前仓库仍然是一个原型项目，不是生产版产品。

现阶段重点是先把最小闭环做稳：

- 开始监听
- 同步录屏
- 会话时间轴
- 监听器引导抽帧
- 多模态分析
- 生成手册 / 步骤 / 技能基础

---

## English Documentation

### 1. Project Positioning

Eyeclaw is a local prototype designed to learn real workflows from arbitrary websites and convert them into:

- readable operating manuals
- replayable action plans
- structured artifacts that can later become skills or agents

The core idea is:

1. capture real user actions first
2. combine recording, event timeline, and multimodal analysis
3. generate SOPs, replay plans, and reusable automation assets

### 2. Product Goal

Eyeclaw is no longer limited to a single target website.

The current goal is to support:

- arbitrary websites
- arbitrary business workflows
- manual checkpoints for authentication and verification

For QR login, OTP flows, CAPTCHAs, and confirmation dialogs, the current strategy is:

- allow human takeover during the session
- record those human-intervention checkpoints
- do not promise full autonomous handling of those authentication steps yet

### 3. Core Capabilities

- capture real browser clicks, input, navigation, tab changes, and scroll events
- maintain a session-level event timeline
- record the active browser tab
- guide key-frame extraction using listener timing instead of uniform frame sampling
- use multimodal models to interpret key frames and action semantics
- output SOPs, replay plans, and assets for future skills or agents

### 4. How the System Is Intended to Work

The target session flow is:

1. click “Start Listening”
2. the system starts, in sync:
   - browser event listening
   - active tab recording
   - session timeline tracking
3. the user performs the real workflow
4. click “Stop Listening”
5. the system now has:
   - listener events
   - a session recording
   - key screenshots
6. analysis combines:
   - listener timing
   - recording timing
   - multimodal frame understanding
7. outputs include:
   - an operating manual
   - a replay plan
   - a foundation for future skills or agent generation

### 5. Local Frontend Address

By default, the frontend runs at:

`http://127.0.0.1:8018`

If the software is installed on someone else’s computer and the frontend service also runs locally on that same machine, the address will usually still be:

`http://127.0.0.1:8018`

`127.0.0.1` always means “this machine itself”, not a specific user’s computer.

Notes:

- the frontend service must already be running
- port `8018` must be available
- in the future, the actual port may change if automatic port fallback is added

### 6. Browser Extension Loading

The browser listener extension lives in:

`browser_listener_extension/`

There are currently two ways to use it:

#### Option A: Load it manually

1. open `edge://extensions`
2. enable Developer Mode
3. click “Load unpacked”
4. choose the `browser_listener_extension` folder

#### Option B: Launch a dedicated Edge profile with the extension preloaded

To reduce manual setup, a dedicated Edge profile can be launched with the extension already attached.

This is suitable for:

- local prototyping
- demos
- internal usage

Important clarification:

- automatic extension loading into a dedicated Edge profile is feasible
- fully silent permanent installation into a user’s primary Edge profile is generally constrained by browser security policy

Example:

```powershell
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$ext = 'C:\Users\majia\Documents\liangzhu\browser_listener_extension'
$profile = 'C:\Users\majia\Documents\liangzhu\.browser\listener-profile'

Start-Process -FilePath $edge -ArgumentList `
  "--user-data-dir=$profile",`
  "--disable-extensions-except=$ext",`
  "--load-extension=$ext",`
  "edge://extensions",`
  "http://127.0.0.1:8018"
```

### 7. Listener Behavior

The listener should remain off by default and only begin collecting after the user explicitly clicks “Start Listening”.

The intended behavior is:

- start collecting only after the user clicks “Start Listening”
- start recording at the same time
- if recording cannot start, the listener session must fail instead of silently continuing
- stop both collection and recording when the user clicks “Stop Listening”
- create a new `session_id` every time a new listening session begins

### 8. Technical Direction

The project is currently built as a local-first prototype around:

- a Python backend
- an Edge / Chrome extension
- browser event listening
- tab recording
- multimodal key-frame analysis
- structured SOP and replay-plan generation

### 9. Local Setup

#### 9.1 Create a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

#### 9.2 Install dependencies

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

#### 9.3 Install Playwright browser support

```powershell
.venv\Scripts\python -m playwright install msedge
```

#### 9.4 Create `.env` from the local model config

```powershell
.venv\Scripts\python scripts\import_model_txt.py
```

#### 9.5 Run the HTML console

```powershell
.venv\Scripts\python -m uvicorn app_web:app --host 127.0.0.1 --port 8018
```

Then open:

`http://127.0.0.1:8018`

### 10. Security Notes

- do not commit `.env`
- do not commit `model.txt`
- do not expose secrets in screenshots or recordings
- rotate keys immediately if they are exposed

### 11. Current Status

This repository is still a prototype, not a production-grade product.

The near-term priority is to make the minimal closed loop reliable:

- start listening
- start recording at the same time
- maintain a unified session timeline
- extract frames using listener guidance
- run multimodal analysis
- generate a manual, action plan, and skill/agent foundation
