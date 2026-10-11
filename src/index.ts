/**
 * `dsh-computer-use` — the Host half.
 *
 * It is the frame source for the observation surface, and nothing else: no input
 * injection, no agent state of its own. The MCP server writes
 * /tmp/dsh-computer-use/{latest.jpg,status.txt,card.txt}; this half reads those
 * files and re-serves them over three authenticated GET routes on the page's own
 * origin:
 *
 *   GET /_computer-use/stream        — MJPEG live frames (ScreenCaptureKit helper
 *                                      when available, `screencapture` fallback),
 *                                      started and stopped on demand;
 *   GET /_computer-use/preview.jpg   — one frame, for debugging without a stream;
 *   GET /_computer-use/status.json   — { action, imageMtime, actionMtime, card,
 *                                      killed, agentRunning?, now } and the
 *                                      "someone is watching" signal (?view=1);
 *   GET /_computer-use/stop          — 急停：写 <BASE>/kill，MCP 服务器在下一个
 *                                      可中断点（≤0.1s）进入只读；?on=0 或
 *                                      /resume 解除。这是用户侧的第三个入口
 *                                      （另两个是浮窗上的 ⏹ 与 MCP 的 stop 工具）。
 *   GET /_computer-use/settings.json — 设置页（Settings → 电脑操控）的状态：偏好 +
 *                                      观察窗运行期状态；POST 同一路径改偏好，落盘到
 *                                      ~/.dsh/computer-use/settings.json 并镜像进
 *                                      <BASE>/panel.hidden 与 <BASE>/panel.conf；
 *   POST /_computer-use/permissions  — 打开系统设置的辅助功能面板（权限卡片上那个按钮）。
 *
 * The observation surface itself is the native floating window
 * (`server/preview_window.py`), which reads the same files; the browser card that
 * used to mount into the page is retired and kept in `retired/preview-card.js`.
 * Nothing in the page is required for the feature to work — the one page-level
 * thing this plugin ships is the **settings section** (`client/settings.js`,
 * declared as `dsh.client`), and losing it only costs the switches, not the tool.
 *
 * Two rules keep a dsh upgrade from taking the page down with us: mount the route
 * on this plugin's own context inside a try, and degrade to logging when a service
 * (`agents`) or a binary (`bin/cua_capture`) is missing.
 *
 * Every runtime resource this half creates — the housekeeping timer, the capture
 * child process, the open MJPEG responses — is owned by `ctx.effect`, so unloading
 * or hot-replacing the plugin releases all three. A capture process left behind is
 * not a cosmetic leak: it keeps the system's screen-recording indicator lit.
 * @module dsh-computer-use
 */

