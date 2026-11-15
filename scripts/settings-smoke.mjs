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
	const requireImpl = (id) => {
		if (breakSeed) throw new Error('no such seed word')
		if (id === 'react') return react
		if (id === '@deepseek-ai/dsh-client-ui-primitives') return prims
		// 这一半区只 require 这两个 seed 词。0.2.2 起图标走官方槽位，`react-dom/client`
		// 随旧的 DOM 补丁一起删了 —— 这里抛错就是「有人又把补丁加回来了」。
		throw new Error('unexpected require: ' + id)
	}
	return { registered: () => registered, requireImpl, calls }
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
 * 来源：`/Applications/DeepSeek Harness.app/Contents/Resources/app/dsh/node_modules/
 * @deepseek-ai/dsh-web-frontend/dist/assets/index-*.js` 里
 * `Object.freeze(Object.defineProperty({__proto__:null, …}))` 那一段的键名。桌面版把 `/assets`
 * 与所有 `@deepseek-ai/*` 客户端包都从 asar 里发 —— **profile 里那份同名包根本不会被读**
 * （实测 atime 停在安装那天，而页面每次启动都会换 bundle 版本号）。
 *
 * **本表按 `dsh@0.2.2-rc.3` 的 asar 那份重新生成**：320 个导出（0.2.0-rc.2 那版是 282 个）。
 * 换 dsh 版本后照同样办法重新生成，别凭 README 或 profile 里的包猜 —— 第一版就抄错了来源：
 * `IconGaugeOutline16` 看着「在表里」，真机上根本没有这个名字（真名 `IconGaugeOutlineMedium`，
 * 两份构建的命名与 tree-shaking 范围都不一样），导航图标怎么改都不生效。
 */
