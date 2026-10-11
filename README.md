[English](README.en.md) | **中文**

# dsh-computer-use

**让 DeepSeek Harness 的 Agent 看屏幕、动鼠标键盘、按元素操作界面。**

一个 DSH 插件（macOS）：内置 stdio MCP 服务器，以 `mcp__computer-use__*` 工具出现在会话里，
并在 **设置 → 电脑操控** 里提供观察窗开关、急停与权限指引。

![设置 → 电脑操控](docs/images/settings-page.png)

## 它长什么样

Agent 每次碰电脑时，屏幕上会浮出一块**观察窗**，实时显示它正在操作的那扇窗。你可以一直看着它做，
也可以按左上角的 ⏹ 立刻叫停（下一个可中断点 ≤0.1 秒）。设置页（上图）里能关掉观察窗、
选回合结束后多久自动收起、手动急停 / 恢复，并查看两项系统权限的状态。

## 运行要求

| | |
|---|---|
| 系统 | macOS 13+（Intel / Apple Silicon 都可以） |
| Python | 3.10+（实测 3.12 最稳；python.org 的 framework 版最好） |
| Node | 22+ 与 npm（构建 Host 半区用；`lib/` 是产物，不在仓库里） |
| DeepSeek Harness | **0.2.0-rc.2 及以上（0.2.x）** —— 在 `peerDependencies` 里声明，不兼容的版本 DSH 会在加载前拦下 |
| 系统权限 | 「辅助功能」+「屏幕录制」两项，见下 |
| 磁盘 | 约 130MB（其中 `.venv` 96MB） |
| 可选 | Xcode Command Line Tools —— 只为编译抓屏 helper；不装会自动退回 `screencapture` |

## 安装

一条命令，可重复执行：

```bash
git clone https://github.com/OGHong/dsh-computer-use && cd dsh-computer-use
sh scripts/install.sh
```

它把这些都做完：

1. 建 `.venv` 并装依赖（含预览浮窗要的 pyobjc）；
2. 构建 Host 半区（`src/index.ts` → `lib/index.js`）；
3. 把 `cordis.patch.yml` 里的绝对路径改成本机插件目录；
4. 没有抓屏 helper 就试着编一个（失败自动退回 `screencapture`）；
5. **接进 profile**：`package.json` 里加 `link:` 依赖 + 加进 `dsh.profile.bundles` +
   建 `node_modules` 符号链接（改前先备份 `package.json`）；
6. 打开「系统设置 → 隐私与安全性」里那两项权限面板。

想先看它要做什么：`sh scripts/install.sh --dry-run`；
指定 profile：`--profile=web`；不想让它开设置面板：`--no-open-settings`。

**之后只剩两件谁也替不了你的事** —— 这不是插件偷懒：

1. **重启 DeepSeek Harness**：宿主与客户端的插件清单都在启动时扫描，热替换不会重读；
2. **在系统设置里点开那两项开关**：「辅助功能」（控制鼠标键盘）与「屏幕录制」（截屏）。
   macOS 的 TCC 权限不允许任何程序给自己授权，只能你点；没授权时截图黑屏、点击无效，
   `screen_info` 会如实报告状态。

## 用起来

在会话里直接说要做的事，例如：

- 「帮我看看屏幕上有什么」→ `screen_info` / `screenshot`
- 「打开计算器，算一下 1234×567」→ `launch_app` + 元素路径点击
- 「把这份文档的标题改成 …」→ `get_ax_state` 观察 + `set_value` / `type_text`

动作有两条路径：**元素路径**（按无障碍元素操作，后台应用也能点、不抢你的鼠标，优先）和
**截图坐标路径**（兜底，用于游戏、画布这类没有无障碍信息的界面）。每个动作都会回一行
`effect:` 证据，`unchanged` 表示这一步没生效。

## 工具一览

| 分类 | 工具 |
|---|---|
| 先看 | `screen_info` `screenshot` `list_apps` `list_windows` |
| 绑定应用（同时取控制器租约） | `get_app` `focus_app` `focus_window` `release_lease` |
| 元素路径（优先） | `get_ax_state` `elements` `click_element` `set_value` `perform_action` `select_text` |
| 坐标路径（兜底） | `click` `drag` `scroll` `type_text` `press_key` `paste` `wait` `launch_app` |
| 批处理与安全 | `sequence` `stop` `resume` `preview` |
| 其它 | `run_shell` `open_permission_settings` |

每个工具的参数、返回的 `effect` 证据与失败码都在 [docs/DESIGN.md](docs/DESIGN.md) 里；
`sequence` 能做的 = 工具能做的（同一份清单，见 `server/action_layer.py`）。

## 设置页（设置 → 电脑操控）

| 控件 | 作用 |
|---|---|
| 自动显示观察窗 | 关掉后不再自动出现，也不再占用屏幕录制与 CPU；Agent 照常操作电脑 |
| 回合结束后自动收起 | 5 秒 / 12 秒 / 30 秒 / 1 分钟 / 2 分钟 |
| 急停 / 恢复 | 与观察窗左上角的 ⏹ 等效：下一个可中断点（≤0.1 秒）停下 |
| 权限 | 一键跳到「系统设置 → 辅助功能」 |

## 配置覆盖

在 profile 的 `cordis.patch.yml` 里按 id 覆盖（不要重复 insert，会双重注册）：

```yaml
- id: computer-use
  config:
    command: /path/to/other/python
    toolCallTimeoutMs: 120000
```

## 已知边界（常用几条）

- `type_text` / `paste` 走剪贴板实现，会覆盖你当前的剪贴板；
- 坐标以**最近一张截图**为基准：全屏截图与单窗口截图的像素原点不同，结果里会回一行 `坐标系:`；
- **急停状态跨会话保留**：按过停止之后，新会话默认仍是只读，用 `resume()` 或浮窗上的 ▶ 恢复；
- 元素索引在两次观察之间保持稳定，界面大改后让 Agent 重新观察即可；
- `a11y` 策略下没有语义动作的动作（拖拽等）会**报错而不降级**，这是有意为之。

## 实现细节

架构、两条路径的设计取舍、多会话隔离、急停与租约、性能实测数字、开发与校验方式，
都在 **[docs/DESIGN.md](docs/DESIGN.md)**。

```bash
npm run verify     # 类型检查 + 构建 + 全部冒烟断言
```

## License

MIT
