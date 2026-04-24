# MusIDE

一款 AI 驱动的音乐制作 IDE，内置多轨道编辑器、波形显示、混音台、钢琴卷帘，以及智能音乐创作助手。

基于 Python Flask 后端 + Web Audio API 前端，提供完整的音轨编辑、音频播放控制、混音处理、AI 音乐创作辅助等功能。

> 基于 [PhoneIDE](https://github.com/ctz168/ide) 改造，将代码编辑器替换为专业级 DAW 风格音乐轨道编辑器。

## 快速开始

### 一行命令安装（推荐）

**全自动**：自动检测平台、安装 Python 依赖、克隆仓库、启动服务、检测端口、打开浏览器。一行命令，开箱即用：

**Windows（PowerShell）：**
```powershell
irm https://raw.githubusercontent.com/ctz168/muside/main/install.ps1 | iex
```

**Linux / macOS / Termux：**
```bash
curl -fsSL https://raw.githubusercontent.com/ctz168/muside/main/install.sh | bash
```

安装完成后浏览器会自动打开，按 Ctrl+C 可停止服务。

> 支持平台：Windows 10/11、Termux、Ubuntu/Debian、Fedora、CentOS、macOS、Alpine、Arch、openSUSE
>
> 默认地址：`http://localhost:12346`

**自定义安装目录：**
```bash
# Linux / macOS
MUSIDE_INSTALL_DIR=~/my-muside curl -fsSL https://raw.githubusercontent.com/ctz168/muside/main/install.sh | bash
# Windows
$env:MUSIDE_INSTALL_DIR="C:\my-muside"; irm https://raw.githubusercontent.com/ctz168/muside/main/install.ps1 | iex
```

**自定义端口：**
```bash
# Linux / macOS
MUSIDE_PORT=8080 curl -fsSL https://raw.githubusercontent.com/ctz168/muside/main/install.sh | bash
# Windows
$env:MUSIDE_PORT="8080"; irm https://raw.githubusercontent.com/ctz168/muside/main/install.ps1 | iex
```

### 手动安装

**Windows：**
```powershell
# 1. 安装 Python 3.8+（去 https://www.python.org/downloads/ 下载，安装时勾选 Add to PATH）
# 2. 打开 PowerShell
pip install flask flask-cors
git clone https://github.com/ctz168/muside.git
cd muside
python muside_server.py
# 浏览器打开 http://localhost:12346
```

**Termux：**
```bash
pkg install python python-pip
pip install flask flask-cors
git clone https://github.com/ctz168/muside.git && cd muside
python3 muside_server.py
```

**Ubuntu / Debian / WSL：**
```bash
sudo apt install python3 python3-pip python3-venv
pip3 install --break-system-packages flask flask-cors
git clone https://github.com/ctz168/muside.git && cd muside
python3 muside_server.py
```

**macOS：**
```bash
brew install python
pip3 install flask flask-cors
git clone https://github.com/ctz168/muside.git && cd muside
python3 muside_server.py
```

**Fedora / CentOS：**
```bash
sudo dnf install python3 python3-pip
pip3 install flask flask-cors
git clone https://github.com/ctz168/muside.git && cd muside
python3 muside_server.py
```

**Alpine：**
```bash
sudo apk add python3 py3-pip
pip3 install --break-system-packages flask flask-cors
git clone https://github.com/ctz168/muside.git && cd muside
python3 muside_server.py
```

### Docker

```bash
docker run -d -p 12346:12346 -v ~/muside_workspace:/workspace python:3.12-slim bash -c \
  "pip install flask flask-cors && git clone --depth 1 https://github.com/ctz168/muside.git /muside && cd /muside && MUSIDE_WORKSPACE=/workspace python3 muside_server.py"
```

启动后浏览器打开 `http://localhost:12346` 即可使用。

## 功能特性

### 多轨道编辑器（DAW 风格）

基于 Web Audio API 和 Canvas 构建的专业级多轨道编辑器，支持最多 16 条音轨。每条音轨可独立设置名称、颜色、音量、声像、静音、独奏和录制准备。支持添加、删除、重命名音轨，双击音轨名即可修改。

**轨道管理**：每条音轨配有音量滑块、声像旋钮、静音/独奏/录制按钮、音频文件加载按钮。默认创建 4 条音轨（主旋律、伴奏、鼓组、贝斯），开箱即用。

### 时间轴与播放控制

水平时间轴，基于 BPM 的节拍/小节网格（默认 120 BPM，4/4 拍）。播放头（红色竖线）在播放时实时移动，点击时间轴即可跳转。传输栏提供播放、暂停、停止、录制、循环按钮，以及时间显示（MM:SS:ms）和总时长。支持缩放（按钮 + Ctrl+滚轮），BPM 和拍号可在传输栏中实时调整。

### 波形显示

每条音轨在 Canvas 上渲染立体声波形（左声道在上，右声道在下）。加载音频文件后自动提取峰值数据绘制波形，波形颜色与音轨颜色一致，视觉直观。

### 音频播放引擎（Web Audio API）

基于 AudioContext 的播放引擎，支持多条音轨同时播放。每条音轨通过 GainNode 控制音量、StereoPannerNode 控制声像。主音量通过主 GainNode 控制。完整的播放/暂停/停止状态管理，支持循环模式。Solo/Mute 逻辑完善（Solo 时仅播放 Solo 音轨，Mute 音轨静音）。自动处理移动端 AudioContext 恢复。

### 音频片段管理

每条音轨可包含多个音频片段，每个片段有起始时间、持续时间、偏移量、文件路径和波形数据。片段在时间轴上以彩色块渲染，带有波形覆盖。点击选中片段（高亮边框），拖拽可水平移动，拖拽左/右边缘（6px 热区）可裁剪片段。

### 混音台

可切换的垂直混音台视图，将所有音轨显示为垂直通道。每个通道配有垂直推子（音量）、声像滑块、VU 表（峰值电平）、静音/独奏按钮。VU 表通过 AnalyserNode 数据实时更新，主通道配有主音量推子。

### 钢琴卷帘

基础钢琴卷帘编辑器，包含音轨选择器、左侧钢琴键和顶部节拍网格。支持 3 个八度范围（C3-B5），点击添加/删除音符，音符以与音轨颜色匹配的矩形显示。

### AI 音乐创作助手

右侧滑出面板集成 LLM 对话功能，支持配置任意 OpenAI 兼容 API（自定义 API 地址）。AI 内置音乐创作专家系统提示词，具备乐理、编曲、音频工程、MIDI 编程等专业能力。AI Agent 内置 18 种音频工具：播放/停止/暂停/跳转音频、加载/编辑/导出/录制音频、音轨管理（增删查看）、音轨属性设置（音量/声像/静音/独奏）、项目设置（BPM/拍号/项目信息）。对话历史自动保存，支持多轮对话和多会话管理。

### 文件管理

完整的文件树浏览体验，支持打开任意文件夹作为工作空间。可以新建音频文件和目录、重命名、删除。文件列表自动识别音频文件类型并显示对应图标。

### Git 集成

内置全套 Git 操作界面：查看状态、提交日志、分支切换、暂存区管理、提交、远程推送、拉取、仓库克隆、Diff 查看、Stash 暂存。

### 代码运行

支持直接在 IDE 中运行代码和命令行工具（如 ffmpeg、sox 等音频处理工具）。自动检测系统中已安装的编译器和运行时。运行输出通过 SSE 实时流式推送，运行中随时可以终止进程。

### 移动端优化

专为手机触屏设计：从左侧边缘右滑打开项目侧边栏，从右侧边缘左滑打开 AI 对话面板。深色 Catppuccin 配色方案，长时间音乐制作不伤眼。

## 项目结构

```
ctz168/muside/
├── muside_server.py              # Flask 入口，注册 Blueprint
├── utils.py                      # 共享工具函数、常量、配置管理
├── requirements.txt              # Python 依赖 (flask, flask-cors)
├── install.sh                    # Linux/macOS 全自动安装（安装+启动+打开浏览器）
├── install.ps1                   # Windows PowerShell 全自动安装（安装+启动+打开浏览器）
├── start.sh                      # 启动脚本（处理端口占用）
├── routes/
│   ├── __init__.py
│   ├── files.py                  # 文件 CRUD：列表、读取、保存、创建、删除、重命名、搜索
│   ├── run.py                    # 代码执行：运行、停止、进程列表、SSE 输出流
│   ├── git.py                    # Git 操作：status/log/branch/checkout/add/commit/push/pull/clone/diff/stash
│   ├── chat.py                   # LLM 对话 + AI Agent：60 种工具（含 18 种音频工具），OpenAI 兼容协议
│   ├── venv.py                   # 虚拟环境：创建、激活、包列表、编译器检测
│   ├── server_mgmt.py            # 服务管理：健康检查、配置、状态、重启、日志（SSE）
│   ├── browser.py                # 浏览器工具：网页截图、搜索
│   ├── debug.py                  # 调试面板：编译器选择、venv 管理
│   ├── update.py                 # 更新检查：代码更新(ctz168/muside)
│   ├── ast_index.py              # AST 索引：代码分析
│   └── system_prompt.txt         # AI 系统提示词（音乐创作专家）
└── static/
    ├── index.html                # 单页 DAW 界面（工具栏 + 传输栏 + 轨道编辑器 + 混音台 + AI面板）
    ├── css/
    │   └── style.css             # Catppuccin 深色主题，响应式布局
    ├── js/
    │   ├── app.js                # 主入口：手势控制、侧边栏切换、键盘快捷键
    │   ├── track-editor.js       # 多轨道编辑器：音轨/时间轴/波形/播放引擎/混音台/钢琴卷帘
    │   ├── files.js              # 音频文件树浏览与管理
    │   ├── git.js                # Git 操作界面
    │   ├── search.js             # 全局搜索与替换
    │   ├── terminal.js           # 代码运行与输出
    │   ├── chat.js               # LLM 对话与 Agent 工具执行（含音频工具 UI）
    │   ├── project.js            # 项目管理面板
    │   ├── editor.js             # 编辑器兼容层
    │   ├── debug.js              # 调试面板：编译器选择、venv 管理
    │   ├── debugger.js           # 调试器
    │   └── browser.js            # 浏览器工具
    └── vendor/
        ├── codemirror/           # CodeMirror 5（保留兼容）
        └── marked/               # Markdown 渲染器
```

## API 接口

服务端运行在 `http://localhost:12346`，所有 API 均返回 JSON。

### 文件管理

| 方法 | 接口 | 说明 |
|------|------|------|
| GET | `/api/files/list?path=<dir>` | 列出目录文件 |
| GET | `/api/files/read?path=<file>` | 读取文件内容 |
| POST | `/api/files/save` | 保存文件 |
| POST | `/api/files/create` | 创建文件/目录 |
| POST | `/api/files/delete` | 删除文件/目录 |
| POST | `/api/files/rename` | 重命名文件/目录 |
| POST | `/api/files/open_folder` | 打开文件夹为工作空间 |
| POST | `/api/search` | 全局搜索 |
| POST | `/api/search/replace` | 全局替换 |

### 代码执行

| 方法 | 接口 | 说明 |
|------|------|------|
| POST | `/api/run/execute` | 执行代码 |
| POST | `/api/run/stop` | 终止运行 |
| GET | `/api/run/processes` | 列出运行中进程 |
| GET | `/api/run/output` | 获取进程输出 |
| GET | `/api/run/output/stream` | SSE 实时输出流 |

### Git 操作

| 方法 | 接口 | 说明 |
|------|------|------|
| GET | `/api/git/status` | Git 状态 |
| GET | `/api/git/log` | 提交日志 |
| GET | `/api/git/branch` | 分支列表 |
| GET | `/api/git/diff` | 查看 Diff |
| GET | `/api/git/remote` | 远程仓库信息 |
| POST | `/api/git/checkout` | 切换分支 |
| POST | `/api/git/add` | 暂存文件 |
| POST | `/api/git/commit` | 提交 |
| POST | `/api/git/push` | 推送 |
| POST | `/api/git/pull` | 拉取 |
| POST | `/api/git/clone` | 克隆仓库 |
| POST | `/api/git/stash` | Stash |
| POST | `/api/git/reset` | Reset |

### AI 对话

| 方法 | 接口 | 说明 |
|------|------|------|
| POST | `/api/chat/send` | 发送消息（非流式） |
| POST | `/api/chat/send/stream` | 发送消息（SSE 流式） |
| GET | `/api/chat/history` | 获取对话历史 |
| POST | `/api/chat/clear` | 清除对话历史 |
| GET | `/api/llm/config` | 获取 LLM 配置 |
| POST | `/api/llm/config` | 更新 LLM 配置 |

### 虚拟环境 & 编译器

| 方法 | 接口 | 说明 |
|------|------|------|
| GET | `/api/compilers` | 检测可用编译器 |
| POST | `/api/venv/create` | 创建虚拟环境 |
| POST | `/api/venv/activate` | 激活虚拟环境 |
| GET | `/api/venv/list` | 列出虚拟环境 |
| GET | `/api/venv/packages` | 查看已安装包 |

### 服务管理

| 方法 | 接口 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 获取配置 |
| POST | `/api/config` | 更新配置 |
| GET | `/api/server/status` | 服务器状态（端口、内存、进程数） |
| POST | `/api/server/restart` | 重启服务器 |
| POST | `/api/server/logs` | 获取日志 |
| GET | `/api/server/logs/stream` | SSE 实时日志流 |
| GET | `/api/system/info` | 系统信息 |

### 更新

| 方法 | 接口 | 说明 |
|------|------|------|
| POST | `/api/update/check` | 检查更新 |
| POST | `/api/update/apply` | 应用代码更新 |

## AI 音乐工具列表

AI Agent 内置 18 种音频/音乐制作工具，可直接通过对话调用：

### 播放控制

| 工具名 | 说明 |
|--------|------|
| `play_audio` | 播放音频 |
| `stop_audio` | 停止播放 |
| `pause_audio` | 暂停播放 |
| `seek_audio` | 跳转到指定位置 |

### 音频文件操作

| 工具名 | 说明 |
|--------|------|
| `load_audio` | 加载音频文件到音轨（支持 .wav/.mp3/.ogg/.flac/.aiff/.aac/.m4a） |
| `edit_audio` | 编辑音频（裁剪/淡入淡出/标准化/反转/变速/变调） |
| `export_audio` | 导出音频 |
| `record_audio` | 录制音频 |

### 音轨管理

| 工具名 | 说明 |
|--------|------|
| `list_tracks` | 列出所有音轨信息 |
| `add_track` | 添加新音轨 |
| `remove_track` | 删除音轨 |

### 音轨属性

| 工具名 | 说明 |
|--------|------|
| `set_track_volume` | 设置音轨音量（0-1） |
| `set_track_pan` | 设置音轨声像（-1 到 1） |
| `set_track_mute` | 设置音轨静音 |
| `set_track_solo` | 设置音轨独奏 |

### 项目设置

| 工具名 | 说明 |
|--------|------|
| `set_bpm` | 设置 BPM（20-300） |
| `set_time_signature` | 设置拍号 |
| `get_project_info` | 获取项目完整信息 |

## 轨道编辑器 API

`TrackEditor` 是前端轨道编辑器模块，提供以下公共 API：

```javascript
TrackEditor.init()                          // 初始化到 #track-editor-container
TrackEditor.addTrack(name)                  // 添加音轨
TrackEditor.removeTrack(id)                 // 删除音轨
TrackEditor.getTracks()                     // 获取所有音轨数据
TrackEditor.play()                          // 开始播放
TrackEditor.pause()                         // 暂停播放
TrackEditor.stop()                          // 停止并回到起始位置
TrackEditor.record()                        // 切换录制模式
TrackEditor.setBPM(bpm)                     // 设置 BPM
TrackEditor.setTimeSignature(num, den)      // 设置拍号
TrackEditor.loadAudioFile(trackId, file, startTime)  // 加载音频到音轨
TrackEditor.seek(time)                      // 跳转到指定时间
TrackEditor.getCurrentTime()                // 获取当前播放时间
TrackEditor.getDuration()                   // 获取项目总时长
TrackEditor.setTrackVolume(trackId, vol)    // 设置音量 (0-1)
TrackEditor.setTrackPan(trackId, pan)       // 设置声像 (-1 到 1)
TrackEditor.setTrackMute(trackId, bool)     // 设置静音
TrackEditor.setTrackSolo(trackId, bool)     // 设置独奏
TrackEditor.exportState()                   // 导出项目状态为 JSON
TrackEditor.importState(json)               // 导入项目状态
TrackEditor.resize()                        // 处理窗口大小变化
```

## 配置说明

IDE 配置存储在 `~/.muside/config.json`：

```json
{
  "workspace": "~/muside_workspace",
  "venv_path": "",
  "compiler": "python3",
  "theme": "claude",
  "font_size": 14,
  "tab_size": 4,
  "show_line_numbers": true,
  "github_token": "",
  "github_auth_method": ""
}
```

LLM API 配置存储在 `~/.muside/llm_config.json`：

```json
{
  "models": [
    {
      "name": "OpenAI",
      "provider": "openai",
      "api_type": "openai",
      "api_key": "sk-xxx",
      "api_base": "https://api.openai.com/v1",
      "model": "gpt-4o-mini",
      "enabled": true,
      "temperature": 0.7,
      "max_tokens": 100000,
      "max_context": 128000,
      "reasoning": true
    }
  ],
  "system_prompt": "你是一个专业的音乐创作助手..."
}
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MUSIDE_PORT` | `12346` | 服务监听端口 |
| `MUSIDE_HOST` | `0.0.0.0` | 绑定地址 |
| `MUSIDE_WORKSPACE` | `~/muside_workspace` | 默认工作空间路径 |

## 手势操作（移动端）

| 手势 | 功能 |
|------|------|
| 左侧边缘右滑 | 打开项目侧边栏 |
| 右侧边缘左滑 | 打开 AI 对话面板 |
| 在已打开的侧栏上左滑 | 关闭侧栏 |
| 长按文件 | 弹出上下文菜单（重命名、删除等） |

## 更新机制

```bash
cd muside
git pull
# 重启 muside_server.py 即可
```

## 环境要求

| 项目 | 最低要求 |
|------|----------|
| Python | 3.8+ |
| 依赖包 | flask >= 3.0.0, flask-cors >= 4.0.0 |
| Git | 用于克隆仓库（安装脚本会自动安装） |
| 操作系统 | Windows 10/11, macOS, Linux (Termux/Ubuntu/Debian/Fedora/CentOS/Alpine/Arch) |
| 浏览器 | Chrome / Firefox / Safari / Edge（近两年版本，需支持 Web Audio API） |
| 推荐音频工具 | ffmpeg, sox（用于音频格式转换和处理） |

## 相关仓库

| 仓库 | 说明 |
|------|------|
| **ctz168/muside** (本仓库) | MusIDE 音乐制作 IDE（Flask 后端 + DAW 前端） |
| [ctz168/ide](https://github.com/ctz168/ide) | PhoneIDE 代码编辑器 IDE（原项目） |

## 技术栈

- **后端**: Python Flask + Flask-CORS
- **前端**: 原生 HTML/CSS/JavaScript（无框架）
- **音频引擎**: Web Audio API（AudioContext / GainNode / StereoPannerNode / AnalyserNode / BufferSourceNode）
- **渲染**: Canvas 2D（波形、时间轴、钢琴卷帘）
- **AI 集成**: OpenAI 兼容 API 协议（支持多模型配置）
- **实时通信**: Server-Sent Events (SSE)
- **主题**: Catppuccin Mocha 深色配色

## 许可证

MIT License
