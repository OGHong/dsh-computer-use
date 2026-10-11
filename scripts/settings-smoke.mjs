/**
 * 设置页（Settings → 电脑操控）的冒烟检查：把「注册 → apply → 渲染 → 点一下」真的跑一遍。
 *
 * 为什么需要：这一页是**手写**的 bundle（没有构建步骤，见 client/settings.js 的模块说明），
 * `node --check` 只看语法，看不到这几件事 —— 而它们每一个都能静默坏掉：
 *
 *   * 注册进哪个槽位、order/label 对不对（注册错槽位 = 设置里根本找不到这一栏）；
 *   * 开关反映的是哪份状态（把 autoShow 读反了，用户就会看到「关着」而窗还在弹）；
 *   * 点一下发出去的请求长什么样（路径/字段写错 = 点了没反应）；
 *   * 拿不到 host 时会不会把整页带崩（上一半区的三连坑就是这么漏出去的）。
 *
 * 用法：node scripts/settings-smoke.mjs
 */
const path = await import('node:path').then((m) => m.default.join(process.cwd(), 'client/settings.js'))

/** 假 React：只保留这一页真正用到的那几个 hook（useState 的返回值由调用方按顺序供给）。 */
function installStubs({ states = [], fetchImpl, breakSeed = false } = {}) {
	let registered = null
	let cursor = 0
	const calls = []
	globalThis.window = { __ModuleLoader__: { load: (m) => { registered = m } } }
	globalThis.fetch = async (url, init) => {
		calls.push({ url, init })
		if (!fetchImpl) return { ok: false, status: 500, json: async () => ({}) }
		return fetchImpl(url, init)
	}
	const react = {
		createElement: (type, props, ...children) => ({ type, props: { ...(props || {}), children: children.length > 1 ? children : children[0] } }),
		useState: (init) => {
			const value = cursor < states.length ? states[cursor] : typeof init === 'function' ? init() : init
			cursor += 1
			return [value, () => {}]
		},
		useRef: (init) => ({ current: init }),
		useEffect: () => {},
		useCallback: (fn) => fn
	}
	// 假 primitives **只提供运行时真的有的导出**：引到表外的东西会在这里就断（见 RUNTIME_PRIMITIVES）
	const prims = {}
	for (const name of ['Switch', 'Pill', 'Button', 'StateDot', 'Tooltip', 'Tag', 'DisclosureRow']) {
		if (!RUNTIME_PRIMITIVES.has(name)) throw new Error(`假 primitives 用了运行时没有的导出：${name}`)
		prims[name] = (props) => ({ type: name, props })
	}
	// 自绘的导航图标按壳的线宽常量对齐粗细（这是个数字，不是组件）
	prims.ICON_MEDIUM_STROKE = 1.3
	// 假 react-dom/client：只记下 render 出来的节点与 unmount 次数（导航行图标用例要断言）
	const rendered = []
	const roots = []
	const reactDomClient = {
		createRoot: (container) => {
			const root = {
				container,
				unmounted: 0,
				render: (node) => rendered.push({ container, node }),
				unmount: () => { root.unmounted += 1 }
			}
			roots.push(root)
			return root
		}
	}
	const requireImpl = (id) => {
		if (breakSeed) throw new Error('no such seed word')
		if (id === 'react') return react
		if (id === '@deepseek-ai/dsh-client-ui-primitives') return prims
		if (id === 'react-dom/client') return reactDomClient
		throw new Error('unexpected require: ' + id)
	}
	return { registered: () => registered, requireImpl, calls, rendered, roots }
}

/**
 * 假 DOM：只搭出 `installNavIcon` 真正会碰的那点结构 ——
 * `document.body`、一个 `[role="dialog"]`、里面一个导航行 `button`（第一个子元素是齿轮 svg、
 * 文本等于本页 label、行高 40px），外加一个记录回调的假 MutationObserver。
 * 故意不含任何壳的类名：那段代码只认结构，这里也只用结构验证它。
 */
