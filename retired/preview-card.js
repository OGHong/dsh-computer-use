/**
 * `dsh-computer-use` — the browser half: **已退休的内嵌预览卡片**。
 *
 * 2026-10 用户要求预览面「一直都是浮窗」：能拖到 dsh 窗口之外、浮在别的应用之上，
 * 那是 DOM 做不到的（卡片被窗口裁剪）。所以这一半区不再声明 —— `package.json` 里的
 * `dsh.client` 与 `exports["./client"]` 已撤掉，dsh 不会再把它注入页面；预览面由
 * `server/preview_window.py` 那个原生浮窗承担。
 *
 * 实现完整保留在树里（插件没有版本库，删了就找不回来），但**搬到了 `retired/`**：
 * 它不在 `package.json` 的 `files` 白名单里，所以不再随包发布 —— 发布物里只该有真正
 * 会被加载的东西（dsh-plugin-dev 的打包规则：`files` 白名单外的一律不进包）。
 * 要恢复卡片：把它移回构建路径（或直接作为 `lib/client.js` 提供），再把 `dsh.client`
 * 与 `exports["./client"]` 加回 `package.json` 即可，Host 半区的路由
 * （`/_computer-use/stream`、`status.json`）与 `view=1` 需求信号都还在，行为与退休前一致。
 * 冒烟检查（scripts/client-smoke.mjs）也仍然指着这个文件跑。
 *
 * ---- 以下为退休前的说明 ----
 * 行为对齐 ZCode / Qoder 的 computer-use 画中画：无常驻标题栏（悬停才浮出控件与动作字幕）、
 * 有帧且 Agent 活跃时才出现、停手自动挂起、动作一回来就复现、整块画面可拖、画面等比不裁切、
 * 回合结束打绿色 ✓。画面走 MJPEG 推流（`/_computer-use/stream`），不轮询取图。
 *
 * 挂载策略：**不依赖 dsh 的 slot 名单**，自己往 document.body 建容器挂载。卡片本来就
 * portal 到 body，slot 只是替我们调一次 render，却把整张窗绑死在 `shell.overlay` 这个名字上
 * —— dsh 一改 slot 命名，卡片就无声消失且没有任何提示。
 *
 * 容错原则：client 条目抛错会拖垮整页启动，所以所有外部依赖（seed 词、DOM、fetch、
 * localStorage）都必须可失败：缺什么就安静地不出现，绝不让宿主页面一起倒。
 * @module dsh-computer-use/client
 */

const MODULE_ID = 'dsh-computer-use'
const CHANNEL = '/_computer-use'
const STORE_KEY = `${MODULE_ID}.preview`
const POS_KEY = `${STORE_KEY}.pos`
const POLL_MS = 1000 // 只带动作字幕与开关状态；画面是推流，不需要高频轮询
const DONE_MS = 20000 // 停手 20 秒 → 完成态 ✓
const DORMANT_MS = 120000 // 停手 2 分钟 → 挂起
const ACTION_MS = 6000 // 动作字幕只报 6 秒内的「刚刚」
const WIDTH = 320 // 卡片宽度（CSS px）：248 时连窗口标题都糊，320 能看清标题栏与按钮位置

