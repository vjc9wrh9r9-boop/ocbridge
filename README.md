# OCBridge — 把 OpenCode 免费模型接入 WorkBuddy

> A lightweight bridge that brings OpenCode's free model pool (Zen free tier) into WorkBuddy —
> native Windows GUI & system tray, built-in engine, one-click login. Pure Python standard library.

## 这是什么

OCBridge 在本机运行一个小型 OpenAI 兼容服务，把 **OpenCode Zen 免费模型池**（例如 `deepseek-v4.1-flash-free`，cost = 0）转接给 **WorkBuddy** 的自定义模型使用：

```
WorkBuddy (自定义模型)
   ↓ OpenAI 协议   http://127.0.0.1:8899/v1/chat/completions
OCBridge (本程序，纯 Python 标准库)
   ↓ opencode 本地 API (按需启动，空闲自动退出)
opencode serve (本机引擎)
   ↓
OpenCode Zen 免费模型 (cost=0)
```

两个关键设计：

- **懒启动 + 空闲自退**：不使用时 opencode 后端零内存占用（常驻仅 ~30MB）
- **内建登录（OAuth Device Flow）**：无需安装 OpenCode 客户端，浏览器确认一次即可

> 为什么需要"引擎"：OpenCode 免费层对调用来源有校验，纯 API 直调会被拒绝；
> 必须经由 opencode 本机组件发起调用。本程序通过其本地 API 完成（见 `bridge.py`）。

## 功能特性

- 原生 Windows GUI（Tkinter）+ 系统托盘（pystray）+ 命令行（CLI）双形态
- 自动发现免费模型：**按价格字段筛选**（input/output/cache 全为 0），不靠名字猜
- 可用性探测 + 缓存（6 小时），导入前过滤不可用模型
- 工具调用（Tool Calls）：模型输出 JSON 信封，桥转译为标准 OpenAI `tool_calls`，
  **执行权始终在客户端（WorkBuddy）侧**
- 会话缓存 + 增量发送：多轮对话只发送新消息
- 一键同步到 WorkBuddy 的 `models.json`：owner 标记 / 备份 / 原子写 / 不碰用户其他条目
- 登录态导出与续期命令（`export-auth` / `refresh-auth`）

## 快速开始（Windows，源码运行）

需要 Python 3.10+：

```bash
git clone https://github.com/vjc9wrh9r9-boop/ocbridge
cd ocbridge

python bridge.py setup --download   # 下载 opencode 引擎 (自动识别平台, 走国内镜像)
python bridge.py login              # 登录 (浏览器确认, 无需安装 OpenCode 客户端)
python bridge.py serve              # 启动桥服务

# 另开一个终端:
python bridge.py sync               # 扫描免费模型并导入 WorkBuddy
```

然后重启 WorkBuddy 客户端，在模型选择器中选择 `OC · xxx`。

### 命令行参考

```
python bridge.py serve        启动桥服务 (默认命令, 双击打包版等价)
python bridge.py sync         扫描免费模型 -> 探测 -> 导入 WorkBuddy (--fast 跳过探测)
python bridge.py status       检查登录 / 引擎 / 后端 / 模型状态
python bridge.py login        登录 OpenCode (Device Flow, 免客户端)
python bridge.py refresh-auth 续期登录凭据 (30 天)
python bridge.py export-auth  导出登录态 (换机场景, 可选)
python bridge.py import-auth  导入登录态 (换机场景, 可选)
python bridge.py setup        环境自检 (--download 自动下载引擎)
python bridge.py uninstall    从 WorkBuddy 移除本桥导入的模型
```

## 打包为 exe（可选）

```bash
pip install pyinstaller pystray pillow

# GUI 版 (onedir, 启动快)
pyinstaller --onedir --windowed --name OCBridge --icon OCBridge.ico \
  --hidden-import pystray._win32 ocbridge_gui.py

# CLI 版 (独立单文件)
pyinstaller --onefile --console --name OCBridge-CLI --icon OCBridge.ico \
  ocbridge_cli_entry.py
```

（仓库内提供 `ocbridge_dual.spec` 参考配置。）

## 常见问题

- **WorkBuddy 看不到模型**：重启 WorkBuddy 客户端；确认已执行 `sync`。
- **提示未登录**：`python bridge.py login` 完成一次浏览器确认。
- **找不到引擎**：`python bridge.py setup --download` 自动下载。
- **免费池变化**：官方为"限时"策略，模型可能增减；重跑 `sync` 刷新即可。

## 免责声明

本项目为**非官方工具**，与 OpenCode、WorkBuddy 及其关联公司**没有任何关联**。
软件按"原样"提供、使用风险自担；请确保你的使用方式符合上游服务的条款。
完整条款请阅读 **[DISCLAIMER.md](DISCLAIMER.md)**——使用本软件即表示你已理解并接受。

## 来源与致谢

设计思路参考了 [OC Ferry](https://github.com/HRuiCcc/RuiC-oc-ferry) 与
[OW Bridge](https://github.com/louchi1984-coder/ow-bridge)，上游依赖
[OpenCode](https://github.com/anomalyco/opencode) 等。详见
**[DISCLAIMER.md#来源与致谢credits--sources](DISCLAIMER.md)**。

## License

[MIT](LICENSE)