import { execFile, spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { HostContext, ToolExecution } from './types.js'

/** Cordis plugin name. */
export const name = 'computer-use'

/**
 * 静态依赖 = **空**。
 *
 * 以前这里写的是 `['connection', 'webServer']`，于是整个 host 半区在 tui / headless 这类没有
 * web 服务的 profile 里**永远不会挂载** —— 而「这次调用属于哪个会话」的调用者身份管线正是在
 * 这一半区里（`mountCallerIdentity`）。结果是：没有 web 服务的 profile 里多会话缺陷静默复发。
 *
 * 现在拆开：身份管线只要有 `ctx.on` 就挂（任何 profile 都成立），预览路由走
 * `ctx.inject(['connection', 'webServer'], …)` 动态注入，服务不在就只是没有预览路由。
 */
export const inject: string[] = []

/**
 * The MCP server's scratch directory (latest.jpg / status.txt live here).
 * `DSH_CUA_DIR` lets the smoke tests run against an isolated directory instead of
 * racing the real session's frame writer.
 */
const BASE = process.env.DSH_CUA_DIR || '/tmp/dsh-computer-use'
/** The route prefix this plugin owns. */
const CHANNEL = '/_computer-use'
/** 观察窗画面：MCP 服务器与这里的实时抓屏共用同一个文件。 */
const SHOT = `${BASE}/latest.jpg`
/** 抓屏落点，压完尺寸后原子替换 SHOT。 */
const SHOT_TMP = `${BASE}/live_tmp.jpg`
/**
 * 急停标志（kill switch）。**格式必须与 server/killswitch.py 一致**：
 * `on:<毫秒时间戳>:<原因>` = 只读；文件不存在 = 正常。
 * 用户侧的三个入口（浮窗 ⏹ / 这条路由 / MCP 的 stop 工具）写的是同一个文件，
 * 因为「立刻中止正在跑的动作」只能靠文件：那时 MCP 服务器正卡在动作里，
 * 工具调用那条路走不通。
 */
const KILL = `${BASE}/kill`
/**
 * 实时抓屏的最短间隔。一帧 = screencapture 154ms + sips 136ms ≈ 290ms，
 * 所以 400ms 已经是这条通道的实际上限（~2.5fps）；再低只会让抓屏排队叠起来。
 * 要 30fps 得换 ScreenCaptureKit 流（见 README「观察窗」一节的 Tier 2）。
 */
const LIVE_MS = 400

/** SCK 抓帧 helper（scripts/build_capture.sh 编到 bin/cua_capture）。 */
const CAPTURE_BIN = fileURLToPath(new URL('../bin/cua_capture', import.meta.url))
/**
 * 卡片只有 248 CSS px（dpr 2 = 496 设备像素），512 宽足够。
 * 实测 512@30fps：29.2fps、31.6KB/帧、~920KB/s、helper 8.7% CPU —— 比 15fps 只多
 * 4.7 个百分点（帧间变化小，每帧体积基本不变），所以默认吃满 30。
 */
const FRAME_W = Number(process.env.DSH_CUA_STREAM_WIDTH || 512)
const FRAME_FPS = Number(process.env.DSH_CUA_STREAM_FPS || 30)
/** 没人看了就关流：抓屏进程活着 = 系统录屏灯常亮 + 持续吃 CPU，绝不能漏。 */
const IDLE_OFF_MS = 5000
/** 与浏览器半区同一套「在用电脑」窗口：超过这么久没动作就不再为画面花钱。 */
const DORMANT_MS = 120000
const BOUNDARY = 'cuaframe'
/** 给面板落盘的间隔：面板 250ms 轮一次，写更密只是白吃磁盘。 */
const FILE_MS = 1000 / 10

let capturing = false
const clients = new Set<ServerResponse>()
/** 最近一次「有人要看」的信号：view=1 轮询 或 某个 MJPEG 连接成功收下一帧。 */
let lastDemand = 0
let proc: ReturnType<typeof spawn> | null = null
let pending: Buffer = Buffer.alloc(0)
let latestFrame: Buffer | null = null
/** 连续启动即失败（权限被撤 / 二进制不兼容）时先冷却 60 秒，而不是整场会话永久降级。 */
let captureFailures = 0
let captureRetryAt = 0
/** 我们自己收掉的进程（WeakSet 而不是给对象打标记：退出回调是异步的，
 *  期间可能已经换了新的 proc，标记必须跟着那一个进程走）。 */
const stoppedByUs = new WeakSet<object>()
let lastFileWrite = 0

/**
 * 自己抓一帧屏幕，让观察窗在 Agent 没动作时也是活的（对齐 ZCode/Qoder 的持续画面）。
 *
 * 单飞 + 原子替换：`screencapture` 一次要 100~400ms，绝不能让 250ms 的轮询把它叠成
 * 一串；写临时文件再 rename，避免卡片读到半张 JPEG。Agent 自己截屏写的也是 SHOT，
 * 两边都走原子替换，互不撕裂。
 */
function refreshShot(): void {
  if (capturing || process.env.DSH_CUA_LIVE === '0') return
  capturing = true
  execFile('screencapture', ['-x', '-o', '-t', 'jpg', SHOT_TMP], (error) => {
    if (error) {
      capturing = false
      return
    }
    // 原图是 Retina 全屏（~3024px），压到长边 1600 再分发，省掉每秒几 MB 的传输
    execFile('sips', ['-Z', '1600', SHOT_TMP], (resizeError) => {
      capturing = false
      if (resizeError || !existsSync(SHOT_TMP)) return
      try {
        renameSync(SHOT_TMP, SHOT)
      } catch {
        return
      }
      // 关键：回落也必须喂 MJPEG 客户端，否则没装 helper 的机器上卡片会永久断流
      if (clients.size > 0) emitFrame(readFileSync(SHOT))
    })
  })
}

/**
 * 预览浮窗（OS 级观察窗）还在吗？它是独立进程、只读 latest.jpg，所以「有人在看」这件事
 * 只能靠它写在 preview.lock 里的 pid 判断 —— 它是推流要不要继续喂的依据。
 */
function panelAlive(): boolean {
  return panelPid() > 0
}

/**
 * 一帧进来，喂两个去处：内存里的 MJPEG 客户端 + 落盘（浮窗与回落路径都按文件出帧）。
 * 画面没变就直接丢掉：Agent 停下来想事情时桌面往往是静止的，而 SCK 照样按 30fps
 * 推等价帧 —— 省掉的是渲染进程每帧一次的 JPEG 解码与重绘，以及一次落盘。
 */
function emitFrame(frame: Buffer): void {
  if (latestFrame && latestFrame.length === frame.length && Buffer.compare(latestFrame, frame) === 0) return
  latestFrame = frame
  // 浮窗每 250ms 看一次文件、回落路径也从文件取帧，按 FILE_MS 落盘就够；
  // MJPEG 客户端拿的是内存里的这一帧
  if (Date.now() - lastFileWrite >= FILE_MS) {
    lastFileWrite = Date.now()
    try {
      mkdirSync(BASE, { recursive: true })
      writeFileSync(SHOT_TMP, frame)
      renameSync(SHOT_TMP, SHOT)
    } catch {
      /* 下一轮还会再写，这里不值得抛 */
    }
  }
  // 注意：写成功**不能**当作「有人要看」。卡片卸载后 Chromium 未必立刻关掉 MJPEG
  // 连接，连接会一直「收下」帧并把需求信号续满 —— 表现就是抓屏进程永不退出。
  for (const res of [...clients]) {
    try {
      res.write(`--${BOUNDARY}\r\ncontent-type: image/jpeg\r\ncontent-length: ${frame.length}\r\n\r\n`)
      res.write(frame)
      res.write('\r\n')
    } catch {
      clients.delete(res)
    }
  }
}

/**
 * 定期清扫：不依赖连接的 close 事件（dsh 的 webServer 包装层不保证把它送到，
 * Chromium 取消 MJPEG 连接的时机也不确定）。只看「还有没有人要帧」这个信号：
 * 卡片隐藏后既不再带 view=1，也没有连接收下帧，超时即关流。
 *
 * 由 `apply()` 里的 `ctx.effect` 按 2 秒一跑，随插件一起卸载 —— 放在模块顶层
 * 会让热替换后的旧定时器永远活着。
 */
/**
 * 调用者身份（任务书《computer-use 多会话隔离》B 方案）。
 *
 * 这个 MCP 服务器是**宿主级单例**：一个 DSH 实例里所有会话共用它，而 stdio MCP 协议
 * **不带任何身份字段**（dsh-mcp-client 只发 `{name, arguments}`，参数还是冻结的）。
 * 唯一知道「这次调用属于哪个会话」的地方是 host 半区 —— `tools/pre-execute` 的
 * `exec.agent.id` 就是会话 id（spike 实测：`session-78c637…`）。
 *
 * 所以这一半区做两件事：
 *
 *  1. **闸门**：computer-use 的调用一次只放一个过去，直到它的 `post-execute` 回来。
 *     没有它，两个会话同时调用时 `caller.json` 是谁后写的就归谁 —— 服务器会把 A 的动作
 *     算到 B 头上，缺陷原样复现。串行化顺带保住了「同一块鼠标键盘」这条物理约束。
 *  2. **写 `caller.json`**：`{agent, tool, seq, at}`，原子替换。MCP 服务器在每次
 *     `tools/call` 开头读它，把绑定与租约按会话分份。
 *
 * 读不到这个文件（老版本 host 半区、闸门超时后的残留）时，服务器退回「一个共享桶」，
 * 也就是旧行为 —— 降级而不是坏掉。
 */
const CALLER = `${BASE}/caller.json`
/** 只管这个命名空间下的工具；别的工具（内置 read/bash…）一概不碰，也不写文件。 */
const CU_TOOL_PREFIX = 'mcp__computer-use__'
/**
 * 闸门保险丝：`toolCallTimeoutMs` 是 120 秒，取它 +30 秒。真被触发说明 `post-execute`
 * 没回来（极少见），此时**宁可放行下一个**也不能把所有人的电脑操作卡死。
 */
const GATE_FUSE_MS = 150_000
/** 闸门当前被谁持有（序号）与等待队列；0 = 空闲。 */
let gateHolder = 0
let gateTimer: ReturnType<typeof setTimeout> | null = null
const gateQueue: Array<() => void> = []
let callerSeq = 0

function armFuse(): void {
  if (gateTimer) clearTimeout(gateTimer)
  gateTimer = setTimeout(() => {
    gateTimer = null
    process.stderr.write('dsh-computer-use: 调用者闸门超时（post-execute 未回），强制放行下一个\n')
    openGate(gateHolder)
  }, GATE_FUSE_MS)
  // 定时器不参与事件循环存活判定：宿主退出时不该被它拖住
  ;(gateTimer as unknown as { unref?: () => void }).unref?.()
}

/** 等闸门。拿到之后必须保证最终 `openGate(seq)`（post-execute / result / 保险丝）。 */
async function awaitGate(): Promise<void> {
  if (gateHolder === 0) return
  await new Promise<void>((resolve) => gateQueue.push(resolve))
}

/** 交棒/放行。`seq` 对不上 = 重复释放（post-execute 与 result 都会到），忽略即可。 */
function openGate(seq: number): void {
  if (seq !== 0 && seq !== gateHolder) return
  if (gateTimer) {
    clearTimeout(gateTimer)
    gateTimer = null
  }
  const next = gateQueue.shift()
  if (next !== undefined) {
    next() // 交棒：gateHolder 由下一个调用在自己的 pre 里改写
    return
  }
  gateHolder = 0
}

/** 原子写 `caller.json`；返回本次的闸门序号。 */
function publishCaller(agent: string, tool: string): number {
  callerSeq += 1
  gateHolder = callerSeq
  armFuse()
  const payload = { agent, tool, seq: callerSeq, at: Date.now() }
  try {
    mkdirSync(BASE, { recursive: true })
    writeFileSync(`${CALLER}.tmp`, JSON.stringify(payload))
    renameSync(`${CALLER}.tmp`, CALLER)
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 写 caller.json 失败 (${String(error)})\n`)
  }
  return callerSeq
}

/**
 * 挂上调用者身份管线，返回 disposer。
 *
 * `pre-execute` / `post-execute` 是 **waterfall**：监听器必须 `return next()`，否则工具调用会
 * 挂住 —— 这也正是闸门能生效的原因（spike 实测：pre 里 sleep 2 秒，工具真的等满 2 秒才跑）。
 */
function mountCallerIdentity(ctx: HostContext): () => void {
  const offPre = ctx.on?.(
    'tools/pre-execute',
    async (exec: ToolExecution, next: () => Promise<unknown>) => {
      if (!String(exec?.name ?? '').startsWith(CU_TOOL_PREFIX)) return next()
      await awaitGate()
      const agent = String(exec?.agent?.id ?? '') || 'unknown'
      lastCuAgent = agent
      const seq = publishCaller(agent, String(exec.name))
      exec.__cuaGateSeq = seq
      return next()
    },
    { prepend: true }
  )
  const offPost = ctx.on?.(
    'tools/post-execute',
    async (exec: ToolExecution, _result: unknown, next: () => Promise<unknown>) => {
      if (String(exec?.name ?? '').startsWith(CU_TOOL_PREFIX)) openGate(exec.__cuaGateSeq ?? 0)
      return next()
    },
    { prepend: true }
  )
  const offResult = ctx.on?.('tools/result', (exec: ToolExecution) => {
    // 兜底：正常路径 post-execute 已经放行过（openGate 幂等），这里只兜「post 没到」的异常收尾
    if (String(exec?.name ?? '').startsWith(CU_TOOL_PREFIX)) openGate(exec.__cuaGateSeq ?? 0)
  })
  return () => {
    for (const off of [offPre, offPost, offResult]) {
      if (typeof off === 'function') {
        try {
          ;(off as () => void)()
        } catch {
          /* 卸载路径不抛 */
        }
      }
    }
    if (gateTimer) {
      clearTimeout(gateTimer)
      gateTimer = null
    }
    gateHolder = 0
    gateQueue.length = 0
  }
}

/**
 * 「这一轮还在不在跑」—— 浮窗靠它决定什么时候收起、什么时候打完成勾。
 *
 * 浮窗以前只看得到「最后一次动作是什么时候」，于是只有两个选择：要么很晚才收起（原来 120 秒，
 * 任务早结束了窗还杵着），要么在 agent 想事情的几十秒里把窗收掉（长回合里闪进闪出）。
 * host 半区知道得更多：`exec.agent.id`（谁在用电脑）+ `agents` 服务（它现在还在不在跑）。
 */
const TURN = `${BASE}/turn.json`
/** 最近一次 computer-use 调用属于哪个会话（pre-execute 记下来的）。 */
let lastCuAgent = ''
/** housekeeping 是定时器回调、拿不到 ctx，只能在这里存一份（apply 时赋值）。 */
let hostCtx: HostContext | null = null
let lastTurnKey = ''
let lastTurnAt = 0

/** 这个会话现在还在跑吗？拿不到 `agents` 服务时返回 null（浮窗退回纯时间判定）。 */
function agentRunning(id: string): boolean | null {
  if (!id) return false
  try {
    const listed = hostCtx?.get('agents')?.list?.()
    if (!Array.isArray(listed)) return null
    const hit = listed.find((a) => a?.id === id)
    return hit ? hit.status === 'running' : false // 会话已经没了 → 没在跑
  } catch {
    return null
  }
}

/**
 * 把「这一轮还在不在跑」写给浮窗。值变了立刻写；没变也每 10 秒续一次心跳
 * （浮窗拿 `at` 判断新鲜度，超过 15 秒就当拿不到、退回时间判定）。
 */
function publishTurn(): void {
  if (!lastCuAgent) return
  const running = agentRunning(lastCuAgent)
  const key = `${lastCuAgent}|${String(running)}`
  const now = Date.now()
  // 没变也每 4 秒续一次心跳：浮窗那侧的新鲜窗口是 15 秒（留 3 倍余量，避免边界抖动）
  if (key === lastTurnKey && now - lastTurnAt < 4_000) return
  lastTurnKey = key
  lastTurnAt = now
  try {
    mkdirSync(BASE, { recursive: true })
    writeFileSync(`${TURN}.tmp`, JSON.stringify({ agent: lastCuAgent, running, at: now }))
    renameSync(`${TURN}.tmp`, TURN)
  } catch {
    /* 下一轮再试：写不进去最多是浮窗晚一点收起 */
  }
}

/**
 * 浮窗在看着吗 = 进程活着 **且** 正显示着。
 *
 * 只看 `preview.lock` 的 pid 会让「收起之后」继续抓屏：浮窗收起时会写 `panel.state=hidden`，
 * 据此停流 —— 抓屏进程活着意味着系统录屏灯亮着，一秒都不该白亮。
 * 读不到 panel.state（老浮窗）时退回旧行为（只要进程在就算在看）。
 */
export function panelWatching(): boolean {
  if (!panelAlive()) return false
  try {
    return readFileSync(`${BASE}/panel.state`, 'utf8').trim().startsWith('up')
  } catch {
    return true
  }
}

/** 动作时间戳（status.txt 的 mtime），读不到就是 0（未知）。 */
function activityMtime(): number {
  try {
    return Math.floor(statSync(`${BASE}/status.txt`).mtimeMs)
  } catch {
    return 0
  }
}

/** 急停是不是已触发（文件以 `on` 开头）。读不到 = 正常。 */
function killReason(): string {
  try {
    const raw = readFileSync(KILL, 'utf8').trim()
    return raw.startsWith('on') ? raw.split(':').slice(2).join(':') || '已停止' : ''
  } catch {
    return ''
  }
}

/**
 * 写/清急停标志。写临时文件再 rename：Python 那边可能正在读它，半个文件会被读成
 * 「没停」—— 那正是这个开关最不能出的错。
 */
function setKill(on: boolean, reason: string): void {
  try {
    mkdirSync(BASE, { recursive: true })
    if (!on) {
      rmSync(KILL, { force: true })
      return
    }
    const tmp = `${KILL}.tmp`
    writeFileSync(tmp, `on:${Date.now()}:${reason || '用户点了停止'}`)
    renameSync(tmp, KILL)
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 写急停标志失败 (${String(error)})\n`)
  }
}

