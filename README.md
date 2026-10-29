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
| 数据时间 | 不返回 | 给到日期，附带 `age_days`（差几天） |
| 能不能核对 | 只给 id | 给 `open_url`，点开自己看 |
| 时延 | 一次网络往返 | 检索 20ms，算上 Node 启动约 400ms |

关键在于**可核对**。内置接口不告诉你某条专家在不在公开市场里，你也没法验证它推荐的到底存不存在。这个 skill 返回深链，点一下就能看。

## 安装

需要 **Node 18 以上**（脚本用的是全局 `fetch`，16 和 17 没有，会报 `fetch is not defined`）。

还要能跑 Python 3，只用来解客户端登录态，纯标准库，不用 pip install。多数情况下你不用自己装——WorkBuddy 桌面端自带一份，脚本会优先用它（`~/.workbuddy/binaries/python/versions`）。实在找不到再设 `WB_PYTHON` 指过去。

Windows 上客户端要**开着并且登录着**，脚本要读它的进程；客户端版本过老也可能取不到，`export-token.py` 会直接告诉你差在哪。

装的位置是 WorkBuddy 的 skill 目录，**放进去就会被加载，不用注册、不用改配置**：

```bash
# macOS / Linux
git clone https://github.com/blueofsky/find-experts.git ~/.workbuddy/skills/find-experts

# Windows PowerShell
git clone https://github.com/blueofsky/find-experts.git "$env:USERPROFILE\.workbuddy\skills\find-experts"
```

没有 git 就用 zip（下载地址 `https://github.com/blueofsky/find-experts/archive/refs/heads/main.zip`）：

```bash
# macOS / Linux，解压出来叫 find-experts-main，要改成 find-experts
curl -L -o fe.zip https://github.com/blueofsky/find-experts/archive/refs/heads/main.zip
unzip fe.zip -d ~/.workbuddy/skills/ && mv ~/.workbuddy/skills/find-experts-main ~/.workbuddy/skills/find-experts
```

```powershell
# Windows PowerShell，同样要改名
Invoke-WebRequest -Uri https://github.com/blueofsky/find-experts/archive/refs/heads/main.zip -OutFile fe.zip
Expand-Archive fe.zip -DestinationPath "$env:USERPROFILE\.workbuddy\skills"
Rename-Item "$env:USERPROFILE\.workbuddy\skills\find-experts-main" "find-experts"
```

装之前先确认依赖在：

```bash
node -v      # 要 v18 以上
python -V    # 要 3.x；Windows 上脚本只认 python 这个命令，不认 py
```

如果 `python -V` 报「不是内部或外部命令」，但 `py -V` 有输出，那就设一个环境变量把路径指过去：`WB_PYTHON=py`。或者干脆跳过 python 用 token（见「它会读什么」）。

**目录层级要弄对**：`~/.workbuddy/skills/find-experts/SKILL.md` 是对的，`~/.workbuddy/skills/find-experts/find-experts/SKILL.md` 加载不到。

### 让 agent 帮你装

把下面这段直接发给 WorkBuddy，地址不用改：

> 装一下这个 skill：https://github.com/blueofsky/find-experts
> 装到 `~/.workbuddy/skills/find-experts`（Windows 是 `%USERPROFILE%\.workbuddy\skills\find-experts`），目录层级要正好是 `skills/find-experts/SKILL.md`，别多套一层。
> 装完 cd 进去跑 `node scripts/find.mjs "跨境支付合规" --top 3`，能出结果就说明装好了。
> 第一次跑会停 20 到 60 秒（看网速），那是在抓两万多条数据建索引，不是卡住，别中断它。

最后那句不能省。没有它，agent 有可能会认为进程挂了、重复重试，或者跑一半就回来问你。

如果 agent 执行命令前弹了确认框（它要往 `~/.workbuddy/` 里写东西），点同意就行。

### 装完自己验

**验收标准只有一条：第二步能出结果。** 文件在那儿（第一步）不算装上，可能缺依赖或取不到凭证。

