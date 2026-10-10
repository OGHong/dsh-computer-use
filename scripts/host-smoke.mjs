/**
 * Host 半区的冒烟检查：用假的 ctx 把路由挂起来，逐条打请求，断言响应。
 *
 * 为什么需要：`node --check` 看不到「引用了没声明的变量」（captureDisabled 就漏过一次，
 * 表现是 /stream 直接 Failed to fetch、卡片永久断流）。这里不依赖 dsh 就能抓出来。
 * 用法：node scripts/host-smoke.mjs
 */
process.env.DSH_CUA_STREAM = '0' // 不去 spawn 真的抓屏进程，专测路由与降级分支
// 隔离的状态目录：真实 dsh 会话正在往 /tmp/dsh-computer-use/latest.jpg 以 10 次/秒
// 写帧，共用一个目录会让「有没有开录屏」这类断言被它搅浑，也会把垃圾写进现场。
process.env.DSH_CUA_DIR = `/tmp/dsh-cua-smoke.${process.pid}`
const { mkdirSync, writeFileSync, existsSync, rmSync } = await import('node:fs')
const DIR = process.env.DSH_CUA_DIR
mkdirSync(DIR, { recursive: true })
/**
 * 自带夹具，**不再**从 /tmp/dsh-computer-use 拷真实现场：那是真实会话的目录，一次截图都
 * 还没发生的时候它连 latest.jpg 都没有（拷不到就直接 ENOENT 崩掉），而且测试也不该依赖
 * 「这台机器上恰好存在某个文件」。这是一张合法的 1×1 JPEG，够走完
 * 「读文件 → 按 image/jpeg 回包」这条路。
 */
const TINY_JPEG = Buffer.from(
	'/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==',
	'base64'
)
writeFileSync(`${DIR}/latest.jpg`, TINY_JPEG)
writeFileSync(`${DIR}/status.txt`, 'screen_info\n')

const path = await import('node:path').then((m) => m.default.join(process.cwd(), 'lib/index.js'))
const mod = await import('file://' + path)

let handler = null
const ctx = {
	// Cordis 的 ctx.effect(fn, name) 会立刻跑 fn 并返回 disposer；缺了它 apply 会静默失败
	effect: (fn) => fn(),
	connection: { admit: () => ({}) }, // 全放行
	webServer: {
		register: (route) => {
			handler = route.handler
			// 真实 webserver 返回的是 disposer **函数**（`() => table.delete(path)`），
			// 不是 { dispose } 对象 —— 假件跟着真形状走，否则测的是一个不存在的世界。
			return () => {
				handler = null
			}
		}
	},
	get: (name) => {
		if (name === 'agents') return { list: () => [{ status: 'idle' }] }
		throw new Error(`unexpected service: ${name}`)
	}
}

mod.apply(ctx)
if (!handler) throw new Error('apply 没有挂上路由')

const call = (url) =>
	new Promise((resolve, reject) => {
		const out = { status: 0, headers: null, body: Buffer.alloc(0), ended: false }
		const res = {
			writeHead: (status, headers) => { out.status = status; out.headers = headers ?? {} },
			write: (chunk) => { out.body = Buffer.concat([out.body, Buffer.from(chunk)]) ; return true },
			end: (chunk) => { if (chunk) out.body = Buffer.concat([out.body, Buffer.from(chunk)]) ; out.ended = true; resolve(out) },
			on: () => {}
		}
		const req = { url, method: 'GET', on: (_event, cb) => { if (_event === 'close') req._close = cb } }
		try { handler(req, res) } catch (error) { reject(error) }
		setTimeout(() => resolve({ ...out, timedOut: true }), 1500)
	})

const status = await call('/_computer-use/status.json?view=1')
if (status.status !== 200) throw new Error(`status.json 期望 200，实际 ${status.status}`)
const json = JSON.parse(status.body.toString())
for (const key of ['action', 'imageMtime', 'actionMtime', 'card', 'killed', 'agentRunning', 'now']) {
	if (!(key in json)) throw new Error(`status.json 少了字段 ${key}：${status.body}`)
}
if (json.killed !== false) throw new Error('刚起来就说自己处于急停')
console.log(`✓ status.json 200，字段齐全：agentRunning=${json.agentRunning} card=${String(json.card).slice(0, 8)}… killed=${json.killed}`)

