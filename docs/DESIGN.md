# 设计笔记 · dsh-computer-use

这是**实现细节**，不是使用说明 —— 面向使用者的介绍在 [README](../README.md)。
这里记录的是：架构、两条操作路径的取舍、多会话隔离、急停与租约、性能实测数字、
开发与校验方式，以及踩过的坑（写给以后改这个插件的人，包括我自己）。

---

## 实现概览

让 DeepSeek Harness 的 Agent **看屏幕、动鼠标键盘、按元素操作界面**：对齐 ZCode 内置
computer-use 的能力。内置一个 stdio MCP 服务器（`server/server.py` + `server/ax_bridge.py`，
标准库 JSON-RPC + 纯 ctypes 的 Accessibility / CoreGraphics + screencapture），通过插件自带的
`cordis.patch.yml` 注册为 `@deepseek-ai/dsh-mcp-client` 实例（行 id `computer-use`）。
服务器侧另有两个小模块：`server/action_layer.py`（动作层/渲染层契约：`ActionResult`、
`possibly_sent`、急停与租约的入口闸门）、`server/killswitch.py`（急停）、`server/lease.py`
（控制器租约）。

## 两条操作路径

**元素路径（优先）** —— 无障碍树：
`list_apps` → `get_app`（绑定应用/列窗口/自动给 Electron 开 AX）→
`get_ax_state`（语义元素树，diff 模式只回新增/变化行）→
`click_element`（AXPress 语义点击，后台应用生效不抢鼠标）/ `set_value`（直写文本框）/
`perform_action`（AXRaise 等）/ `select_text`（按前后缀设选区）。
参数按 `inputSchema` 声明的类型过一遍再进工具（模型常把数字发成字符串 `{"x":"500"}`；官方 SDK
靠 pydantic 顺手转，手写协议层必须自己做，否则坐标换算会变成 `'500' * 1512 / 1512` 那种
TypeError）；`sequence` 的**内层** args 走同一个转换（SDK 的校验本来就只到最外层）—— 因此内层实现的形参
名**必须**覆盖该工具对外声明的每个键：转换对未知键是**静默丢弃**，一旦漂移就是「参数写了但不生效」。
真事故：内层 `press_key` 曾用 `hold=` 而工具声明的是 `hold_seconds=`，于是 sequence 里的长按静默
退化成单击（无报错、无日志）。现在 wire check 拿真实 `tools/list` 的 schema 与内层签名逐个对拍。带 `.code`
的类型化异常原样透出，不包成 `ERROR[INTERNAL]`，调用方才能按码分支。

每个动作返回 `effect: changed/unchanged` 证据 —— 它真的去遍历界面树（操作前后各取一次
`role|title|value|description|位置|尺寸` 指纹，菜单栏只走到 3 层以省 100ms），不是摆设；
指纹只在 `dispose` 之前取子节点，否则遍历会静默退化成「只哈希根节点」，永远报 unchanged。
错误为类型化码（PERMISSION_DENIED / STALE_STATE / ELEMENT_UNAVAILABLE / NOT_SETTABLE…）。

**坐标路径（兜底）** —— 截图：
`screenshot`（`display=` 多显示器、`window_id=` 按窗口截取；Retina 自动换算）+
`click` / `paste`（`format=text/md/html`，html 富文本）/ `type_text` / `press_key`
（支持长按）/ `scroll` / `drag`。适用于无 AX 的场景。

`click` / `drag` 带**落点保护**：已绑定目标应用时，先用 CGWindowList 查这一点上真正排在最
前面的是谁的窗口 —— 不是绑定应用的窗口就返回 `ERROR[NOT_FRONTMOST]` 并且**不注入任何点击**
（实测「照点再附一句警告」拦不住事故：坐标是按目标窗口截图换算的，一旦被别的窗盖住就点到
无关窗口上了）。要点那个窗口时显式带 `force=true`。点确实落在绑定应用自己的窗口上时，先激活
应用、必要时 `AXRaise` 那一扇窗（非 key window 的第一次点击只被系统用来激活窗口，模型会误以为
没点动而重复点 —— 抬窗之后一次点击就生效，实测 `AXMain` 当场变 True）。

判据丢了就**拒绝**，不猜：窗口列表读不到（窗口服务/录屏权限异常）时返回 `ERROR[TARGET_UNKNOWN]`
并拒绝执行 —— 曾经这里是 `except → None`，「查不到」与「这一点上没有窗口」混成同一个值，等于
窗口服务一抖保护就静默取消（而这一点可能正落在消息区、列表项或删除按钮上）。`force=true` 仍是
逃生口；真的没有窗口（点在桌面空白处）照旧放行。

**聚焦管理**：`focus_app` / `focus_window`（NSRunningApplication 激活 + AXRaise，
比裸 AXRaise 可靠）。键盘类动作（`type_text` / `press_key` / `paste`）会**自动把
绑定的目标应用带到前台**再输入。坐标点击（`click` / `drag`）只在「点的位置确实压在
绑定应用自己的窗口上」（查 CGWindowList 最前面那层，绕开会铺满整屏的桌面窗口）时才替它
激活 —— 否则 macOS 的第一下只用来激活窗口，Agent 会误以为没生效而重复点击；点在别的窗口
上时绝不抢前台，只在结果里附警告。同名多进程时 `get_app` 报 AMBIGUOUS_APP 会标出哪个是前台。

另有：`list_windows`（全局窗口清单含 window_id）、`launch_app`（启动并验证进程存活）、
`run_shell`（60 秒强杀 + 防呆提示）、`screen_info`（显示器 + 权限 + 急停 + 租约状态自查）、
`open_permission_settings`、`preview`（预览浮窗开关；显示时会把浮窗、× 与 ⏹ 的坐标一起报出来）、
`stop` / `resume` / `release_lease`（控制面）。共 **28 个工具**；`get_ax_state(screenshot=true)`
一次拿「元素树 + 一张图」，`get_app(focus=true)` 绑定并带到前台。

## 一轮多步 · 防重放 · 急停 · 租约（v2）

改造的动机、取舍与验收标准写在 `computer-use-v2-设计任务书.md`；这里只记结论与实现。

**动作层 / 渲染层 / 注册表三层分离**（`server/action_layer.py` 是契约）。每个动作是 `_do_*`：
执行 + 采证，只返回 `ActionResult{ok, effect, evidence, settle_ms, possibly_sent, error, detail}`，
**一句自然语言都不许有**；`_render_*` 把它变成给模型看的话；`_register()` 把两者绑成 MCP 工具
并登记进 `_ACTIONS`。工具的签名与注解直接借动作层函数的（`__signature__` / `__annotations__`），
于是「工具对外声明的参数」与「sequence 内层能接的参数」永远是同一份 —— v1 那种「声明了
`hold_seconds`、内层叫 `hold`、参数被静默丢弃」的漂移在这一层根本表达不出来（wire check 仍逐工具对拍）。

**`sequence` 不再有白名单。** v1 只能跑 click / type_text / press_key / scroll / launch_app / wait，
而 `mcp.instructions` 自己写着「优先元素路径」—— 最该批处理的那条路反而一步一回合。现在 `_ACTIONS`
里的每个动作都能进批处理（元素路径 + 坐标路径），并且：

- **`{"observe": {...}}` 伪步**：批处理中途插一次观察（等价 `get_ax_state`，`diff` 默认 true）。
  元素索引自**最近一次观察**：不插 observe 时索引保持有效（与 diff 模式同一套语义），
  插了之后必须用新索引 —— 结果里会明说这一点（索引不变量）。
- **失败默认停止**：`ELEMENT_UNAVAILABLE` / `STALE_STATE` 提示「重新观察」，拿旧索引硬点是更坏的选择。
- **`on_unchanged: "abort"`**：某步 `effect == unchanged` 就停下来。
- **`possibly_sent == true` 立即停止**，并把「先 observe 确认、不要重复同一动作」写进结果。
- 每步回**真实 `settle_ms`**；`wait` 步改成「界面稳定即返回」，固定秒数只是上限。
- **返回已执行步骤的完整清单**：中途出错时，这个列表就是已经发生的副作用，不许缩成一行错误
  （设计任务书 §7 风险 2：模型看不到已经发生的副作用，会当成「什么都没做」重来一遍）。
- 一次结果**最多一张栅格图**（§7 风险 5）：只有最后一个 observe / screenshot 步会附图。
- `step_timeout_ms` 是**每步的等待预算**（`_settle` 不会超过它），`sequence_timeout_ms` 是总预算、
  在步与步之间检查 —— 单个已下发的 CGEvent/AX 调用不可抢占，所以这是预算而不是强杀线；
  真要立刻停就用急停。

**`possibly_sent`（防重放）** 的判定规则写死在 `action_layer.py`：