```bash
# 1. 文件到位
ls ~/.workbuddy/skills/find-experts/SKILL.md

# 2. 能跑。首次会等 20 到 60 秒，出结果就算通了
node ~/.workbuddy/skills/find-experts/scripts/find.mjs "跨境支付合规" --top 3

# 3. 可选：8 条回归用例，看 Top1 / Recall@5 / Recall@10 三个数（要先跑过第 2 步，它依赖已建好的索引）
node ~/.workbuddy/skills/find-experts/scripts/selftest.mjs
```

Windows PowerShell 里 `~` 不会被展开，用 `$env:USERPROFILE`：

```powershell
node "$env:USERPROFILE\.workbuddy\skills\find-experts\scripts\find.mjs" "跨境支付合规" --top 3
```

第 2 步如果弹了「是否允许读取文件」的授权框，点允许。那是脚本在找客户端的登录态。

之后直接问它就行：

> 有没有擅长做竞品分析的专家

装完在**当前**会话里不一定立刻认得这个 skill，新开一个会话最保险。

### 目录已经存在怎么办

如果你以前从市场装过一份，目录名可能是 `find-experts__skillhub` 这种带后缀的，和 `find-experts` 会并存、互相打架。先看一眼：

```bash
ls ~/.workbuddy/skills/ | grep find-experts
```

有多份就删掉其余的，只留一个。`git clone` 到已存在的非空目录会直接失败（`destination path already exists`），先删再克隆。

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

这三行出来之前会停 **20 到 60 秒**（看网速），那是抓取和建索引。之后就一直这么快了：

- 抓 2.9 万条：6 并发、100 条一页，实测 20 到 30 秒（网络差会到一分钟）
- 建索引 5 秒，产出约 **56 MB** 的 `data/index.bin`
- 日常检索 20ms 级别，算上 Node 启动约 400ms

这几十秒只发生一次。**之后数据超过 7 天会在后台自动补刷**，主流程不等它，照常返回旧结果，下次查询就是新的。日常使用不产生网络请求。

## 它会读什么

这段必须写清楚，因为脚本确实要读一点东西才能调接口。

**要读的**：WorkBuddy 客户端的登录态，从里面解出 `accessToken`。三条途径按平台走，按顺序试，前一条不成就下一条：

- **Windows**：先看客户端自己的数据目录（DPAPI 按当前用户解开，纯 `ctypes` 调 `crypt32`）；目录里解不出来，就**枚举 WorkBuddy 进程、读它的进程内存**找那把密钥（上限 3 GB，只读）
- **macOS**：Keychain；不成就临时起一个独立临时目录的调试端口实例（用完即关，不碰你正在用的那个窗口）
- **Linux**：调试端口

读进程内存这条是实话，也是这个 skill 里最"重"的一步，你可以按需决定接不接受。它只做 `ReadProcessMemory`，不写、不注入、不改客户端任何文件。

**不会做的**：

- 不读你的对话内容、会话记录、文件
- 不读专家使用记录、偏好、任何业务数据
- 不写客户端文件、不改客户端配置、不装任何东西到客户端
- token 不发往任何第三方，只用于请求 `www.workbuddy.cn` 的官方接口
- token 存本地 `data/wb-token.env`；如果走过调试端口那条路，还会缓存一把 `data/atrest.key`（用来省掉下次重新解密的开销）。两个文件都不进 git

代码在 `scripts/client_auth.py`，1481 行，可以逐行看。它是 `totorosir-workbuddy-score` 这个 skill 里 `buddy_station.py` 的精确子集，按函数名用 AST 抽取，实现未改动。

**不想让它读客户端也行**：自己抓一个 token 设到环境变量里，脚本就完全不碰 python、也不碰上面那三条途径：

```bash
export WB_ACCESS_TOKEN=<JWT>              # macOS / Linux / Git Bash
set WB_ACCESS_TOKEN=<JWT>                 # Windows cmd
$env:WB_ACCESS_TOKEN = "<JWT>"            # Windows PowerShell，只对当前窗口有效
```

必须是 JWT（三段、点分隔），脚本会校验 `exp`，过期的不认。注意 PowerShell 里别用 `set`，那是 `Set-Variable` 的别名，会静默建个变量、脚本根本读不到。

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
| 手动 | `node scripts/update.mjs` | 立即全量抓一遍，20 到 30 秒 |
| 索引丢了或坏了 | 下次检索时自动 | 同步抓一遍（就是首次那个 20 到 60 秒） |