window.__ModuleLoader__?.load({
	id: MODULE_ID,
	factory: (require) => {
		const module = { exports: {} }
		const exports = module.exports
		Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

		// 任何一个 seed 词被改名/缺失都不能把异常抛给宿主：拿不到就不挂观察窗。
		let deps = null
		try {
			deps = { react: require('react'), runtime: require('react/jsx-runtime'), root: require('react-dom/client') }
		} catch {
			deps = null
		}

		const style = {
			card: {
				position: 'fixed',
				right: 16,
				bottom: 96,
				width: WIDTH,
				minHeight: 80,
				borderRadius: 10,
				overflow: 'hidden',
				background: '#101014',
				boxShadow: '0 6px 24px rgba(0,0,0,.45)',
				zIndex: 2147483000,
				font: '12px/1.4 -apple-system, "PingFang SC", sans-serif',
				color: '#c9c9d1',
				cursor: 'grab',
				userSelect: 'none',
				touchAction: 'none'
			},
			image: { width: '100%', display: 'block', minHeight: 80, objectFit: 'contain' },
			tools: {
				position: 'absolute',
				right: 6,
				top: 6,
				display: 'flex',
				gap: 2,
				padding: '1px 2px',
				borderRadius: 6,
				background: 'rgba(16,16,20,.72)'
			},
			caption: {
				position: 'absolute',
				left: 0,
				right: 0,
				bottom: 0,
				padding: '3px 8px',
				background: 'rgba(16,16,20,.72)',
				color: '#d7d7de',
				overflow: 'hidden',
				whiteSpace: 'nowrap',
				textOverflow: 'ellipsis'
			},
			button: {
				border: 'none',
				background: 'transparent',
				color: '#c2c2cc',
				cursor: 'pointer',
				padding: '0 4px',
				font: '12px/16px -apple-system, "PingFang SC", sans-serif'
			},
			pill: {
				position: 'fixed',
				right: 16,
				bottom: 96,
				zIndex: 2147483000,
				border: '1px solid #2c2c34',
				borderRadius: 999,
				background: '#17171c',
				color: '#c9c9d1',
				cursor: 'pointer',
				padding: '4px 10px',
				font: '12px/1.4 -apple-system, "PingFang SC", sans-serif',
				boxShadow: '0 4px 14px rgba(0,0,0,.4)'
			},
			waiting: { padding: '14px 10px', textAlign: 'center', opacity: 0.55 },
			badge: {
				position: 'absolute',
				left: '50%',
				top: '50%',
				transform: 'translate(-50%, -50%)',
				width: 52,
				height: 52,
				borderRadius: '50%',
				background: 'rgba(46,184,84,.92)',
				color: '#fff',
				fontSize: 26,
				fontWeight: 700,
				display: 'flex',
				alignItems: 'center',
				justifyContent: 'center',
				boxShadow: '0 2px 10px rgba(0,0,0,.5)'
			}
		}

		/** localStorage 在隐私模式/受限沙箱下会抛，统一走这两个安全壳。 */
		const read = (key, fallback) => {
			try {
				const raw = localStorage.getItem(key)
				return raw === null ? fallback : raw
			} catch {
				return fallback
			}
		}
		const save = (key, value) => {
			try {
				localStorage.setItem(key, value)
			} catch {}
		}

		/** 存过的坐标可能来自更大的窗口：越界就回落右下角，否则表现为「有元素但看不见」。 */
		const storedPos = () => {
			try {
				const p = JSON.parse(read(POS_KEY, 'null'))
				if (!Number.isFinite(p?.x) || !Number.isFinite(p?.y) || p.x < 0 || p.y < 0) return null
				if (p.x > window.innerWidth - WIDTH || p.y > window.innerHeight - 24) return null
				return p
			} catch {
				return null
			}
		}


			/**
			 * 观察窗该不该出现。三个条件缺一不可：真的截到过图（没首帧不 promote）、
			 * 2 分钟内有过 computer-use 动作（`_note()` 写的 status.txt），或者用户自己
			 * 把它收成小圆标留着当入口（那是显式意图，不受挂起影响）。
			 */
			function shouldShow(state) {
				if (state.imageMtime === 0) return false
				if (state.mode === 'pill') return true
				return state.now - state.activity <= DORMANT_MS
			}

		function ObservationWindow() {
			const { react } = deps
			const { jsx: h, jsxs: hh } = deps.runtime
			// mode: 'open' 展开 | 'pill' 小圆标 | 'off' 用户关掉
			const [mode, setMode] = react.useState(() => read(STORE_KEY, 'open'))
			const [pos, setPos] = react.useState(storedPos)
			const [imageMtime, setImageMtime] = react.useState(0)
			const [actionMtime, setActionMtime] = react.useState(0)
			const [action, setAction] = react.useState('')
			const [running, setRunning] = react.useState(undefined)
			const [hostHidden, setHostHidden] = react.useState(false)
			const [streamBroken, setStreamBroken] = react.useState(false)
			const [hover, setHover] = react.useState(false)
			const [now, setNow] = react.useState(() => Date.now())
			const cardRef = react.useRef(null)
			const dragRef = react.useRef(null)
			const cardToken = react.useRef(null)
			const wakeAt = react.useRef(0)
			/** 上一帧是否真的上屏：只有在看的时候才声明「有人要看」，驱动 Host 开流。 */
			const viewing = react.useRef(false)

			const rememberMode = (next) => {
				save(STORE_KEY, next)
				setMode(next)
			}

			react.useEffect(() => {
				const timer = setInterval(() => {
					fetch(`${CHANNEL}/status.json${viewing.current ? '?view=1' : ''}`, { credentials: 'same-origin' })
						.then((r) => (r.ok ? r.json() : null))
						.then((j) => {
							if (!j) return
							setNow(Number(j.now) > 0 ? Number(j.now) : Date.now())
							setAction(typeof j.action === 'string' ? j.action : '')
							setRunning(typeof j.agentRunning === 'boolean' ? j.agentRunning : undefined)
							if (Number(j.actionMtime) > 0) setActionMtime(Number(j.actionMtime))
							if (Number(j.imageMtime) > 0) {
								setImageMtime(Number(j.imageMtime))
								setStreamBroken(false) // 推流断过又恢复时，把 <img> 挂回去
							}
							const card = String(j.card || 'on')
							const first = cardToken.current === null
							const changed = cardToken.current !== card
							cardToken.current = card
							setHostHidden(card.startsWith('off'))
							// Agent 的 preview(true) 是显式指令：既覆盖用户「×」留下的 off，
							// 也覆盖空闲挂起，让窗口立刻回到眼前。
							if (changed && !first && !card.startsWith('off')) {
								wakeAt.current = Date.now()
								rememberMode('open')
							}
						})
						.catch(() => {})
				}, POLL_MS)
				return () => clearInterval(timer)
			}, [mode])

			// 窗口缩小后卡片可能被推出视口、再也拖不回来。
			react.useEffect(() => {
				const onResize = () => setPos(storedPos())
				window.addEventListener('resize', onResize)
				return () => window.removeEventListener('resize', onResize)
			}, [])

			// ---- 拖动：整块画面都是握把。落在按钮上时绝不 preventDefault —— 取消
			// pointerdown 会连带抑制兼容的 mousedown/mouseup，浏览器就不再派发 click。----
			const onDown = (e) => {
				if (e.target?.closest?.('button')) return
				const rect = cardRef.current?.getBoundingClientRect()
				if (!rect) return
				e.preventDefault()
				dragRef.current = { dx: e.clientX - rect.left, dy: e.clientY - rect.top }
				try {
					e.currentTarget.setPointerCapture(e.pointerId)
				} catch {}
			}
			const onMove = (e) => {
				if (!dragRef.current) return
				// 纵向按实际高度夹取：高度随截屏宽高比变化，只夹一点会把底部推出窗口。
				const height = cardRef.current?.offsetHeight || 24
				setPos({
					x: Math.max(4, Math.min(window.innerWidth - WIDTH - 4, e.clientX - dragRef.current.dx)),
					y: Math.max(4, Math.min(window.innerHeight - height - 4, e.clientY - dragRef.current.dy))
				})
			}
			const onUp = () => {
				dragRef.current = null
				setPos((p) => {
					if (p) save(POS_KEY, JSON.stringify(p))
					return p
				})
			}

			const gone = () => {
				viewing.current = false
				return null
			}
			if (hostHidden || mode === 'off') return gone()

			// 出现与否只看「computer-use 自己有没有被用过」。
			// 不能拿 agentRunning 当依据：那是 dsh 里任意一个回合在跑，于是你写代码、
			// 聊天这类普通任务也会把窗叫出来（而 latest.jpg 是跨会话留着的旧截图，
			// 一直有帧可显示）—— 实测就是这个现象。
			const activity = Math.max(actionMtime, wakeAt.current)
			if (!shouldShow({ imageMtime, activity, now, mode })) return gone()

			// 完成态：回合结束就打 ✓（对齐 ZCode 的 turn-ended → 完成徽章）；
			// 拿不到该事件时退回「20 秒没有新动作」的时间推断。
			const done = running === undefined ? now - activity > DONE_MS : !running
			if (mode === 'pill') {
				viewing.current = false
				return h('button', {
					style: style.pill,
					title: '打开 computer-use 实时预览',
					onClick: () => rememberMode('open'),
					children: done ? '✓ computer-use' : '🖥 computer-use'
				})
			}

			const cardStyle = { ...style.card }
			if (pos) {
				cardStyle.left = pos.x
				cardStyle.top = pos.y
				cardStyle.right = 'auto'
				cardStyle.bottom = 'auto'
			}
			viewing.current = true

			return hh('div', {
				ref: cardRef,
				style: cardStyle,
				onPointerDown: onDown,
				onPointerMove: onMove,
				onPointerUp: onUp,
				onPointerEnter: () => setHover(true),
				onPointerLeave: () => setHover(false),
				children: [
					streamBroken
						? h('div', { style: style.waiting, children: '观察流已断开，等待下一帧…' })
						: h('img', {
								// MJPEG：Chromium 自己滚帧，不需要轮询也不需要 cache-buster
								src: `${CHANNEL}/stream`,
								style: style.image,
								alt: 'computer-use 最新截屏',
								onError: () => setStreamBroken(true)
						  }),
					done ? h('div', { style: style.badge, children: '✓' }) : null,
					hover
						? hh('div', { style: style.tools, children: [
								h('button', {
									style: style.button,
									title: '收起成小圆标',
									onClick: () => rememberMode('pill'),
									children: '—'
								}),
								h('button', {
									style: style.button,
									title: '隐藏（Agent 可用 preview 工具重新显示）',
									onClick: () => rememberMode('off'),
									children: '×'
								})
							] })
						: null,
					hover && !done && action && now - actionMtime < ACTION_MS
						? h('div', { style: style.caption, children: action })
						: null
				]
			})
		}

		/**
		 * Self-mount into document.body, deferred one turn so the host page finishes
		 * booting first. Any failure logs and leaves the host untouched.
		 */
		function apply(ctx) {
			if (!deps || typeof document === 'undefined') {
				ctx.logger?.warn?.(`${MODULE_ID}: 缺 react / react-dom/client，观察窗不挂载（宿主页面不受影响）`)
				return
			}
			setTimeout(() => {
				try {
					const host = document.createElement('div')
					host.id = `${MODULE_ID}-observation`
					document.body.appendChild(host)
					deps.root.createRoot(host).render(deps.runtime.jsx(ObservationWindow, {}))
				} catch (error) {
					ctx.logger?.warn?.(`${MODULE_ID}: 观察窗挂载失败，已跳过：${String(error)}`)
				}
			}, 0)
		}

		exports.apply = apply
		exports.shouldShow = shouldShow
		return module.exports
	}
})