const RUNTIME_PRIMITIVES = new Set([
	'AnimatedRows', 'BrandWordmark', 'Button', 'CODE_HIGHLIGHT_EXTENSIONS', 'Checkbox', 'CodeBlock', 'CommandText', 'ConnectionIndicator',
	'DEFAULT_DIFF_MAX_LINES', 'DEFAULT_READ_MAX_LINES', 'DEFAULT_SEARCH_MAX_LINES', 'DEFAULT_TERMINAL_MAX_LINES', 'DOCUMENT_ZOOM', 'DetailDocument', 'DiffBlock', 'DisclosureRow',
	'DraggableImage', 'FISH_LOGO_PATH', 'FISH_LOGO_VIEWBOX', 'FenceCodeBlock', 'FenceMathBlock', 'FileTypeIcon', 'FishLogo', 'GuideArtworkBrowser',
	'GuideArtworkFiles', 'HoverCard', 'HoverCardGroup', 'ICON_MEDIUM_STROKE', 'ICON_REGULAR_STROKE', 'ICON_SYMBOLS', 'IconAgentPresetOutlineMedium', 'IconAgentPresetOutlineRegular',
	'IconAlarmClockOutlineMedium', 'IconAlarmClockOutlineRegular', 'IconApiOutlineMedium', 'IconApiOutlineRegular', 'IconAppsAddOutlineMedium', 'IconAppsAddOutlineRegular', 'IconArchiveCheckOutlineMedium', 'IconArchiveCheckOutlineRegular',
	'IconArchiveOffOutlineMedium', 'IconArchiveOffOutlineRegular', 'IconArchiveOutlineMedium', 'IconArchiveOutlineRegular', 'IconBranchOutlineMedium', 'IconBranchOutlineRegular', 'IconBrowseOutlineMedium', 'IconBrowseOutlineRegular',
	'IconCheckCircleFillMedium', 'IconCheckCircleFillRegular', 'IconCheckCircleOutlineMedium', 'IconCheckCircleOutlineRegular', 'IconCheckOutlineMedium', 'IconCheckOutlineRegular', 'IconChecklistOutlineMedium', 'IconChecklistOutlineRegular',
	'IconChevronDownOutlineMedium', 'IconChevronDownOutlineRegular', 'IconChevronLeftOutlineMedium', 'IconChevronLeftOutlineRegular', 'IconChevronRightOutlineMedium', 'IconChevronRightOutlineRegular', 'IconChevronUpOutlineMedium', 'IconChevronUpOutlineRegular',
	'IconChevronsUpDownOutlineMedium', 'IconChevronsUpDownOutlineRegular', 'IconClockOutlineMedium', 'IconClockOutlineRegular', 'IconCloseCircleFillMedium', 'IconCloseCircleFillRegular', 'IconCloseFillMedium', 'IconCloseFillRegular',
	'IconCloseOutlineMedium', 'IconCloseOutlineRegular', 'IconCodeOutlineMedium', 'IconCodeOutlineRegular', 'IconCompactOutlineMedium', 'IconCompactOutlineRegular', 'IconCompareSplitOutlineMedium', 'IconCompareSplitOutlineRegular',
	'IconContextInjectionOutlineMedium', 'IconContextInjectionOutlineRegular', 'IconCopyOutlineMedium', 'IconCopyOutlineRegular', 'IconCordisPluginOutlineMedium', 'IconCordisPluginOutlineRegular', 'IconDarkOutlineMedium', 'IconDarkOutlineRegular',
	'IconDataOutlineMedium', 'IconDataOutlineRegular', 'IconDatabaseOutlineMedium', 'IconDatabaseOutlineRegular', 'IconDeliverDocMedium', 'IconDeliverDocRegular', 'IconDislikeFillMedium', 'IconDislikeFillRegular',
	'IconDislikeOutlineMedium', 'IconDislikeOutlineRegular', 'IconDownloadOutlineMedium', 'IconDownloadOutlineRegular', 'IconDragHandleOutlineMedium', 'IconDragHandleOutlineRegular', 'IconEditOutlineMedium', 'IconEditOutlineRegular',
	'IconEllipsisOutlineMedium', 'IconEllipsisOutlineRegular', 'IconEnhanceOutlineMedium', 'IconEnhanceOutlineRegular', 'IconEyeOffOutlineMedium', 'IconEyeOffOutlineRegular', 'IconEyeOutlineMedium', 'IconEyeOutlineRegular',
	'IconFlatListOutlineMedium', 'IconFlatListOutlineRegular', 'IconFolderCloseMedium', 'IconFolderCloseRegular', 'IconFolderOpenMedium', 'IconFolderOpenOutlineMedium', 'IconFolderOpenOutlineRegular', 'IconFolderOpenRegular',
	'IconFollowsystemOutlineMedium', 'IconFollowsystemOutlineRegular', 'IconFullscreenOutlineMedium', 'IconFullscreenOutlineRegular', 'IconGaugeOutlineMedium', 'IconGaugeOutlineRegular', 'IconGlobeOutlineMedium', 'IconGlobeOutlineRegular',
	'IconGoalOutlineMedium', 'IconGoalOutlineRegular', 'IconInfoOutlineMedium', 'IconInfoOutlineRegular', 'IconInspectOutlineMedium', 'IconInspectOutlineRegular', 'IconLayersOutlineMedium', 'IconLayersOutlineRegular',
	'IconLightOutlineMedium', 'IconLightOutlineRegular', 'IconLikeFillMedium', 'IconLikeFillRegular', 'IconLikeOutlineMedium', 'IconLikeOutlineRegular', 'IconLinkOutlineMedium', 'IconLinkOutlineRegular',
	'IconListPenOutlineMedium', 'IconListPenOutlineRegular', 'IconLoadingOutlineMedium', 'IconLoadingOutlineRegular', 'IconMicrophoneOutlineMedium', 'IconMicrophoneOutlineRegular', 'IconNewChatOutlineMedium', 'IconNewChatOutlineRegular',
	'IconNowrapFillMedium', 'IconNowrapFillRegular', 'IconPanelLeftOutlineMedium', 'IconPanelLeftOutlineRegular', 'IconPaperPlaneOutlineMedium', 'IconPaperPlaneOutlineRegular', 'IconPaperclipOutlineMedium', 'IconPaperclipOutlineRegular',
	'IconPauseOutlineMedium', 'IconPauseOutlineRegular', 'IconPersonalizationOutlineMedium', 'IconPersonalizationOutlineRegular', 'IconPinFillMedium', 'IconPinFillRegular', 'IconPinOutlineMedium', 'IconPinOutlineRegular',
	'IconPlanOutlineMedium', 'IconPlanOutlineRegular', 'IconPlayOutlineMedium', 'IconPlayOutlineRegular', 'IconPluginPinwheelOutlineMedium', 'IconPluginPinwheelOutlineRegular', 'IconPlusOutlineMedium', 'IconPlusOutlineRegular',
	'IconProjectAddOutlineMedium', 'IconProjectAddOutlineRegular', 'IconQuestionOutlineMedium', 'IconQuestionOutlineRegular', 'IconQueueOutlineMedium', 'IconQueueOutlineRegular', 'IconRefreshOutlineMedium', 'IconRefreshOutlineRegular',
	'IconRightUpOutlineMedium', 'IconRightUpOutlineRegular', 'IconSaveOutlineMedium', 'IconSaveOutlineRegular', 'IconSearchOutlineMedium', 'IconSearchOutlineRegular', 'IconSendOutlineMedium', 'IconSendOutlineRegular',
	'IconSettingsOutlineMedium', 'IconSettingsOutlineRegular', 'IconShareOutlineMedium', 'IconShareOutlineRegular', 'IconShieldOutlineMedium', 'IconShieldOutlineRegular', 'IconSkillOutlineMedium', 'IconSkillOutlineRegular',
	'IconSlidersTwoOutlineMedium', 'IconSlidersTwoOutlineRegular', 'IconSparkleMedium', 'IconSparkleRegular', 'IconStopFillMedium', 'IconStopFillRegular', 'IconThinkOutlineMedium', 'IconThinkOutlineRegular',
	'IconTrashOutlineMedium', 'IconTrashOutlineRegular', 'IconTreeCornerMedium', 'IconTreeCornerRegular', 'IconTriangleRightFillMedium', 'IconTriangleRightFillRegular', 'IconUnarchiveOutlineMedium', 'IconUnarchiveOutlineRegular',
	'IconUploadOutlineMedium', 'IconUploadOutlineRegular', 'IconUserOutlineMedium', 'IconUserOutlineRegular', 'IconUsersOutlineMedium', 'IconUsersOutlineRegular', 'IconWarningOutlineMedium', 'IconWarningOutlineRegular',
	'IconWarningTriangleOutlineMedium', 'IconWarningTriangleOutlineRegular', 'IconWorkspaceTreeOutlineMedium', 'IconWorkspaceTreeOutlineRegular', 'IconWrapFillMedium', 'IconWrapFillRegular', 'IconWrapLinesOutlineMedium', 'IconWrapLinesOutlineRegular',
	'ImageLightbox', 'InlineEditor', 'Input', 'JsonBlock', 'JsonTree', 'LinkIconMedium', 'LinkIconRegular', 'MENU_TRANSITION_MS',
	'MarkdownDelegateProvider', 'MarkdownText', 'Menu', 'MenuGroup', 'MenuItemButton', 'MenuSurface', 'Modal', 'PathLabel',
	'PermissionIconAutoReviewMedium', 'PermissionIconAutoReviewRegular', 'PermissionIconFullAccessMedium', 'PermissionIconFullAccessRegular', 'PermissionIconReadOnlyMedium', 'PermissionIconReadOnlyRegular', 'PermissionIconWorkspaceWriteMedium', 'PermissionIconWorkspaceWriteRegular',
	'Pill', 'PluginArtworkDefault', 'PluginArtworkLoop', 'PluginArtworkSearch', 'PluginArtworkSubagent', 'PluginArtworkTerminal', 'QuotePreview', 'ReadBlock',
	'ReferenceChip', 'ReferenceIconMedium', 'ReferenceIconRegular', 'RiskConfirmation', 'SHIELD_OUTLINE_PATH', 'SearchBlock', 'SegmentedControl', 'SegmentedTabs',
	'SettingsForm', 'SettingsFormModel', 'SettingsSecretField', 'SettingsValueField', 'ShortcutKeys', 'StateDot', 'Switch', 'Tag',
	'TerminalBlock', 'TextShimmer', 'Toast', 'Tooltip', 'UserMessageBubble', 'UserTextRun', 'WebBlock', 'ZoomControls',
	'classifyFileType', 'classifyLinkPath', 'closeTopModal', 'detailDocumentState', 'diffTotals', 'extractMarkdownPlainText', 'fileExtension', 'fileSizeText',
	'focusWithoutRing', 'formatQuoteText', 'isBehindModal', 'isDarwinDesktop', 'isMobileDevice', 'languageForPath', 'modalSelector', 'observeComposition',
	'observeStickyMenuGroups', 'pointerModality', 'rankByName', 'relativeTime', 'settingsNumberField', 'settingsTextField', 'useAnchoredMaxHeight', 'useClipboardCopy',
	'useCodeHighlighter', 'useDetailDocumentLabels', 'useDismissOnOutsidePointer', 'useMarkdownDelegate', 'useModalLayer', 'usePointerGrace', 'useRowDragPreview', 'writeClipboard',
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
const glyph = /const ComputerGlyph = \(\{ size = 16 \}\) =>[\s\S]*?\n\n/.exec(source)?.[0] ?? ''
if (!glyph) fail('源码里找不到 ComputerGlyph？导航行图标的守卫失效了')
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
// 0.2.2 起图标走官方槽位：旧的 DOM 补丁必须**真的删干净**（两套并存时，删掉的那套只是没人
// 调用，却仍然让人以为「还要自己补图标」）。这里按代码形状查，注释里提到这些词不算。
for (const [what, pattern] of [
	['改用官方槽位后不再 require react-dom', /require\('react-dom/],
	['DOM 补丁', /new MutationObserver/],
	['DOM 补丁的入口', /function installNavIcon/],
	['DOM 补丁插入的标记', /data-cu-nav-icon/],
	['自检上报端点', /\/nav-icon/]
]) {
	if (pattern.test(source)) fail(`旧的导航图标 DOM 补丁没删干净：${what}（${pattern}）`)
}

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
let icon = null
const ctx = {
	effect: (fn) => fn(),
	locale: {
		register: (_ns, value) => { dicts = value; return () => {} },
		bind: () => (key) => (dicts && dicts.zh && dicts.zh[key]) || key
	},
	slots: {
		// 这一半区只进两个槽位：设置页本身 + 它的导航行图标（0.2.2 起的官方图标位）
		inject: (name, callback) => {
			if (name !== 'settings.section' && name !== 'settings.section.icon') fail(`inject 了错的槽位：${name}`)
			callback()
		},
		register: (spec, component) => {
			if (spec.name === 'settings.section.icon') icon = { spec, component }
			else section = { spec, component }
			return () => {}
		}
	}
}
clientExports.apply(ctx)
if (!section) fail('apply 没有注册设置页')
if (section.spec.name !== 'settings.section' || section.spec.id !== 'dsh-computer-use') fail(`注册的槽位/键不对：${JSON.stringify(section.spec)}`)
if (section.spec.order !== 2) fail(`order 期望 2（通用/手机访问之后），实际 ${section.spec.order}`)
if (section.spec.label() !== '电脑操控') fail(`label 没有走词典：${section.spec.label()}`)
if (dicts.en?.section !== 'Computer use') fail('英文词典缺失或键不对')
// 导航行图标：壳按 `only: row.id` 取，所以注册 id 必须**等于** section 的 id，否则永远不渲染
if (!icon) fail('apply 没有注册导航行图标（settings.section.icon）')
if (icon.spec.id !== section.spec.id)
	fail(`图标注册 id 必须等于 section 的 id：${icon.spec.id} vs ${section.spec.id}`)
console.log(`✓ 注册 settings.section：id=${section.spec.id} order=${section.spec.order} label=${section.spec.label()}；图标进 settings.section.icon（同 id）`)

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

// --- 用例 6：导航行图标 = 官方槽位 settings.section.icon（0.2.2 起） ----------------
//   壳的渲染是 `renderSlot('settings.section.icon', {size:16, active}, {only: row.id, fallback: navIcon(row.id)})`，
//   所以这一半区只要交一个「拿 size 画字形」的组件。这里钉住：尺寸跟 size 走、颜色走 currentColor
//   （选中态/深色主题由壳的 color 继承下来）、线宽与壳的图标同粗细、形状是那台显示器。
const glyphNodes = flatten(page.__internals.ComputerGlyph({ size: 16, active: true }))
const svg = glyphNodes[0]
if (!svg || svg.type !== 'svg') fail(`图标没有渲染成 svg：${svg?.type}`)
if (svg.props.width !== 16 || svg.props.height !== 16)
	fail(`图标尺寸没有跟 size 走：${svg.props.width}×${svg.props.height}`)
if (page.__internals.ComputerGlyph({ size: 20 }).props.width !== 20) fail('size=20 时图标没有跟着放大')
if (svg.props.viewBox !== '0 0 16 16') fail(`图标的 viewBox 不对：${svg.props.viewBox}`)
if (svg.props.fill !== 'none') fail('图标应当是 fill: none 的描边图')
if (svg.props.strokeWidth !== 1.3) fail(`图标没有用壳的线宽（ICON_MEDIUM_STROKE）：${svg.props.strokeWidth}`)
if (svg.props['aria-hidden'] !== 'true') fail('图标没有标 aria-hidden（壳的按钮自己带无障碍名）')
const paths = glyphNodes.filter((n) => n.type === 'path')
if (paths.length !== 3) fail(`图标应是 3 条描边（机身 + 支颈 + 底座），实际 ${paths.length} 条`)
for (const stroke of paths) {
	if (stroke.props.stroke !== 'currentColor')
		fail(`图标的描边没有走 currentColor（选中态/深色主题就跟不上了）：${stroke.props.stroke}`)
}
console.log('✓ 导航行图标：官方槽位组件按 size 画 16px 自绘显示器（currentColor / 线宽 1.3 / 3 条描边）')

// --- 用例 6b：老壳没有这个槽位（inject 直接抛 / register 抛）→ 这一页照常出现 -------
//   图标位是 0.2.2 才有的：更老的 dsh 上 `settings.section.icon` 根本不存在。这条钉住
//   「失败只等于没有自定义图标」—— 设置页与其它注册必须照旧完成，且绝不把异常抛给宿主。
let seen = null
for (const [what, slots] of [
	['inject 抛', {
		inject: (name, callback) => {
			if (name === 'settings.section.icon') throw new Error('unknown slot')
			callback()
		},
		register: (spec) => { seen = spec; return () => {} }
	}],
	['register 抛', {
		inject: (_name, callback) => callback(),
		register: (spec) => {
			if (spec.name === 'settings.section.icon') throw new Error('slot is not declared')
			seen = spec
			return () => {}
		}
	}]
]) {
	peek = installStubs()
	await import('file://' + path + `?v=6b-${what}`)
	const client6b = peek.registered().factory(peek.requireImpl)
	seen = null
	let thrown = null
	try {
		client6b.apply({
			effect: (fn) => fn(),
			locale: { register: () => () => {}, bind: () => (key) => key },
			slots
		})
	} catch (error) {
		thrown = error
	}
	if (thrown) fail(`${what} 时把异常抛给了宿主页面：${thrown}`)
	if (!seen) fail(`${what} 时设置页也没注册上（失败只该等于没有自定义图标）`)
	if (seen.name !== 'settings.section') fail(`${what} 时注册的槽位不对：${seen.name}`)
}
console.log('✓ 没有图标槽位（老壳）时安静降级：设置页照常注册，异常不外抛')

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