| 情况 | possibly_sent |
|---|---|
| 参数校验失败 / 元素查不到 / 租约被占 / 急停 | `false`（肯定没下发，改参数重试是安全的） |
| 动作**已发出**后超时、连接中断、被急停打断 | `true`（**可能已经点下去了**） |
| 动作正常返回（无论 effect） | `false` |

模块级 `mark_sent()` 在每个真正下发的地方（CGEvent 投递 / osascript / 子进程启动）**之前**打标记；
能明确判定「没送出去」的分支（如 AXPress 返回错误码）显式写回 `false`。为 `true` 时结果尾部附
§3.4 那段指引（不要重复同一动作，先 `get_ax_state` 观察再决定）。

**`strategy` 三态**（click / click_element / drag / press_key / scroll / set_value / type_text / paste）：

- `auto`（默认）：能走语义就走语义 —— `click_element` 优先 AXPress、没有才坐标兜底；
  `click` 保持坐标工具的本义（真实指针点击，不偷偷改语义）；键盘优先 System Events、失败降级 CGEvent。
- `a11y`：**强制无障碍语义路径，失败即报错，不静默降级**（`ACTION_UNAVAILABLE`）。
  `click` 在这一档用 `AXUIElementCopyElementAtPosition` 把「一个点」变成「一个元素」，再往上找
  4 层祖先里的 AXPress；没有就不点 —— 不会出现「说好了不碰用户鼠标，还是碰了」。
- `event`：强制合成 CGEvent。拖拽只有这一条路（AX 没有 drag），`set_value` 只有无障碍一条路
  （`event` 会明确报错，而不是假装成功）。

**急停三档**（一份状态，`server/killswitch.py`）：

1. `DSH_CUA_DISABLE=1` —— 服务器启动即只读，本进程内解除不了；
2. MCP 工具 `stop(reason)` / `resume()`；
3. 用户侧：预览浮窗左上角的 **⏹ / ▶** 键，或 host 半区的 `/_computer-use/stop`（`?on=0` / `/resume` 解除）。

三档写的是同一个文件 `$DSH_CUA_DIR/kill`（`on:<毫秒>:<原因>`），因为「立刻中止正在跑的动作」只能靠文件：
那一刻 MCP 服务器正卡在动作里，工具调用那条路走不通。触发后**动作类**工具一律回 `ERROR[KILLED]`
（`run_shell` / `launch_app` / `sequence` 也在内），**观察类**工具（`get_ax_state` / `screenshot` /
`list_apps` / `screen_info` …）照常可用；正在跑的等待与批处理在下一个可中断点（≤0.1 秒）退出；
**已经下发的动作不会被撤回**。急停期间浮窗一直显示（否则用户找不到恢复入口）。状态刻意**跨会话保留**
——用户按了停止、Agent 换个会话又自己动起来是最糟的结果，所以 `screen_info` / 浮窗 / `resume()` 的文案里
都写了怎么恢复。

**控制器租约（controller lease）取代「禁止子代理用电脑」的一刀切。** 互斥锁只管「一个动作占用物理输入」，
管不了两个会话**轮流**操作同一台电脑（A 在填表单、B 把前台抢走点了别处，两边都以为自己在操作那个应用）。
现在 `get_app` / `focus_app` 会申请租约（一个持有者一个文件 `$DSH_CUA_DIR/leases/<pid>.json`，默认 TTL
60 秒、每次动作续期、进程退出 atexit 自动释放）：另一个会话申请同一目标时拿到
`ERROR[CONTROLLER_BUSY]` + 持有者与剩余时间（仍可观察、只是动作被拦），`get_app(force=true)` 可抢占
（写 `leases.log`），被抢的一方下一次动作会被拦住。持有者崩了（强杀 / OOM）留下的租约在下一次扫描时
回收 —— `kill(pid,0)` 的 EPERM 必须读成「活着」，把它读成「已退出」就会去回收一份还活着的租约，那是这个
模块最不能出的错。目标不同的会话仍然放行，只在结果里带一句「另一个会话正在操作 X，会互相抢前台」。
**用户接管检测**是启发式的：记住我们上一次动作落下的光标位置，下一次动作前发现它自己动过
（>12pt 且间隔 >0.7 秒）就在结果里提示「可能用户正在接管，先停下来问他」——只提示、不阻断。

## 多会话：一个 DSH 实例里的多个会话

**根因**：这个 MCP 服务器是**宿主级单例** —— 一条 `insert` 让整个 DSH 实例只起一个 `server.py`，
而它同时服务七八个会话。绑定（`_bound_app`）、租约（`lease._mine`）、观察器都是**进程内全局状态**，
于是会话 A 的动作可能落到会话 B 绑定的应用上（实测事故：绑定从「计算器」变成「DeepSeek Harness」，
`release_lease` 释放掉的是别人的租约）。租约也挡不住：租约文件是 `leases/<持有进程 pid>.json`，
而所有会话的持有者都是同一个 pid，`guard()` 必然跳过它。

**修法：host 半区交出调用者身份，服务器按会话分份（B 方案，已落地）。**

stdio MCP 协议**不带任何身份字段**（`dsh-mcp-client` 只发 `{name, arguments}`，参数还是 `deepFreeze`
的，钩子改不了 —— 见 `dsh-tools/README.md:230`）。唯一知道「这次调用属于哪个会话」的地方是 host 半区：

```
tools/pre-execute (exec.agent.id = session-78c637…)
        │  ① 闸门：computer-use 调用一次只放一个过去（否则 caller.json 归谁后写的就归谁）
        │  ② 原子写 $DSH_CUA_DIR/caller.json = {agent, tool, seq, at}
        ▼
server.py 每次 tools/call 开头读它 → 绑定/令牌/租约按会话分份 → 用完存回
```

实测（隔离 headless 里挂探针）：`pre-execute` 拿得到 `exec.agent.id`，而且钩子是**真的闸门**
—— 在 pre 里 sleep 2 秒，工具就真的等满 2 秒才跑（内置 `read` 只花 0ms）；错误路径也有
`post-execute`。`post-execute` / `result` / 150 秒保险丝三条路都能放行，重复或迟到的 post 按序号
幂等忽略（不会把闸门错误地交给别人）。

真实回归（一个 DSH 实例 + 两个并发 subagent + 主会话，三者共用一个 `server.py`）：三方各自拿到
**自己的** `会话身份`、**自己的** `已绑定应用`（计算器 / 访达 / 未绑定）与**自己的** `binding_id`，
互不顶替、零 `CONTROLLER_BUSY`（目标不同）；其中一方还能看到「另一个会话 X 正在操作访达（剩余 55 秒）」
的提示 —— 状态可见、动作受限。

**降级路径**：读不到 `caller.json`（老版本 host 半区、别的 DSH 版本、文件过期）时，服务器把所有调用
放进一个 `shared` 桶 —— 也就是**旧行为**，并在 `screen_info` 里如实写「会话身份: 未启用」。降级而不是坏掉。

**另外两道纵深防线**（不依赖身份，仍然保留）：

1. **绑定令牌 `binding_id`**：`get_app` / `get_ax_state` 回显，动作类工具（含 `sequence`、
   `release_lease`）带错即 `ERROR[BINDING_CHANGED]`、一次都不下发；带旧令牌**观察**会明确警告
   「这棵树不是你要观察的应用」；没带令牌 = 放行但附一句自我纠正提醒。
2. **索引纪元**（`ax_bridge.bump_index_epoch`）：每次换绑把索引分配起点抬高十万位，两个绑定期发出的
   索引**数值上永不重叠** —— 即使身份管线整个失效（老 host 半区），旧索引也只会 `ELEMENT_UNAVAILABLE`，
   不可能命中新应用树里的同号元素。

**不做 per-agent 子进程（A 方案）**：DSH 的官方形态是有的，第一方先例就是 `dsh-acp` 的
`agentCtx.plugin(McpClient, config)`（`dsh-acp/lib/index.js:216-219`），生命周期挂在 agent scope fiber 上，
agent 作用域还允许复用同名 `serverName`（`dsh-mcp-client/lib/index.js:768-772` + README:128）。
但**第三方插件拿不到那个模块**：本插件在隔离 headless 里实测 `import('@deepseek-ai/dsh-mcp-client')`
→ `ERR_MODULE_NOT_FOUND`（app 的副本在 `app.asar/dsh/node_modules`，不在插件的解析链上；profile 的
`node_modules/@deepseek-ai/` 只有 `cosmokit`/`schemastery`）。要走 A 就得在 host 半区自建 MCP 客户端
（JSON-RPC + `ctx.tools.register` 的工具/结果映射 + 接 `attachments`/`llm.resolveModelInfo` 的图片投影），
那是一次压在「工具能不能注册出来」生死线上的架构级改动 —— **明确不做**。

