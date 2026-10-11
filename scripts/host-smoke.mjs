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
// 偏好落盘目录同样隔离：**绝不能**拿用户真实的 ~/.dsh/computer-use/settings.json 做断言，
// 更不能把测试写的值留在他机器上（设置页写一次就是改掉了他自己的观察窗开关）。
process.env.DSH_CUA_SETTINGS_DIR = `${process.env.DSH_CUA_DIR}/settings`
// 指引里那个「打开辅助功能设置」按钮在测试里不该真的弹出一个系统设置窗口。
process.env.DSH_CUA_NO_OPEN = '1'
const { mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } = await import('node:fs')
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

/**
 * 假 ctx 的**服务访问语义照抄 cordis**（2026-10-12 那条 400 事故的教训）。
 *
 * 真实 cordis 里，访问一个**没注入**的服务属性会抛 `cannot get property "x" without inject`；
 * 只有 `ctx.inject([...], scope => …)` 交出来的那个 scope 才拿得到注入过的服务。这条规则以前
 * 没被模拟：假 ctx 把 connection/webServer 直接挂在外层，于是 handler 里写 `ctx.connection`
 * 在冒烟里照样跑绿，而生产里一访问就抛 —— webserver 把它兜成一个**没有响应体的 400**，
 * 用户看到的只是「拿不到电脑操控的状态（HTTP 400）」，连是哪儿抛的都看不出来。
 *
 * `ctx.get(name)` 是免注入读法（真实 cordis 亦然），所以它留在外层。
 */
function makeCtx({ connection, webServer, get, effect = (fn) => fn() }) {
	const scope = { effect, get, connection, webServer }
	const ctx = { effect, get, inject: (deps, callback) => {
		for (const dep of deps) if (scope[dep] === undefined) throw new Error(`inject: 假 ctx 里没有 ${dep}`)
		callback(scope)
	} }
	for (const name of ['connection', 'webServer']) {
		Object.defineProperty(ctx, name, {
			get() {
				throw new Error(`cannot get property "${name}" without inject`)
			}
		})
	}
	return ctx
}

let handler = null
const ctx = makeCtx({
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
})

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

/**
 * POST 一个 JSON 请求体。监听器是同步挂上的（handleSettingsRequest 在第一个 await 之前
 * 就调了 readJsonBody），所以 handler 返回之后立刻喂 data/end 不会丢事件。
 */