// --- 急停路由：与 server/killswitch.py 共用同一个文件，格式必须对得上 -----------
// 这是用户侧的第三个入口（另两个：浮窗上的 ⏹、MCP 的 stop 工具）。写错格式 = 停止键
// 按下去却什么都没发生，属于最不能出的错，所以要断言文件的**字节**而不是只看 HTTP 200。
const killed = await call('/_computer-use/stop?reason=host-smoke')
if (killed.status !== 200) throw new Error(`stop 期望 200，实际 ${killed.status}`)
const killBody = JSON.parse(killed.body.toString())
if (killBody.killed !== true) throw new Error('stop 没有报告 killed=true')
const killRaw = (await import('node:fs')).readFileSync(`${DIR}/kill`, 'utf8').trim()
if (!/^on:\d+:host-smoke$/.test(killRaw)) throw new Error(`急停文件格式与 killswitch.py 不一致：${killRaw}`)
const statusKilled = JSON.parse((await call('/_computer-use/status.json')).body.toString())
if (statusKilled.killed !== true) throw new Error('status.json 没反映急停状态')
// 再按一次必须是幂等的，不能把状态翻回去
await call('/_computer-use/stop')
if (!(await import('node:fs')).readFileSync(`${DIR}/kill`, 'utf8').startsWith('on:')) throw new Error('重复 stop 把急停翻掉了')
const resumed = await call('/_computer-use/resume')
if (JSON.parse(resumed.body.toString()).killed !== false) throw new Error('resume 没有报告 killed=false')
if (existsSync(`${DIR}/kill`)) throw new Error('resume 之后急停文件还在（服务器会继续只读）')
console.log('✓ 急停路由：格式与 killswitch.py 对齐、幂等、resume 真的清掉了标志')

const shot = await call('/_computer-use/preview.jpg')
console.log(`✓ preview.jpg ${shot.status} ${shot.headers['content-type']}，${shot.body.length} 字节`)
if (shot.status !== 200 || shot.headers['content-type'] !== 'image/jpeg') throw new Error('preview.jpg 不对')

const missing = await call('/_computer-use/nope')
if (missing.status !== 404) throw new Error(`未知路径期望 404，实际 ${missing.status}`)
console.log('✓ 未知路径 404')

const wrongMethod = await new Promise((resolve) => {
	const res = { writeHead: (s) => resolve(s), end: () => {}, write: () => true }
	handler({ url: '/_/status.json', method: 'POST', on: () => {} }, res)
})
if (wrongMethod !== 405) throw new Error(`POST 期望 405，实际 ${wrongMethod}`)
console.log('✓ 非 GET 405')

const stream = await call('/_computer-use/stream')
const type = stream.headers?.['content-type'] ?? ''
if (!String(type).startsWith('multipart/x-mixed-replace')) throw new Error(`stream 期望 MJPEG，实际 ${JSON.stringify(stream.headers)}`)
console.log(`✓ /stream 走 MJPEG（${type.split(';')[1] ?? ''}），DSH_CUA_STREAM=0 时由 screencapture 回落喂帧`)

console.log('✓ Host 半区路由全部通过（跑在隔离目录里，不碰真实现场）')

// 命令行构造：有目标窗口就按窗口抓，没有就整屏（上一轮「切流代码静默没插进去」
// 就是因为这段逻辑没有直接断言）
const { captureArgs } = await import(`${process.cwd()}/lib/index.js`)
const withWin = captureArgs(20049, 4321)
const noWin = captureArgs(0, 4321)
if (withWin[withWin.indexOf('--window') + 1] !== '20049') throw new Error('--window 没接上')
if (noWin.includes('--window')) throw new Error('没有目标窗口时不该带 --window')
console.log(`✓ 抓屏命令行：有窗口带 --window（${withWin.length} 项），无窗口整屏（${noWin.length} 项）`)