**全局 vs 隔离的边界**（刻意如此，改任何一条都要先想清楚）：

| 状态 | 现状 | 为什么 |
|---|---|---|
| 绑定 / 令牌 | **按会话**（有身份时）；无身份退回一个共享桶 | 有身份就严格隔离，没身份就降级 —— 不留半吊子 |
| 租约 | **按会话**（`leases/<会话>.json`；无身份时 `<pid>.json`，与旧版同名） | 同一实例内也能 `CONTROLLER_BUSY`；跨进程/跨实例本来就能仲裁 |
| AX 观察器 / 索引 | **按 pid 共享**（索引是「哪个应用的第几号元素」，跨会话看同一个应用本就该共享） | 这是资源缓存而不是会话状态；换绑时的撞号风险由索引纪元兜住 |
| 急停 `kill` 文件 | **刻意全局** | 用户按一次 ⏹ 就要停掉这台电脑上的一切电脑操作，而不是某一个会话 |
| 预览浮窗 / `/_computer-use/*` | **刻意全局**（host 半区只有一份） | 唯一的观察面与恢复入口，多会话共用一份画面才是对的 |
| 物理输入互斥锁 | 单进程内（`threading.RLock`） | 只有一个服务器进程，所以它天然跨会话生效；调用者闸门再加一层「一次一个调用」 |
| 请求串行 | host 半区闸门（同一次一个 computer-use 调用；150 秒保险丝） | 身份文件必须与调用一一对应；顺带保住「同一块鼠标键盘」这条物理约束 |

**没有合并的旧名**（§3.7 本身标着「可选，需评估兼容性」）：`click` 与 `click_element` 保持两个工具 ——
合成一个「index 或 x/y 二选一」的 `click` 会让 `x`/`y` 从 required 变成可选，把「忘了传坐标」这类错误
从参数校验阶段推到运行期，收益只是数字好看。`elements` / `focus_app` 保留为别名，新增能力一律走加法
（`get_ax_state(screenshot=)` / `get_app(focus=)` / `get_app(force=)`）。

**§3.8 双语工具展示名：调研结论 = 插件层做不到，记为已知限制。** 证据（对 `app.asar` 里的
`dsh/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js` 与 `dsh-client-ui-tool/lib/client.js` 逐行核对）：

- mcp-client 的 `syncTools()` 只搬 6 个字段（name / rawName / description / inputSchema / outputSchema /
  taskRequired），全文 `title` / `annotations` / `_meta` / `displayName` 命中数为 0 —— 在 MCP 的
  `tools/list` 里声明 `title` 或 `annotations.title` 不会有任何效果；工具名还要过 `[^A-Za-z0-9_-]`
  过滤（中文当 wire 名不可能）。
- 客户端工具卡的标题表是**闭集**（`TOOL_TITLE_KEYS` / `TOOL_VARIANTS`），`mcp__…` 一律落到
  `tool.title.generic`，摘要里原样拼 wire 名；全库搜 `mcp__` 只有 mcp-client 自己命中，没有任何
  client-ui 包认识它。
- 唯一能产出「工具名 → 展示名」的机制是 client 侧的 keyed slot `tool.call.toolview`
  （精确字符串匹配、无通配、同名要换 priority）。本插件的 client 半区**已退休**（`dsh.client` 撤掉、
  实现搬到 `retired/`），要用就得把它加回来并按 tools/list 逐工具注册 zh/en 双标题；审批横幅走的仍是
  raw 名，host 半区没有任何改名/改展示名的事件可用（`tools/pre-execute` 等全在执行层）。

结论：**记为已知限制，不做**。真要做，最小可行动作 = 复活 client 半区 + 28 条 keyed slot 注册；
更彻底的做法要改 DSH 本体（让 mcp-client 透传 MCP `title`，client 通用卡优先使用它）。

**预览面只有一个：OS 级浮窗**（`server/preview_window.py`，原生 NSPanel，对齐 ZCode / Qoder
的 computer-use 画中画）。它是独立窗口而不是宿主页面里的一块 DOM —— 所以**能拖到桌面任何
位置，包括 dsh 窗口之外、浮在别的应用之上**；宽度 320 pt，等比不裁切；拖完的落点记在
`panel.pos` 里，下次拉起还在原地（默认右下角）。

- **随用随现**：第一次真的操作电脑时自动拉起（`server.py` 的 `_note()` 里 `_ensure_panel()`），
  这一轮结束、安静 `DSH_CUA_HIDE_S`（默认 12 秒）自己收起来，下一次动作自己回来，会话结束（MCP 服务器退出）随之退出，不留孤儿窗。
  判据是 computer-use 自己的动作时间戳（status.txt / heartbeat），**不是「dsh 里有没有回合在跑」**
  ——否则你做写代码、聊天这类普通任务时窗也会被叫出来（`latest.jpg` 是跨会话留着的旧截图），
  实测踩过。`DSH_CUA_NO_PANEL=1` 可整条关掉；`preview(enable=True/False)` 手动强制显示/收起，
  显示时会把浮窗坐标报给 Agent（同一套截图坐标系，便于拖动或点它的 ×）。
- **完成勾与收起时机只看 agent，不看画面**（用户实测报过两个症状：绿勾被鼠标一动就抹掉、任务
  结束后窗还杵着两分钟）。判据拆成两侧：
  - host 半区写 `turn.json` = `{agent, running, at}`（`agent` = 最近一次 computer-use 调用所属会话，
    `running` 来自 `agents` 服务）。于是浮窗知道「**这一轮还在不在跑**」，而不是只知道「最后一次
    动作多久以前」；
  - 浮窗侧是一个**纯函数** `_panel_state(now, agent_ts, running)`：`running` 为真 → 一直上屏；
    这一轮刚结束 → **立刻**打绿 ✓（1.5 秒地板防抖），再安静 `DSH_CUA_HIDE_S`（默认 12 秒，原来
    写死 120 秒）收起；拿不到 `turn.json`（老 host 半区）→ 退回纯时间判定（安静 `DSH_CUA_DONE_S`
    默认 8 秒打勾、`DSH_CUA_DORMANT_S` 默认 60 秒收起）。**输入里没有帧 mtime** —— SCK 推流只要
    画面有变化（用户动一下鼠标、屏幕上任何动画）就重写 `latest.jpg`，把判据挂在那上面就是那个 bug。
    这一段的矩阵由 `scripts/panel-check.py` 打（含「鼠标移动不改变结论」那条）；
  - 同一个 `running` 还管第二件事：用户点过的 × 什么时候解除。那是另一个纯函数
    `_hide_expired(hide_at, agent_ts, now, running, prev_running)`，判据同样只看轮次
    （**下一轮开始**才解除），绝不看「安静了多久」—— 见下面 × 那条。
  - 浮窗收起时写 `panel.state=hidden`，host 半区据此**停掉抓屏**（原来只要进程还在就一直抓，
    收起后还能白抓两分钟 —— 抓屏进程活着 = 系统录屏灯亮着）。
- **左上角 ⏹ / ▶ 是急停开关**（§3.5 第三档）：按下写 `$DSH_CUA_DIR/kill`，MCP 服务器在下一个
  可中断点（≤0.1 秒）进入只读，动作类工具一律回 `ERROR[KILLED]`；再按一下恢复。急停期间浮窗
  **一直显示**（这是用户侧唯一的恢复入口，藏起来等于让用户没法恢复），底色转红、按钮变 ▶。
  为什么走文件而不是 HTTP 或工具调用：那一刻 MCP 服务器正卡在某个动作里，工具调用那条路走不通。
  写的是临时文件再 `os.replace` —— 服务器每 120ms 就可能读一次，半个文件会被读成「没停」。
- **× 是「这一轮先别给我看」，不是「以后别再出现」**（这条是真事故）：以前 × 和  `preview(enable=False)` 写的是同一个 `panel.hidden=off`，于是用户随手点一下 × 就等于打开了一个
  粘性 opt-out —— 而用户侧没有任何恢复入口（没有菜单栏图标、没有 Dock、GUI 里也没有开关，内嵌
  卡片已退休），只能求 Agent 调 `preview(True)`，或者知道 `/tmp/dsh-computer-use/panel.hidden`
  这个文件名。现在拆成两件事：× 只 orderOut（写 `panel.hidden=hide:<ts>`，进程留着），**这一轮里
  一直算数**，等**下一轮开始**（`turn.json` 里 running 由否转是）自己回来 —— 用户不需要找任何
  「重新打开」的入口。解除的判据是**轮次**，不是「安静了多久」：2026-10-11 用户报「观察窗又无法
  手动关闭了」，根因就是 `_hide_expired` 还在用 `turn.json` 出现之前的假设（「安静 2 分钟 =
  上一轮活儿干完了」）—— 而 `status.txt` / `heartbeat` 只有 computer-use 调用会碰，Agent 在思考、
  读文件、写代码时同样「安静」，这一轮远没结束。实测：安静已 195 秒时点 ×，0.30 秒后窗回到屏上；
  安静不足 120 秒时，也总在安静到 120 秒那一刻自己弹回来。现在解除只看「下一轮开始了没有」，
  拿不到 `turn.json`（老 host 半区，或 host 拿不到 `agents` 服务、写的是 `"running": null`）时
  才退回时间判定 `HIDE_QUIET_S`（默认 2 分钟）。「以后别再自动出现」归 `preview(enable=False)` 与
  **设置页那个总开关**（见下面「设置页」一节；用户侧终于有了入口），
  它仍是粘性的，但结果文案明确写出恢复办法，且 off 期间每个动作的结果尾部都会带上那条提示
  （v2 里它是结构化 hint 码：动作层只塞 `detail["hints"]`，文案在 `action_layer.HINTS`，
  v1 那句 `_panel_hint()` 已随分层一起去掉），Agent 不会在用户已经失去监督面的情况下继续闷头操作。