/** 最新一帧的时间戳，没有帧就是 0。 */
function frameMtime(): number {
  try {
    return Math.floor(statSync(`${BASE}/latest.jpg`).mtimeMs)
  } catch {
    return 0
  }
}

/**
 * ---- 设置页（Settings → 电脑操控）的偏好与运行时通道 --------------------------
 *
 * 用户侧终于有了一个**找得到的开关**。以前「以后别再自动出现」只有 Agent 的
 * `preview(enable=False)` 能写，而用户侧没有任何恢复入口（README 里那条真事故：随手点一下
 * 浮窗的 × 就等于打开了一个他再也关不掉的开关）。这一节给设置页两样东西：
 *
 *   1. **偏好落盘**（`~/.dsh/computer-use/settings.json`）：`/tmp` 会被系统清掉，
 *      所以「不再自动出现」必须有一份活过重启的权威副本；
 *   2. **运行时通道**：把偏好镜像成浮窗与 MCP 服务器**本来就在读**的两个文件 ——
 *      `panel.hidden`（粘性开关，server.py 认 `off`）与 `panel.conf`（浮窗每拍重读：
 *      收起延时 / 「这一轮先收起」请求）。于是设置页不需要重启任何进程就生效，
 *      也不引入第二套只有这个页面认识的状态。
 *
 * 谁说了算：`panel.hidden` 是**运行时真相**（Agent 的 preview(False) 也写它），读设置时
 * 把它折叠回落盘的那份（`reconcileAutoShow`）—— 否则 Agent 关掉之后设置页还显示「开着」，
 * 重启还会把它复活。
 */