// --- 隐私/开销闸门：普通任务时不该偷偷开录屏 ---
const { statSync, utimesSync } = await import('node:fs')
const LATEST = `${DIR}/latest.jpg`
const stamp = (f, secAgo) => { const t = new Date(Date.now() - secAgo * 1000); utimesSync(f, t, t) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
await sleep(1600) // 让上面 /stream 那次回落抓屏先落地，免得把这一段的判定搅浑

stamp(`${DIR}/status.txt`, 180) // 3 分钟没动过电脑 = 普通任务
let before = statSync(LATEST).mtimeMs
await call('/_computer-use/status.json?view=1')
await sleep(1600)
if (statSync(LATEST).mtimeMs !== before) throw new Error('只是卡片在轮询就开了录屏（普通任务会亮录屏灯）')
console.log('✓ 超过 2 分钟没动作时，view=1 不再触发抓屏（录屏灯不亮）')

stamp(`${DIR}/status.txt`, 2) // 刚用过电脑
before = statSync(LATEST).mtimeMs
await call('/_computer-use/status.json?view=1')
await sleep(2000)
if (statSync(LATEST).mtimeMs === before) throw new Error('在用电脑时反而没补帧 —— 闸门关得太死')
console.log('✓ 真在用电脑时仍然正常补帧（闸门没把功能关掉）')

// --- 预览浮窗（唯一的预览面）在跑时也要喂帧 ---
// 它不像卡片那样轮询 /status.json，只读 latest.jpg + 自己写在 preview.lock 里的 pid；
// 这条闸门要是只认 view=1，浮窗就会一直显示一张冻住的旧帧。
const { spawn } = await import('node:child_process')
const fakePanel = spawn('sleep', ['30'], { stdio: 'ignore' })   // 顶替浮窗进程，只为拿一个活 pid
writeFileSync(`${DIR}/preview.lock`, String(fakePanel.pid))
stamp(`${DIR}/status.txt`, 2)
before = statSync(LATEST).mtimeMs
await call('/_computer-use/status.json')                        // 注意：不带 view=1
await sleep(2000)
if (statSync(LATEST).mtimeMs === before) throw new Error('浮窗在跑却没补帧 —— 它会一直显示冻住的旧帧')
console.log('✓ 预览浮窗在跑时补帧（不依赖卡片的 view=1）')

// --- 起流的真实触发点是定时器，不是路由 -----------------------------------------
// 浮窗一句 HTTP 都不发（只读文件 + AppKit），会请求 /status.json 的客户端是已退休的
// 卡片。所以「谁来决定起流」必须在 2 秒定时器里也成立：这里**完全不碰路由**，只保留
// 浮窗 pid 与「刚动过」，直接驱动一次定时器回调。
stamp(LATEST, 30) // 让帧源确实过期，补帧才有可观测的 mtime 变化
const { housekeeping } = await import(`${process.cwd()}/lib/index.js`)
before = statSync(LATEST).mtimeMs
housekeeping()
await sleep(2000)
if (statSync(LATEST).mtimeMs === before) {
	throw new Error('定时器没有按浮窗的需求起流 —— 生产配置下就再没人起流了（画面会冻住）')
}
console.log('✓ 定时器自己会发现浮窗在看并起流（不依赖任何 HTTP 请求）')

fakePanel.kill()

// --- 生命周期：卸载要把这条半区持有的资源一起收回 -------------------------------
// dsh-plugin-dev 的铁律：所有副作用都必须挂在 ctx.effect 上，随插件 fiber 释放归还。
// 漏掉这步的代价不是「难看」：抓屏 helper 会活下来，系统托盘上的录屏指示灯一直亮，
// 热替换几次就攒下一串进程。这条断言直接盯着「一个已经打开的 MJPEG 连接有没有被收掉」。
const effects = []
let handler2 = null
const ctx2 = {
	effect: (fn, label) => {
		const dispose = fn()
		effects.push({ label, dispose })
		return dispose
	},
	connection: { admit: () => ({}) },
	webServer: {
		register: (route) => {
			handler2 = route.handler
			return () => {
				handler2 = null
			}
		}
	},
	get: (name) => {
		if (name === 'agents') return { list: () => [] }
		throw new Error(`unexpected service: ${name}`)
	}
}
mod.apply(ctx2)
const pipeline = effects.find((entry) => String(entry.label).includes('frame pipeline'))
if (!pipeline) throw new Error('帧管线没有登记 ctx.effect —— 定时器会随热替换泄漏')
if (typeof pipeline.dispose !== 'function') throw new Error('帧管线的 effect 没有返回 disposer')
if (effects.length !== 2) throw new Error(`期望 2 个受管副作用（帧管线 + 路由），实际 ${effects.length}`)

const held = { status: 0, ended: false }
const heldRes = {
	writeHead: (status) => { held.status = status },
	write: () => true,
	end: () => { held.ended = true },
	on: () => {}
}
handler2({ url: '/_computer-use/stream', method: 'GET', on: () => {} }, heldRes)
if (held.status !== 200) throw new Error(`/stream 期望 200，实际 ${held.status}`)
if (held.ended) throw new Error('MJPEG 连接不该自己结束')

pipeline.dispose()
if (!held.ended) throw new Error('卸载后那条 MJPEG 连接还挂着 —— shutdown 没生效')
pipeline.dispose() // 幂等：disposer 被重复调用也必须安全
console.log('✓ 卸载收回帧管线：定时器清掉、已打开的 MJPEG 连接被结束（disposer 幂等）')

// 热替换：同一进程里再挂一次必须照常工作（旧实例已 dispose，新的不能踩到旧状态）
let handler3 = null
mod.apply({
	...ctx2,
	webServer: { register: (route) => { handler3 = route.handler; return () => { handler3 = null } } }
})
if (typeof handler3 !== 'function') throw new Error('热替换后路由没有重新挂上')
for (const entry of effects.slice(2)) {
	// 两种 disposer 都要认：帧管线 effect 自己返回的清理函数，以及）webServer.register 的 disposer
	if (typeof entry.dispose === 'function') entry.dispose()
	else entry.dispose?.dispose?.()
}
console.log('✓ 热替换后再挂一次正常（旧 disposer 已释放，新实例照常工作）')

rmSync(DIR, { recursive: true, force: true })
