# 内部实现与调研结论

**维护者用，agent 执行时不需要读。** 这里记的是「为什么这么设计」和挖客户端代码拿到的证据。SKILL.md 只保留结论，证据都在这里，将来客户端升级、行为变了，回来核对这些。

## `data/` 是什么

纯派生物，四样东西全都能从接口和客户端登录态重新生成：

```
data/index.bin      ~57 MB  检索索引（约 2 MB/千条，随库规模涨）
data/meta.json       1 KB   条数、抓取时间、分类分布、凭证剩余天数
data/history.json    1 MB   每个专家首次/末次出现日期（当前无读方）
data/wb-token.env    1 KB   登录凭证，权限 600
```

**整个目录可以删**。下次 `find.mjs` 发现索引不在会自动重建，实测 20~30 秒（256 次分页请求）；代价只有时间，没有数据损失。唯一会归零的是 `history.json` 里的日期记录，而它目前没有任何读方。

之所以叫 `data` 不叫 `cache`：它含凭证，且重建要跑 256 次请求，不是随手可丢的廉价缓存，放 `cache` 反而容易被清理工具扫掉。

## `+` 面板：数据同源，排序不可信

面板底层也是 `market/expert/list`，参数 `{keyword, expertType:'agent', sortBy:'use_count', sortOrder:'desc'}`。**数据和本地索引同源，差别全在排序。**

`sortBy:'use_count'` 服务端**没有生效**。实测搜「微信小程序开发者」返回顺序是 uc=undefined → 432 → 852 → 118 → 3105271，完全不是降序。所以面板的排序既不是精确匹配优先，也不是使用量优先。实测两个查询：搜「AI短剧制作」目标排第 6，搜「微信小程序开发者」时 uc=310 万、标题完全一致那条排第 5；同样的查询本地检索都排第 1。详细对比数据见 `tuning.md`。

**单选**：`expert-picker-TiSz7KpM.js` 里 `mode:i='multi'` 只是组件默认值，调用方传的是 `mode:'single'`（该 chunk 内 0 处、别的 chunk 里 3 处）。用户实测选中一个即完成，不累积。

**留在当前会话的机制**：`+` → 专家 → 选中，走 `addSessionCreatedExperts(conversationId, ids)`，写 `localStorage` 里一个 `Map<conversationId, expertIds[]>`（`persistSessionCreatedExperts()` 只保留最近 20 个会话）。picker 的 `onSelect` 具体调用点**没抓到**，这条是推断。

## 深链：能定位，会离开对话

`workbuddy://experts?id=ex_xxx` 已实测可用。主进程 `isMarketDetailDeepLink()` 通过 `LEGACY_MARKET_DETAIL_ID_KEYS = {experts:'expertId', ...}` 匹配，判断条件是 `hasNonEmptyParam(n,'id') || hasNonEmptyParam(n, r)`——**`id` 和 `expertId` 两个参数名都认**。

命中后走独立分支 `handleOpenUrlAfterRendererReady` → `webContents.send('app:openUrl')`，复用同一主窗口（`showOrCreate()`）。

**没有返回栈**：`experts` 走业务 host 分支（`handleDesktopOpenUrl` 里 `It(host)` → 交给侧边栏容器），不进 `mapDeepLinkToRoute` 的路由映射（那边用 `replace:true`）。所以跳过去之后，用户只能从左侧会话列表把原对话点回来。

到详情页再点「召唤」是另一条路：`session.prepare({context:{expert}})` + `navigation.goHome()`，**切到新会话**，不是加到当前会话。

## 为什么不能代用户召唤或安装

- **召唤**：采纳状态是渲染进程里的 `Map<conversationId, expertIds[]>` 加 localStorage，脚本是客户端外的独立进程，够不着。深链的十个处理器里也没有 summon 分支。
- **安装**：`installExpert` 要下载 zip、解压到配置目录、写 marketplace manifest（还带 preCheck 安全校验）；`activateExpert` 绑在 `conversationId` 上属于会话运行态。看着最像口子的 `expert/install` 需要 `sharecode`，而 `get-by-ids` 返回的 31~32 个字段里没有这个字段（只有分享链才有）。

