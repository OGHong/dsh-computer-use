/**
 * `dsh-computer-use` — 浏览器半区：设置页「电脑操控」。
 *
 * 这一半区只做一件事：在 **设置 → 电脑操控** 里给用户一个找得到的开关。观察窗本身只有两个
 * 键（右上 × = 本回合不再显示、左上 ⏹ = 急停），而「以后别再自动出现」以前只有 Agent 的
 * `preview(enable=false)` 能写 —— 用户想恢复也没有入口（README 里那条真事故：随手点一下 ×
 * 就等于打开了一个他再也关不掉的开关）。这一页承担：
 *
 *   * 观察窗总开关（以后还自不自动出现）、回合结束后多久收起；
 *   * 急停 / 恢复（与浮窗上的 ⏹、MCP 的 stop 工具写的是同一个文件）；
 *   * 观察窗当前状态与最近一次电脑操作；
 *   * 首次使用要开的两项系统权限（含一键打开系统设置面板）。
 *
 * 另外顺手补一件事：设置导航里「电脑操控」那一行的图标（平台没有图标位，见 `installNavIcon`）。
 *
 * 观察窗那两个临时动作（右上 × = 本回合不再显示、左上 ⏹ = 急停）留在窗口自己身上：
 * 设置页不放它们的镜像入口（用户 2026-10-12 的要求），只保留**粘性**的那个总开关。
 *
 * 版式：三张卡片（观察窗 / 控制 / 权限），控件全部来自官方 primitives（`Switch` /
 * `Pill` / `Button` / `StateDot`），卡片本身用主题 token 手写 —— 与「通用」设置页
 * 和 dsh-pocket 的「手机访问」同一套观感（背景层 + 1px 描边 + 12 圆角），字号按用户要求比官方
 * 设置行大一档（行标题 15 / 说明 13）。
 *
 * 与 Host 半区（`lib/index.js`）的契约只有同源 HTTP：
 *
 *   GET  /_computer-use/settings.json   → { settings, runtime, choices, paths }
 *   POST /_computer-use/settings.json   → 改偏好（部分字段即可），回同一份形状
 *   POST /_computer-use/permissions     → 打开系统设置的辅助功能面板
 *
 * 走同源 fetch 而不是 RPC：这些路由本来就在页面自己的 origin 上，而 `connection.admit`
 * 已经守住了那道门（与 `/stream`、`/status.json` 同一条路）。
 *
 * 为什么是**手写**的 bundle（没有构建步骤）：这一半区只有一页，用不上打包器；而它必须与 dsh
 * 的模块表对齐（seed 词表 + `window.__ModuleLoader__.load` 协议）。dsh 改了 seed 词或协议时，
 * 下面的 try/catch 会让这一页安静地不出现，绝不把宿主页面一起带崩 —— 沿用已退休卡片的容错原则：
 * seed 词、fetch、locale、slots 每一个都必须可失败。
 *
 * @module dsh-computer-use/client
 */

const MODULE_ID = 'dsh-computer-use'
const CHANNEL = '/_computer-use'
const NS = 'dsh-computer-use'
/** 状态轮询间隔：这一页开着时才知道浮窗是起来了还是收起了。一个 GET，不碰抓屏。 */
const POLL_MS = 2000