function makeFakeDom({ label = '电脑操控', height = 40, gearTag = 'svg', strict = true, decoys = strict } = {}) {
	const observers = []
	const makeEl = (tag) => {
		const el = {
			tagName: tag.toUpperCase(),
			children: [],
			attrs: {},
			textContent: '',
			isConnected: true,
			style: {
				display: '',
				cssText: '',
				removeProperty: (name) => { if (name === 'display') el.style.display = '' }
			},
			setAttribute: (name, value) => { el.attrs[name] = value },
			hasAttribute: (name) => name in el.attrs,
			insertBefore: (node, ref) => {
				const at = ref ? el.children.indexOf(ref) : 0
				el.children.splice(at < 0 ? 0 : at, 0, node)
				node.parent = el
			},
			remove: () => {
				if (el.parent) el.parent.children = el.parent.children.filter((child) => child !== el)
				el.isConnected = false
			},
			getBoundingClientRect: () => ({ height }),
			querySelectorAll: () => []
		}
		Object.defineProperty(el, 'firstElementChild', { get: () => el.children[0] || null })
		return el
	}
	const gear = makeEl(gearTag)
	const label2 = makeEl('span')
	label2.textContent = label
	const row = makeEl('button')
	row.children = [gear, label2]
	row.textContent = label
	gear.parent = row
	// 两个诱饵（都排在真行前面）：文本一样，但结构特征不全 —— 那段代码必须跳过它们
	const decoyNoIcon = makeEl('button')
	decoyNoIcon.textContent = label
	const decoyShortGear = makeEl('svg')
	const decoyShortLabel = makeEl('span')
	decoyShortLabel.textContent = label
	const decoyShort = makeEl('button')
	decoyShort.children = [decoyShortGear, decoyShortLabel]
	decoyShort.textContent = label
	decoyShortGear.parent = decoyShort
	decoyShort.getBoundingClientRect = () => ({ height: 24 })
	const dialog = makeEl('div')
	dialog.attrs.role = 'dialog'
	dialog.querySelectorAll = (selector) => (selector === 'button' ? [decoyNoIcon, decoyShort, row] : [])
	const body = makeEl('body')
	const document = {
		body,
		createElement: (tag) => makeEl(tag),
		// 宽松兜底那一遍是在整个文档里按「文本 + 行里有图标」找的，所以这里也要能按 button 查
		querySelectorAll: (selector) => {
			if (selector === '[role="dialog"]') return [dialog]
			if (selector === 'button') return decoys ? [decoyNoIcon, decoyShort, row] : [decoyNoIcon, row]
			return []
		}
	}
	// strict=false：壳「换了行高」的情况 —— 严格匹配不该命中，宽松兜底要接上
	if (!strict) {
		row.getBoundingClientRect = () => ({ height: 36 })
		dialog.querySelectorAll = (selector) => (selector === 'button' ? [row] : [])
	}
	class FakeObserver {
		constructor(callback) { this.callback = callback; this.connected = true; observers.push(this) }
		observe() {}
		disconnect() { this.connected = false }
	}
	return { document, MutationObserver: FakeObserver, row, gear, dialog, observers, decoyNoIcon, decoyShort, decoyShortGear }
}

/** 把元素树摊平成节点表：函数组件就地展开，字符串/原语节点收下来。 */
function flatten(el, out = []) {
	if (el === null || el === undefined || typeof el === 'boolean') return out
	if (Array.isArray(el)) {
		for (const child of el) flatten(child, out)
		return out
	}
	if (typeof el === 'string' || typeof el === 'number') {
		out.push({ type: '#text', text: String(el) })
		return out
	}
	const { type, props } = el
	if (typeof type === 'function') return flatten(type(props || {}), out)
	out.push({ type: typeof type === 'string' ? type : String(type), props: props || {} })
	flatten(props && props.children, out)
	return out
}

const find = (nodes, type, predicate) => nodes.find((n) => n.type === type && (!predicate || predicate(n.props)))
const texts = (nodes) => nodes.filter((n) => n.type === '#text').map((n) => n.text).join('')

/**
 * 运行时 primitives 的导出表（**页面真正加载的那份壳**的清单）。
 *
 * 来源：`/Applications/DeepSeek Harness.app/Contents/Resources/app.asar` →
 * `dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/index-*.js` 里
 * `const sE=Object.freeze({...})` 那一段的键名。桌面版把 `/assets` 与所有
 * `@deepseek-ai/*` 客户端包都从 asar 里发 —— **profile 里那份同名包根本不会被读**
 * （实测 atime 停在安装那天，而页面每次启动都会换 bundle 版本号）。
 *
 * 这张表必须抄对来源，代价是真金白银踩过一次：第一版抄了 profile 里那份构建，于是
 * `IconGaugeOutline16` 看着「在表里」，真机上却根本不存在（真名是 `IconGaugeOutlineMedium`，
 * 两份构建的命名与 tree-shaking 范围都不一样），导航图标怎么改都不生效。所以：
 * **换 dsh 版本后重新生成这张表**，别凭 README 或 profile 里的包猜。
 */