- **Host 侧的推流闸门跟着浮窗走**：浮窗不像卡片那样轮询 HTTP，它只读 `latest.jpg` + 把 pid 写进
  `preview.lock`，所以 Host 半区认这个 pid 决定要不要喂帧（`npm run check` 里四条断言分别钉住
  「普通任务不该开录屏」「在用电脑时要补帧」「浮窗在跑时要补帧」「**不靠任何 HTTP 请求也要起流**」）。
  没人在看就 5 秒内收掉抓屏进程 —— 它活着意味着系统录屏灯常亮。
- **起流的触发点是 2 秒定时器，不是路由**（这条是真事故）：起流原本只写在 `status.json` /
  `/stream` 两个 handler 里，而唯一会请求它们的客户端是已退休的卡片 —— 浮窗一句 HTTP 都不发
  （只读文件 + AppKit）。卡片一退休，「浮窗开着就一直有画面」就再没人触发过：Agent 一停手画面
  就冻住。现在 `housekeeping()` 每 2 秒自己按「浮窗活着 **且** 最近有动作」起流，handler 只是
  复用同一个 `pumpFrames()`。顺带修掉「浮窗隐藏了还在抓屏」：活动窗口之外一律停（浮窗自己也是
  按同一条判据隐藏的），不再靠 `preview.lock` 里那个能挂到 900 秒的 pid。
- **内嵌卡片半区已退休**：它是 DOM，被 dsh 窗口裁剪、拖不出去，满足不了「一直都是浮窗」。
  `package.json` 里的 `dsh.client` 与 `exports["./client"]` 已撤掉（dsh 不再注入），实现搬到
  `retired/preview-card.js`（文件头写了复活办法，冒烟仍指着它跑）。它**不在 `files` 白名单里**——
  发布包里只留真正会被加载的东西。Host 的路由与 `view=1` 需求信号都还在。

**实时画面（两条通道，自动降级）**：预览要"活着"，光靠 Agent 截屏不够（Agent 不动就没帧）。

- **SCK 流（首选，实测 29.2fps）**：`capture/cua_capture.swift` 用 ScreenCaptureKit 推帧
  → stdout 长度前缀 JPEG 帧流 → Host 半区解析。浮窗与回落路径都按 `latest.jpg` 出帧
  （每帧原子替换）；`GET /_computer-use/stream`（MJPEG `multipart/x-mixed-replace`）仍在，
  是给（退休的）卡片实现和调试用的。
  构建：`sh scripts/build_capture.sh`（需要 Xcode Command Line Tools；产物
  `bin/cua_capture`，已 ad-hoc 签名）。脚本**显式钉死 `-target arm64-apple-macos13` 与
  `x86_64-apple-macos13` 再 `lipo` 成通用二进制** —— swiftc 默认按当前这台机器的系统和架构编，
  实测那样出来的产物是 `minos 27.2 + 单 arm64`，换台 Mac（Intel 或任何旧一点的系统）会直接
  dyld 报错起不来。工具链缺失或某个架构编不出来都不是死路：Host 半区找不到 `bin/cua_capture`
  就自动退回 `screencapture` 那条 ~2.5fps 的路。
- **`screencapture` 回落（~2.5fps）**：没有 helper 二进制（未装工具链）时自动走这条，
  单帧 154ms 抓屏 + 136ms 缩放 ≈ 290ms，是这条通道的物理上限。
- **按需启停**：只有浮窗在跑（或卡片实现被复活后轮询带 `?view=1`）时才开流，停 5 秒无需求
  就杀抓屏进程——抓屏进程活着意味着系统录屏灯常亮并持续吃 CPU，绝不能留孤儿。
  实测：看的人在场 → helper 起、端到端 ~29fps；没人看 → 6 秒内 helper 退出。
- 状态目录默认 `/tmp/dsh-computer-use`，可用 `DSH_CUA_DIR` 整体改（三个半区都认，冒烟测试
  就跑在隔离目录里，不会往真实现场写帧）
- 调参：`DSH_CUA_STREAM_WIDTH`（默认 512，浮窗 320 pt @dpr2 够用）、
  `DSH_CUA_STREAM_FPS`（默认 30）、`DSH_CUA_STREAM=0` 强制走回落、
  `DSH_CUA_LIVE=0` 完全关掉自补帧。实测 512@30fps：31.6KB/帧、~920KB/s、helper 8.7% CPU（15fps 档是 33.7KB/帧、4.0% CPU，帧间变化小所以每帧体积几乎不变）。

## 设置页（完整）

用户侧终于有一个**找得到的开关**。观察窗自己那两个键（× = 这一轮先别给我看、⏹ = 急停）都只解决
「现在」，而「以后别再自动出现」以前只有 Agent 的 `preview(enable=False)` 能写 —— 用户想恢复也没有
入口（上面那条真事故）。现在这一栏承担：

| 控件 | 作用 | 落点 |
|---|---|---|
| 自动显示观察窗 | 关掉后不再自动弹出，并结束当前浮窗进程 | `~/.dsh/computer-use/settings.json` + `<BASE>/panel.hidden=off` |
| 回合结束后自动收起 | 5 / 12 / 30 秒 / 1 / 2 分钟（默认 12 秒，一排 Pill） | `<BASE>/panel.conf` |
| 急停 / 恢复 | 与 ⏹、MCP 的 `stop` 工具写同一个文件 | `<BASE>/kill` |
| 状态行 | 观察窗在不在屏上 + 最近一次电脑操作 | — |
| 权限 | 两项系统权限怎么给（含「打开辅助功能设置」按钮） | — |

分工与「谁说了算」：

- **`panel.hidden` 是运行时真相**：Agent 的 `preview(False)` 也写它，所以读设置时把它折叠回落盘的
  偏好 —— 否则 Agent 关掉之后设置页还显示「开着」，重启还会把它复活。
- **`panel.conf` 是浮窗每拍重读的偏好镜像**（1 秒 TTL）：收起延时、以及「关掉总开关时让已经开着的那块窗
  当场消失」这条请求都从这里走，
  所以改设置**不需要重启任何进程**。里面带毫秒 nonce（严格递增），值变了才算一条新指令。
- **`/tmp` 会被系统清掉**，所以「不再自动出现」另有一份落盘副本；宿主启动时只强制 off 这一个方向，
  反向（重新打开）必须由用户或 Agent 明确表达。
- **粘性 off 不许被 × 覆盖**：`_save_hidden` 拒绝用 `hide`/`auto` 覆盖 `off`（`on` 可以）—— 否则
  用户刚在设置页关掉观察窗，随手点一下 ×（或下一轮开始那次自动解除）就把它偷偷打开了。

HTTP 契约（同源，走 `connection.admit`，与 `/stream`、`/status.json` 同一道门）：

```
GET  /_computer-use/settings.json   → { settings, runtime, choices, paths }
POST /_computer-use/settings.json   → 部分字段即可；坏值保留原值、坏 JSON 400
POST /_computer-use/permissions     → 打开系统设置的辅助功能面板（DSH_CUA_NO_OPEN=1 时不弹窗）
POST /_computer-use/nav-icon        → 导航图标补丁的自检上报（写 <BASE>/nav-icon.log，超 8KB 留尾部）
```

