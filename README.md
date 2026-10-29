# find-experts

给 WorkBuddy 用的专家检索 skill。本地建索引，按需求描述找专家，返回可点开的深链。

适用场景：你有个活不知道派给谁，或者记得专家名字要定位到具体那一个，或者搜到几个同名的要分辨哪个才是真在用的。

## 为什么不用内置的专家搜索

WorkBuddy 内置的 `search_plugins` 能用，但它返回的信息太少，判断不了：

| | 内置 search_plugins | 这个 skill |
|---|---|---|
| 候选数量 | 给几条，不说排序依据 | `--top` 最多给 100 条，每条有匹配得分 |
| 返回字段 | 名称、描述 | 加上使用量、版本、更新时间、作者账号、作者自写问法、分类、深链 |
| 库规模 | 不返回 | 全量快照，`total` 明确告诉你多少条 |
| 数据时间 | 不返回 | 精确到分钟，附带 `age_days` |
| 能不能核对 | 只给 id | 给 `open_url`，点开自己看 |
| 时延 | 一次网络往返 | 检索 20ms，算上 Node 启动约 300ms |

关键在于**可核对**。内置接口不告诉你某条专家在不在公开市场里，你也没法验证它推荐的到底存不存在。这个 skill 返回深链，点一下就能看。

## 安装

需要 Node 16 以上。还需要一个 Python 3，只用来解客户端登录态，纯标准库，不用 pip install。不想装 python 就自己给 token（见下面「它会读什么」）。

```bash
# macOS / Linux
git clone https://github.com/blueofsky/find-experts.git ~/.workbuddy/skills/find-experts

# Windows PowerShell
git clone https://github.com/blueofsky/find-experts.git "$env:USERPROFILE\.workbuddy\skills\find-experts"
```

没有 git 就下 zip，解压后把整个目录放到 `~/.workbuddy/skills/find-experts`。注意是 **skill 目录本身**放在 skills 下，别多套一层。

装完不用重启，直接问：

> 有没有擅长做竞品分析的专家

## 第一次使用会慢

首次没有索引，脚本会先抓一遍市场全量。输出大概长这样：

```
需求：AI短剧制作
库：29424 条 | 数据日期：2026-10-29 | 命中 gram 5/5 | 23ms

#1  AI短剧制作   [agent]  140.14
    账号 仁者爱短剧导演 · ex_i485AEnvscLv
    一句话或大纲做成AI短剧，写剧本、定角色、拆分镜到出片，支持Seedance与MiniMax H3
```

（条数每天会变，日期是你本地数据的时间）

这三行出来之前会停 **40 秒左右**，那是抓取和建索引。之后就一直这么快了：

- 抓 2.9 万条：6 并发、100 条一页，约 40 秒
- 建索引 **57 MB**，存在 `data/index.bin`
- 日常检索 20ms 级别，算上 Node 启动约 300ms

这 40 秒只发生一次。**之后数据超过 7 天会在后台自动补刷**，主流程不等它，照常返回旧结果，下次查询就是新的。日常使用不产生网络请求。

## 它会读什么

这段必须写清楚，因为脚本确实要读一点东西才能调接口。

**要读的**：WorkBuddy 客户端的登录态，从里面解出 `accessToken`。三条途径按平台走：

- **Windows**：客户端自己的数据目录，用 DPAPI（当前用户）解开，纯 `ctypes` 调 `crypt32`
- **macOS**：Keychain，以及临时起一个独立临时目录的调试端口实例（用完即关，不碰你正在用的窗口）
- **Linux**：调试端口

**不会做的**：

- 不读你的对话内容、会话记录、文件
- 不读专家使用记录、偏好、任何业务数据
- token 不发往任何第三方，只用于请求 `www.workbuddy.cn` 的官方接口
- token 存在本地 `data/wb-token.env`，权限 600，`.gitignore` 已排除

代码在 `scripts/client_auth.py`，1481 行，可以逐行看。它是 `totorosir-workbuddy-score` 这个 skill 里 `buddy_station.py` 的精确子集，按函数名用 AST 抽取，实现未改动。

**不想让它读客户端也行**：自己抓一个 token 设到环境变量里，脚本就完全不碰 python：

```bash
export WB_ACCESS_TOKEN=<JWT>       # macOS / Linux
set WB_ACCESS_TOKEN=<JWT>          # Windows cmd
```

必须是 JWT（三段、点分隔），脚本会校验 `exp`，过期的不认。

## 数据从哪来

`https://www.workbuddy.cn/portal/operation-platform/market/expert/list`

这是专家市场页面自己在用的接口，公开数据，不需要特殊权限（带了登录态而已）。抓下来的字段就是市场页面上能看到那些：

```
id  title  account  agent  type  src  cats  tags  desc  qp  uc  rr  icon  upd  home  ver  plug
```

其中 `uc` 是使用人数、`qp` 是作者自己写的推荐问法、`upd` 是最后更新时间、`agent` 是作者账号。没有任何字段是你账号独有的。

