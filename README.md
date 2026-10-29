# find-experts

给 WorkBuddy 用的专家检索 skill。把需求描述丢进去，从公开市场里找专家，返回可以点开的链接。

适用：手里有个活不知道派给谁；记得名字要定位到那一个；搜到几个同名的要分辨谁还在用。

比内置的 `search_plugins` 多给几样东西：全量候选（最多 100 条，带匹配得分）、使用量、作者账号、更新时间、可点开的 `open_url`，以及库一共有多少条、数据是哪天的。核心是**能自己核对**——链接点开就在那儿，不存在也看得见。

## 安装

需要 **Node 18+**（脚本用全局 `fetch`，16/17 会报 `fetch is not defined`）。Windows 上还要 WorkBuddy 客户端**开着并登录着**，脚本要从它的登录态里取 token；取 token 那步用 Python 3，纯标准库，一般不用你装，脚本会优先用桌面端自带的。

放进 skill 目录就会自动加载，不用注册、不用改配置：

```bash
# macOS / Linux
git clone https://github.com/blueofsky/find-experts.git ~/.workbuddy/skills/find-experts
```

```powershell
# Windows PowerShell
git clone https://github.com/blueofsky/find-experts.git "$env:USERPROFILE\.workbuddy\skills\find-experts"
```

没 git 就下 zip：`https://github.com/blueofsky/find-experts/archive/refs/heads/main.zip`。解压出来叫 `find-experts-main`，**要改成 `find-experts`**。

层级必须是 `skills/find-experts/SKILL.md`，多套一层加载不到。

### 让 agent 帮你装

把这段直接发给 WorkBuddy，地址不用改：

> 装一下这个 skill：https://github.com/blueofsky/find-experts
> 装到 `~/.workbuddy/skills/find-experts`（Windows 是 `%USERPROFILE%\.workbuddy\skills\find-experts`），层级要正好是 `skills/find-experts/SKILL.md`。
> 装完跑 `node scripts/find.mjs "跨境支付合规" --top 3`，能出结果就算装好了。
> 第一次跑会停 20 到 60 秒，那是在抓数据建索引，不是卡住，别中断它。

最后一句别省。没有它，agent 可能以为进程挂了、反复重试，或者跑一半回来问你。

### 自己验

文件在那儿不算装上，能出结果才算：

```bash
node ~/.workbuddy/skills/find-experts/scripts/find.mjs "跨境支付合规" --top 3
```

PowerShell 里 `~` 不展开，用 `$env:USERPROFILE`。跑的时候可能弹「允许读取文件」，点允许——那是它在找客户端的登录态。

装完**当前**会话不一定立刻认得这个 skill，新开一个最稳。

### 目录已存在

以前从市场装过的话，目录名可能是 `find-experts__skillhub`，会和平共存、互相打架。`ls ~/.workbuddy/skills/ | grep find-experts` 看一眼，多的删掉。`git clone` 到非空目录会直接失败，先删再克隆。

## 第一次会慢

第一次没有索引，得先抓一遍市场全量，**20 到 60 秒**（看网速）。之后日常检索 400ms 左右，不联网。

## 它会读什么

要调接口就得有登录态，所以脚本会从客户端取 `accessToken`，按平台试三条路：

- **Windows**：先试客户端自己的数据目录（DPAPI 按当前用户解开）；解不出来就枚举 WorkBuddy 进程、**读它的进程内存**找那把密钥（上限 3 GB，只读）
- **macOS**：Keychain；不成就起一个临时目录的调试端口实例，用完即关，不碰你正在用的窗口
- **Linux**：调试端口

读进程内存这条照实说，你按需判断接不接受。只做 `ReadProcessMemory`，不写、不注入、不改客户端任何文件。

不做的：不读对话记录和业务数据，不写客户端配置，token 不发往任何第三方（只用于请求 `www.workbuddy.cn` 的官方接口）。取到的 token 存本地 `data/wb-token.env`，不进 git。

**不想让它碰客户端**：自己抓个 JWT 设进去，脚本就完全不碰 python，也不走上面三条路：

```bash
export WB_ACCESS_TOKEN=<JWT>        # macOS / Linux / Git Bash
$env:WB_ACCESS_TOKEN = "<JWT>"      # Windows PowerShell，只对当前窗口有效
```

必须是三段点分隔的 JWT，脚本会校验过期时间。

## 数据与更新

数据来自专家市场页面自己在用的接口，公开数据。**这个 skill 没有自带定时任务**，三道机制兜着：

- 索引不存在或坏了 → 下次检索时同步抓，就是首次那 20 到 60 秒
- 数据超过 7 天 → 下次检索时后台补刷，主流程不等它，照常返回旧结果
- 想立刻刷 → `node scripts/update.mjs`

后台刷新有 1 小时退避和并发锁，不会重复触发。想彻底关掉自动刷，设 `FIND_EXPERTS_NO_AUTO=1`。

升级 `git -C ~/.workbuddy/skills/find-experts pull`，卸载就是删目录。

## 命令

平时这些是模型自己调的，你也可以直接敲：

```bash
node scripts/find.mjs "跨境支付合规" --top 5        # 普通检索
node scripts/find.mjs "代码审计" --cat 技术工程      # 限定分类，必须写全名
node scripts/find.mjs "带团队做整件事" --type team   # 只看团队
node scripts/find.mjs --detail ex_xxx               # 按 id 查作者和安装包
node scripts/find.mjs "调研" --json                 # 机器可读
```

退出码：`0` 正常、`1` 参数写错、`2` 凭证失效、`3` 已有更新进程在跑、`4` 数据护栏拦下、`5` 网络或接口失败并退避 1 小时、`6` 还在退避期，本次跳过。

## 已知边界

**不认同义词，也吃跨字组合的亏。**「涨粉」和「增长」在索引里毫无关系；问句里的字和标题里的字凑不成同一个词，就会零命中——搜「把录音整理成一本二十万字的书」会漏掉「成书定稿专家」，换成搜「成书定稿」它排第一。所以搜不到时换词，往**你要的成果形态**那一侧换。

**只覆盖公开市场。** 私有专家、企业内部专家不在接口返回里。搜不到只能说「公开市场里没有」，不等于客户端里没有。

**macOS / Linux 没在真机验证过。** 取凭证的代码路径写全了，只在 Windows 上实际跑通。其他平台建议直接用 `WB_ACCESS_TOKEN`。

**数据是快照**，最长旧 7 天。**依赖私有接口**：接口改路径，表现是退出码 `5`；改登录态加密格式，表现是退出码 `2`。两种都得改代码。

**`data/` 别整个打包发给别人**，里面有你的真 token，拿到就能用你的账号调接口。`.gitignore` 排除了它，但手动复制、压缩不受约束。

## 非官方

个人做的，不是 WorkBuddy 官方工具。报问题带上 `node scripts/find.mjs <你的查询> --json` 的输出。

`references/` 是写给想改这个 skill 的人看的，运行时不读、删了照样能用。MIT，`scripts/client_auth.py` 派生自 totorosir-workbuddy-score（MIT-0），详见 LICENSE。