客户端半区是**手写**的 bundle（`client/settings.js`，没有构建步骤）：一页而已，用不上打包器；它只
require 平台 seed 词（`react` + `@deepseek-ai/dsh-client-ui-primitives`），任何一个缺失都安静地不出现，
绝不把宿主页面带崩。版式是三张卡片（观察窗 / 控制 / 权限），控件全部来自官方 primitives
（`Switch` / `Pill` / `Button` / `StateDot`），卡片用主题 token 自己写（背景层 + 1px 描边 +
12 圆角，与「通用」设置页、dsh-pocket 的「手机访问」同一套观感），字号比官方设置行大一档
（行标题 15 / 说明 13）—— 用户 2026-10-12 的要求。同一轮还按用户要求做了三次减法，都在
`scripts/settings-smoke.mjs` 里钉成「不该再出现」的断言：

  * **删掉三段说明**：三个「关掉」的区别、多会话共用键鼠、排查路径；
  * **删掉两个开关/入口**：「完成后打勾」（打勾本来就是默认行为）与「这一轮先收起 / 恢复显示」
    （浮窗上的 × 已经管这件事，设置页不再放镜像入口；`POST /_computer-use/panel` 随之删除，
    `panel.conf` 的 `hideRequest` 现在只由「关掉总开关」产生）；
  * **用词规范化**：页面上不再出现口语的「这一轮」，统一成产品自己的说法「回合」；
    状态文案改成「运行中（显示）/（已收起）/ 未运行（下次操作时自动启动）/ 已关闭（不再自动显示）」。`scripts/settings-smoke.mjs` 钉住四件事：注册进 `settings.section`
（id/order/label）、开关与状态反映的是哪份状态、点一下发出的请求长什么样、拿不到宿主时不崩。

**新增或改动客户端半区后必须重启一次 DeepSeek Harness**：客户端清单在宿主启动时扫描并缓存
（`dsh-client-modules` 的 `pkgMeta` 按 `baseUrl\0loaderName` 记住结论），改 `package.json` 里的
`dsh.client` 不会热生效。

### 导航图标：为什么设置页要自己动手补一个

设置导航那一行的图标是壳里写死的一张 `id → 图标` 表（`navIcon(id)`，未命中就兜底成齿轮），而设置页
注册项只有 `id` / `order` / `label` —— **平台没有给第三方设置页留图标位**。那张表在**应用自带的
asar 里**：桌面版把 `/assets` 与所有 `@deepseek-ai/*` 客户端包都从 `app.asar/dsh` 发（profile 里那份
同名包**根本不会被读** —— 实测它的 atime 停在安装那天），asar 又是应用签名的一部分，改不得。所以只剩
一条路：**在页面里给那一行补图标**（`client/settings.js` 的 `installNavIcon`）。

它的几条克制原则：

* **只认结构，不认壳的哈希类名** —— 优先认「设置对话框（`[role="dialog"]`）里、文本完全等于本页
  label、行首就是图标 svg、行高 40px（壳的导航行规格）」这四条；四条全中才动手。壳哪天改了行高或
  图标不再是第一个子元素，再退一步按「文本 + 行里有图标」认（`scripts/settings-smoke.mjs` 里对两遍
  匹配都有用例，还有「文本一样但没有图标」「文本一样但行高不对」两个诱饵，断言严格那一遍一个都不碰）；
* **壳重渲染会换掉那一行**（切语言、关掉再打开设置面板），所以挂一个 body 级 MutationObserver 防抖
  补一次，自己插入的节点带 `data-cu-nav-icon` 标记，绝不重复插；
* **卸载时全部还原**（图标卸载、齿轮复原、观察器断开），冒烟用例逐条钉住。

补丁**补不上时会留下现场**：`/_computer-use/nav-icon` 把结论追加到 `<BASE>/nav-icon.log`
（补上了记一行 `patched`；设置对话框开着却认不出那一行，记一行 `probe` 并带上本页 label、对话框数、
按钮文本抽样）—— 失败时页面上看不出任何区别，全靠这一行才知道为什么。日志超过 8KB 只留尾部。

图标是**自己画的**（`client/settings.js` 的 `ComputerIcon`）：运行时那 188 个图标里没有
电脑 / 显示器 / 笔记本（只有仪表、滑块、面板、全屏、检查这一类），而这一栏就是「电脑操控」，
所以照壳自己的画法画了一台显示器 —— `viewBox 0 0 16 16`、`fill: none`、每条 path
`stroke: currentColor`、线宽取 primitives 的 `ICON_MEDIUM_STROKE`（1.3，与相邻图标同粗细），
也不额外设 linecap（壳的图标都是默认方头）。形状：机身（圆角矩形）+ 支颈 + 底座，共三条描边，
冒烟里逐条钉住（viewBox / fill / stroke / aria-hidden / 描边条数）。

**为什么不自选现成图标**：运行时图标集是 188 个、`…OutlineMedium` / `…OutlineRegular` 两档，
里面**没有电脑 / 显示器 / 笔记本**。中间试过让设置页自己选（7 个候选 + 偏好项 + 实时重画），
用户 2026-10-12 明确不要这种，于是收掉，直接定成自绘的这一台。

**教训（两次白重启换来的）**：磁盘上有**两份** `@deepseek-ai/*` 客户端包 —— 页面真正加载的是
**asar 里那份**（profile 里同名包的命名与 tree-shaking 范围都不一样，实测 atime 停在安装那天）。
第一版照着 profile 那份挑了 `IconGaugeOutline16`，真机上根本没这个名字，图标怎么改都不生效。
所以 `scripts/settings-smoke.mjs` 里那张运行时导出表（282 个名字）是按 **asar 那份**生成的，
页面用到的每个 `prims.X` 都要在表里 —— 换 dsh 版本后重新生成，别凭 profile 或 README 猜。

## 轻量（实测数字）

| | 常驻 RSS | 启动 | 说明 |
|---|---|---|---|
| 本插件工具服务器 | 稳态常驻 **16MB**（启动峰值 23.8MB；纯 CPython 3.12 解释器地板就有 12.3MB）/ 跑满一轮 33MB 峰值 | 84ms（含换 UIElement bundle 的一次 execve；首轮自建 bundle 再 +48ms） | dsh 里子进程实测；`lsof` 确认空载时一个系统框架都没映射（CDLL 也延迟到第一次真用）；全程无 AppKit/Quartz/pyobjc/pydantic/httpx/Pillow/pyautogui |
| 抓屏 helper | **没人看 = 0 进程**；在看 30MB / 4.8% CPU | — | 没人看约 6 秒后自己退出，系统录屏灯跟着灭；缩放走 vImage 而不是 CoreImage，省下 11MB 与整条 Metal 管线 |
| 预览浮窗（独立进程，用到才起） | ~35MB | — | 与 MCP 服务器同生共死；这一轮结束 + 安静 `DSH_CUA_HIDE_S`（默认 12 秒）只 orderOut 不退出，所以下一次动作立刻回来；点 × 同样只 orderOut 但**这一轮一直算数**（下一轮开始才自己回来），不再制造「关了就打不开」的粘性状态 |
| Qoder computer-use | bridge 10MB 常驻 + runtime ~52MB 按需 | — | 他们的 runtime 会话结束就退，所以纯空载比我们要小；真在用电脑时是 62MB 级 |

ZCode 的 computer-use 进程本机当前没在跑，量不到，只作为形态参照。
一轮改造从 105MB / 506ms 降到稳态 16MB / 39ms；「Agent 真的在操作电脑」时 33MB（以前一调 AX/窗口就要把 pyobjc 整套拖进来，是 ~65MB），开着预览推流时合计 ~63MB —— 与 Qoder 在用电脑时的 ~62.8MB 同级，而他们的 bridge+runtime 是常态常驻。砍掉的东西（每条都先量再改）：

- **不用官方 MCP SDK**：`import mcp.server.fastmcp` 一个动作 +42MB / +290ms —— 那是整套 web 栈
  （anyio + httpx + httpx-sse + jsonschema + pydantic + pydantic-settings + uvicorn），而 stdio
  传输只需要「一行一条 JSON-RPC」。`server/mcp_stdio.py` 只用标准库实现
  `initialize / tools/list / tools/call / ping / notifications`：未知方法回 `-32601`，声明过的
  空能力（prompts / resources / logging）回空集合而不是报错，工具异常回 `isError` 而不是打断连接，
  图片工具回 `content[{type:image,data,mimeType}]`；一次调用可以回**多块** content（文本 + 最多一张图，
  给 `get_ax_state(screenshot=true)` 与 sequence 收尾的 observe 用）。报文形状是拿官方 SDK 的真机回包逐字段对拍出来的，
  `npm run wire`（`scripts/mcp_wire_check.py`）会把这套断言重跑一遍 —— 手写协议层最怕的就是
  「看着对、发出去不对」，所以这条进了 `npm run check`。
- **不用 pyautogui**：它 `import` 就把 Quartz + AppKit + Foundation + objc + PIL + tkinter 全拖进来
  （55MB → 101MB），而它做的事就是几个 `CGEventPost`。鼠标/键盘/滚轮/拖拽/长按/按键全走
  `server/cg_input.py` 的纯 ctypes CGEvent；顺带干掉它内置的 `PAUSE=0.25`，每个输入动作少等
  0.25 秒（实测 click 756ms、scroll 604ms、drag 947ms，含 600ms 界面稳定等待）。FAILSAFE 保留：
  光标在左上角直接抛 `FailSafe` 拒绝注入。