const ZH = {
  section: '电脑操控',
  intro: '控制 Agent 的屏幕观察与鼠标键盘操作（computer-use 工具）。此处设置仅影响观察窗口的显示，不影响 Agent 操作电脑的能力。',
  groupPanel: '观察窗',
  autoShow: '自动显示观察窗',
  autoShowDesc: '关闭后不再自动出现，也不再占用屏幕录制与 CPU；Agent 仍可正常操作电脑，仅不再显示画面。',
  autoHide: '回合结束后自动收起',
  autoHideDesc: '回合结束后，经过所选时长自动收起；回合进行中始终显示。',
  seconds: '{n} 秒',
  minutes: '{n} 分钟',
  groupControl: '控制',
  estop: '急停',
  estopOn: '已停止：动作类工具会立即返回错误，直到恢复。',
  estopOff: '当前正常。急停会在下一个可中断点（≤0.1 秒）停止 Agent 的动作，与观察窗左上角的 ⏹ 等效。',
  stop: '急停',
  estopReason: '用户从设置页按了急停',
  resume: '恢复',
  statusUp: '运行中（显示中）',
  statusHidden: '运行中（已收起）',
  statusDown: '未运行（下次操作时自动启动）',
  statusOff: '已关闭（不再自动出现）',
  statusKilled: '已停止',
  statusLastAction: '最近一次电脑操作',
  agoNever: '无记录',
  agoNow: '刚刚',
  agoSeconds: '{n} 秒前',
  agoMinutes: '{n} 分钟前',
  agoHours: '{n} 小时前',
  groupPerm: '权限',
  permBody: '首次使用需在「系统设置 → 隐私与安全性」中为 DeepSeek Harness 开启两项权限：「辅助功能」（控制鼠标键盘）与「屏幕录制」（截屏）。未授权时点击无效、截图为黑屏。',
  openPerm: '打开辅助功能设置',
  loading: '正在读取状态…',
  unreachable: '无法读取状态（{error}）。请从本机 dsh 页面打开此设置。'
}

const EN = {
  section: 'Computer use',
  intro: 'Controls the agent’s screen observation and mouse/keyboard operation (the computer-use tools). These settings affect only the observation window; they do not limit what the agent may do.',
  groupPanel: 'Observation window',
  autoShow: 'Show the observation window automatically',
  autoShowDesc: 'When off, the window no longer appears automatically and no longer uses screen recording or CPU. The agent keeps working; the picture is simply not shown.',
  autoHide: 'Auto-hide after a turn ends',
  autoHideDesc: 'Hide the window this long after a turn ends; it stays visible while a turn is running.',
  seconds: '{n}s',
  minutes: '{n} min',
  groupControl: 'Controls',
  estop: 'Emergency stop',
  estopOn: 'Stopped: action tools fail immediately until you resume.',
  estopOff: 'Normal. The stop halts the agent’s next action at its next interruptible point (≤0.1s); equivalent to the ⏹ in the window corner.',
  stop: 'Stop',
  estopReason: 'stopped from the computer-use settings page',
  resume: 'Resume',
  statusUp: 'Running (visible)',
  statusHidden: 'Running (hidden)',
  statusDown: 'Not running (starts on the next action)',
  statusOff: 'Disabled (no longer appears automatically)',
  statusKilled: 'Stopped',
  statusLastAction: 'Last computer action',
  agoNever: 'none',
  agoNow: 'just now',
  agoSeconds: '{n}s ago',
  agoMinutes: '{n} min ago',
  agoHours: '{n} h ago',
  groupPerm: 'Permissions',
  permBody: 'Two system permissions are required the first time: in System Settings → Privacy & Security, enable “Accessibility” (mouse and keyboard) and “Screen Recording” (screenshots) for DeepSeek Harness. Without them, clicks have no effect and screenshots are black.',
  openPerm: 'Open Accessibility settings',
  loading: 'Reading status…',
  unreachable: 'Cannot read status ({error}). Open this page from the local dsh interface.'
}