const RUNTIME_PRIMITIVES = new Set([
	'BrandWordmark', 'Button', 'CODE_HIGHLIGHT_EXTENSIONS', 'Checkbox', 'CodeBlock', 'ConnectionIndicator',
	'DEFAULT_DIFF_MAX_LINES', 'DEFAULT_READ_MAX_LINES', 'DEFAULT_SEARCH_MAX_LINES', 'DEFAULT_TERMINAL_MAX_LINES', 'DiffBlock', 'DisclosureRow',
	'FISH_LOGO_PATH', 'FISH_LOGO_VIEWBOX', 'FileTypeIcon', 'FishLogo', 'GuideArtworkBrowser', 'GuideArtworkFiles',
	'HoverCard', 'ICON_MEDIUM_STROKE', 'ICON_REGULAR_STROKE', 'IconAgentPresetOutlineMedium', 'IconAgentPresetOutlineRegular', 'IconAlarmClockOutlineMedium',
	'IconAlarmClockOutlineRegular', 'IconApiOutlineMedium', 'IconApiOutlineRegular', 'IconArchiveCheckOutlineMedium', 'IconArchiveCheckOutlineRegular', 'IconArchiveOffOutlineMedium',
	'IconArchiveOffOutlineRegular', 'IconArchiveOutlineMedium', 'IconArchiveOutlineRegular', 'IconBranchOutlineMedium', 'IconBranchOutlineRegular', 'IconBrowseOutlineMedium',
	'IconBrowseOutlineRegular', 'IconCheckCircleFillMedium', 'IconCheckCircleFillRegular', 'IconCheckCircleOutlineMedium', 'IconCheckCircleOutlineRegular', 'IconCheckOutlineMedium',
	'IconCheckOutlineRegular', 'IconChecklistOutlineMedium', 'IconChecklistOutlineRegular', 'IconChevronDownOutlineMedium', 'IconChevronDownOutlineRegular', 'IconChevronLeftOutlineMedium',
	'IconChevronLeftOutlineRegular', 'IconChevronRightOutlineMedium', 'IconChevronRightOutlineRegular', 'IconChevronUpOutlineMedium', 'IconChevronUpOutlineRegular', 'IconChevronsUpDownOutlineMedium',
	'IconChevronsUpDownOutlineRegular', 'IconClockOutlineMedium', 'IconClockOutlineRegular', 'IconCloseCircleFillMedium', 'IconCloseCircleFillRegular', 'IconCloseFillMedium',
	'IconCloseFillRegular', 'IconCloseOutlineMedium', 'IconCloseOutlineRegular', 'IconCodeOutlineMedium', 'IconCodeOutlineRegular', 'IconCompactOutlineMedium',
	'IconCompactOutlineRegular', 'IconCompareSplitOutlineMedium', 'IconCompareSplitOutlineRegular', 'IconContextInjectionOutlineMedium', 'IconContextInjectionOutlineRegular', 'IconCopyOutlineMedium',
	'IconCopyOutlineRegular', 'IconCordisPluginOutlineMedium', 'IconCordisPluginOutlineRegular', 'IconDarkOutlineMedium', 'IconDarkOutlineRegular', 'IconDataOutlineMedium',
	'IconDataOutlineRegular', 'IconDatabaseOutlineMedium', 'IconDatabaseOutlineRegular', 'IconDeliverDocMedium', 'IconDeliverDocRegular', 'IconDislikeFillMedium',
	'IconDislikeFillRegular', 'IconDislikeOutlineMedium', 'IconDislikeOutlineRegular', 'IconDownloadOutlineMedium', 'IconDownloadOutlineRegular', 'IconEditOutlineMedium',
	'IconEditOutlineRegular', 'IconEllipsisOutlineMedium', 'IconEllipsisOutlineRegular', 'IconEnhanceOutlineMedium', 'IconEnhanceOutlineRegular', 'IconFlatListOutlineMedium',
	'IconFlatListOutlineRegular', 'IconFolderCloseMedium', 'IconFolderCloseRegular', 'IconFolderOpenMedium', 'IconFolderOpenOutlineMedium', 'IconFolderOpenOutlineRegular',
	'IconFolderOpenRegular', 'IconFollowsystemOutlineMedium', 'IconFollowsystemOutlineRegular', 'IconFullscreenOutlineMedium', 'IconFullscreenOutlineRegular', 'IconGaugeOutlineMedium',
	'IconGaugeOutlineRegular', 'IconGlobeOutlineMedium', 'IconGlobeOutlineRegular', 'IconGoalOutlineMedium', 'IconGoalOutlineRegular', 'IconInfoOutlineMedium',
	'IconInfoOutlineRegular', 'IconInspectOutlineMedium', 'IconInspectOutlineRegular', 'IconLightOutlineMedium', 'IconLightOutlineRegular', 'IconLikeFillMedium',
	'IconLikeFillRegular', 'IconLikeOutlineMedium', 'IconLikeOutlineRegular', 'IconLinkOutlineMedium', 'IconLinkOutlineRegular', 'IconListPenOutlineMedium',
	'IconListPenOutlineRegular', 'IconLoadingOutlineMedium', 'IconLoadingOutlineRegular', 'IconMicrophoneOutlineMedium', 'IconMicrophoneOutlineRegular', 'IconNewChatOutlineMedium',
	'IconNewChatOutlineRegular', 'IconNowrapFillMedium', 'IconNowrapFillRegular', 'IconPanelLeftOutlineMedium', 'IconPanelLeftOutlineRegular', 'IconPaperPlaneOutlineMedium',
	'IconPaperPlaneOutlineRegular', 'IconPaperclipOutlineMedium', 'IconPaperclipOutlineRegular', 'IconPauseOutlineMedium', 'IconPauseOutlineRegular', 'IconPersonalizationOutlineMedium',
	'IconPersonalizationOutlineRegular', 'IconPinFillMedium', 'IconPinFillRegular', 'IconPinOutlineMedium', 'IconPinOutlineRegular', 'IconPlanOutlineMedium',
	'IconPlanOutlineRegular', 'IconPlayOutlineMedium', 'IconPlayOutlineRegular', 'IconPluginPinwheelOutlineMedium', 'IconPluginPinwheelOutlineRegular', 'IconPlusOutlineMedium',
	'IconPlusOutlineRegular', 'IconProjectAddOutlineMedium', 'IconProjectAddOutlineRegular', 'IconQuestionOutlineMedium', 'IconQuestionOutlineRegular', 'IconQueueOutlineMedium',
	'IconQueueOutlineRegular', 'IconRefreshOutlineMedium', 'IconRefreshOutlineRegular', 'IconRightUpOutlineMedium', 'IconRightUpOutlineRegular', 'IconSearchOutlineMedium',
	'IconSearchOutlineRegular', 'IconSendOutlineMedium', 'IconSendOutlineRegular', 'IconSettingsOutlineMedium', 'IconSettingsOutlineRegular', 'IconShareOutlineMedium',
	'IconShareOutlineRegular', 'IconShieldOutlineMedium', 'IconShieldOutlineRegular', 'IconSkillOutlineMedium', 'IconSkillOutlineRegular', 'IconSlidersTwoOutlineMedium',
	'IconSlidersTwoOutlineRegular', 'IconSparkleMedium', 'IconSparkleRegular', 'IconStopFillMedium', 'IconStopFillRegular', 'IconThinkOutlineMedium',
	'IconThinkOutlineRegular', 'IconTrashOutlineMedium', 'IconTrashOutlineRegular', 'IconTreeCornerMedium', 'IconTreeCornerRegular', 'IconTriangleRightFillMedium',
	'IconTriangleRightFillRegular', 'IconUnarchiveOutlineMedium', 'IconUnarchiveOutlineRegular', 'IconUserOutlineMedium', 'IconUserOutlineRegular', 'IconUsersOutlineMedium',
	'IconUsersOutlineRegular', 'IconWarningOutlineMedium', 'IconWarningOutlineRegular', 'IconWarningTriangleOutlineMedium', 'IconWarningTriangleOutlineRegular', 'IconWorkspaceTreeOutlineMedium',
	'IconWorkspaceTreeOutlineRegular', 'IconWrapFillMedium', 'IconWrapFillRegular', 'IconWrapLinesOutlineMedium', 'IconWrapLinesOutlineRegular', 'ImageLightbox',
	'Input', 'JsonBlock', 'JsonTree', 'LinkIconMedium', 'LinkIconRegular', 'MarkdownDelegateProvider',
	'MarkdownText', 'Menu', 'MenuGroup', 'MenuItemButton', 'MenuSurface', 'Modal',
	'PathLabel', 'PermissionIconFullAccessMedium', 'PermissionIconFullAccessRegular', 'PermissionIconReadOnlyMedium', 'PermissionIconReadOnlyRegular', 'PermissionIconWorkspaceWriteMedium',
	'PermissionIconWorkspaceWriteRegular', 'Pill', 'PluginArtworkDefault', 'PluginArtworkLoop', 'PluginArtworkSearch', 'PluginArtworkSubagent',
	'PluginArtworkTerminal', 'ReadBlock', 'ReferenceIconMedium', 'ReferenceIconRegular', 'RiskConfirmation', 'SHIELD_OUTLINE_PATH',
	'SearchBlock', 'SegmentedControl', 'SegmentedTabs', 'SettingsForm', 'SettingsFormModel', 'SettingsSecretField',
	'SettingsValueField', 'ShortcutKeys', 'StateDot', 'Switch', 'Tag', 'TerminalBlock',
	'TextShimmer', 'Toast', 'Tooltip', 'WebBlock', 'classifyFileType', 'classifyLinkPath',
	'closeTopModal', 'diffTotals', 'extractMarkdownPlainText', 'fileExtension', 'fileSizeText', 'focusWithoutRing',
	'isBehindModal', 'isDarwinDesktop', 'languageForPath', 'modalSelector', 'observeComposition', 'observeStickyMenuGroups',
	'pointerModality', 'projectUserText', 'rankByName', 'relativeTime', 'settingsNumberField', 'settingsTextField',
	'useAnchoredMaxHeight', 'useAnchoredPosition', 'useCodeHighlighter', 'useDismissOnOutsidePointer', 'useModalLayer', 'writeClipboard',
])