- **不用 Pillow**：截图缩放交给系统自带的 `sips`，尺寸从 `-g pixelWidth/pixelHeight` 读回。
- **热路径不碰 pyobjc**：前台判定用 `GetFrontProcess`/`GetProcessPID`；激活走 System Events
  （组合键本来就在用 osascript）；显示器几何用 `CGGetActiveDisplayList` + `CGDisplayBounds`
  （本来就是「左上角、点」，与 `CGEventPost` 同一套坐标）；窗口清单与点击命中判定用 ctypes 版
  `CGWindowListCopyWindowInfo`（`ax.window_list()`）；应用枚举用 `lsappinfo list`（半秒缓存），
  本地化名字照常是「访达 / 备忘录」，英文名照旧靠 bundle id 匹配。
- **helper 丢掉 CoreImage/AppKit**：缩放改 `vImageScale_ARGB8888` + `CGContext`（SCK 的 pixelFormat 已钉成 32BGRA，布局正好对上），实测 41MB / 7.2% → 30MB / 4.8%，帧率仍是 29.2fps、512x332 JPEG 逐帧解得开。
- **系统框架也延迟到第一次真用**：`cg_input.LazyLib` 让 CoreGraphics / ApplicationServices / CoreFoundation / libproc 都在首次属性访问时才 `dlopen`（空载 `lsof` 里 0 个系统框架）。顺带修了一个更值钱的漏洞：`ax_bridge.ax_trusted()` 原先**先试** `from Quartz import AXIsProcessTrusted`，而它被那条权限提示（v1 的 `_ax_hint()`，v2 的 `AX_PERMISSION` 码）挂在每个 click / type / press 的结果里 —— 也就是「Agent 一动就加载 pyobjc」，实测改成纯 ctypes 后同样的动作序列峰值从 ~65MB 降到 33MB。同一轮里还修了 AX 消息超时：老代码把 2 秒超时设在一个随即被丢弃的 systemwide 引用上，等于没设，现在设到 `for_pid` / `systemwide` 真正返回的元素上，卡死的 App 变成可读错误而不是整次工具调用挂住。
- 只剩两处还碰 pyobjc，且都是**用到才加载**：`paste(format=html)` 的 NSPasteboard 富文本，以及
  `paste(format=html)` 的富文本剪贴板，以及预览浮窗 `server/preview_window.py`（独立进程，
  只在自动拉起或 `preview(True)` 之后存在）。
- `server/requirements.txt` 因此只剩 `pyobjc-framework-Cocoa` 一项；`.venv` 里 mcp/pydantic/httpx/
  uvicorn/pyautogui/pillow 都不再是运行依赖（旧 venv 里还留着它们，重装才干净）。

### 每个动作的实测耗时（同一台机器、同一轮测试）

| 动作 | 最初 | 现在 |
|---|---|---|
| `click` | ~1006ms | **504ms** |
| `scroll` | ~854ms | **482ms** |
| `press_key` | ~1500ms | **634ms** |
| `click_element` | ~1100ms | **617ms** |
| `drag` | ~1300ms | **818ms** |
| `focus_app` | ~400ms | **181ms** |

两处来源：去掉 pyautogui 自带的 `PAUSE=0.25`；把「每个动作盲睡 600ms」换成
`_settle()` 自适应等稳定（先给 250ms 下限，之后每 120ms 采一次界面指纹，连续两次一致
就返回，最多仍不超过 600ms），以及 `_activate_pid` 用轮询前台 pid 代替固定睡 350ms。
读不到 AX 或没绑定应用时 `_settle()` 退回原来的固定停顿，所以只会更快、不会更糙。

## 开发与校验

```
src/index.ts        宿主半区的唯一真源（TypeScript）
lib/index.js        构建产物：`npm run build`（tsc）生成，profile 直接加载它
server/*.py         内置 MCP 服务器（Python，见下）
retired/            已退休的内嵌卡片实现：不进构建、不进发布包，只留档 + 冒烟
scripts/            install.sh / build_capture.sh（用户向）与 *-smoke、lint、wire（开发向）
```
- **源码是 `src/`，`lib/` 是产物**：改完跑 `npm run build`（`tsc -p tsconfig.build.json`，
  输出 `lib/index.js`）。路径与 `exports` 都没变，profile 的 `link:` 挂载不用动。
- **不 import 任何 `@deepseek-ai/*` 包**：宿主契约（`name`/`inject`/`apply`/`ctx.get`/`ctx.effect`）
  在 `src/types.d.ts` 里写成本地结构性类型。于是插件能脱离宿主独立类型检查，也不会因为本地
  `node_modules` 里多出一份 cordis 而在运行时踩到「原型实例不相等」。
- `npm run typecheck` 严格模式（`strict` + `noUncheckedIndexedAccess`）零错误；
  `npm run verify` = typecheck + build + check。
- **所有副作用都挂在 `ctx.effect` 上**：帧管线的定时器与抓屏子进程、预览路由，卸载（或热替换）
  时一起归还 —— 抓屏进程活下来不只是漏一个进程，它会让系统录屏灯一直亮着。host-smoke 里有一条
  断言直接盯着「已经打开的 MJPEG 连接有没有被收掉」。
- `npm run check` = `client-smoke`（退休卡片）、`host-smoke`、`lint_py.sh`、
  `mcp_wire_check.py`（对着真实 stdio 逐字段对拍协议面）。所有测试都跑在隔离目录
  （`DSH_CUA_DIR=/tmp/dsh-cua-smoke.<pid>`、wire check 用 `/tmp/dsh-cua-wire.<pid>` 并在结束时删掉）/
  独立子进程里，自带夹具，不读真实现场、不写 `~/.dsh`。
- **wire check 现在覆盖 v2 的每条新语义，且全部不碰真实输入**（挑的都是「本来就会报错」的分支）：
  sequence 的未知工具 / 空数组 / 非法 `on_unchanged` / 缺必填参数 / 失败即停 / observe 伪步 /
  `wait` 走稳定判定；`strategy` 三态与「a11y 不静默降级」；`possibly_sent` 的三条判定规则；
  急停三档（环境变量 / 急停文件 / `stop`+`resume`，其中文件档与 `resume` 各起一个独立进程验证跨进程一致）；
  租约仲裁（`CONTROLLER_BUSY` / `force` 抢占 / 死进程回收 / 被抢后拦动作 / 释放）；
  以及动作层契约（每个动作都是 `ActionResult`、签名覆盖工具声明、`on_unchanged` 真的会停、
  `step_timeout_ms` 真的压住 `_settle`）。wire check 共 131 条断言（`npm run verify` 合计 222 条），其中两组是**一个服务器进程 + 多个逻辑调用者/会话**的多会话拓扑（生产缺陷的忠实复现：从服务器这一侧看，宿主机就是一条 stdio 连接上交替来的两组调用）。
  浮窗上那个 ⏹ 的接线（`NSButton.target/action` → `onStop_` → 写/清标志）用一次性脚本验证：
  它要起 NSApplication，所以不进 `npm run check`（会弹窗），只在改动浮窗时手动跑一次。
- **发布包只装会被加载的东西**（`files` 白名单，`npm pack --dry-run` 可审）：`lib/index.js`
  与 `server/*.py`、抓屏 helper、两个用户向脚本、`cordis.patch.yml`、README/LICENSE —— 共 20 个
  文件。`src/`、`retired/`、`scripts/*-smoke.mjs`、`scripts/mcp_wire_check.py`、
  `scripts/lint_py.sh`、`runtime/`、`.venv/` 都不进包，所以 `npm run check` / `verify`
  要在源码树里跑。
- `mcp_wire_check.py` 末尾还有一段**进程内**回归闸门（拿真实 `tools/list` 的 schema 与 `sequence`
  内层签名逐个对拍，以及断言落点保护在窗口列表读不到时**拒绝**而不是放行）—— 这两类漂移都不会
  报错，只会静默失效，所以必须由断言盯着。计时一律用 `time.monotonic()`：墙钟在机器休眠时会跳，
  实测把 1.5 秒的等待量成过 899.91 秒，进而把「取消打断了动作」判成假失败。

### 隔离集成验证（DSH_HOME 不在 `~/.dsh` 里跑）

`npm run check` 验的是协议面与各半区的形状，验不到「真的被 dsh 当插件加载起来」。这一步要跑真宿主，
但**绝不能拿 `~/.dsh` 当试验场**（dsh-plugin-dev 铁律 1）。可复现的做法（本机已跑通）：

