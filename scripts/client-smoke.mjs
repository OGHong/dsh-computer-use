/**
 * 浏览器半区的冒烟检查：真的把「注册 → apply → 挂载 → 渲染组件」跑一遍。
 *
 * 这一半区已**退休**（预览面由 server/preview_window.py 的原生浮窗承担，
 * package.json 里没有 dsh.client 声明，页面不会注入它），实现躺在 retired/preview-card.js。
 * 冒烟仍然保留：把 `dsh.client` 与 `exports["./client"]` 加回去就能复活，行为不能悄悄坏掉。
 *
 * 为什么需要它：`node --check` 只看语法，看不到「用了没声明的名字」（setTick / dragRef /
 * viewing 三连坑都是这一类），也看不到「dsh 换了 seed 词会不会把整页启动带崩」。
 * 改完 retired/preview-card.js 跑一下：node scripts/client-smoke.mjs
 */
const path = await import('node:path').then((m) => m.default.join(process.cwd(), 'retired/preview-card.js'))

/** @type {(name:string)=>any} 由被测模块使用的 require；seed 词表变了这里就会走到降级分支 */
let requireImpl

function installStubs() {
	const store = new Map()
	let mounted = null
	let hostId = null
	globalThis.window = { __ModuleLoader__: { load: (m) => { registered = m } }, innerWidth: 1280, innerHeight: 820 }
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v))
	}
	globalThis.document = {
		createElement: () => ({ set id(v) { hostId = v } }),
		body: { appendChild: () => {} }
	}
	globalThis.fetch = async () => ({ ok: false })
	const react = {
		useState: (init) => {
			const value = typeof init === 'function' ? init() : init
			// 「0 = 还没数据」的初值换成新鲜时间戳，好把整条渲染路径走完
			return [value === 0 ? Date.now() : value, () => {}]
		},
		useRef: (init) => ({ current: init }),
		useEffect: () => {}
	}
	requireImpl = (id) => {
		if (id === 'react') return react
		if (id === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }
		if (id === 'react-dom/client') return { createRoot: () => ({ render: (el) => { mounted = el } }) }
		throw new Error('unexpected require: ' + id)
	}
	return () => ({ mounted: () => mounted, hostId: () => hostId })
}

let registered = null

// --- 用例 1：正常挂载并渲染出展开的观察窗 ---
const peek = installStubs()
await import('file://' + path)
if (!registered) throw new Error('模块没有向 __ModuleLoader__ 注册')

const warnings = []
const clientExports = registered.factory(requireImpl)
clientExports.apply({ logger: { warn: (m) => warnings.push(m) } })
await new Promise((r) => setTimeout(r, 5)) // apply 里是 setTimeout(0) 延后一帧挂载

const mountedEl = peek().mounted()
if (!mountedEl) throw new Error('apply 没有挂载组件')
const rendered = mountedEl.type()
if (!rendered || rendered.type !== 'div') throw new Error(`期望展开卡片(div)，实际 ${rendered && rendered.type}`)
const kids = (rendered.props.children || []).filter(Boolean)
const img = kids[0]
if (img.props.src !== '/_computer-use/stream') throw new Error('画面没走 MJPEG 推流，而是 ' + img.props.src)
if (!rendered.props.onPointerDown || !rendered.props.onPointerUp) throw new Error('握把事件丢了，卡片会拖不动')
console.log(`✓ 挂载 + 渲染通过：div，子节点 ${kids.length} 个，画面 = ${img.props.src}`)
console.log(`✓ 挂载容器 = #${peek().hostId()}`)
// 卡片这一半区已退休（预览面改由 server/preview_window.py 的原生浮窗承担），
// 但实现与冒烟仍保留：dsh.client 声明加回去就能复活，行为不能悄悄坏掉。
if (typeof rendered.props.style?.width !== 'number') throw new Error('卡片尺寸样式丢了')
console.log(`✓ 退休的卡片实现仍可挂载（宽度 ${rendered.props.style.width} px，当前不注入页面）`)

// --- 用例 1b：出现判定 —— 关键是「dsh 里有回合在跑」绝不能让窗出现 ---
const now = Date.now()
const rules = [
	['刚用过电脑 → 出现', clientExports.shouldShow({ imageMtime: now, activity: now - 5000, now, mode: 'open' }), true],
	['2 分钟没动过（普通任务中）→ 不出现', clientExports.shouldShow({ imageMtime: now, activity: now - 180000, now, mode: 'open' }), false],
	['没截到过图 → 不出现', clientExports.shouldShow({ imageMtime: 0, activity: now, now, mode: 'open' }), false],
	['用户收成小圆标 → 一直在', clientExports.shouldShow({ imageMtime: now, activity: now - 999999, now, mode: 'pill' }), true]
]
for (const [label, got, want] of rules) {
	if (got !== want) throw new Error(`出现判定错了：${label} 期望 ${want}，实际 ${got}`)
	console.log(`✓ ${label}`)
}

// --- 用例 2：seed 词缺失时必须安静降级，绝不能抛 ---
registered = null
const peek2 = installStubs()
await import('file://' + path + '?v=2')
const brokenRequire = () => { throw new Error('no such seed word') }
let threw = null
try {
	registered.factory(brokenRequire).apply({ logger: { warn: (m) => warnings.push(m) } })
} catch (error) {
	threw = error
}
if (threw) throw new Error(`seed 词缺失时把异常抛给了宿主页面：${threw}`)
if (peek2().mounted()) throw new Error('seed 词缺失却仍然挂载了')
await new Promise((r) => setTimeout(r, 5))
console.log('✓ seed 词缺失时安静降级（宿主页面不受影响）')
console.log(`✓ 降级留了痕：${warnings.at(-1)?.slice(0, 46)}…`)