/** 粘性开关文件：与 server/server.py 的 PANEL_HIDDEN、preview_window.py 的 HIDDEN 同一个。 */
const HIDDEN = `${BASE}/panel.hidden`
/** 浮窗每拍读一次的偏好镜像（见 server/preview_window.py 的 `_conf`）。 */
const CONF = `${BASE}/panel.conf`
/**
 * 偏好落盘位置。`DSH_CUA_SETTINGS_DIR` 让冒烟检查跑在隔离目录里 —— 绝不能拿用户的真实
 * 偏好做断言，更不能把测试写的值留在他机器上。
 */
const SETTINGS_FILE = join(
  process.env.DSH_CUA_SETTINGS_DIR || join(homedir(), '.dsh', 'computer-use'),
  'settings.json'
)
/** 设置页给用户的收起延时选项（秒）；默认 12 秒与浮窗的 DSH_CUA_HIDE_S 对齐。 */
const AUTO_HIDE_CHOICES = [5, 12, 30, 60, 120]
const DEFAULT_AUTO_HIDE = 12

/** 设置页能改的两件事（都是「看」的偏好，没有一个会影响 Agent 能不能操作电脑）。 */
export interface PanelPrefs {
  /** 以后还自不自动拉起观察窗。false = 粘性关闭，只有设置页或 preview(true) 能解。 */
  autoShow: boolean
  /** 这一轮结束、安静这么多秒之后收起观察窗。 */
  autoHideSeconds: number
}

const DEFAULT_PREFS: PanelPrefs = { autoShow: true, autoHideSeconds: DEFAULT_AUTO_HIDE }

/** 内存里的当前偏好（第一次读盘后一直用这份；设置页写它就是改它）。 */
let prefs: PanelPrefs | null = null
/**
 * `panel.conf` 里的「这一轮先收起」请求（浮窗据此立刻 orderOut）。
 *
 * 唯一的产生者是 `applyAutoShow(false)`：用户把总开关关掉时，光写 `panel.hidden=off` 只能拦住
 * **以后**的自动拉起，已经开着的那块窗还得让它当场消失。带上毫秒 nonce（值变了才算一条新指令），
 * 方向固定为 `hide`（`release` 走浮窗自己的语义：下一轮开始自动解除）。
 */
let hideRequest: { action: '' | 'hide'; at: number } = { action: '', at: 0 }

/**
 * 严格递增的 nonce（毫秒）。
 *
 * 读侧（浮窗、MCP 服务器）判断「这是不是一条新指令」靠的是**值变了没有**。`Date.now()`
 * 在同一毫秒里会给出同一个值 —— 两次快速请求就会被读侧当成一条，第二次静默丢失。
 * 这里保证同一毫秒内的下一次请求也拿到更大的值。
 */
let nonce = 0
function nextNonce(): number {
  nonce = Math.max(Date.now(), nonce + 1)
  return nonce
}

/** 读 `panel.hidden` 的裸值（`off` / `hide` / `on` / `auto` / 空）。 */
function hiddenValue(): string {
  try {
    return readFileSync(HIDDEN, 'utf8').trim().split(':')[0] || ''
  } catch {
    return ''
  }
}

/**
 * 写 `panel.hidden`。带毫秒时间戳的 nonce 语义与 server.py / preview_window.py 一致：
 * 只有值真的变了才算一次新指令。原子写 —— 那两个读侧可能正在读它。
 */
function writeHidden(state: 'off' | 'on' | 'hide' | 'auto'): void {
  try {
    mkdirSync(BASE, { recursive: true })
    const tmp = `${HIDDEN}.tmp`
    writeFileSync(tmp, `${state}:${nextNonce()}`)
    renameSync(tmp, HIDDEN)
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 写 panel.hidden 失败 (${String(error)})\n`)
  }
}

/**
 * 收起延时的合法范围：太小会闪、太大等于没收起，两端都夹住。
 * 非法值（非数字、null）返回 `null` —— 调用方据此**保留原值**，而不是回落到默认值。
 */
function clampSeconds(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === 'boolean') return null
  const n = Math.round(Number(value))
  if (!Number.isFinite(n)) return null
  return Math.min(Math.max(n, 1), 3600)
}

/**
 * 用一份补丁盖在已有偏好上。**坏值保留原值，不回落到默认值** —— 一次带错字段的 POST
 * 不该把用户其它设置悄悄重置掉（设置页只发它真正改的那个字段）。
 */
function mergePrefs(before: PanelPrefs, patch: unknown): PanelPrefs {
  const raw = (typeof patch === 'object' && patch !== null ? patch : {}) as Record<string, unknown>
  const seconds =
    raw.autoHideSeconds === undefined ? before.autoHideSeconds : clampSeconds(raw.autoHideSeconds) ?? before.autoHideSeconds
  return {
    autoShow: typeof raw.autoShow === 'boolean' ? raw.autoShow : before.autoShow,
    autoHideSeconds: seconds
  }
}

/** 读落盘的偏好（只读一次）。文件不在 / 坏了 / 字段非法都用默认值，绝不抛。 */
function loadPrefs(): PanelPrefs {
  if (prefs) return prefs
  try {
    prefs = mergePrefs(DEFAULT_PREFS, JSON.parse(readFileSync(SETTINGS_FILE, 'utf8')))
  } catch {
    prefs = { ...DEFAULT_PREFS }
  }
  return prefs
}

/** 落盘（原子写）：设置页改一下就得活过重启，`/tmp` 里的镜像活不过重启。 */
function savePrefs(): void {
  const current = loadPrefs()
  try {
    mkdirSync(dirname(SETTINGS_FILE), { recursive: true })
    const tmp = `${SETTINGS_FILE}.tmp`
    writeFileSync(tmp, JSON.stringify(current, null, 2))
    renameSync(tmp, SETTINGS_FILE)
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 写设置文件失败 (${String(error)})\n`)
  }
}