const fail = (message) => {
	throw new Error(message)
}

const { readFileSync } = await import('node:fs')
/** 被测页面的源码：导出表守卫与词典守卫都从它里面数 key。 */
const source = readFileSync(path, 'utf8')

// --- 用例 4a：用到的 primitives 导出必须**在运行时真的存在** ----------------------
//   这条是本轮真事故的守卫：`SegmentedControl` 在 README 里是官方组件，但运行时的壳做了
//   tree-shaking，它并不在导出表里 —— 假 primitives 照样给，页面到真机上整页崩。
const primsUsed = new Set([...source.matchAll(/prims\.([A-Za-z0-9_]+)/g)].map((m) => m[1]))
const missing = [...primsUsed].filter((name) => !RUNTIME_PRIMITIVES.has(name))
if (missing.length) fail(`页面引了运行时 primitives 里没有的导出（真机上会整页崩）：${missing.join(', ')}`)
if (primsUsed.size === 0) fail('没有从 primitives 取任何组件？守卫失效了')
// 导航行图标是自绘的（运行时那 188 个图标里没有电脑/显示器），所以这里钉的是它的「像不像壳的图标」：
// viewBox 16、fill none、stroke=currentColor、线宽取自 primitives 的 ICON_MEDIUM_STROKE。
if (!RUNTIME_PRIMITIVES.has('ICON_MEDIUM_STROKE')) fail('运行时没有 ICON_MEDIUM_STROKE？自绘图标的线宽就没法对齐了')
if (!/prims\.ICON_MEDIUM_STROKE/.test(source)) fail('自绘图标没有取壳的线宽常量（粗细会和相邻图标不一致）')
const glyph = /const ComputerIcon = \(\{ size = 16, className \}\) =>[\s\S]*?\n\n/.exec(source)?.[0] ?? ''
if (!glyph) fail('源码里找不到 ComputerIcon？导航行图标的守卫失效了')
for (const [what, pattern] of [
	['viewBox 16', /viewBox: '0 0 16 16'/],
	['fill none', /fill: 'none'/],
	['stroke=currentColor', /stroke: 'currentColor'/],
	['aria-hidden', /'aria-hidden': 'true'/]
]) {
	if (!pattern.test(glyph)) fail(`自绘的导航图标缺少「${what}」（会和壳的图标不像一套）`)
}
const strokeCount = (glyph.match(/stroke: 'currentColor'/g) || []).length
if (strokeCount !== 3) fail(`自绘的导航图标应是 3 条描边（机身 + 支颈 + 底座），实际 ${strokeCount} 条`)

