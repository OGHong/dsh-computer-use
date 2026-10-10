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
 *
 * The observation surface itself is the native floating window
 * (`server/preview_window.py`), which reads the same files; the browser card that
 * used to mount into the page is retired and kept in `retired/preview-card.js`.
 * Nothing in the page is required for the feature to work.
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
  try {
    const pid = Number(readFileSync(`${BASE}/preview.lock`, 'utf8').trim())
    if (!pid) return false
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
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
export function apply(ctx: HostContext): void {
  hostCtx = ctx // housekeeping（定时器）要靠它问 agents 服务
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
            const admission = ctx.connection.admit(req)
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
                const listed = ctx.get('agents')?.list?.()
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