后台刷新有保护：1 小时内失败过就不再试（和手动刷新共用同一个退避窗口），写了并发锁，`--refresh` 时不会重复触发。想彻底关掉自动刷新，设 `FIND_EXPERTS_NO_AUTO=1`。

**这个 skill 没有自带定时任务。** 数据能不能保持新鲜全靠上面那三道机制，其中「超过 7 天自动补刷」是主力。你要是想让它每天定点刷，得自己在 WorkBuddy 里建一条 automation 去跑 `update.mjs`，那属于你本机的配置，跟 skill 无关。

**升级这个 skill 本身**：`git clone` 装的 `git -C ~/.workbuddy/skills/find-experts pull`；zip 装的重下一次覆盖，`data/` 留着不用动（索引格式没变的话不用重建）。卸载就是删掉整个目录。

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

退出码：`0` 正常、`1` 参数写错（查询为空、`--type` 给了别的值、`--detail` 的 id 查不到）、`2` 凭证失效、`3` 已有更新进程在跑、`4` 数据护栏拦下（条数突变或页级失败超限，保留旧索引）、`5` 网络或接口失败并进入 1 小时退避、`6` 上次失败不到 1 小时、本次刷新被退避跳过（用的仍是现有数据）。

（PowerShell 里看退出码用 `$LASTEXITCODE`，不是 `$?`）

## 已知边界

这些是真限制，不是没做完：

**bigram 分词，不认同义词。** 「涨粉」和「增长」在索引里毫无关系。更麻烦的是跨字组合：搜「把这堆访谈录音整理成一本二十万字的书」前 20 条里没有「成书定稿校订专家」，因为问句里「书」和「的」组成 `的书`，标题里「书」和「成」组成 `成书`，一个 gram 都碰不上，BM25 直接 0 分进不了候选池。换成搜「成书定稿」，它立刻排第一。

修这个要加 unigram 混合索引，体积上涨、全部回归用例重跑。判断为不划算，所以脚本里的换词规则是：**如果前几条清一色是输入形态（转写、纪要、转文档）而你要的是输出成果（书、定稿、方案），就把输入侧丢掉只搜输出形态。**

**只覆盖公开市场。** 账号维度的私有专家、企业内部专家不在接口返回里。搜不到只能说「公开市场里没有」，不等于客户端里没有——这两件事不一样。

**macOS / Linux 没在真机验证过。** 取凭证的代码路径写全了，但只在 Windows 上实际跑通过。其他平台建议直接用 `WB_ACCESS_TOKEN`。

**数据是快照。** 最长旧 7 天（后台刷新兜着），最坏情况是机器一直没开机，那就停在上次跑的时间。`meta.json` 里的 `data_updated_at` 会如实告诉你。

**依赖私有接口。** `/portal/operation-platform/...` 不是公开 API。WorkBuddy 改接口路径，表现是退出码 `5`（HTTP 404 被归成网络失败）；改登录态加密格式，表现是退出码 `2`。两种都得改代码，不是配置能绕过去的。

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

`references/` 是写给想改这个 skill 的人看的：为什么权重是这套、客户端接口怎么摸出来的、踩过哪些坑。**运行时不读它们**，`SKILL.md` 一次都没引用，删了照样能用。用 `git clone` 装的话它们会跟着下来（`README.md`、`assets/`、`.git/` 也在），嫌乱可以直接删。

**`data/` 千万别整个打包发给别人。** 里面有你的 `wb-token.env`，那是真凭证，拿到就能用你的账号调接口。`.gitignore` 已经排除了它，但手动复制、压缩、上传不受这个约束。

## 非官方

这是个人做的 skill，不是 WorkBuddy 官方工具。它依赖的是市场页面自己在用的接口，接口变了就得改。issue 里报问题带上 `node scripts/find.mjs <你的查询> --json` 的输出，`status`、`failed_pages`、`age_days` 这几个字段能定位大半问题。

## License

MIT。`scripts/client_auth.py` 派生自 totorosir-workbuddy-score（MIT-0），详见 LICENSE。