/** 把偏好镜像成浮窗读的那份 conf（原子写；浮窗按 1 秒 TTL 重读）。 */
function writeConf(): void {
  const current = loadPrefs()
  const payload = {
    autoShow: current.autoShow,
    autoHideSeconds: current.autoHideSeconds,
    hideRequest: hideRequest.action,
    hideRequestAt: hideRequest.at,
    at: Date.now()
  }
  try {
    mkdirSync(BASE, { recursive: true })
    const tmp = `${CONF}.tmp`
    writeFileSync(tmp, JSON.stringify(payload))
    renameSync(tmp, CONF)
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 写 panel.conf 失败 (${String(error)})\n`)
  }
}

/**
 * 把「自不自动出现」落到运行时通道。
 *
 * 关掉时要做三件事，缺一件都会留下「我明明关了它怎么还在」的观感：
 *   1. 写 `panel.hidden=off`（server.py 据此不再自动拉起，粘性，重启后由落盘的偏好续上）；
 *   2. **结束观察窗进程**（SIGTERM，浮窗自己清 lock 再退出）—— 只写文件的话，已经开着的那块
 *      窗还留在屏幕上，直到这一轮结束；
 *   3. 顺手写一次「收起请求」，万一进程没被收掉（权限/异常），它也会立刻从屏上消失。
 */
function applyAutoShow(autoShow: boolean): void {
  if (autoShow) {
    if (hiddenValue() === 'off') writeHidden('on')
    return
  }
  if (hiddenValue() !== 'off') writeHidden('off')
  stopPanel()
  hideRequest = { action: 'hide', at: nextNonce() }
  writeConf()
}

/** 结束观察窗进程（SIGTERM：浮窗装了信号处理器，会先清掉 lock 再退出）。 */
function stopPanel(): boolean {
  const pid = panelPid()
  if (!pid) return false
  try {
    process.kill(pid, 'SIGTERM')
    return true
  } catch {
    return false
  }
}

/** 把运行时真相折叠回落盘的那份偏好（Agent 的 preview(False) 也写 panel.hidden）。 */
function reconcileAutoShow(): void {
  const current = loadPrefs()
  const live = hiddenValue() !== 'off'
  if (live === current.autoShow) return
  current.autoShow = live
  savePrefs()
}

/** 观察窗进程的 pid（`preview.lock` 里的那个，探活之后才认）。0 = 没起来。 */
function panelPid(): number {
  try {
    const pid = Number(readFileSync(`${BASE}/preview.lock`, 'utf8').trim())
    if (!pid) return 0
    process.kill(pid, 0)
    return pid
  } catch {
    return 0
  }
}

/** 观察窗的运行期状态：设置页的状态区与冒烟检查都读它。 */
export function panelStatus(): Record<string, unknown> {
  const pid = panelPid()
  let state = ''
  try {
    state = readFileSync(`${BASE}/panel.state`, 'utf8').trim().split(':')[0] || ''
  } catch {
    /* 老浮窗没有这个文件：那就是「不知道在不在屏上」，按进程活着算 */
  }
  const reason = killReason()
  return {
    panelPid: pid,
    panelUp: pid > 0,
    // 进程活着 + 写着 up = 真的在屏上；读不到 panel.state（老浮窗）时按在屏上算
    panelVisible: pid > 0 && state !== 'hidden',
    hidden: hiddenValue(),
    hideRequest: hideRequest.action,
    hideRequestAt: hideRequest.at,
    killed: reason !== '',
    killReason: reason,
    lastActionAt: activityMtime(),
    frameMtime: frameMtime()
  }
}

/** 设置页读/写一次都回同一份形状：页面拿到响应直接替换本地状态，不需要二次拉取。 */
function settingsPayload(): Record<string, unknown> {
  reconcileAutoShow()
  const current = loadPrefs()
  return {
    settings: { ...current },
    runtime: panelStatus(),
    choices: AUTO_HIDE_CHOICES,
    paths: { settings: SETTINGS_FILE, conf: CONF, base: BASE }
  }
}

/** 打开系统设置的辅助功能面板（与 MCP 的 `open_permission_settings` 同一件事）。 */
function openPermissionSettings(): void {
  // 冒烟检查/无人环境不该真的弹出一个系统设置窗口：DSH_CUA_NO_OPEN=1 时只留一行日志。
  if (process.env.DSH_CUA_NO_OPEN === '1') {
    process.stderr.write('dsh-computer-use: 跳过打开系统设置（DSH_CUA_NO_OPEN=1）\n')
    return
  }
  execFile(
    'open',
    ['x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'],
    () => {
      /* 打不开就算了：这只是一条引导，不该让页面报错 */
    }
  )
}

/** 统一回 JSON（这几个端点都是给设置页看的，一律 no-store）。 */
function respondJson(res: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'content-type': 'application/json',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body)
  })
  res.end(body)
}

/**
 * 读一个小 JSON 请求体。**封顶 64KB**：这是宿主进程里的路由，一个失控的请求体不该
 * 把内存吃穿；读不出合法 JSON 就返回 null，由调用方回 400。
 */
async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown> | null> {
  const chunks: Buffer[] = []
  let size = 0
  return await new Promise((resolve) => {
    let settled = false
    const finish = (value: Record<string, unknown> | null): void => {
      if (settled) return
      settled = true
      resolve(value)
    }
    req.on('data', (chunk: Buffer) => {
      size += chunk.length
      if (size > 64 * 1024) {
        finish(null)
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      try {
        const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
        finish(typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null)
      } catch {
        finish(null)
      }
    })
    req.on('error', () => finish(null))
  })
}

/**
 * 设置页的三个端点（`settings.json` / `panel` / `permissions`）。
 *
 * 单独拎出来是因为它们要**异步读请求体**，而路由本身是同步的（`RouteHandler` 返回 void）：
 * 调用点用 `void` 起这条链，异常在函数内部收口成 500，绝不变成未处理的 rejection。
 */
/**
 * 导航图标补丁的自检日志：追加一行，超过 8KB 就只留尾部 —— 这是个诊断文件，不是账本。
 * @param line - 已拼好的单行记录（调用方负责截断字段长度）。
 */
function appendNavIconLog(line: string): void {
  const file = `${BASE}/nav-icon.log`
  try {
    const previous = existsSync(file) ? readFileSync(file, 'utf8') : ''
    const next = (previous + line + '\n').slice(-8192)
    writeFileSync(file, next)
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 写 nav-icon.log 失败 (${String(error)})\n`)
  }
}

