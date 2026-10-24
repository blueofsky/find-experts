# 故障排查

**agent 平时不用读这个。** 只有 `find.mjs` 返回 `status != ok`、`degraded: true`，或者数据长期不更新时，才翻到这里。

## 退出码

| 码 | 含义 | 怎么办 |
|---|---|---|
| `0` | 成功 | — |
| `1` | 参数错误 | 查命令拼写 |
| `2` | 凭证失效 | 见「凭证」一节 |
| `3` | 已有更新在跑 | 等它跑完，别并发 |
| `4` | 条数护栏触发 | 见「条数护栏」一节 |
| `5` | 网络失败 | 1 小时内不再重试，需要时加 `--force` |
| `6` | 失败退避中 | 同上 |

## 降级模式（本地索引不可用）

触发时 stderr 打一行 `[degraded] …`，JSON 输出里 `degraded: true`。只有两种情况会走到这里：

| 触发条件 | 为什么这时才降级 |
|---|---|
| 更新处于失败退避期（退出码 6） | 本地索引不存在或已损坏，而 `update.mjs` 因为退避拒绝重试；此时网络很可能早恢复了 |
| 索引重建后仍读不出来 | 重建跑通了、文件还是读不出，说明不是数据缺失而是别的问题，只能先走实时接口 |

注意**索引损坏不会直接降级**：`find.mjs` 会先自动重建一次（约 20~30 秒），重建成功就照常用本地。只有重建也被退避挡住才降级。

**不会降级的场景**：凭证失效（退出码 2）、网络不通（退出码 5）、并发锁（3）、条数护栏（4）。前两个实时接口同样调不通，降级等于换个地方报同一个错；后两个改一下本地状态就能恢复，用不着绕。

降级结果来自官方 `keyword` 接口，**只按名字命中和使用量做了粗排，没有走字段加权 BM25**。实测这个接口排序很差：搜「AI短剧制作」，正确那条在官方结果里排第 7，本地索引是第 1。所以要告诉用户「这是降级结果，排名不代表匹配度」，别照常报「最合适的是第 1 个」。恢复方式：`node "~/.workbuddy/skills/find-experts/scripts/update.mjs" --force`（约 20~30 秒）。

这个降级是**容错**，不是「第二种搜索方式」。数据旧了就是旧了，跑 `update.mjs` 更新，不要靠实时接口绕过去——keyword 只返回匹配的那几条，覆盖面比全量索引窄得多。

## 数据长期不更新

**每天 04:30 有一个定时任务在跑**，正常情况下 `stale` 永远是 false。若发现数据长期不更新，按 `2 → 5 → 4` 的顺序排查：

- `2 auth_expired`：客户端登录态失效。让用户重新登录 WorkBuddy 桌面端，再手动跑一次 update
- `5 network_error`：网络或接口问题，1 小时内不会再重试（退避），需要时加 `--force`
- `4 count_guard`：见下

### 条数护栏

抓到的条数与上次偏差超过 **-10% ~ +30%** 时主动中止，防止「半截数据覆盖全量」。确认市场确实有大变动后加 `--force`。

## 索引版本与重建

改权重、改分词、改字段映射之后用 `--rebuild`，别为了调参反复抓全量。

但**索引版本升级（新增字段）后 `--rebuild` 会被拒绝**：旧索引里没有那些字段，重算出来是一份「版本新、字段全空」的半残索引，最坑的是排序指标看着完全正常。这时跑一次正常更新把字段补上。`index.mjs` 的 `VERSION` 变更 = 必须重新抓一次。历次 `VERSION` 改动见 `tuning.md`「索引版本记录」。

## 凭证

token 由 `scripts/lib/export-token.py` 取，落在 `<skill>/data/wb-token.env`（权限 600，`.gitignore` 已排除）。三条来源：

1. **`WB_ACCESS_TOKEN` 环境变量**（最高优先）——设了合法 JWT 就直接用，不启动 python、不碰客户端登录态。无头环境、CI 走这条。
2. **内置 `scripts/lib/client_auth.py`** —— WorkBuddy 5.6.2+ 把 `accessToken` 存成 AES-GCM 信封，这份文件用本机的 atRestSecretKey 解开：先找 DPAPI 加密的密钥文件，找不到就扫客户端进程内存。
3. **`WB_BUDDY_STATION` 指向的外部模块**（应急通道）——内置模块因客户端升级失效时，可以临时指向别的实现。

第 2 条是内联的，**不依赖任何其他 skill**。它来自 totorosir-workbuddy-score 的 `buddy_station.py`（MIT-0），按函数闭包抽出来的子集。v1.5.0 之后 Windows 的 DPAPI / 进程内存、macOS 的 Keychain / CDP 都在，只省掉了 core dump 这类用不上的途径。如果哪天客户端改了加密格式、解密报错，改这一个文件即可。

### 首次运行会弹文件读取授权窗

第一次跑（以及 token 剩余有效期不足 2 小时时重跑），`client_auth.py` 要读客户端登录态文件：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

客户端把这个路径归为敏感位置，会弹「读取文件命中询问规则」，三个选项：允许读取 / 本次会话内允许此目录 / 拒绝。**这是客户端的护栏，不是 skill 出问题**——任何读该路径的程序都会弹。

- 选允许：正常拿到信封，解出 token 存进 `data/wb-token.env`，之后在 JWT `exp` 之前不再碰客户端文件（本机实测约 27 天）
- 选拒绝：拿不到信封，退出码 2，按 `auth_expired` 处理
- 客户端没开或未登录：也是退出码 2，但报错里会按平台列出「可选项」

完全不触发这个弹窗只有两条路：设 `WB_ACCESS_TOKEN` 直接给 JWT（不读客户端任何文件），或走 CDP 起临时实例。

诊断单个环节：

```bash
python "~/.workbuddy/skills/find-experts/scripts/lib/export-token.py" --check   # 只报状态，不写盘、不打印 token
```

输出里的 `module` 字段说清走的是 `builtin` 还是外部模块，`source` 说清 token 来自环境变量还是客户端登录态。

凭证不可用时的兜底：`node "~/.workbuddy/skills/find-experts/scripts/update.mjs" --from-file <接口返回的JSON>`
