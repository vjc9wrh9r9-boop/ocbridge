# 免责声明 / Disclaimer

**最后更新：2026-10-16**

## 中文

### 1. 非官方项目

本软件（下称"OCBridge"或"本项目"）是第三方开源工具，与 **OpenCode**、**WorkBuddy**
及其各自的开发方、运维方、关联公司**没有任何关联**，未被其赞助、授权、认可或支持。
所有第三方名称、商标和标识（包括但不限于 "OpenCode"、"Zen"、"WorkBuddy"）
归其各自所有者所有，仅用于标识和说明用途。

### 2. 使用风险自担

本软件按**"原样"（AS IS）**提供，不附带任何明示或默示的担保，包括但不限于
对适销性、特定用途适用性及不侵权的担保。在适用法律允许的最大范围内，
作者及贡献者对因使用或无法使用本软件而产生的**任何直接、间接、附带、特殊或
后果性损害不承担任何责任**（包括但不限于账号限制、服务中断、数据丢失或利润损失）。

### 3. 遵守上游服务条款

本软件通过你本机安装的 OpenCode 组件，访问其对外公开的免费模型服务。
免费模型池的**可用性、额度与规则完全由上游自行决定，可能随时变更、限流或终止**。
你应当自行确保你的使用方式符合 OpenCode 及相关服务提供方的服务条款、
使用政策以及你所在司法辖区的适用法律。若你不认同或无法遵守相关条款，
请勿使用本软件。

### 4. 账号与数据

本软件**不收集、不上传、不分发任何账号凭据或使用数据**。登录凭据仅存储在你
本机（`~/.local/share/opencode` 目录），由你自行保管与处置。
请勿将凭据文件（如 `*-auth*.json`）分享、提交或公开给任何人。

### 5. 合法用途

本软件仅供**个人学习、技术研究及在合法授权范围内的个人使用**。
使用者应自行承担因违反任何适用法律或第三方条款而产生的一切后果。

### 6. 条款完整性

如本声明的任何条款被有管辖权的机构认定为不可执行，其余条款仍完全有效。

---

## English (Summary)

OCBridge is an **unofficial, third-party** open-source tool and is **not affiliated with,
endorsed by, sponsored by, or otherwise connected to** OpenCode, WorkBuddy, or their
respective owners. All third-party names and trademarks belong to their respective owners.

The software is provided **"AS IS"**, without warranty of any kind, express or implied.
To the maximum extent permitted by applicable law, the authors and contributors shall
**not be liable for any damages** arising from the use of or inability to use this software.

Free model availability, quotas and rules are determined solely by the upstream provider
and may change or be terminated at any time. **You are solely responsible** for ensuring
your use complies with the terms of service of upstream providers and all applicable laws.

This software does **not collect or transmit** any account credentials or usage data.
Login credentials are stored only on your local machine. Never share your credential files.

---

# 来源与致谢 / Credits & Sources

## 设计参考（仅借鉴思路，代码为独立实现）

- [**OC Ferry** — HRuiCcc/RuiC-oc-ferry](https://github.com/HRuiCcc/RuiC-oc-ferry)
  ——"把 OpenCode 的免费模型摆渡给 WorkBuddy"。本项目参考了其：
  按价格字段筛选免费模型的判定方式、`models.json` 的安全合并策略（owner 标记 / 只动自己的条目）、
  JSON 信封（信封协议）表达工具调用的思路。
- [**OW Bridge** — louchi1984-coder/ow-bridge](https://github.com/louchi1984-coder/ow-bridge)
  ——托盘应用形态与"隔离运行、模型导入"的产品思路。

感谢以上项目的作者们为社区提供的探索。

## 上游依赖与运行组件

| 组件 | 说明 | 许可证 |
|---|---|---|
| [OpenCode](https://github.com/anomalyco/opencode) | 免费模型的提供方；本软件的调用引擎（由 `setup --download` 从官方 npm 渠道获取，**本仓库不包含该二进制**） | MIT |
| [PyInstaller](https://pyinstaller.org/) | 打包为 exe 的工具（仅构建时使用） | GPL-2.0 with exception |
| [pystray](https://github.com/moses-palmer/pystray) | 系统托盘支持 | LGPL-3.0 |
| [Pillow](https://python-pillow.org/) | 图标绘制 | MIT-CMU |
| Python 标准库 | tkinter / sqlite3 / urllib / threading 等 | PSF |

## 本项目

代码为独立编写（MIT 协议，见 [LICENSE](LICENSE)）。
如果你认为本项目中的任何内容侵犯了你的权益，请提 Issue 联系处理。