两条路都断，别再试。

## 没有 web 端专家市场

`www.workbuddy.cn/app/experts` 只是个 SPA 壳，`/experts`、`/market` 直接 404，前端没有市场路由，带上 `?keyword=` 也不会被解析。想让用户看到某个专家只有三条路：`+` 面板搜 `title`、左侧边栏「专家」搜 `title`、把 `open_url`（`workbuddy://experts?id=ex_xxx`）给用户点。**别拼 http 链接。**

> 2026-10-24：删掉了 `find.mjs --open`。它实现没问题（win/mac/linux 三分支），但那是脚本从外部调 `cmd.exe /c start`，没有用户手势就抢前台，而且详情页会离开当前对话。`open_url` 交给渲染层、用户自己点，效果一样且不越界。

## 为什么不做 keyword 搜索

全库索引已经在本地，官方 `keyword` 接口覆盖的范围和它一模一样，多一条路只会得到更差的排序（对比数据见 `tuning.md`）。数据旧了就更新，不是换个接口查。用户想按名字精确搜，直接让他用 `+` → 专家 面板，那是官方 keyword 入口，还不离开对话。

## `uc` 口径

`use_count` 来自列表接口，与专家中心排行榜的 `useCount` **不是一套数**（吴八哥两边分别是 1111 万 / 1974 万），不要混着引用。

## `get-by-ids` 字段数不固定

取决于专家来源，实测同一天：

| 来源 | 字段数 | 差异 |
|---|---|---|
| openapi | 32 | 有 `download_url`、`developer`、`fingerprint` |
| official_sync | 31 | `developer` 是 `undefined`，无 `fingerprint` |

所以 `--detail` **不是「看更详细的介绍」**，专家描述在检索结果里已经全了；它只补作者和安装包，而且作者可能没有。

## 跨平台取密钥：实测结论（2026-10-24）

`client_auth.py` 从上游 `buddy_station.py`（MIT-0）补回了 macOS 的 Keychain、CDP 调试端口两条途径，Windows 进程内存那条原样保留。三条路的现状不同：

| 途径 | 平台 | 状态 |
|---|---|---|
| DPAPI + 客户端数据目录 | Windows | 实测可用 |
| `ReadProcessMemory` 扫进程内存 | Windows | 实测可用（本机密钥来源就是它） |
| Keychain `security find-generic-password` | macOS | 上游自己注释「5.6.2 未把密钥写进 Keychain，仅作兼容兜底」 |
| CDP 调试端口 | macOS 主途径 / 其他平台兜底 | **本机实测失败**，见下 |

**CDP 实测记录**（`CodeBuddy CN.exe`，2026-10-24 版）：

- 起实例 → 连 Inspector → `Runtime.evaluate` 全链路通了，耗时 4.1 秒
- 但上游那句取密钥的表达式失败：`No such binding was linked: electron_browser_workbuddy_storage`
- 继续排查，这个 build 里 inspector 作用域**没有 `require`**（`typeof require === 'undefined'`），`process.mainModule` 也没了，`import('node:module')` 被 vm 拦掉（`ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`）
- 轮询了 1.6 秒确认 binding 不是延迟注册，是这个名字根本不存在
- `process.type` 是 `browser`，说明确实连到了主进程，不是连错了目标

**所以「照抄上游就能在 mac 上跑」这个假设不成立**。上游那条表达式依赖 `electron_browser_workbuddy_storage` 这个内部 binding 名字，build 一换就断。macOS 的客户端是不是这个名字我不知道，没法验证。

保留这段代码的理由不是「它能用」，是**它比没有强**：Windows 上永远走不到（内存扫描先命中），mac 上失败时会写一条精确诊断（`cdp.inspector: failed（...）`），用户至少知道断在哪一环，而不是看到一句「凭证失效」。

真要给 macOS 一个确定可用的方案，方向不是继续猜 binding，而是走网页会话授权（浏览器里跑 snippet 拿凭据），也就是上游的通道 B。代价是用户要走一次手动授权，好处是不依赖客户端内部实现。