索引格式是自己定的单文件二进制（magic `EXF1`），可以随时用 `node scripts/update.mjs --rebuild` 从零重建，重建约 4 秒。

## 更新

三道机制，互不冲突：

| 什么时候 | 触发 | 干什么 |
|---|---|---|
| 数据超过 7 天 | 下次检索时自动 | 后台起一个进程刷，主流程不等 |
| 手动 | `node scripts/update.mjs` | 立即全量抓一遍，约 40 秒 |
| 索引丢了或坏了 | 下次检索时自动 | 同步抓一遍（就是首次那个 40 秒） |

后台刷新有保护：1 小时内失败过就不再试（和手动刷新共用同一个退避窗口），写了并发锁，`--refresh` 时不会重复触发。想彻底关掉自动刷新，设 `FIND_EXPERTS_NO_AUTO=1`。

## 直接敲命令

平时这些命令是模型自己调的，你也可以直接敲：

```bash
node scripts/find.mjs "跨境支付合规" --top 5        # 普通检索
node scripts/find.mjs "代码审计" --cat 技术工程      # 限定分类，必须写全名
node scripts/find.mjs "带团队做整件事" --type team   # 只看团队
node scripts/find.mjs --detail ex_xxx               # 按 id 查作者和安装包
node scripts/find.mjs "调研" --top 3 --explain      # 看每条命中了哪些 gram
node scripts/find.mjs "调研" --json                 # 机器可读
```

退出码：`0` 正常、`2` 凭证失效、`3` 已有更新进程在跑、`4` 数据护栏拦下（条数突变或页级失败超限，保留旧索引）、`5` 网络失败并进入 1 小时退避、`6` 上次失败不到 1 小时、本次刷新被退避跳过（用的仍是现有数据）。

## 已知边界

这些是真限制，不是没做完：

**bigram 分词，不认同义词。** 「涨粉」和「增长」在索引里毫无关系。更麻烦的是跨字组合：搜「把这堆访谈录音整理成一本二十万字的书」前 20 条里没有「成书定稿校订专家」，因为问句里「书」和「的」组成 `的书`，标题里「书」和「成」组成 `成书`，一个 gram 都碰不上，BM25 直接 0 分进不了候选池。换成搜「成书定稿」，它立刻排第一。

修这个要加 unigram 混合索引，体积上涨、全部回归用例重跑。判断为不划算，所以脚本里的换词规则是：**如果前几条清一色是输入形态（转写、纪要、转文档）而你要的是输出成果（书、定稿、方案），就把输入侧丢掉只搜输出形态。**

**只覆盖公开市场。** 账号维度的私有专家、企业内部专家不在接口返回里。搜不到只能说「公开市场里没有」，不等于客户端里没有——这两件事不一样。

**macOS / Linux 没在真机验证过。** 取凭证的代码路径写全了，但只在 Windows 上实际跑通过。其他平台建议直接用 `WB_ACCESS_TOKEN`。

**数据是快照。** 最长旧 7 天（后台刷新兜着），最坏情况是机器一直没开机，那就停在上次跑的时间。`meta.json` 里的 `data_updated_at` 会如实告诉你。

**依赖私有接口。** `/portal/operation-platform/...` 不是公开 API，WorkBuddy 改了路径或改了登录态加密格式，这套就失效。失效时的表现是退出码 2，按提示走。

## 目录

```
find-experts/
├── SKILL.md              给模型读的说明书：什么时候用、怎么排、怎么答
├── README.md             你正在看的
├── LICENSE               MIT，含第三方署名
├── scripts/
│   ├── find.mjs          主入口：检索、精排前的召回、输出
│   ├── update.mjs        刷新索引
│   ├── fetch.mjs         分页抓取
│   ├── index.mjs         索引读写
│   ├── text.mjs          分词与 BM25 打分
│   ├── token.mjs         凭证取用与缓存
│   ├── client_auth.py    从客户端登录态解 token
│   ├── export-token.py   上面那个的调用封装
│   ├── selftest.mjs      8 个回归用例
│   ├── deploy.mjs        维护者工具：双环境同步，使用不需要
│   └── pack.mjs          维护者工具：打包，使用不需要
└── references/           设计取舍、排障、调参记录。只在仓库里，运行不需要
```

`references/` 是写给想改这个 skill 的人看的：为什么权重是这套、客户端接口怎么摸出来的、踩过哪些坑。**运行时不读它们**，`SKILL.md` 也一次都没引用，装到 `~/.workbuddy/skills/` 下只有 `SKILL.md`、`LICENSE`、`scripts/` 和 `data/` 四样东西。

## 非官方

这是个人做的 skill，不是 WorkBuddy 官方工具。它依赖的是市场页面自己在用的接口，接口变了就得改。issue 里报问题带上 `node scripts/find.mjs <你的查询> --json` 的输出，`status`、`failed_pages`、`age_days` 这几个字段能定位大半问题。

## License

MIT。`scripts/client_auth.py` 派生自 totorosir-workbuddy-score（MIT-0），详见 LICENSE。
