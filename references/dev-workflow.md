# 双环境工作流

开发与运行分在两个目录，`scripts/deploy.mjs` 单向同步。**不随包发布**。

| | 路径 | 角色 |
|---|---|---|
| 开发 | `D:\AI\AIGC\laoli-kitchen\laoli-skills\find-experts` | git 仓库、唯一真相源、打包 | 
| 生产 | `C:\Users\myjob\.workbuddy\skills\find-experts` | WorkBuddy 加载的真实 skill，**只读** |

## 文件归属

| 内容 | 归谁 | 说明 |
|---|---|---|
| `SKILL.md` `LICENSE` `scripts/` | 开发 → 部署到生产 | deploy 的同步范围，与发布包完全一致 |
| `references/` | **只开发有** | 维护者文档，agent 不读；生产端 deploy 时会连目录一起删 |
| `dist/` | 只开发有 | 打包产物，生产不需要 |
| `.git` `.gitignore` `.gitattributes` | 只开发有 | 生产不是仓库 |
| `data/` | **两边各一份** | 运行时状态，见下 |

生产目录最终长这样，跟别人装完是一样的：

```
find-experts/
├── SKILL.md
├── LICENSE
├── data/          # 索引 + token，运行时产生
└── scripts/
```

## 日常流程

```bash
# 1. 改完在开发环境测
node scripts/selftest.mjs

# 2. 看看会动哪些文件
node scripts/deploy.mjs --dry-run

# 3. 同步到生产，顺带冒烟
node scripts/deploy.mjs --verify

# 4. 要发版才打包
node scripts/pack.mjs          # -> dist/find-experts-v<版本>.zip
```

deploy 只做三件事：新增目标缺的、覆盖内容不同的、删掉源端已不存在的（限 `scripts/` 内）。它**不碰** `data/`、`dist/`、`.git`、`.workbuddy`。

`references/` 是唯一的例外——不在同步范围，反而会把生产端的整个目录删掉。因为它是维护者文档，`SKILL.md` 不引用它，装到别人机器上也不存在，生产端留着没有意义。

## data/ 为什么各留一份

`data/` 是索引和 token，属运行时状态，跟代码隔离是两件事：

| | 生产 | 开发 |
|---|---|---|
| 谁在写 | 每天 04:30 定时任务 + 真实调用 | 只有你手动跑 |
| 新鲜度 | 每天更新 | 停在你上次跑的时候 |
| token | 真实登录态解出来的 | 从生产拷的副本 |

**所以开发环境的数据通常比生产旧。** 动检索逻辑（分词、权重、`index.mjs` 的 `VERSION`）之前先拉一次，否则回归指标测的是旧数据：

```bash
node scripts/update.mjs --force     # 约 20~30 秒
```

两边都不要提交。`/data/` 已在 `.gitignore` 里，但 `data/wb-token.env` 是真 token——**别把开发目录整个打包发人**。

## 回滚

生产目录**没有 git**，回滚只能在开发环境做完再 deploy：

```bash
git tag                          # 找版本
git stash                        # 有未提交改动先存
git checkout v1.5.2
node scripts/deploy.mjs --verify
git checkout main
```

同理，**任何情况下都不要直接改生产目录**。改了就会跟开发漂移，下次 deploy 一是静默覆盖、二是 `diff` 里看不出谁对。

## 几个容易踩的地方

- **版本号在 `SKILL.md` 的 frontmatter**，deploy 不会 bump。改了代码要不要升版本由你判断，但要发 zip 就得升
- **定时任务绑的是生产路径**（`~/.workbuddy/skills/find-experts/scripts/update.mjs`）。生产目录别改名，一改定时任务就指向不存在的地方
- **从市场重装 skill 会覆盖生产目录**。如果生产已经 deploy 了比市场上新的版本，重装等于回退，装完记得再 deploy 一次
- **`references/` 只存在于开发环境**，`SKILL.md` 不引用它。生产端和发布包里都没有这个目录，所以维护笔记永远不会泄漏给用户
- **`pack.mjs` 和 `deploy.mjs` 都不进包**，两个脚本在 `SKIP` 名单里