console.log(`✓ primitives 导出表：用到的 ${primsUsed.size} 个（${[...primsUsed].join(', ')}）都在运行时清单里`)

// --- 用例 1：注册进 settings.section，order/label 正确 -------------------------
let peek = installStubs()
await import('file://' + path)
if (!peek.registered()) fail('模块没有向 __ModuleLoader__ 注册')
if (peek.registered().id !== 'dsh-computer-use') fail(`模块 id 不对：${peek.registered().id}`)

const clientExports = peek.registered().factory(peek.requireImpl)
if (clientExports.name !== 'dsh-computer-use') fail('插件 name 不对')
if (String(clientExports.inject) !== 'slots,locale') fail(`inject 面不对：${clientExports.inject}`)

let dicts = null
let section = null
const ctx = {
	effect: (fn) => fn(),
	locale: {
		register: (_ns, value) => { dicts = value; return () => {} },
		bind: () => (key) => (dicts && dicts.zh && dicts.zh[key]) || key
	},
	slots: {
		inject: (name, callback) => { if (name !== 'settings.section') fail(`inject 了错的槽位：${name}`); callback() },
		register: (spec, component) => { section = { spec, component }; return () => {} }
	}
}
clientExports.apply(ctx)
if (!section) fail('apply 没有注册设置页')
if (section.spec.name !== 'settings.section' || section.spec.id !== 'dsh-computer-use') fail(`注册的槽位/键不对：${JSON.stringify(section.spec)}`)
if (section.spec.order !== 2) fail(`order 期望 2（通用/手机访问之后），实际 ${section.spec.order}`)
if (section.spec.label() !== '电脑操控') fail(`label 没有走词典：${section.spec.label()}`)
if (dicts.en?.section !== 'Computer use') fail('英文词典缺失或键不对')
console.log(`✓ 注册 settings.section：id=${section.spec.id} order=${section.spec.order} label=${section.spec.label()}`)