window.__ModuleLoader__?.load({
  id: MODULE_ID,
  factory: (require) => {
    const module = { exports: {} }
    const exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    // 任何一个 seed 词被改名/缺失都不能把异常抛给宿主：拿不到就不挂这一页。
    // `@deepseek-ai/dsh-client-ui-primitives` 是平台 seed 词（开关/分段控件/按钮/状态点都从它来，
    // 样式随壳的样式表一起加载），所以这里不需要 CSS 也不引入额外依赖。
    let React = null
    let prims = null
    let ReactDOM = null
    try {
      React = require('react')
      prims = require('@deepseek-ai/dsh-client-ui-primitives')
    } catch {
      React = null
      prims = null
    }
    // `react-dom/client` 只为下面那个「导航行图标」用：拿不到就不补图标，这一页照常。
    try {
      ReactDOM = require('react-dom/client')
    } catch {
      ReactDOM = null
    }

    const h = React ? React.createElement : null

    /** 取 key → 替换 {占位符} → 字符串（与 dsh-pocket 同一套，不依赖 t() 的插值能力）。 */
    const fmt = (t, key, vars) => {
      let text = t(key)
      if (vars) {
        for (const [name, value] of Object.entries(vars)) {
          text = String(text).split(`{${name}}`).join(String(value))
        }
      }
      return text
    }

    const ago = (t, ms) => {
      if (!ms) return t('agoNever')
      const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000))
      if (seconds < 5) return t('agoNow')
      if (seconds < 60) return fmt(t, 'agoSeconds', { n: seconds })
      if (seconds < 3600) return fmt(t, 'agoMinutes', { n: Math.round(seconds / 60) })
      return fmt(t, 'agoHours', { n: Math.round(seconds / 3600) })
    }

    /** 收起延时的显示文案：整分钟按分钟说（120 秒 = 2 分钟，比「120 秒」好读）。 */
    const delayLabel = (t, seconds) =>
      seconds >= 60 && seconds % 60 === 0 ? fmt(t, 'minutes', { n: seconds / 60 }) : fmt(t, 'seconds', { n: seconds })

    /**
     * 版式：卡片 = 背景层 + 1px 描边 + 12 圆角（与「通用」设置页、dsh-pocket 的设置页同一套观感），
     * 卡片内每行一条分隔线。字号比官方设置行大一档（用户 2026-10-12 要求「字稍微调大一点」）：
     * 行标题 15/22、说明 13/20、卡片标题 14/600、状态行 14。
     */
    const styles = {
      root: { maxWidth: 640, fontSize: 14, color: 'var(--dsw-alias-label-primary)', paddingBottom: 8 },
      intro: { color: 'var(--dsw-alias-label-secondary)', fontSize: 13, lineHeight: '20px' },
      card: {
        marginTop: 16,
        background: 'var(--dsw-alias-bg-layer-1)',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 12,
        padding: '2px 18px'
      },
      cardTitle: { margin: '16px 0 2px', fontSize: 14, fontWeight: 600, lineHeight: '20px' },
      divider: { borderBottom: '1px solid var(--dsw-alias-border-l2)' },
      row: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 20, padding: '15px 0' },
      rowStacked: { display: 'flex', flexDirection: 'column', gap: 12, padding: '15px 0' },
      rowTitle: { fontSize: 15, lineHeight: '22px' },
      rowDesc: { marginTop: 4, fontSize: 13, lineHeight: '20px', color: 'var(--dsw-alias-label-secondary)' },
      rowActions: { display: 'flex', alignItems: 'center', gap: 8, flex: 'none' },
      pillRow: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
      statusRow: { display: 'flex', alignItems: 'center', gap: 8, padding: '14px 0 16px', fontSize: 14 },
      statusValue: { display: 'inline-flex', alignItems: 'center', gap: 7 },
      statusAgo: { marginLeft: 'auto', color: 'var(--dsw-alias-label-secondary)', fontSize: 13 },
      muted: { color: 'var(--dsw-alias-label-secondary)', fontSize: 13, lineHeight: '20px' },
      error: { color: 'var(--dsw-alias-state-error-primary)', fontSize: 13, lineHeight: '20px', marginTop: 10 }
    }

    /** 一张卡片：标题 + 若干行（卡片最后一行用 `divider={false}` 收尾）。 */
    const Card = ({ title, children }) =>
      h('section', { style: styles.card }, h('h3', { style: styles.cardTitle }, title), children)

    const Row = ({ title, description, control, stacked = false, divider = true }) =>
      h(
        'div',
        { style: { ...(stacked ? styles.rowStacked : styles.row), ...(divider ? styles.divider : null) } },
        h(
          'div',
          { style: { minWidth: 0, flex: stacked ? undefined : 1 } },
          h('div', { style: styles.rowTitle }, title),
          description ? h('div', { style: styles.rowDesc }, description) : null
        ),
        h('div', { style: styles.rowActions }, control)
      )

    /** 观察窗状态那一行的人话 + 状态点颜色（绿=在屏上，灰=收起了/没起来，黄=急停）。 */
    const panelStatusText = (t, runtime) => {
      if (runtime.killed) return 'statusKilled'
      if (!runtime.panelUp) return runtime.hidden === 'off' ? 'statusOff' : 'statusDown'
      return runtime.panelVisible ? 'statusUp' : 'statusHidden'
    }

    function ComputerUseSettings(props) {
      // 注入面没到（槽位换了 owner 实现）时退回 key 本身：宁可显示英文 key 也不整页崩掉
      const t = props.t || ((key) => key)
      const [data, setData] = React.useState(null)
      const [error, setError] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      // 轮询撞上用户刚点的那一下会把旧状态盖回来：busy 期间跳过这一拍
      const busyRef = React.useRef(false)
      busyRef.current = busy

      const refresh = React.useCallback(async () => {
        try {
          const res = await fetch(`${CHANNEL}/settings.json`, {
            cache: 'no-store',
            headers: { accept: 'application/json' }
          })
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          setData(await res.json())
          setError('')
        } catch (err) {
          setError(String((err && err.message) || err))
        }
      }, [])

      React.useEffect(() => {
        refresh()
        const timer = setInterval(() => {
          if (!busyRef.current) refresh()
        }, POLL_MS)
        return () => clearInterval(timer)
      }, [refresh])

      /** 发一个动作；响应是状态形状就直接采用，否则（急停/权限）再拉一次。 */
      const send = async (path, body) => {
        setBusy(true)
        try {
          const res = await fetch(CHANNEL + path, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(body || {})
          })
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          const json = await res.json().catch(() => null)
          if (json && json.settings) setData(json)
          else await refresh()
          setError('')
        } catch (err) {
          setError(String((err && err.message) || err))
        } finally {
          setBusy(false)
        }
      }

      if (!data) {
        return h(
          'div',
          { style: styles.root },
          error
            ? h('div', { style: { ...styles.error, marginTop: 0 } }, fmt(t, 'unreachable', { error }))
            : h('div', { style: styles.muted }, t('loading'))
        )
      }

      const settings = data.settings || {}
      const runtime = data.runtime || {}
      const choices = Array.isArray(data.choices) && data.choices.length ? data.choices : [5, 12, 30, 60, 120]
      const disabled = busy === true

      return h(
        'div',
        { style: styles.root },
        h('div', { style: styles.intro }, t('intro')),

        error ? h('div', { style: styles.error }, fmt(t, 'unreachable', { error })) : null,

        h(
          Card,
          { title: t('groupPanel') },
          h(Row, {
            title: t('autoShow'),
            description: t('autoShowDesc'),
            control: h(prims.Switch, {
              checked: settings.autoShow !== false,
              disabled,
              label: t('autoShow'),
              onChange: (next) => send('/settings.json', { autoShow: next })
            })
          }),
          // 选项单独占一行、横向排开：比塞在行尾的下拉更好点也更清楚。
          // 用 `Pill`（一排各自独立的 chip）而不是 `SegmentedControl`：两者运行时都有，
          // 但分段控件会撑出一整块带滑块的容器，在这一行里比 chip 重。
          h(Row, {
            stacked: true,
            title: t('autoHide'),
            description: t('autoHideDesc'),
            control: h(
              'div',
              { style: styles.pillRow, role: 'group', 'aria-label': t('autoHide') },
              choices.map((seconds) =>
                h(
                  prims.Pill,
                  {
                    key: seconds,
                    active: Number(settings.autoHideSeconds || 12) === seconds,
                    disabled,
                    onClick: () => send('/settings.json', { autoHideSeconds: seconds })
                  },
                  delayLabel(t, seconds)
                )
              )
            )
          }),
          h(
            'div',
            { style: styles.statusRow },
            h(
              'span',
              { style: styles.statusValue },
              h(prims.StateDot, { state: runtime.killed ? 'warning' : runtime.panelVisible ? 'done' : 'idle' }),
              h('span', null, t(panelStatusText(t, runtime)))
            ),
            h('span', { style: styles.statusAgo }, `${t('statusLastAction')}：${ago(t, runtime.lastActionAt)}`)
          )
        ),

        h(
          Card,
          { title: t('groupControl') },
          h(Row, {
            divider: false,
            title: t('estop'),
            description: runtime.killed ? t('estopOn') : t('estopOff'),
            control: h(
              prims.Button,
              {
                size: 'sm',
                variant: runtime.killed ? 'primary' : 'outline',
                disabled,
                // 急停的路由从 **query** 里读原因（它本来是个 GET 端点），所以原因挂在路径上；
                // 恢复走 /resume，不带参数。
                onClick: () =>
                  send(runtime.killed ? '/resume' : `/stop?reason=${encodeURIComponent(t('estopReason'))}`, {})
              },
              runtime.killed ? t('resume') : t('stop')
            )
          })
        ),

        h(
          Card,
          { title: t('groupPerm') },
          h('div', { style: { ...styles.muted, padding: '8px 0 0' } }, t('permBody')),
          h(
            'div',
            { style: { padding: '14px 0 18px' } },
            h(prims.Button, { size: 'sm', variant: 'outline', disabled, onClick: () => send('/permissions', {}) }, t('openPerm'))
          )
        )
      )
    }

    /**
     * 设置导航里那一行的图标 —— 平台没有图标位，所以由这一半区自己补。
     *
     * 导航图标是壳里写死的一张 `id → 图标` 表（未命中就兜底成齿轮），而设置页注册项只有
     * `id` / `order` / `label`。那张表在**应用自带的 asar 里**：桌面版把 `/assets` 与所有
     * `@deepseek-ai/*` 客户端包都从 `app.asar/dsh` 发（profile 里那份同名包根本不会被读，
     * 实测 atime 停在安装那天），而 asar 是签名的一部分，改不得。所以只剩一条路：在页面里给
     * 那一行补图标 —— 把壳的兜底齿轮藏起来，在它前面放上我们自己的图标。
     *
     * 三条克制原则：
     *
     *   * **只认结构，不认壳的哈希类名**：对话框（`[role=dialog]`）里、文本完全等于本页
     *     label、第一个子元素是 svg、行高 40px（壳的导航行规格）—— 四条全中才动手，
     *     否则一个节点都不碰；
     *   * **壳重渲染会换掉那一行**（切语言、关掉再打开设置面板），所以挂一个 body 级
     *     MutationObserver 防抖补一次；自己插入的节点带标记，绝不重复插；
     *   * **卸载时全部还原**：图标卸载、齿轮复原、观察器断开。
     *
     * 任何一步失败都只是「没有自定义图标」，不影响这一页本身。
     */
    /**
     * 导航行图标：一台显示器 —— **自己画的**。
     *
     * 为什么自己画：运行时那 188 个图标里没有电脑 / 显示器 / 笔记本（有仪表、滑块、面板、全屏、
     * 检查……），而这一栏就是「电脑操控」。所以照壳自己的画法画一个 16×16：`viewBox 0 0 16 16`、
     * `fill: none`、每个 path `stroke: currentColor`、线宽取 primitives 的 `ICON_MEDIUM_STROKE`
     * （1.3，与相邻图标同粗细），也不额外设 linecap —— 壳的图标都是默认方头，跟着走才不像外来户。
     * 形状就是最普通的显示器：机身（圆角矩形）+ 支颈 + 底座。
     */
    // 注意 seed 词缺失时 prims 是 null：这一行在模块加载时就跑，必须自己判空（否则整页被带崩）
    const MEDIUM_STROKE = prims && typeof prims.ICON_MEDIUM_STROKE === 'number' ? prims.ICON_MEDIUM_STROKE : 1.3
    const ComputerIcon = ({ size = 16, className }) =>
      h(
        'svg',
        {
          width: size,
          height: size,
          className,
          viewBox: '0 0 16 16',
          fill: 'none',
          xmlns: 'http://www.w3.org/2000/svg',
          'aria-hidden': 'true',
          strokeWidth: MEDIUM_STROKE
        },
        h('path', {
          d: 'M2.6 2.5H13.4C13.95 2.5 14.4 2.95 14.4 3.5V10.1C14.4 10.65 13.95 11.1 13.4 11.1H2.6C2.05 11.1 1.6 10.65 1.6 10.1V3.5C1.6 2.95 2.05 2.5 2.6 2.5Z',
          stroke: 'currentColor'
        }),
        h('path', { d: 'M8 11.1V13.3', stroke: 'currentColor' }),
        h('path', { d: 'M5.6 13.5H10.4', stroke: 'currentColor' })
      )

    /**
     * 设置导航里那一行的图标 —— 平台没有图标位，所以由这一半区自己补。
     *
     * 导航图标是壳里写死的一张 `id → 图标` 表（未命中就兜底成齿轮），而设置页注册项只有
     * `id` / `order` / `label`。那张表在**应用自带的 asar 里**：桌面版把 `/assets` 与所有
     * `@deepseek-ai/*` 客户端包都从 `app.asar/dsh` 发（profile 里那份同名包根本不会被读，
     * 实测 atime 停在安装那天），而 asar 是签名的一部分，改不得。所以只剩一条路：在页面里给
     * 那一行补图标 —— 把壳的兜底齿轮藏起来，在它前面放上我们自己的图标。
     *
     * 三条克制原则：
     *
     *   * **只认结构，不认壳的哈希类名**：对话框（`[role=dialog]`）里、文本完全等于本页
     *     label、第一个子元素是 svg、行高 40px（壳的导航行规格）—— 四条全中才动手，
     *     否则一个节点都不碰；
     *   * **壳重渲染会换掉那一行**（切语言、关掉再打开设置面板），所以挂一个 body 级
     *     MutationObserver 防抖补一次；自己插入的节点带标记，绝不重复插；
     *   * **卸载时全部还原**：图标卸载、齿轮复原、观察器断开。
     *
     * 任何一步失败都只是「没有自定义图标」，不影响这一页本身。
     */
    const NAV_MARK = 'data-cu-nav-icon'

    function installNavIcon(translate, reactDom) {
      if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') return () => {}
      if (!h) return () => {}
      if (!reactDom || typeof reactDom.createRoot !== 'function') return () => {}

      /** 当前补过的那一行（壳重渲染后节点会脱离文档，靠 isConnected 判断该收掉）。 */
      let live = null
      let timer = 0
      let reported = false
      let probes = 0

      /**
       * 自检上报：补上了记一行；**设置对话框开着却认不出那一行**也记一行，带上现场。
       *
       * 为什么值得写：这个补丁失败时页面上看不出任何区别（还是壳的兜底齿轮），而失败原因只有
       * 页面自己知道。Host 半区把它追加到 `<BASE>/nav-icon.log`（`/_computer-use/nav-icon`），
       * 于是「为什么图标还是齿轮」有据可查，而不是靠猜。上报本身失败无所谓。
       */
      const report = (outcome, detail) => {
        try {
          const pending = fetch(`${CHANNEL}/nav-icon`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ outcome, detail })
          })
          if (pending && typeof pending.catch === 'function') pending.catch(() => {})
        } catch {
          /* 诊断而已，不上报也没关系 */
        }
      }

      const undo = () => {
        if (!live) return
        const { holder, root, gear } = live
        live = null
        try {
          root.unmount()
        } catch {
          /* 行可能已经被壳删掉了，卸载失败无所谓 */
        }
        if (gear && gear.isConnected) gear.style.removeProperty('display')
        if (holder.isConnected) holder.remove()
      }

      /** 把一行改成我们的图标：藏掉壳的兜底图标，在行首放上我们的。 */
      const decorate = (row, gear) => {
        const holder = document.createElement('span')
        holder.setAttribute(NAV_MARK, '')
        holder.style.cssText = 'display:inline-flex;flex:none;width:16px;height:16px'
        if (gear) gear.style.display = 'none'
        row.insertBefore(holder, row.firstChild)
        const root = reactDom.createRoot(holder)
        root.render(h(ComputerIcon, { size: 16 }))
        live = { row, holder, root, gear }
        if (!reported) {
          reported = 'patched'
          report('patched', `computer-icon label=${String(translate('section'))}`)
        }
      }

      /** 壳的兜底图标：行里第一个 svg 子元素（没有就返回 null）。 */
      const shellIcon = (row) => {
        for (const child of row.children) {
          if (child.tagName && child.tagName.toLowerCase() === 'svg') return child
        }
        return null
      }

      const patch = () => {
        timer = 0
        // 行被壳换掉、或壳重渲染时把我们插的节点摘掉了 → 收掉重补
        if (live && (!live.row.isConnected || !live.holder.isConnected)) undo()
        if (live) return
        const label = String(translate('section'))

        const dialogs = document.querySelectorAll('[role="dialog"]')

        // 第一遍（严格）：设置对话框里的导航行 —— 文本完全等于本页 label、行首就是图标、
        // 行高 40px（壳的导航行规格）。四条全中才动手，绝不误伤别的按钮。
        for (const dialog of dialogs) {
          for (const row of dialog.querySelectorAll('button')) {
            if (row.textContent.trim() !== label) continue
            const gear = row.firstElementChild
            if (!gear || gear.tagName.toLowerCase() !== 'svg') continue
            if (Math.round(row.getBoundingClientRect().height) !== 40) continue
            decorate(row, gear)
            return
          }
        }

        // 第二遍（宽松）：壳换了行高或图标不再是第一个子元素时，靠「文本 + 行里有图标」认。
        // 只在严格匹配一个都没命中时才走这一步 —— 宁可图标晚一拍补上，也不要因为壳改了
        // 一个数值就永远补不上（这一轮就是这么白重启过两次的）。
        for (const row of document.querySelectorAll('button')) {
          if (row.textContent.trim() !== label) continue
          const gear = shellIcon(row)
          if (!gear) continue
          decorate(row, gear)
          return
        }

        // 两遍都没认出来，而设置对话框明明开着 → 这就是「图标补不上」的现场，记一次（最多三次，
        // 免得开关面板时把日志刷满）：按钮文本抽样最能说明问题（文案变了？结构变了？）。
        if (dialogs.length > 0 && probes < 3) {
          probes += 1
          const buttons = [...document.querySelectorAll('button')]
          const sample = buttons
            .map((button) => String(button.textContent || '').trim().slice(0, 24))
            .filter(Boolean)
            .slice(0, 8)
            .join(' | ')
          report('probe', `label=${label} dialogs=${dialogs.length} buttons=${buttons.length} sample=${sample}`)
        }
      }

      const schedule = () => {
        if (!timer) timer = setTimeout(patch, 150)
      }
      const observer = new MutationObserver(schedule)
      observer.observe(document.body, { childList: true, subtree: true })
      schedule()

      return () => {
        observer.disconnect()
        if (timer) clearTimeout(timer)
        undo()
      }
    }

    const name = MODULE_ID
    // 设置页与「通用」设置同一个槽位；locale 提供中英词典，slots 提供注册面。
    const inject = ['slots', 'locale']

    function apply(ctx) {
      // seed 词缺失（dsh 换了模块表）：安静地不出现，绝不抛给宿主页面。
      if (!React || !prims || !h) return
      let translate = (key) => key
      try {
        ctx.effect(() => ctx.locale.register(NS, { zh: ZH, en: EN }), 'computer-use: settings dictionaries')
        translate = ctx.locale.bind(NS)
      } catch {
        /* 没有 locale 就用 key 本身：这一页宁可显示英文 key，也不该整个不出现 */
      }
      // 导航行图标：平台没给第三方设置页留图标位，自己补一个（见 installNavIcon）。
      try {
        ctx.effect(() => installNavIcon(translate, ReactDOM), 'computer-use: nav icon')
      } catch {
        /* 补不上就还是壳的兜底齿轮，不影响这一页 */
      }
      ctx.slots.inject('settings.section', () =>
        ctx.slots.register(
          {
            name: 'settings.section',
            id: MODULE_ID,
            // 2 = 「通用」与「手机访问」之后、模型之前：它是个常用的日常开关，不该沉到列表底部
            order: 2,
            label: () => translate('section'),
            inject: () => ({ t: translate })
          },
          ComputerUseSettings
        )
      )
    }

    exports.name = name
    exports.inject = inject
    exports.apply = apply
    // 冒烟检查用：不改行为，只把这一页的字典与组件暴露出来（scripts/settings-smoke.mjs）
    exports.__internals = { ZH, EN, ComputerUseSettings }
    return module.exports
  }
})