```bash
cd plugins/dsh-computer-use
mkdir -p .dsh-dev && cp ~/.dsh/.credentials.yaml .dsh-dev/     # 隔离 home 里没有凭据，模型调不动
cat > .dsh-dev/cua.patch.yml <<'EOF'                            # host 半区用绝对路径：
- insert:                                                       # 隔离 profile 里没有 dsh-computer-use 这个包
    - id: computer-use
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: computer-use
        command: /ABS/PATH/plugins/dsh-computer-use/.venv/bin/python
        args: [/ABS/PATH/plugins/dsh-computer-use/server/server.py]
        env: {}
        toolCallTimeoutMs: 120000
    - id: computer-use-host
      name: '/ABS/PATH/plugins/dsh-computer-use/lib/index.js'
      config: {}
EOF
DSH_ALPHA_HOME="$(pwd)/.dsh-dev" dsh-alpha dev \
  --from-default-profile headless --patch "$(pwd)/.dsh-dev/cua.patch.yml" \
  headless "用 computer-use 做只读冒烟：screen_info / list_apps / get_app / get_ax_state(diff) / sequence 的 observe 伪步 / release_lease"
```

- `dsh-alpha` 会把 `DSH_HOME` 硬钉在 `~/.dsh-alpha`（它自己的家目录），所以要用它认的
  `DSH_ALPHA_HOME` 指到隔离目录 —— 用 `DSH_HOME=` 是无效的，实测会去动 alpha 自己的 profile。
- **host 半区在 headless 里会停在 `pending (waiting for services: connection, webServer)`** ——
  这是对的：它 inject 的两个服务只有 web app 有。要验的就是「缺服务时安静待着，不是把宿主带崩」。
- 真动作那一轮（**不抢前台、不碰鼠标**）跑的是：`run_shell("open -g -a Calculator")` →`get_app`（`focus=false`）
  →`get_ax_state` →**一次 `sequence` 按 1、+、2、=**（4 步 AXPress，`settle_ms` 457/474/467/451）
  →再观察读回 `= 3` →`osascript -e 'quit app "Calculator"'`。这条链路同时验了元素路径、
  批处理、`effect` 证据与「AXPress 对后台应用有效」。
- `.dsh-dev/` 已在 `.gitignore` 里；里面那份 `.credentials.yaml` 是复制的，不想要就删掉，
  下次跑再复制（它不会写回 `~/.dsh`）。

## 维护守则（为什么长这样）

- 改完 `src/index.ts` 要 `npm run build`，然后**重启 DeepSeek Harness** 才生效；
  改完 `server/*.py` 同样要重启（MCP 服务器是常驻子进程）；改完 `capture/cua_capture.swift`
  要重跑 `sh scripts/build_capture.sh`。
- `npm run check` 里的 `client-smoke` / `host-smoke` 真的把半区执行一遍（注册 → apply → 渲染 /
  起服务 → 打路由），抓 `node --check` 抓不到的「用了没声明的名字」；`lint_py.sh` 先无条件跑两条
  「真炸过」的守则（CF 借用引用必须 retain、滚动单位必须是 Line=1），再用 pyflakes 扫
  `server/*.py`（没装就跳过），专防「函数删了、调用点还留着」—— 预览浮窗和错误分支平时没人
  手动点，这类问题一跑就是 NameError。
- **路由 handler 只能用「注入后的 scope」，不能碰外层 `ctx` 的服务属性**（2026-10-12 真事故）：
  本插件的静态 `inject` 是空数组（为了在 tui/headless 里也能挂上身份管线），而 cordis 里访问一个
  **没注入**的服务属性会抛 `cannot get property "connection" without inject` —— handler 一进来就抛，
  webserver 把它兜成一个**没有响应体的 400**，用户看到的只有「拿不到电脑操控的状态（HTTP 400）」，
  连是哪儿抛的都看不出来（`/_computer-use/*` 这几条路由从 `inject: []` 那次重构起就一直是这样，
  只是卡片退休后再没人调用，直到设置页第一个去调）。规矩：`ctx.inject([...], scope => …)` 拿到的
  scope 才能读服务（`scope.connection`），或者用免注入的 `ctx.get('name')`；handler 主体另有一层
  try/catch（`reportRouteError`），异常会回 500 + 原因并写进 `<BASE>/host-error.log`。
  `scripts/host-smoke.mjs` 的假 ctx 现在照抄 cordis 这条语义（未注入访问就抛），再犯会当场红。
- **Dock 上不留 Python 图章（自建 UIElement bundle）**：framework 版解释器的主程序在
  `Resources/Python.app` 里，进程一旦和窗口服务器接上，LaunchServices 就按这个 bundle 把进程
  **以及它 spawn 的子进程**（osascript / screencapture / sips / lsappinfo…）登记成一个应用，
  Dock 上于是多出一枚 Python 火箭，并且一直留到进程退出 —— dsh 每开一轮电脑操作就攒一枚。
  三条走过的弯路（macOS 27.2 实测）：`cg_input.hide_from_dock()` 那种
  `TransformProcessType→UIElement` 只改得动 `lsappinfo` 里的 type，**拦不住 Dock 那颗图块**；
  把解释器拷出 bundle 跑，Dock 会把图块记到最近的上级应用 dsh 头上（多一枚「DeepSeek
  Harness」）；只把激活那条路径从 osascript 换成 AX 也没用 —— 截图走的 `screencapture`
  一样能把图块带出来。真正管用的是 plist 级的 `LSUIElement`：`server/ui_element_app.py`
  在进 MCP 循环之前把进程换进自建 bundle（`runtime/DshComputerUse.app`：plist + ad-hoc 签名
  + 指回 venv 的 `pyvenv.cfg`/`lib` 符号链接），`os.execv` 换进程映像，pid 与 stdio 管道不变，
  dsh 侧完全无感。识别不出「住在 `.app` 里的那份解释器」或签名失败就直接不换 —— 最坏表现是
  Dock 上多一枚图章，绝不是工具起不来；`DSH_CUA_NO_REEXEC=1` 可整条关掉。
  坑：framework 里那两份二进制不是一回事，`Resources/Python.app/Contents/MacOS/Python`
  （app 主机程序）才管用，`bin/python3.12` 塞进自建 bundle 照样出图块 —— 实测踩过。
  构建只在第一次启动时做（拷 115KB + ad-hoc 签名 + 跑一次验证，实测冷启动 132ms），之后每次启动
  只多一次 execve（52ms → 84ms）；解释器换了会自动重建。
- **宿主断开就退，别把最后几秒睡完**：读 stdin 的循环一进到动作里（`wait` / `sequence` /
  每步 `_settle`）就没人看管道了，dsh 这时断开连接我们还在原地睡 —— 实测滞留过一批
  20~37MB 的 python 进程，每个都带一枚火箭。`mcp_stdio` 把读放进守护线程，EOF 一到就置
  `host_gone`；动作里的停顿统一用 `pause()`，醒来发现宿主没了直接 `SystemExit`。
  键位按住-松开之间（`_hold_combo` / `cg_input.hold`）刻意不接这条，避免断开时留下按住的键。
- **取消要真的打断动作**：dsh 的 mcp-client 给每次调用都带取消信号，宿主按停止键 / 调用超时
  发的就是 `notifications/cancelled`。这条通知以前被当成「未知通知」丢掉，`wait 10` 会照跑满
  10 秒（表现：按了停止，鼠标还在自己动）。现在读线程**就地**处理它（主线程正卡在动作里，
  没人看队列），`pause()` 在 50ms 粒度上让出并抛 `Cancelled`；`Cancelled` 继承 `BaseException`，
  不会被工具里成片的 `except Exception` 吞成一条 isError。按 MCP 规范，被取消的请求**不回包**，
  服务器继续服务下一条（`mcp_wire_check.py` 里三条断言钉住：不回包、真的提前中断、之后照常工作）。
- 半区之间**不共享状态**：唯一的接口是 `/tmp/dsh-computer-use/` 里那几个文件。两个面
  （Host 抓帧 / 预览浮窗 / 落盘回落）读同一份帧与同一套活跃判定，所以行为不会分叉。
- **dsh 升级不能带走宿主页面**：client 半区自己 `document.body` 挂载（不依赖 `shell.overlay`
  之类的插槽），require 全包在 try 里，缺 seed 词就不挂载、只留一条日志；Host 半区的路由挂在
  自己的 context 上并 try 住，`agents` 服务或 helper 二进制缺失时降级成时间推断 / 回落抓屏。
  任何一侧失效，最坏表现是「没有预览」，不是「dsh 打不开」。
- `latest.jpg` 只有**一个写入者序列**：Host 的抓帧与 `server.py` 的截屏都走「临时文件 +
  `os.replace`」原子替换，读侧（`<img>` / NSImage）永远不会拿到半张 JPEG。