// --- 词典必须齐 —— 少一个 key 用户就会在界面上看到一个英文 key -------------------
//   文案全部走 t()，所以「用了哪个 key」可以从源码里数出来；这里把源码里的 key 与两份词典
//   对一遍（含状态函数返回的那几个 key，它们不是 t('…') 字面量）。要放在 apply 之后：
//   dicts 是 apply 里注册进来的。
const used = new Set()
for (const match of source.matchAll(/\b(?:t|translate)\('([A-Za-z]+)'\)/g)) used.add(match[1])
for (const match of source.matchAll(/fmt\(t, '([A-Za-z]+)'/g)) used.add(match[1])
for (const key of ['statusKilled', 'statusOff', 'statusDown', 'statusUp', 'statusHidden']) used.add(key)
// 图标文案是 t(NAV_ICON_LABELS[name]) 动态取的，正则扫不到 —— 从那张表里补进来
for (const match of source.matchAll(/(Icon[A-Za-z0-9_]+):\s*'(icon[A-Za-z]+)'/g)) used.add(match[2])
const missingZh = [...used].filter((key) => dicts.zh[key] === undefined)
const missingEn = [...used].filter((key) => dicts.en[key] === undefined)
if (missingZh.length) fail(`中文词典缺 key：${missingZh.join(', ')}`)
if (missingEn.length) fail(`英文词典缺 key：${missingEn.join(', ')}`)
const unused = Object.keys(dicts.zh).filter((key) => !used.has(key))
if (unused.length) fail(`词典里有没人用的 key（要么补文案要么删掉）：${unused.join(', ')}`)
console.log(`✓ 词典齐：${used.size} 个 key 中英都有，没有多余条目`)


// --- 用例 2：拿一份状态渲染出来，开关/下拉/急停按钮都要反映它 -------------------
const state = {
	settings: { autoShow: false, autoHideSeconds: 30 },
	runtime: { panelUp: false, panelVisible: false, hidden: 'off', killed: false, lastActionAt: Date.now() - 180000 },
	choices: [5, 12, 30, 60, 120],
	paths: { base: '/tmp/dsh-computer-use', settings: '/Users/x/.dsh/computer-use/settings.json' }
}
peek = installStubs({ states: [state, '', false] })
await import('file://' + path + '?v=2')
const page = peek.registered().factory(peek.requireImpl)
const tree = page.__internals.ComputerUseSettings({ t: (key) => dicts.zh[key] || key })
const nodes = flatten(tree)

const autoShowSwitch = find(nodes, 'Switch', (p) => p.label === '自动显示观察窗')
if (!autoShowSwitch) fail('渲染结果里没有「自动显示观察窗」开关')
if (autoShowSwitch.props.checked !== false) fail('autoShow=false 时开关却是打开的（读反了）')
if (find(nodes, 'Switch', (p) => p.label === '完成后打勾 ✓')) fail('「完成后打勾」开关不该再出现（用户 2026-10-12：默认就是打勾）')
const delayPills = nodes.filter((n) => n.type === 'Pill')
if (delayPills.length !== 5) fail(`收起延时应是 5 个 Pill，实际 ${delayPills.length} 个`)
if (texts(nodes).includes('外观')) fail('「外观」选择器已经按用户要求收掉了，不该再出现')
// 60 秒以上按分钟说（「1 分钟」比「60 秒」好读），这条也钉住
if (delayPills.map((p) => p.props.children).join('|') !== '5 秒|12 秒|30 秒|1 分钟|2 分钟')
	fail(`延时选项不对：${delayPills.map((p) => p.props.children).join('|')}`)
const activePills = delayPills.filter((p) => p.props.active === true)
if (activePills.length !== 1 || activePills[0].props.children !== '30 秒')
	fail(`当前值没有落在唯一的 active Pill 上：${JSON.stringify(activePills.map((p) => p.props.children))}`)
const estop = find(nodes, 'Button', (p) => p.children === '急停')
if (!estop) fail('渲染结果里没有急停按钮')
const body = texts(nodes)
if (!body.includes('已关闭（不再自动出现）')) fail('观察窗状态没有跟着 hidden=off 走')
if (!body.includes('3 分钟前')) fail('最近一次操作没有按相对时间显示')
if (!body.includes('回合结束后自动收起')) fail('收起延时那一行的用词没跟上（应为「回合结束后自动收起」）')
if (!body.includes('辅助功能')) fail('权限说明缺失')
for (const title of ['观察窗', '控制', '权限']) if (!body.includes(title)) fail(`卡片标题缺失：${title}`)
// 用户明确要求删掉的东西：删了就该真的不在（免得以后又长回来）
for (const gone of [
	'三个「关掉」不是一回事',
	'多个会话共用同一套鼠标键盘',
	'排查',
	'/tmp/dsh-computer-use',
	'.dsh/computer-use/settings.json',
	'这一轮先收起',
	'恢复显示',
	'完成后打勾'
]) {
	if (body.includes(gone)) fail(`用户要求删掉，但还在页面上：${gone}`)
}
// 用词规范化：产品自己的说法是「回合」，页面上不该再出现口语的「这一轮」
if (body.includes('这一轮')) fail('页面文案里还有口语化的「这一轮」，应统一成「回合」')
console.log('✓ 渲染：卡片 + 开关/延时 Pill/急停按钮/状态都在；用户删掉的说明与入口确实不在')

// --- 用例 3：点一下开关 → 发出的请求必须打到对的端点与字段 ---------------------
peek = installStubs({
	states: [state, '', false],
	fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ settings: state.settings, runtime: state.runtime }) })
})
await import('file://' + path + '?v=3')
const page2 = peek.registered().factory(peek.requireImpl)
const nodes2 = flatten(page2.__internals.ComputerUseSettings({ t: (key) => dicts.zh[key] || key }))
const switch2 = find(nodes2, 'Switch', (p) => p.label === '自动显示观察窗')
switch2.props.onChange(true)
nodes2.filter((n) => n.type === 'Pill').find((p) => p.props.children === '1 分钟').props.onClick()
await new Promise((resolve) => setTimeout(resolve, 5))
const urls = peek.calls.map((c) => c.url)
if (!urls.includes('/_computer-use/settings.json')) fail(`开关没有打到 settings.json：${urls.join(', ')}`)
if (urls.some((url) => url.includes('/panel'))) fail(`不该再往 /panel 发请求（那个端点已删）：${urls.join(', ')}`)
const patched = peek.calls.filter((c) => c.url === '/_computer-use/settings.json' && c.init?.method === 'POST').map((c) => JSON.parse(c.init.body))
if (!patched.some((body) => body.autoShow === true)) fail(`开关没有发出 {autoShow:true}：${JSON.stringify(patched)}`)
if (!patched.some((body) => body.autoHideSeconds === 60)) fail(`延时选项没有发出 {autoHideSeconds:60}：${JSON.stringify(patched)}`)
console.log('✓ 交互：开关 → POST settings.json{autoShow}；延时 Pill → {autoHideSeconds}')