const postJson = (url, body) =>
	new Promise((resolve, reject) => {
		const out = { status: 0, headers: null, body: Buffer.alloc(0), ended: false }
		const res = {
			writeHead: (status, headers) => { out.status = status; out.headers = headers ?? {} },
			write: (chunk) => { out.body = Buffer.concat([out.body, Buffer.from(chunk)]) ; return true },
			end: (chunk) => { if (chunk) out.body = Buffer.concat([out.body, Buffer.from(chunk)]) ; out.ended = true; resolve(out) },
			on: () => {}
		}
		const listeners = new Map()
		const req = {
			url,
			method: 'POST',
			on: (event, cb) => {
				const arr = listeners.get(event) ?? []
				arr.push(cb)
				listeners.set(event, arr)
			}
		}
		try { handler(req, res) } catch (error) { reject(error) }
		const raw = body === undefined ? '' : typeof body === 'string' ? body : JSON.stringify(body)
		for (const cb of listeners.get('data') ?? []) cb(Buffer.from(raw))
		for (const cb of listeners.get('end') ?? []) cb()
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

// --- 设置页（Settings → 电脑操控）：偏好落盘 + 两个运行时通道 --------------------
// 这一页是用户侧**唯一**能关掉「观察窗自动出现」并再打开的地方，所以三件事都要断言：
//   1. 偏好真的落到磁盘（/tmp 会被系统清掉，重启后必须还在）；
//   2. 镜像真的写到浮窗与 MCP 服务器读的那两个文件里（否则改了没反应）；
//   3. 坏输入不改变已有偏好、坏 JSON 回 400（一次手滑不该把用户的设置重置掉）。
const SETTINGS_FILE = `${process.env.DSH_CUA_SETTINGS_DIR}/settings.json`
const readJsonFile = (file) => JSON.parse(readFileSync(file, 'utf8'))

const initial = await call('/_computer-use/settings.json')
if (initial.status !== 200) throw new Error(`settings.json 期望 200，实际 ${initial.status}`)
const initialBody = JSON.parse(initial.body.toString())
if (initialBody.settings?.autoShow !== true || initialBody.settings?.autoHideSeconds !== 12)
	throw new Error(`默认偏好不对：${JSON.stringify(initialBody.settings)}`)
if (!Array.isArray(initialBody.choices) || !initialBody.choices.includes(12))
	throw new Error(`收起延时选项缺失：${JSON.stringify(initialBody.choices)}`)
if (initialBody.runtime?.hidden !== '')
	throw new Error(`还没动过开关，panel.hidden 却是 ${initialBody.runtime?.hidden}`)
if (existsSync(SETTINGS_FILE)) throw new Error('只是读一次就把偏好文件写出来了')
console.log(`✓ 设置页默认状态：autoShow=${initialBody.settings.autoShow} autoHide=${initialBody.settings.autoHideSeconds}s（偏好只有这两项，见 README「设置页」）`)

// 关掉总开关：三处必须同时变 —— 落盘、panel.hidden=off（server.py 认它）、panel.conf 的收起请求
const off = await postJson('/_computer-use/settings.json', { autoShow: false })
const offBody = JSON.parse(off.body.toString())
if (offBody.settings.autoShow !== false) throw new Error('POST autoShow=false 没有生效')
if (readJsonFile(SETTINGS_FILE).autoShow !== false) throw new Error('偏好没有落盘')
if (!readFileSync(`${DIR}/panel.hidden`, 'utf8').startsWith('off:'))
	throw new Error(`panel.hidden 没有写成 off：${readFileSync(`${DIR}/panel.hidden`, 'utf8')}`)
const confOff = readJsonFile(`${DIR}/panel.conf`)
if (confOff.hideRequest !== 'hide' || !(confOff.hideRequestAt > 0))
	throw new Error(`panel.conf 没有带上收起请求：${JSON.stringify(confOff)}`)
console.log(`✓ 关掉观察窗：偏好落盘 + panel.hidden=off + panel.conf 收起请求（nonce=${confOff.hideRequestAt}）`)

// 改一个别的字段：不能把 autoShow 带回默认值（补丁是合并，不是替换）
const delay = await postJson('/_computer-use/settings.json', { autoHideSeconds: 30 })
const delayBody = JSON.parse(delay.body.toString())
if (delayBody.settings.autoHideSeconds !== 30 || delayBody.settings.autoShow !== false)
	throw new Error(`部分更新把别的字段冲掉了：${JSON.stringify(delayBody.settings)}`)
if (readJsonFile(`${DIR}/panel.conf`).autoHideSeconds !== 30)
	throw new Error('收起延时没有镜像进 panel.conf（浮窗读的是它）')
console.log('✓ 部分更新只改目标字段，并镜像进 panel.conf（浮窗据此改收起时机）')

// 坏值/坏 JSON：保留原值 + 400，绝不重置
const badValue = await postJson('/_computer-use/settings.json', { autoHideSeconds: 'abc' })
if (JSON.parse(badValue.body.toString()).settings.autoHideSeconds !== 30)
	throw new Error('非法值把收起延时重置了（应当保留原值）')
const badBody = await postJson('/_computer-use/settings.json', '{ 这不是 json')
if (badBody.status !== 400) throw new Error(`坏 JSON 期望 400，实际 ${badBody.status}`)
console.log('✓ 非法值保留原值、坏 JSON 回 400（手滑一次不该把用户的设置重置掉）')

// 导航图标补丁的自检上报：页面补不上时把现场写进 <BASE>/nav-icon.log（问「为什么图标还是齿轮」的答案）
const navReport = await postJson('/_computer-use/nav-icon', {
	outcome: 'probe',
	detail: 'label=电脑操控 dialogs=1 buttons=9 sample=账号与余额 | 通用设置'
})
if (navReport.status !== 200) throw new Error(`nav-icon 上报期望 200，实际 ${navReport.status}`)
const navLog = readFileSync(`${DIR}/nav-icon.log`, 'utf8')
if (!navLog.includes('probe') || !navLog.includes('dialogs=1'))
	throw new Error(`nav-icon.log 没有记下现场：${JSON.stringify(navLog)}`)
const navBad = await postJson('/_computer-use/nav-icon', '{ 坏 json')
if (navBad.status !== 400) throw new Error(`nav-icon 坏 JSON 期望 400，实际 ${navBad.status}`)
if (readFileSync(`${DIR}/nav-icon.log`, 'utf8') !== navLog)
	throw new Error('坏 JSON 也写了日志（不该留半条记录）')
// 日志是诊断文件不是账本：超长内容只留尾部
for (let i = 0; i < 40; i += 1) {
	await postJson('/_computer-use/nav-icon', { outcome: 'probe', detail: 'x'.repeat(500) })
}
if (readFileSync(`${DIR}/nav-icon.log`, 'utf8').length > 8192)
	throw new Error('nav-icon.log 没有截断（会无限长大）')
console.log('✓ 导航图标自检：现场落盘、坏 JSON 回 400 不留半条、日志超过 8KB 只留尾部')

// 关掉总开关要**真的把浮窗进程收掉**：只写文件的话，已经开着的那块窗还留在屏幕上。
// 走用户的实际路径：先打开（这时窗可以在跑）→ 再关掉。
await postJson('/_computer-use/settings.json', { autoShow: true })
const childProcess = await import('node:child_process')
const victim = childProcess.spawn('sleep', ['30'], { stdio: 'ignore' })
writeFileSync(`${DIR}/preview.lock`, String(victim.pid))
const offAgain = await postJson('/_computer-use/settings.json', { autoShow: false })
if (offAgain.status !== 200) throw new Error('再次关闭总开关失败')
await new Promise((resolve) => setTimeout(resolve, 600))
if (victim.exitCode === null && victim.signalCode === null)
	throw new Error('关掉观察窗开关后浮窗进程还活着（用户会看到窗还在屏幕上）')
console.log(`✓ 关掉总开关会结束浮窗进程（SIGTERM，浮窗自己清 lock）：signal=${victim.signalCode}`)

// Agent 的 preview(False)/preview(True) 也写同一个文件：读设置时必须把它折叠回落盘的偏好，
// 否则设置页会显示「开着」、重启还会把 Agent 关掉的窗复活。
writeFileSync(`${DIR}/panel.hidden`, `off:${Date.now()}`)
if (JSON.parse((await call('/_computer-use/settings.json')).body.toString()).settings.autoShow !== false)
	throw new Error('panel.hidden=off 时设置页没显示「已关闭」')
if (readJsonFile(SETTINGS_FILE).autoShow !== false) throw new Error('运行时真相没有折叠回落盘的偏好')
writeFileSync(`${DIR}/panel.hidden`, `on:${Date.now()}`)
if (JSON.parse((await call('/_computer-use/settings.json')).body.toString()).settings.autoShow !== true)
	throw new Error('panel.hidden=on 时设置页没显示「自动显示」')
if (readJsonFile(SETTINGS_FILE).autoShow !== true) throw new Error('重新打开没有折叠回偏好')
console.log('✓ Agent 的 preview(false/true) 与设置页共用同一份真相（panel.hidden → 折叠回落盘偏好）')

// 指引里那个按钮：不真的弹系统设置窗口（DSH_CUA_NO_OPEN=1），只验它通
const perm = await postJson('/_computer-use/permissions', {})
if (perm.status !== 200 || JSON.parse(perm.body.toString()).ok !== true)
	throw new Error(`permissions 期望 200 {ok:true}，实际 ${perm.status}`)
console.log('✓ 打开权限设置：路由通（DSH_CUA_NO_OPEN=1 时不真的弹窗）')

// --- 路由异常必须留下可排查的痕迹（2026-10-12 那条 400 事故的教训）-------------
// webserver 对 handler 抛出的异常只有一个动作：回一个**没有响应体的 400**。那次用户看到的
// 就只有「拿不到电脑操控的状态（HTTP 400）」，连是哪儿抛的都不知道。现在异常会被收口成
// 500 + 一行 host-error.log —— 这条断言钉住「以后再抛也查得出来」。
let handlerBoom = null
mod.apply(
	makeCtx({
		connection: {
			admit: () => {
				throw new Error('cannot get property "connection" without inject')
			}
		},
		webServer: { register: (route) => { handlerBoom = route.handler; return () => { handlerBoom = null } } },
		get: () => undefined
	})
)
if (typeof handlerBoom !== 'function') throw new Error('异常用例没有挂上路由')
const boom = await new Promise((resolve) => {
	const out = { status: 0, body: '' }
	const res = {
		headersSent: false,
		writeHead: (status) => { out.status = status; res.headersSent = true },
		end: (chunk) => resolve({ ...out, body: String(chunk ?? '') }),
		write: () => true,
		on: () => {}
	}
	handlerBoom({ url: '/_computer-use/settings.json', method: 'GET', on: () => {} }, res)
})
if (boom.status !== 500) throw new Error(`路由抛异常时期望 500，实际 ${boom.status}（400 就是被 webserver 兜走了）`)
if (!boom.body.includes('without inject')) throw new Error(`500 的响应体没有带上原因：${boom.body}`)
const routeLog = readFileSync(`${DIR}/host-error.log`, 'utf8')
if (!routeLog.includes('without inject')) throw new Error('异常没有写进 host-error.log（排查时看不到）')
console.log('✓ 路由异常收口：500 + 原因 + host-error.log（不再是一个查不出原因的裸 400）')
rmSync(`${DIR}/host-error.log`, { force: true })

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
const ctx2 = makeCtx({
	// Cordis 的 ctx.effect(fn, label) 会立刻跑 fn 并把返回值当 disposer 收集
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
})
mod.apply(ctx2)
const pipeline = effects.find((entry) => String(entry.label).includes('frame pipeline'))
if (!pipeline) throw new Error('帧管线没有登记 ctx.effect —— 定时器会随热替换泄漏')
if (typeof pipeline.dispose !== 'function') throw new Error('帧管线的 effect 没有返回 disposer')
if (effects.length !== 3) throw new Error(`期望 3 个受管副作用（帧管线 + 调用者身份 + 路由），实际 ${effects.length}`)
if (!effects.some((e) => String(e.label).includes('caller identity')))
	throw new Error('调用者身份管线没有登记 ctx.effect —— 热替换会留下野监听器')

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
// 注意要用 makeCtx 造一个**新的** ctx：`{...ctx2}` 展开拿不到那两个访问器属性（未注入访问会抛，
// 所以它们是故意不可枚举的），展开出来的对象会继续把路由注册回旧的那个 register 上。
let handler3 = null
mod.apply(
	makeCtx({
		effect: (fn) => fn(),
		connection: { admit: () => ({}) },
		webServer: { register: (route) => { handler3 = route.handler; return () => { handler3 = null } } },
		get: () => undefined
	})
)
if (typeof handler3 !== 'function') throw new Error('热替换后路由没有重新挂上')
for (const entry of effects.slice(2)) {
	// 两种 disposer 都要认：帧管线 effect 自己返回的清理函数，以及）webServer.register 的 disposer
	if (typeof entry.dispose === 'function') entry.dispose()
	else entry.dispose?.dispose?.()
}
console.log('✓ 热替换后再挂一次正常（旧 disposer 已释放，新实例照常工作）')

// --- 调用者身份管线（多会话隔离 B 方案：闸门 + caller.json） ---------------------
// spike 实测：tools/pre-execute 拿得到 exec.agent.id（= 会话 id），且钩子是**闸门**
// （pre 里 sleep 2 秒，工具真的等满 2 秒才跑）。这里把那条契约固化成断言。
const listeners = new Map()
const effects3 = []
const ctx3 = {
	effect: (fn, label) => { const d = fn(); effects3.push({ label, dispose: d }); return d },
	connection: { admit: () => ({}) },
	webServer: { register: () => () => {} },
	get: () => undefined,
	on: (event, fn, options) => {
		const arr = listeners.get(event) ?? []
		arr.push({ fn, options })
		listeners.set(event, arr)
		return () => {
			const now = listeners.get(event) ?? []
			listeners.set(event, now.filter((e) => e.fn !== fn))
		}
	}
}
mod.apply(ctx3)
const pre = listeners.get('tools/pre-execute')?.[0]?.fn
const post = listeners.get('tools/post-execute')?.[0]?.fn
if (typeof pre !== 'function' || typeof post !== 'function')
	throw new Error('调用者身份管线没有登记 pre/post-execute 监听器')
if (listeners.get('tools/pre-execute')[0].options?.prepend !== true)
	throw new Error('pre-execute 没有 prepend —— 别人的钩子可能先改掉 exec')

const callerFile = `${DIR}/caller.json`
const readCaller = () => JSON.parse(readFileSync(callerFile, 'utf8'))
const nextOk = async () => ({ kind: 'allow' })

const execA = { name: 'mcp__computer-use__screen_info', callId: 'c1', agent: { id: 'session-AAA' }, arguments: {} }
await pre(execA, nextOk)
const rec1 = readCaller()
if (rec1.agent !== 'session-AAA' || rec1.tool !== 'mcp__computer-use__screen_info' || typeof rec1.at !== 'number')
	throw new Error(`caller.json 内容不对：${JSON.stringify(rec1)}`)
console.log(`✓ pre-execute 写 caller.json：agent=${rec1.agent} tool=${rec1.tool.split('__').pop()} seq=${rec1.seq}`)

// 闸门：第二个调用必须等第一个的 post-execute
const execB = { name: 'mcp__computer-use__list_apps', callId: 'c2', agent: { id: 'session-BBB' }, arguments: {} }
let bRan = false
const pB = pre(execB, async () => { bRan = true; return { kind: 'allow' } })
await sleep(60)
if (bRan) throw new Error('闸门没生效：第二个 computer-use 调用在第一个 post 之前就派发了')
await post(execA, { isError: false }, async () => ({ kind: 'accept' }))
await pB
if (!bRan) throw new Error('post-execute 之后没有交棒，后面的调用被永久卡住')
const rec2 = readCaller()
if (rec2.agent !== 'session-BBB' || rec2.seq <= rec1.seq)
	throw new Error(`交棒后 caller.json 没更新：${JSON.stringify(rec2)}`)
console.log(`✓ 闸门串行化：A 的 post 之前 B 不派发；交棒后 caller.json → ${rec2.agent}`)

// 迟到的 post（序号已过期）不能把闸门错误地交给别人
const execC = { name: 'mcp__computer-use__click', callId: 'c3', agent: { id: 'session-CCC' }, arguments: {} }
let cRan = false
const pC = pre(execC, async () => { cRan = true; return { kind: 'allow' } })
await sleep(40)
await post(execA, { isError: false }, async () => ({ kind: 'accept' })) // 迟到的 A 的 post
await sleep(60)
if (cRan) throw new Error('过期的 post-execute 把闸门错误地放行了（重复释放没被挡住）')
await post(execB, { isError: false }, async () => ({ kind: 'accept' }))
await pC
if (!cRan) throw new Error('真正持有者的 post 没能交棒')
await post(execC, { isError: false }, async () => ({ kind: 'accept' }))
console.log('✓ 重复/迟到的 post-execute 被幂等挡住（不会误放行）')

// 别的工具（内置 read）不写文件、不占用闸门
const callerBefore = readFileSync(callerFile, 'utf8')
let readNext = false
await pre({ name: 'read', callId: 'c4', agent: { id: 'session-AAA' } }, async () => { readNext = true; return { kind: 'allow' } })
if (!readNext) throw new Error('非 computer-use 工具被闸门拦住了')
if (readFileSync(callerFile, 'utf8') !== callerBefore) throw new Error('非 computer-use 工具改写了 caller.json')
console.log('✓ 只有 mcp__computer-use__* 走闸门（内置工具不受影响、不写文件）')

// 卸载：监听器要一起收回
const identity = effects3.find((e) => String(e.label).includes('caller identity'))
if (!identity || typeof identity.dispose !== 'function') throw new Error('调用者身份管线没有 disposer')
identity.dispose()
if ((listeners.get('tools/pre-execute') ?? []).length !== 0 || (listeners.get('tools/post-execute') ?? []).length !== 0)
	throw new Error('卸载后监听器还挂着 —— 热替换会叠加钩子')
console.log('✓ 卸载收回调用者身份管线（监听器清空）')

// --- 观察窗的收起判据：turn.json（这一轮还在不在跑）+ panel.state（在屏上才抓屏）---
// 用户报的两个症状都在这一层：绿勾被鼠标一动就抹掉（帧 mtime 掺进了判据）、
// 以及任务结束后窗还杵着两分钟。浮窗侧的纯函数由 scripts/panel-check.py 打矩阵，
// 这里验 host 侧写出去的那两个文件。
const turnFile = `${DIR}/turn.json`
const withAgents = (status) => ({
	effect: (fn) => fn(),
	connection: { admit: () => ({}) },
	webServer: { register: () => () => {} },
	get: (name) => (name === 'agents' ? { list: () => [{ id: 'session-AAA', status }] } : undefined),
	on: (event, fn, options) => {
		const arr = listeners.get(event) ?? []
		arr.push({ fn, options })
		listeners.set(event, arr)
		return () => {
			const now = listeners.get(event) ?? []
			listeners.set(event, now.filter((e) => e.fn !== fn))
		}
	}
})
mod.apply(withAgents('running'))
const preTurn = (listeners.get('tools/pre-execute') ?? [])[0]?.fn
if (typeof preTurn !== 'function') throw new Error('重新 apply 之后没有登记 pre-execute')
await preTurn({ name: 'mcp__computer-use__screen_info', callId: 'c9', agent: { id: 'session-AAA' } }, nextOk)
mod.housekeeping()
const turn1 = JSON.parse(readFileSync(turnFile, 'utf8'))
if (turn1.agent !== 'session-AAA' || turn1.running !== true)
	throw new Error(`turn.json 没写出「这一轮在跑」：${JSON.stringify(turn1)}`)
console.log(`✓ turn.json：agent=${turn1.agent} running=${turn1.running}（浮窗据此不收起）`)

mod.apply(withAgents('idle'))
mod.housekeeping()
const turn2 = JSON.parse(readFileSync(turnFile, 'utf8'))
if (turn2.running !== false) throw new Error(`回合结束后 turn.json 没更新：${JSON.stringify(turn2)}`)
console.log('✓ turn.json：回合结束 → running=false（浮窗据此 12 秒后收起、并打完成勾）')

// panel.state：收起之后就别再抓屏了（抓屏进程活着 = 系统录屏灯亮着）
const stateFile = `${DIR}/panel.state`
const fakeWatch = spawn('sleep', ['30'], { stdio: 'ignore' })
writeFileSync(`${DIR}/preview.lock`, String(fakeWatch.pid))
rmSync(stateFile, { force: true })
if (!mod.panelWatching()) throw new Error('没有 panel.state（老浮窗）时应当退回旧行为：算在看')
writeFileSync(stateFile, `hidden:${Date.now()}`)
if (mod.panelWatching()) throw new Error('浮窗收起了却还在抓屏 —— 录屏灯会一直亮着')
writeFileSync(stateFile, `up:${Date.now()}`)
if (!mod.panelWatching()) throw new Error('浮窗在屏上却没抓屏 —— 画面会冻住')
fakeWatch.kill()
console.log('✓ panel.state：收起 → 停流；在屏上 → 继续抓屏；老浮窗（没这文件）退回旧行为')

rmSync(DIR, { recursive: true, force: true })