物理鼠标键盘由服务器内互斥锁串行化：同一时刻只有一个动作占用输入，
被占用时报 `ERROR[CONTROLLER_BUSY]`（对齐 ZCode 的同名错误）；跨会话/跨子代理那一层
由控制器租约仲裁（同一个错误码，但带持有者与剩余 TTL，见上文 v2 一节）。

## 安装细节

```
sh scripts/install.sh        # 一条命令：venv + 依赖 + patch 路径 + 抓屏 helper
```

脚本做的事（可重复执行）：建 `.venv` 并装 `server/requirements.txt`（含预览浮窗要的
pyobjc）；把 `cordis.patch.yml` 里 `command` / `args` 的绝对路径改成本机插件目录；
没有 `bin/cua_capture` 时尝试编译（失败不阻塞，会自动退回 `screencapture`）。
之后还剩两步手工：

1. 把插件加进 profile：插件页安装本包，或在 profile 的 `package.json` 里声明依赖 +
   加进 `dsh.profile.bundles`，然后重启 DeepSeek Harness。
2. 系统设置 → 隐私与安全性 → 给 **DeepSeek Harness** 授予「辅助功能」和
   「屏幕录制」权限（截图黑屏 / 点击无效就是这两项没给）。

### 换一台 Mac

同上，一条 `sh scripts/install.sh` + 那两步手工。为什么这样就够、以及什么是每台机器
各自的：

- **patch 里的绝对路径是唯一写死机器路径的地方**（`command` / `args` 指向 `.venv` 和
  `server.py`）—— 脚本按脚本所在目录改写它。dsh 的 patch 加载只把 `insert` 的
  **name** 解析成文件 URL，`config` 值原样传给 mcp-client，所以这里必须是真实路径。
- `runtime/DshComputerUse.app`（LSUIElement 自建 bundle）是**首次启动时按本机 Python
  生成**的，不需要拷；`/tmp/dsh-computer-use` 里的状态也是临时的。
- `bin/cua_capture` 是 `x86_64 + arm64`、`minos 13.0` 的通用二进制，Intel / Apple
  Silicon 都能直接用，不装 Xcode CLT 也行（退回 `screencapture`，~2.5fps）。
- 要求：**macOS 13+**、**Python 3.10+**（实测 3.12）。python.org 的 framework 版最好
  —— Dock 图章那个自建 bundle 就是从它拷解释器的；Homebrew / PATH 里的普通 Python
  本来也不在 Dock 上留图章，代码里找不到 framework 的 app 主机程序就跳过换 bundle。
- 降级都是有意的：没装 pyobjc → 没有预览浮窗，工具照常；没给权限 → 工具明确报错并
  指路「系统设置」。

不需要额外步骤：首次启动时服务器会自己把解释器搬进
`runtime/DshComputerUse.app`（`LSUIElement=true` 的自建 bundle，见下面「Dock 上不留
Python 图章」），Dock 上就不会再攒 Python 火箭。这个目录是生成物，删掉会按需重建；
`DSH_CUA_NO_REEXEC=1` 可以让它整个不换（排障用）。

改完 `server/*.py` / `lib/*.js` 要重启 DeepSeek Harness 生效；只改了 python 侧时，
杀掉 `server.py` 进程也行 —— mcp-client 会自动重连并读到新代码。**客户端半区**
（`client/settings.js` 或 `package.json` 里的 `dsh.client`）同样要重启：客户端清单在宿主启动时
扫描并缓存（`pkgMeta` 按 `baseUrl\0loaderName` 记住结论），热替换不会重读它。

## 已知边界（完整）

- 元素路径不受显示器限制（AX 坐标覆盖全部显示器）；截屏多显示器已支持（`display=`），
  坐标换算按截图所属显示器/窗口的全局几何处理
- `type_text` 走剪贴板粘贴，会覆盖当前剪贴板；`paste` 亦然
- **坐标基准跟着「最近一张截图」切换**：全屏截图与单窗口截图的像素原点不同，`click` /
  `scroll` / `drag` 会换算到不同的全局位置。为避免静默点错，这三个动作的结果里都会回一行
  `坐标系: 窗口[下载] 824x399 点 @(363,263) → 截图 1600x774px`，跨窗口截图操作前先看它
- 部分原生应用不实现 `AXRaise`（实测计算器）：`focus_window` 会退回激活应用并如实说明
  「窗口未单独提升」，不再直接报失败
- Electron 应用首次 `get_app`/`get_ax_state` 会自动设置 AXManualAccessibility 开启 AX
- 元素索引在两次观察之间保持稳定（diff 模式下未变化的行不重编号）；
  界面大改后请重新观察。批处理里插了 `observe` 之后必须用新索引（结果里会明说）
- **`CONTROLLER_BUSY` 现在是两层**：同一服务器内的并发动作（瞬时互斥锁，v1 就有）与
  **跨会话/子代理的租约仲裁**（`get_app` 申请、每个动作续期、TTL 60 秒、`force` 可抢占）。
  仍然感知不到「别的自动化工具（或用户自己写的脚本）在动鼠标」——用户接管只是启发式提示：
  下一次动作前发现光标自己动过就提示一句，不阻断
- **急停状态跨会话保留**（`$DSH_CUA_DIR/kill`）：用户按过停止之后，新会话默认仍是只读，
  恢复要 `resume()` 或浮窗上的 ▶。这是有意的 fail-safe 方向，不是 bug
- **`step_timeout_ms` 是预算不是强杀线**：已下发的单个 CGEvent / AX 调用不可抢占，
  它约束的是每步的等待与 settle；要「立刻停」用急停（可中断点在 `pause` 与 `_settle` 里，≤0.1 秒）
- `a11y` 策略下没有语义动作的动作（拖拽、`set_value(event)`、长按、`a11y` 双击）一律**报错不降级**；
  这是设计意图，不是缺陷
- **画面盯的是哪扇窗**：MCP 服务器在 `get_app` / `focus_window` / `screenshot(window_id=)`
  之后把目标窗口写进 `focus.json`，Host 半区据此给抓屏进程加 `--window <CGWindowID>` ——
  SCK 走 `SCContentFilter(desktopIndependentWindow:)`，于是预览画面显示的就是 Agent 正在操作的
  那一扇窗（对齐 Qoder 的窗口级画中画），窗口被别的窗盖住也照样看得清。目标变了会自动切流；
  没有目标、或那一扇窗关了，就退回整屏。
  两个坑都实测过：`desktopIndependentWindow` 会走 CGS 连接，进程没先建立连接会直接
  `Assertion failed: (did_initialize)` 打死（现在起流前先取一次 `CGMainDisplayID` /
  `CGWindowListCopyWindowInfo`）；选目标窗要按**面积**挑，CGWindowList 的前后顺序里
  66x20 的标签选择器小窗会插在文档窗前面
- **多会话隔离依赖 host 半区**：身份来自 `tools/pre-execute` 的 `exec.agent.id`。老版本 host 半区
  （没有这段代码）、或别的 DSH 版本里事件不派发时，服务器退回「一个共享桶」= 旧行为，并在
  `screen_info` 里如实写「会话身份: 未启用」；此时只剩绑定令牌 + 索引纪元两道纵深防线
- **调用者闸门是刻意的串行点**：同一时刻只有一个 computer-use 调用在跑（identity 文件必须与调用
  一一对应），同一实例里的多会话会排队而不是并行抢鼠标；`post-execute` 万一没回来，150 秒保险丝
  强制放行（宁可放行下一个，也不把所有人的电脑操作卡死）
- **AX 观察器按 pid 共享**：这是资源缓存而不是会话状态（索引就是「哪个应用的第几号元素」）。
  生命周期已修：`forget()` 以前是死代码 —— 观察器建了就一直在 `_OBSERVERS` 里，每个节点攥着一份
  retain 过的 `AXUIElementRef` 加整树快照，一轮任务开关十几个应用就是真泄漏。现在
  `observer_for()` 会顺手回收已退出应用的观察器，`_resolve_app` 发现绑定应用退出时立刻回收，
  急停 `stop()` 里 `forget_all()`，并提供显式 `forget_observer(pid)`（wire check 有断言）
- **索引纪元的前提写在 `bump_index_epoch()` 的 docstring 里**，调用方必须保证：① 单线程调用
  （MCP 分发是单线程的，`_bind_app` 是唯一调用点）；② **只在换绑时调、每次换绑都必须调**；
  ③ 同一观察器在一个纪元内新增节点数 < `_EPOCH_STEP`（每次观察上限 2000，留了 50 倍余量）。
  漏调一次，两个绑定期的索引空间就会重叠 —— 那正是这个机制要防的事
- 模型侧建议：GUI 任务用擅长视觉定位的模型 + 适度推理等级，步骤会少很多