// --- 用例 4：拿不到 host（非本机页面 / 路由没挂）时给提示，不崩 ----------------
peek = installStubs({ states: [null, 'HTTP 403', false] })
await import('file://' + path + '?v=4')
const page3 = peek.registered().factory(peek.requireImpl)
const degraded = flatten(page3.__internals.ComputerUseSettings({ t: (key) => dicts.zh[key] || key }))
if (!texts(degraded).includes('HTTP 403')) fail('拿不到状态时没有把原因显示出来')
console.log('✓ 降级：拿不到 host 状态时显示原因（页面不崩）')

// --- 用例 6：导航行图标 —— 平台没有图标位，页面自己在那一行上补一个 -----------------
//   壳的兜底齿轮要被藏起来、我们的图标要插在行首、卸载时要全部还原。
const dom = makeFakeDom()
globalThis.document = dom.document
globalThis.MutationObserver = dom.MutationObserver
// 状态里带 navIcon=滑块：组件一渲染就该把导航行重画成滑块（「不用重启试图标」的全部依据）
peek = installStubs()
await import('file://' + path + '?v=6')
const client6 = peek.registered().factory(peek.requireImpl)
const effects6 = []
let dicts6 = null
client6.apply({
	effect: (fn, label) => { effects6.push({ label, dispose: fn() }) },
	locale: { register: (ns, table) => { dicts6 = table }, bind: () => (key) => dicts6.zh[key] || key },
	slots: { inject: () => {}, register: () => {} }
})
// 补图标是防抖的（150ms），等一拍
await new Promise((resolve) => setTimeout(resolve, 220))
if (dom.gear.style.display !== 'none') fail('导航行的兜底齿轮没有被藏起来')
const holder = dom.row.children[0]
if (!holder || !holder.hasAttribute('data-cu-nav-icon')) fail('导航行行首没有插入我们的图标容器')
const iconRender = peek.rendered.find((r) => r.container === holder)
if (!iconRender) fail('图标容器里没有渲染任何东西')
const iconNodes = flatten(iconRender.node)
if (iconNodes[0]?.type !== 'svg') fail(`导航行里渲染的不是自绘图标：${iconNodes[0]?.type}`)
if (iconNodes[0].props.viewBox !== '0 0 16 16') fail(`自绘图标的 viewBox 不对：${iconNodes[0].props.viewBox}`)
if (iconNodes[0].props.strokeWidth !== 1.3) fail(`自绘图标没有用壳的线宽：${iconNodes[0].props.strokeWidth}`)
if (iconNodes.filter((n) => n.type === 'path').length !== 3) fail('自绘图标不是 3 条描边（机身 + 支颈 + 底座）')
if (iconRender.node.props.size !== 16) fail(`图标尺寸不对：${iconRender.node.props.size}`)
if (!dom.observers.some((o) => o.connected)) fail('没有挂上 MutationObserver（壳重渲染后就不会补了）')
// 自检上报：补上了要留一行（补不上时的现场也走同一条路，见用例 6c）
const navReports = peek.calls.filter((c) => c.url === '/_computer-use/nav-icon')
if (navReports.length !== 1) fail(`导航图标补上后应恰好上报一次，实际 ${navReports.length} 次`)
const navBody = JSON.parse(navReports[0].init.body)
if (navBody.outcome !== 'patched' || !String(navBody.detail).includes('computer-icon'))
	fail(`上报内容不对：${JSON.stringify(navBody)}`)
