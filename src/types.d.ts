/**
 * `dsh-computer-use` 的宿主边界类型。
 *
 * 刻意**不 import 任何 `@deepseek-ai/*` 包**：插件只依赖 Cordis 的结构性契约
 * （`name` / `inject` / `apply` + `ctx.get()` / `ctx.effect()`），把这些契约写成本地
 * 接口，插件就能脱离宿主独立编译与自检 —— 也不会因为本地 node_modules 里多出一份
 * cordis 副本而在运行时发生「原型链断裂 / Symbol 不相等」那类事故。
 *
 * @module dsh-computer-use/types
 */
import type { IncomingMessage, ServerResponse } from 'node:http'

/**
 * 卸载时用来撤销一次注册的句柄。
 *
 * 形状是**函数**，不是 `{ dispose }` 对象：Cordis 的 effect 把回调的返回值当
 * disposer 收集，teardown 时直接调用它（`runDisposable`）。宿主 webserver 的
 * `register()` 返回的也正是 `() => table.delete(path)` 这样一个函数。
 */
export type Disposer = () => void

/** 路由处理函数：宿主交进来的是原生 Node 请求/响应对象。 */
export type RouteHandler = (req: IncomingMessage, res: ServerResponse) => void

/** `webServer.register()` 接收的路由描述（本插件只用前缀路由）。 */
export interface WebServerRoute {
  kind: 'prefix'
  path: string
  handler: RouteHandler
}

/** `webServer` 服务：本插件只用到 `register`（重复路径会抛，调用方自己 try）。 */
export interface WebServerService {
  register(route: WebServerRoute): Disposer
}

/**
 * `connection.admit()` 的判定结果。
 *
 * 放行时返回的对象**不带** `rejection` 这个键（宿主实现如此）。这里写成显式联合而不是
 * 单个可选属性，是为了让调用方必须先把 `undefined` 排除掉才拿得到状态码 —— 于是
 * `if (admission.rejection !== undefined)` 之后它就是 `number`，无需断言。
 */
export type Admission = { rejection: number } | { rejection?: undefined }

/** `connection` 服务：本插件只用到「这道请求放不放行」。 */
export interface ConnectionService {
  admit(req: IncomingMessage): Admission
}

/** `agents.list()` 里本插件关心的形状。`id` 就是会话 id（= `exec.agent.id`）。 */
export interface AgentLike {
  id?: string
  status?: string
}

/** `agents` 服务：只读地看「有没有回合正在跑」。 */
export interface AgentsService {
  list?(): AgentLike[]
}

/** `ctx.logger`：Cordis 4 里它是可调用的，`warn` 不保证存在。 */
export interface Logger {
  warn?(message: string): void
}

/**
 * 本插件用到的 Host 上下文。
 *
 * `ctx.get()` 用重载而不是 `any`：已知的服务给出精确形状（读起来不用断言），
 * 其余保持 `unknown`，逼调用方自己收窄 —— 这个插件对「服务不在」是必须降级而不是抛错的。
 */
export interface HostContext {
  logger?: Logger
  /** 注册一个受管的副作用：回调立刻执行，它返回的 disposer 归 Cordis 托管。 */
  effect(callback: () => unknown, label?: string): unknown
  connection: ConnectionService
  webServer: WebServerService
  get(key: 'agents'): AgentsService | undefined
  get(key: string): unknown
  /**
   * 监听一个 Cordis 事件。这里只用到工具调度的三个事件：
   * `tools/pre-execute` / `tools/post-execute` 是 **waterfall**（监听器必须 `return next()`），
   * `tools/result` 是 emit。`exec.agent.id` 就是**会话 id** —— 服务器靠它区分调用者。
   */
  on?(event: string, listener: (...args: any[]) => any, options?: { prepend?: boolean }): unknown
  /** 动态注入：依赖的服务齐了才执行回调（预览路由用它，tui/headless 里就没有这段）。 */
  inject?(deps: string[], callback: (scope: HostContext) => unknown): unknown
}

/** 一次工具执行（只声明本插件用到的字段；见 dsh-tools 的 ToolExecution）。 */
export interface ToolExecution {
  readonly name?: string
  readonly callId?: string
  readonly arguments?: Record<string, unknown>
  readonly agent?: { readonly id?: string }
  readonly parent?: { readonly callId?: string }
  /** 我们自己在 pre-execute 里记下的闸门序号（用于幂等释放）。 */
  __cuaGateSeq?: number
}