async function handleSettingsRequest(
  endpoint: string,
  req: IncomingMessage,
  res: ServerResponse
): Promise<void> {
  try {
    if (endpoint === 'settings.json') {
      if (req.method === 'GET') {
        respondJson(res, 200, settingsPayload())
        return
      }
      if (req.method !== 'POST') {
        respondJson(res, 405, { error: 'method not allowed' })
        return
      }
      const body = await readJsonBody(req)
      if (body === null) {
        respondJson(res, 400, { error: 'bad json body' })
        return
      }
      const before = { ...loadPrefs() }
      prefs = mergePrefs(before, body)
      savePrefs()
      // 顺序有讲究：autoShow 变了就走 applyAutoShow（它自己会把收起请求与 conf 一起写对），
      // 没变才单独镜像一次 conf（收起延时改了也要落到浮窗那边）。
      if (prefs.autoShow !== before.autoShow) applyAutoShow(prefs.autoShow)
      else writeConf()
      respondJson(res, 200, settingsPayload())
      return
    }
    // nav-icon：客户端那个「导航行图标」补丁的自检上报。
    //
    // 为什么需要它：平台没给第三方设置页留图标位，图标只能在页面里补（见 client/settings.js 的
    // installNavIcon），而补丁失败时**页面上什么都看不出来** —— 还是那个兜底齿轮。补不上的原因
    // （壳改了行高、文案对不上、对话框结构变了……）只有页面自己知道，所以让它写一行到这里：
    // 用户问「为什么图标还是齿轮」时，答案就在这个文件里。
    if (endpoint === 'nav-icon') {
      if (req.method !== 'POST') {
        respondJson(res, 405, { error: 'method not allowed' })
        return
      }
      const body = await readJsonBody(req)
      if (body === null) {
        respondJson(res, 400, { error: 'bad json body' })
        return
      }
      const outcome = String(body.outcome ?? '').slice(0, 40)
      const detail = String(body.detail ?? '').slice(0, 600)
      if (outcome) appendNavIconLog(`${new Date().toISOString()} ${outcome} ${detail}`)
      respondJson(res, 200, { ok: true })
      return
    }
    // permissions：指引区那个「打开辅助功能设置」按钮
    if (req.method !== 'POST') {
      respondJson(res, 405, { error: 'method not allowed' })
      return
    }
    openPermissionSettings()
    respondJson(res, 200, { ok: true })
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 设置路由失败 (${String(error)})\n`)
    respondJson(res, 500, { error: 'internal' })
  }
}

/**
 * 有人看 + Agent 在动 → 把帧源起起来（或按目标窗口换掉）。
 *
 * 为什么要有这个函数：起流的调用点原来**只**在 `status.json` 与 `/stream` 两个
 * handler 里，而唯一会请求这两个路由的客户端是已经退休的浏览器卡片 —— 浮窗只读
 * 文件、一句 HTTP 都不发（`preview_window.py` 里没有任何网络调用）。卡片一退休，
 * 「浮窗开着就一直有画面」就再没人触发过：Agent 一停手画面就冻住。
 *
 * 判据刻意和浮窗自己的 `_fresh()` 用同一把尺子（status.txt/heartbeat 的 mtime、
 * 同一个 DORMANT 窗口）：浮窗只在「有动作」时才上屏，所以在这窗口之外补帧纯属
 * 白烧 CPU 并让系统录屏灯常亮。
 * @param actionMtime - Agent 最后一次动作的时间戳（0 = 未知）。
 */
function pumpFrames(actionMtime: number): void {
  if (actionMtime <= 0 || Date.now() - actionMtime > DORMANT_MS + 10000) return
  // 目标窗口换了（绑了别的应用、或抬了另一扇窗）就把流切过去，否则画面会一直盯在旧窗口上。
  if (proc && focusWindow() !== captureWindow) {
    stopCapture()
    startCapture()
  }
  if (!proc && !startCapture() && Date.now() - frameMtime() > LIVE_MS) refreshShot()
}

/**
 * 定时器回调：先按需喂帧，再回收没人要的流。
 *
 * 由 `apply()` 里的 `ctx.effect` 按 2 秒一跑，随插件一起卸载。导出是为了让冒烟
 * 测试能直接驱动这条路径（起流的真实调用点在生产里就是这个定时器）。
 */
export function housekeeping(): void {
  // 先把「这一轮还在不在跑」写给浮窗（它据此决定收起时间与完成勾）。
  publishTurn()
  // 再喂帧：卡片退休之后，这个定时器是唯一会注意到「浮窗在看」的东西。
  const actionMtime = activityMtime()
  if (panelWatching() && Date.now() - actionMtime <= DORMANT_MS + 10000) {
    lastDemand = Date.now()
    pumpFrames(actionMtime)
  }
  if (Date.now() - lastDemand <= IDLE_OFF_MS) return
  // proc 可能已经死了但客户端连接还挂着：一并收掉，让下次请求时干净重连
  for (const res of clients) {
    try {
      res.end()
    } catch {
      /* 已经断了 */
    }
  }
  clients.clear()
  stopCapture()
}

/** 抓屏进程的命令行：给了窗口 id 就按那一扇窗抓（对齐 Qoder 的窗口级画中画）。 */
export function captureArgs(windowId: number, parentPid: number): string[] {
  const args = ['--width', String(FRAME_W), '--fps', String(FRAME_FPS), '--parent', String(parentPid)]
  if (windowId) args.push('--window', String(windowId))
  return args
}

/** 观察窗当前应该盯哪扇窗（focus.json 由 MCP 服务器在绑定/抬窗/按窗口截图时写）。 */
function focusWindow(): number {
  try {
    const f = JSON.parse(readFileSync(`${BASE}/focus.json`, 'utf8')) as { window?: unknown; ts?: unknown }
    const id = Number(f.window)
    // 十分钟以前报的就别再盯了：窗口大概率已经关了
    return id > 0 && Date.now() - Number(f.ts || 0) < 600000 ? id : 0
  } catch {
    return 0
  }
}

let captureWindow = 0

/**
 * 起 SCK 流。返回 false = 没有 helper 二进制（没装 Swift 工具链 / 没跑构建脚本），
 * 调用方自会退回 `screencapture` 那条 ~2.5fps 的路。
 */
function startCapture(): boolean {
  if (proc || Date.now() < captureRetryAt || process.env.DSH_CUA_STREAM === '0' || !existsSync(CAPTURE_BIN)) return false
  const startedAt = Date.now()
  captureWindow = focusWindow()
  const args = captureArgs(captureWindow, process.pid)
  let child: ReturnType<typeof spawn>
  try {
    child = spawn(
      CAPTURE_BIN,
      args,
      // helper 的 stderr 要接出来：SCK 权限被撤时它只会安静地给黑帧，没有日志就查不出来
      { stdio: ['ignore', 'pipe', 'pipe'] }
    )
  } catch {
    proc = null
    return false
  }
  proc = child
  child.stderr?.setEncoding('utf8')
  let errCarry = ''
  child.stderr?.on('data', (chunk: string) => {
    // 只转启动行与故障行；helper 每 5 秒打一条吞吐统计，别把它灌进宿主日志
    errCarry += chunk
    const lines = errCarry.split('\n')
    errCarry = lines.pop() ?? ''
    for (const line of lines) {
      if (/启动|失败|中断|error|Error|拒绝/.test(line)) process.stderr.write(`dsh-computer-use capture: ${line}\n`)
    }
  })
  child.stdout?.on('data', (chunk: Buffer) => {
    pending = pending.length ? Buffer.concat([pending, chunk]) : chunk
    for (;;) {
      if (pending.length < 4) return
      const len = pending.readUInt32BE(0)
      // 长度离谱 = 帧边界错位（helper 崩了/被截断）：丢掉缓冲重新同步
      if (len < 64 || len > 8_000_000) {
        pending = Buffer.alloc(0)
        return
      }
      if (pending.length < 4 + len) return
      emitFrame(Buffer.from(pending.subarray(4, 4 + len)))
      pending = pending.subarray(4 + len)
    }
  })
  child.on('error', () => {
    proc = null
  })
  child.on('exit', (code) => {
    proc = null
    pending = Buffer.alloc(0)
    // 「收起观察窗」就是 kill 自己的 helper，退出码是 null（被信号打死）。这**不是**故障：
    // 老代码把它记成启动即失败，两次就把整场会话永久降级到 ~1fps —— 表现正是观察窗卡住不动。
    if (stoppedByUs.has(child) || code === null) return
    if (code !== 0 && Date.now() - startedAt < 3000) {
      // 一帧没出就退出 = 环境问题（多半是屏幕录制权限）：冷却后再给一次机会
      if (++captureFailures >= 2) {
        captureFailures = 0
        captureRetryAt = Date.now() + 60000
        process.stderr.write('dsh-computer-use: SCK 抓屏连续启动失败，60 秒内先用 screencapture 回落\n')
      }
    } else {
      captureFailures = 0
    }
  })
  return true
}

function stopCapture(): void {
  const child = proc
  if (!child) return
  stoppedByUs.add(child)
  try {
    child.kill()
  } catch {
    /* 已经没了 */
  }
  proc = null
  pending = Buffer.alloc(0)
}

/**
 * 收工：把这一半区持有的运行期资源全部放掉 —— 所有 MJPEG 连接与抓屏子进程。
 *
 * 为什么必须做：抓屏 helper 活着不只是漏一个进程，它会让系统托盘上的「正在录屏」指示灯
 * 一直亮着，并且持续吃 CPU。插件卸载（或热替换）时必须归零，下一次请求再干净重来。
 */
function shutdown(): void {
  for (const res of clients) {
    try {
      res.end()
    } catch {
      /* 已经断了 */
    }
  }
  clients.clear()
  latestFrame = null
  lastDemand = 0
  captureFailures = 0
  captureRetryAt = 0
  stopCapture()
}

/**
 * 挂载这一半区：帧管线（定时器 + 子进程）和预览路由。
 * @param ctx - Host context.
 */
/**
 * 路由主体：同步处理一次 `/_computer-use/*` 请求。
 *
 * 从 handler 里拆出来，是为了让它整个被 try/catch 包住（见 `reportRouteError`）。
 * 注意它只接 `scope`：handler 里**绝不能**碰外层 `ctx` 的服务属性（未注入访问会抛），
 * 这条规则由 scripts/host-smoke.mjs 的假 ctx 钉住。
 */
function routeRequest(scope: HostContext, req: IncomingMessage, res: ServerResponse): void {

  // **必须走 `scope`，不能走外层 `ctx`**（这条是真事故，2026-10-12 用户报
  // 「拿不到电脑操控的状态（HTTP 400）」）：cordis 里访问一个**没注入**的服务属性会抛
  // `cannot get property "connection" without inject`，而本插件的静态 inject 是空数组
  // （为了在 tui/headless 里也能挂上身份管线，见文件头那段 inject 说明），于是 handler
  // 一进来就抛 → webserver 把它兜成一个**没有响应体的 400**（页面只能看到一个裸状态码）。
  // 只有 `ctx.inject([...], scope => …)` 拿到的 scope 才注入过这两个服务。
  // 冒烟检查的假 ctx 以前把 connection/webServer 直接挂在外面，所以这条看不见 ——
  // 现在假 ctx 照抄 cordis 的语义（未注入就抛），这类错误不会再漏出去。
  const admission = scope.connection.admit(req)
  const rejection = admission.rejection
  if (rejection !== undefined) {
    res.writeHead(rejection)
    res.end(rejection === 401 ? 'unauthorized' : 'forbidden')
    return
  }
  const endpoint = new URL(req.url ?? '/', 'http://dsh.internal').pathname.slice(
    CHANNEL.length + 1
  )
  // 急停两个端点先于「只允许 GET」的方法检查：它们是控制面，不是取数据。
  // stop 只做「停」这一个方向（幂等、失败方向安全）；要恢复走 resume
  // 或 stop?on=0 —— 都不会被一次无意的预取触发成「恢复」。
  if (endpoint === 'stop' || endpoint === 'resume') {
    if (req.method !== 'GET' && req.method !== 'POST') {
      res.writeHead(405, { 'content-type': 'text/plain' })
      res.end('method not allowed')
      return
    }
    const query = new URL(req.url ?? '/', 'http://dsh.internal').searchParams
    const on = endpoint === 'resume' ? false : query.get('on') !== '0'
    const reason = query.get('reason') || '用户从 dsh 页面按了停止'
    setKill(on, reason)
    const body = JSON.stringify({ killed: on, reason: on ? reason : '' })
    res.writeHead(200, {
      'content-type': 'application/json',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(body)
    })
    res.end(body)
    return
  }
  // 设置页（Settings → 电脑操控）：偏好读写、打开权限面板、导航图标补丁的自检上报。
  // 它们要异步读请求体，所以单独走一条链；异常在那条链内部收口成 500。
  if (endpoint === 'settings.json' || endpoint === 'permissions' || endpoint === 'nav-icon') {
    void handleSettingsRequest(endpoint, req, res)
    return
  }
  if (req.method !== 'GET') {
    res.writeHead(405, { 'content-type': 'text/plain' })
    res.end('method not allowed')
    return
  }
  if (endpoint === 'stream') {
    // MJPEG：Chromium 的 <img> 原生滚帧，省掉轮询、省掉 cache-buster。
    // 响应故意不 end —— 连接断了（卡片卸载/隐藏）由 close 事件收工。
    res.writeHead(200, {
      'content-type': `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
      'cache-control': 'no-store, max-age=0',
      connection: 'keep-alive',
      'x-accel-buffering': 'no'
    })
    clients.add(res)
    lastDemand = Date.now()
    if (!startCapture() && latestFrame === null) refreshShot()
    if (latestFrame) {
      try {
        res.write(
          `--${BOUNDARY}\r\ncontent-type: image/jpeg\r\ncontent-length: ${latestFrame.length}\r\n\r\n`
        )
        res.write(latestFrame)
        res.write('\r\n')
      } catch {
        clients.delete(res)
      }
    }
    req.on('close', () => clients.delete(res))
    return
  }
  if (endpoint === 'preview.jpg') {
    if (!existsSync(SHOT)) {
      res.writeHead(404, { 'content-type': 'text/plain' })
      res.end('no screenshot yet')
      return
    }
    const body = readFileSync(SHOT)
    res.writeHead(200, {
      'content-type': 'image/jpeg',
      'cache-control': 'no-store',
      'content-length': body.length
    })
    res.end(body)
    return
  }
  if (endpoint === 'status.json') {
    let action = ''
    let imageMtime = 0
    let actionMtime = 0
    let card = 'on'
    try {
      action = readFileSync(`${BASE}/status.txt`, 'utf8').trim()
      actionMtime = Math.floor(statSync(`${BASE}/status.txt`).mtimeMs)
    } catch {}
    try {
      imageMtime = Math.floor(statSync(`${BASE}/latest.jpg`).mtimeMs)
    } catch {}
    // `card.txt` is a retired channel: the browser card that used to write it
    // (its "collapse me to a pill" state) is gone, so this read can only ever
    // fail and leave the fail-open default. Kept because the field is part of
    // status.json's contract — `retired/preview-card.js` and the host smoke
    // test both still read it — but nothing should treat it as live signal.
    try {
      card = readFileSync(`${BASE}/card.txt`, 'utf8').trim() || 'on'
    } catch {}
    // 「有没有回合正在跑」由 Host 侧的 agents 服务给出：观察窗的完成态/挂起
    // 因此跟着真实事件走，而不是靠「N 秒没截图」猜。服务不在就是 unknown（字段
    // 省略），浏览器半区自会退回时间推断。
    // `Agent.status` is a real getter on the live agent ('idle' | 'running',
    // see dsh-agent-loop) even though the service's published `Agent`
    // interface only declares `id`. Every step of this probe therefore
    // degrades to "not running": an agent service that is missing, renamed or
    // reshaped leaves the field out, and the browser half falls back to its
    // own time-based inference — the safe direction, since the opposite would
    // show a running badge (and keep a capture stream alive) with no evidence.
    let agentRunning: boolean | undefined
    try {
      // `scope.get` 是 cordis 的**免注入**读法（`ctx.get` 也是），这里跟着上面一起
      // 用 scope，免得以后有人把 handler 搬回外层 ctx 时又踩一次同样的坑。
      const listed = scope.get('agents')?.list?.()
      // 只有真的拿到列表才允许回答。`(list ?? []).some(...)` 会在服务缺失/改名时
      // 得到 `false` —— 那是「确定没有回合在跑」，与「不知道」是两回事，消费者
      // （退休卡片/orchestrator）会把这个 false 当成事实。
      agentRunning = Array.isArray(listed) ? listed.some((a) => a?.status === 'running') : undefined
    } catch {}
    const body = JSON.stringify({
      action,
      imageMtime,
      actionMtime,
      card,
      killed: killReason() !== '',
      ...(agentRunning === undefined ? {} : { agentRunning }),
      now: Date.now()
    })
    // 只有卡片真的上了屏（轮询带 view=1）才补帧：隐藏/收起时还每秒抓屏，
    // 就是白烧 CPU 与磁盘，还会让系统的录屏指示灯一直亮着。
    // SCK 流在跑时帧本来就新，不必再走 290ms 的 screencapture 老路。
    const watching = new URL(req.url ?? '/', 'http://dsh.internal').searchParams.get('view') === '1'
    // 「在用电脑」由动作时间戳说话，不能由「有没有卡片在轮询」说话：卡片可能
    // 只是因为 dsh 里有普通任务在跑就挂着，那时开录屏等于白开还亮灯。
    const liveUse = Date.now() - actionMtime <= DORMANT_MS + 10000
    if (watching && liveUse) lastDemand = Date.now()
    // 「有人在看」本身就是重启信号：抓屏进程死了（崩溃 / 被 OOM / 手工 kill）时，
    // 那条 MJPEG 连接还挂着、<img> 卡在 loading 不会自己重连，画面会永久冻死。
    // 看的人 = 预览浮窗（读 preview.lock 的 pid）或（若还留着卡片实现）带 view=1 的轮询。
    // helper 起不来才退回 screencapture 那条老路。
    const demand = liveUse && (panelWatching() || (watching && !card.startsWith('off')))
    if (demand) pumpFrames(actionMtime)
    res.writeHead(200, {
      'content-type': 'application/json',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(body)
    })
    res.end(body)
    return
  }
  res.writeHead(404, { 'content-type': 'text/plain' })
  res.end('unknown endpoint')
}

/** 路由异常落点：一行可排查的记录（`<BASE>/host-error.log`）+ 一个能读懂的状态码。 */
const ROUTE_LOG = `${BASE}/host-error.log`

function reportRouteError(error: unknown, res: ServerResponse): void {
  const detail = error instanceof Error ? (error.stack ?? error.message) : String(error)
  try {
    mkdirSync(BASE, { recursive: true })
    let previous = ''
    try {
      previous = readFileSync(ROUTE_LOG, 'utf8')
    } catch {
      /* 第一次失败，没有历史 */
    }
    // 只留最后 64KB：这是个排查用的滚雪球文件，不该无限长大
    writeFileSync(ROUTE_LOG, `${previous}${new Date().toISOString()} ${detail}\n`.slice(-65536))
  } catch {
    /* 连日志都写不下去时也不能再抛 —— 那又会变成一个裸 400 */
  }
  process.stderr.write(`dsh-computer-use: 路由处理失败 (${detail})\n`)
  if (!res.headersSent) {
    res.writeHead(500, { 'content-type': 'text/plain' })
    res.end(`computer-use route failed: ${detail}`)
  }
}

export function apply(ctx: HostContext): void {
  hostCtx = ctx // housekeeping（定时器）要靠它问 agents 服务

  // 设置页的偏好：启动时先把落盘的那份读出来，再把「不再自动出现」重新写回运行时通道 ——
  // `/tmp/dsh-computer-use` 会被系统清掉，否则用户关掉的开关会在重启后自己复活。
  // 只强制 off 这一个方向：反向（重新打开）必须由用户或 Agent 明确表达。
  try {
    const current = loadPrefs()
    if (!current.autoShow && hiddenValue() !== 'off') writeHidden('off')
    writeConf()
  } catch (error) {
    process.stderr.write(`dsh-computer-use: 初始化观察窗偏好失败 (${String(error)})\n`)
  }

  // 帧管线：定时器与抓屏子进程都挂在这一次 effect 上，插件卸载即全部归还。
  try {
    ctx.effect(() => {
      const timer = setInterval(housekeeping, 2000)
      // 定时器不参与事件循环的存活判定：宿主退出时不该被它拖住
      timer.unref()
      return () => {
        clearInterval(timer)
        shutdown()
      }
    }, 'computer-use: frame pipeline')
  } catch (error) {
    process.stderr.write(`dsh-computer-use: could not start the frame pipeline (${String(error)})\n`)
  }

  // 调用者身份管线：**第一件事**。它只需要 ctx.on，任何 profile 都能挂上；
  // 少了它，同一个 DSH 实例里的多个会话就会共用一份绑定与租约（多会话隔离缺陷）。
  try {
    ctx.effect(() => mountCallerIdentity(ctx), 'computer-use: caller identity')
  } catch (error) {
    process.stderr.write(`dsh-computer-use: could not mount the caller-identity pipeline (${String(error)})\n`)
  }

  try {
    const mountRoutes = (scope: HostContext) =>
      scope.effect(
        () =>
          scope.webServer.register({
          kind: 'prefix',
          path: CHANNEL,
          handler: (req: IncomingMessage, res: ServerResponse) => {
            // 整个路由主体包在 try/catch 里：webserver 对 handler 抛出的异常只有一个动作 ——
            // 回一个**没有响应体的 400**。没有这层，用户看到的就只有「拿不到电脑操控的状态
            // （HTTP 400）」，连是哪儿抛的、抛了什么都不知道（2026-10-12 那条事故）。
            try {
              routeRequest(scope, req, res)
            } catch (error) {
              reportRouteError(error, res)
            }
          }
          }),
        'computer-use: preview routes'
      )
    if (typeof ctx.inject === 'function') {
      // 服务齐了才挂路由；tui / headless 里 connection / webServer 不存在，跳过即可。
      ctx.inject(['connection', 'webServer'], (scope: HostContext) => {
        mountRoutes(scope)
      })
    } else {
      mountRoutes(ctx) // 老的/测试用的 ctx 没有 inject：按老样子直接挂
    }
  } catch (error) {
    process.stderr.write(`dsh-computer-use: could not mount preview routes (${String(error)})\n`)
  }
}