if (!String(navBody.detail).includes('电脑操控')) fail(`自检里没带本页 label：${navBody.detail}`)

// 卸载：齿轮复原、容器移除、观察器断开
const navEffect = effects6.find((e) => String(e.label).includes('nav icon'))
if (!navEffect) fail('导航行图标没有注册成 effect（插件卸载时收不掉）')
navEffect.dispose()
if (dom.gear.style.display !== '') fail('卸载后齿轮没有复原')
if (holder.isConnected) fail('卸载后图标容器还留在 DOM 里')
if (dom.observers.some((o) => o.connected)) fail('卸载后 MutationObserver 没有断开')
console.log('✓ 导航行图标：藏齿轮 → 行首插自绘显示器（16px / 3 条描边）→ 卸载全还原')

// --- 用例 6b：壳改了行高（严格匹配不中）时，宽松兜底仍然把图标补上 -----------------
const loose = makeFakeDom({ strict: false })
globalThis.document = loose.document
globalThis.MutationObserver = loose.MutationObserver
peek = installStubs()
await import('file://' + path + '?v=6b')
const client6b = peek.registered().factory(peek.requireImpl)
const effects6b = []
let dicts6b = null
client6b.apply({
	effect: (fn, label) => { effects6b.push({ label, dispose: fn() }) },
	locale: { register: (ns, table) => { dicts6b = table }, bind: () => (key) => dicts6b.zh[key] || key },
	slots: { inject: () => {}, register: () => {} }
})
await new Promise((resolve) => setTimeout(resolve, 220))
if (loose.gear.style.display !== 'none') fail('行高不是 40 时没有走宽松兜底（图标补不上）')
if (!loose.row.children[0]?.hasAttribute('data-cu-nav-icon')) fail('宽松兜底没有把图标插到行首')
// 宽松那一遍也不能见谁碰谁：文本一样但没有图标的按钮必须跳过
if (loose.decoyNoIcon.children.length !== 0) fail('宽松兜底碰了「文本一样但没有图标」的按钮')
for (const effect of effects6b) if (typeof effect.dispose === 'function') effect.dispose()
delete globalThis.document
delete globalThis.MutationObserver
console.log('✓ 导航行图标（宽松兜底）：壳改了行高也照样补上')

// --- 用例 6c：设置对话框开着却认不出那一行 → 把现场写进自检日志 ---------------------
//   （这一轮真事故：图标名字取错，页面上完全看不出区别；有这一行才能一眼看出原因）
const blind = makeFakeDom({ label: '别的文案', decoys: false })
globalThis.document = blind.document
globalThis.MutationObserver = blind.MutationObserver
peek = installStubs()
await import('file://' + path + '?v=6c')
const client6c = peek.registered().factory(peek.requireImpl)
const effects6c = []
let dicts6c = null
client6c.apply({
	effect: (fn, label) => { effects6c.push({ label, dispose: fn() }) },
	locale: { register: (ns, table) => { dicts6c = table }, bind: () => (key) => dicts6c.zh[key] || key },
	slots: { inject: () => {}, register: () => {} }
})
await new Promise((resolve) => setTimeout(resolve, 220))
const probes = peek.calls.filter((c) => c.url === '/_computer-use/nav-icon')
if (probes.length !== 1) fail(`认不出那一行时应当上报一次现场，实际 ${probes.length} 次`)
const probeBody = JSON.parse(probes[0].init.body)
if (probeBody.outcome !== 'probe') fail(`上报的 outcome 不对：${probeBody.outcome}`)
if (!String(probeBody.detail).includes('电脑操控')) fail(`现场里没有本页 label：${probeBody.detail}`)
if (blind.gear.style.display !== '') fail('没认出行却动了壳的图标')
for (const effect of effects6c) if (typeof effect.dispose === 'function') effect.dispose()
delete globalThis.document
delete globalThis.MutationObserver
console.log('✓ 导航行图标自检：认不出那一行时把现场（label/对话框数/按钮文本抽样）报给 host')

delete globalThis.document
delete globalThis.MutationObserver

// --- 用例 5：seed 词缺失 → 安静地不出现，绝不抛给宿主页面 ----------------------
peek = installStubs({ breakSeed: true })
await import('file://' + path + '?v=5')
let threw = null
let registeredSection = null
try {
	const broken = peek.registered().factory(peek.requireImpl)
	broken.apply({
		effect: (fn) => fn(),
		locale: { register: () => () => {}, bind: () => (key) => key },
		slots: { inject: (_n, cb) => cb(), register: (spec) => { registeredSection = spec } }
	})
} catch (error) {
	threw = error
}
if (threw) fail(`seed 词缺失时把异常抛给了宿主页面：${threw}`)
if (registeredSection) fail('seed 词缺失却仍然注册了设置页')
console.log('✓ seed 词缺失时安静降级（宿主页面不受影响）')

console.log('✓ 设置页冒烟全部通过（不碰真实偏好：所有请求都打在假 fetch 上）')
