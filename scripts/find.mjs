#!/usr/bin/env node
// find.mjs  语义召回入口：BM25（字段加权）从全量专家里召回候选，交给 agent 精排。
//
// 用法：
//   node find.mjs "我这堆访谈录音想整理成书" --top 20 --json
//   node find.mjs "小红书怎么涨粉" --cat 营销增长 --type agent
//   node find.mjs "..." --refresh        先刷新再检索
//
// 说明：本脚本只做「召回」，不做最终判断。字面相似不等于需求匹配，
//      最终选哪 3 个由调用它的 agent 读 hits 后再定。

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readIndex } from './lib/index.mjs';
import { queryGrams, BM25, ucBoost, makeTitleBoost } from './lib/text.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const DATA = path.join(ROOT, 'data');
const IDX = path.join(DATA, 'index.bin');
const META = path.join(DATA, 'meta.json');
const UPDATE = path.join(HERE, 'update.mjs');

const TTL_DAYS = 7;

const argv = process.argv.slice(2);
const FLAGS = new Set(['--json', '--refresh', '--explain', '--quiet', '--help', '-h']);
const VALUED = new Set(['--top', '--cat', '--type', '--detail', '--open']);

const opts = { top: 20, cat: null, type: null, json: false, refresh: false, explain: false, quiet: false, detail: null, open: null };
const words = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--help' || a === '-h') {
    console.log('用法: node find.mjs "<需求>" [--top 20] [--cat 分类] [--type agent|team] [--json] [--refresh] [--explain]');
    console.log('      node find.mjs --detail <expert_id> [--json]     查作者与安装包（不一定有 developer，official_sync 来源的没有）');
    console.log('      node find.mjs --open <expert_id>                直接调起客户端打开这个专家');
    process.exit(0);
  }
  if (a === '--json' || a === '--refresh' || a === '--explain' || a === '--quiet') { opts[a.slice(2)] = true; continue; }
  if (VALUED.has(a)) {
    const v = argv[++i];
    if (a === '--top') opts.top = Math.max(1, Math.min(100, parseInt(v, 10) || 20));
    else if (a === '--cat') opts.cat = v;
    else if (a === '--detail') opts.detail = v;
    else if (a === '--open') opts.open = v;
    else opts.type = v;
    continue;
  }
  if (FLAGS.has(a)) continue;
  words.push(a);
}

const query = words.join(' ').trim();
if (!query && !opts.detail && !opts.open) {
  console.error('缺少查询内容。用法: node find.mjs "<需求>" [--top 20] [--json]');
  process.exit(1);
}

const say = (...a) => { if (!opts.quiet) console.error(...a); };
const readMeta = () => { try { return JSON.parse(fs.readFileSync(META, 'utf8')); } catch { return null; } };

function runUpdate(extra = []) {
  say('[update] 数据缺失或需要刷新，先抓取…');
  const r = spawnSync(process.execPath, [UPDATE, ...extra], { stdio: ['ignore', 'ignore', 'inherit'] });
  return r.status;
}

// 专家详情深链。格式从客户端 app.asar 里挖出来的：
//   isMarketDetailDeepLink 用 LEGACY_MARKET_DETAIL_ID_KEYS 判 host，
//   experts/expert -> expertId，且接受 `id` 或 `expertId` 任一参数名。
// 主进程命中后走专门的 market detail 分支（app:openUrl IPC），不是普通路由。
//
// 之前用 `https://www.workbuddy.cn/app/experts?keyword=` 是错的：那是 SPA 壳，
// 前端没有解析 keyword 的代码，点开只会落到应用首页。web 端没有专家市场。
const openUrl = (id) => `workbuddy://experts?id=${encodeURIComponent(id || '')}`;

// 名字直达先验由 lib/text.mjs 提供，selftest 用的是同一份实现，避免两边打分逻辑分叉。
const titleBoost = makeTitleBoost(query);

// 条目统一构造，降级路径也走这里，免得 agent 要分辨两套结构
function hitOf(d, rank, extra = {}) {
  return {
    rank,
    title: d.title,
    account: d.account,
    type: d.type,
    cats: d.cats,
    tags: d.tags,
    desc: d.desc,
    uc: d.uc,
    src: d.src,
    upd: d.upd,
    ver: d.ver,
    home: d.home,
    plug: d.plug,
    open_url: openUrl(d.id),
    ...extra,
  };
}

// --open：把客户端切到这个专家的详情页。走系统注册的 workbuddy:// 协议，
// 不依赖聊天界面允不允许点自定义协议。
// 注意会离开当前对话（详情页没有返回栈），所以是显式动作，别当默认落地方式。
if (opts.open) {
  const url = openUrl(opts.open);
  let r;
  if (process.platform === 'win32') r = spawnSync('cmd.exe', ['/c', 'start', '', url], { stdio: 'ignore' });
  else if (process.platform === 'darwin') r = spawnSync('open', [url], { stdio: 'ignore' });
  else r = spawnSync('xdg-open', [url], { stdio: 'ignore' });
  if (r.error || r.status) {
    console.error(`调起客户端失败：${r.error ? r.error.message : `退出码 ${r.status}`}`);
    console.error(`手动打开：${url}`);
    process.exit(1);
  }
  console.log(`已发给客户端：${url}`);
  console.log('WorkBuddy 窗口应该已经切到这个专家的详情页。');
  console.log('');
  console.log('注意：详情页上点「召唤」会切到一个新的专家会话，不是在当前对话里换角色。');
  console.log('当前这个对话还在左侧会话列表里，点一下就回来。');
  process.exit(0);
}

// --detail：查作者和安装包。这两个字段只有 get-by-ids 有（list 的 29 个字段里没有），
// 也只有这两个是新的——其余字段和检索结果完全相同，所以它不是「看更多描述」。
if (opts.detail) {
  const { getByIds } = await import('./lib/fetch.mjs');
  let raw;
  try {
    ({ raw } = await getByIds([opts.detail], { log: say }));
  } catch (e) {
    const auth = e?.constructor?.name === 'AuthError';
    console.error(`查询失败：${e.message}`);
    console.error(auth ? '客户端登录态失效，在 WorkBuddy 桌面端重新登录后重试' : '网络不可用，稍后重试');
    process.exit(auth ? 2 : 5);
  }
  if (!raw.length) {
    console.error(`接口没有返回 ${opts.detail}。id 写错了，或者这个专家已下架。`);
    process.exit(1);
  }
  const e = raw[0];
  const dev = e.developer || {};
  const detail = {
    id: e.expert_id,
    title: e.profession_zh || '',
    account: e.display_name_zh || '',
    type: e.expert_type === 'team' ? 'team' : 'agent',
    cats: (e.categories_detail || []).map((c) => c.name_zh),
    tags: e.tags_zh || [],
    desc: e.description_zh || '',
    version: e.version || e.plugin_version || '',
    plugin_id: e.plugin_id || '',
    homepage: e.homepage || '',
    lifecycle: e.lifecycle_status || '',
    created_at: e.created_at || '',
    updated_at: e.updated_at || '',
    developer: { party_id: dev.party_id || '', name: dev.display_nickname || '', avatar: dev.avatar_cos_url || '' },
    download_url: e.download_url || '',
    open_url: openUrl(e.expert_id),
  };
  if (opts.json) {
    console.log(JSON.stringify(detail, null, 2));
  } else {
    console.log(`${detail.title}   [${detail.type}]   ${detail.id}`);
    console.log(`账号 ${detail.account || '-'}${detail.developer.name ? ` · 开发者 ${detail.developer.name}` : ''}`);
    console.log(`分类 ${detail.cats.join(' / ') || '-'} | 版本 ${detail.version || '-'} | ${detail.lifecycle || '-'}`);
    console.log(`发布 ${detail.created_at.slice(0, 10) || '-'} | 更新 ${detail.updated_at.slice(0, 10) || '-'}`);
    console.log(`\n${detail.desc}`);
    if (detail.tags.length) console.log(`\n标签 ${detail.tags.join(' / ')}`);
    if (detail.homepage) console.log(`主页 ${detail.homepage}`);
    console.log(`打开 ${detail.open_url}`);
    if (detail.download_url) console.log(`安装包 ${detail.download_url}`);
  }
  process.exit(0);
}

// 退出码 → 给人看的处置方式。只甩数字没人知道该怎么办。
const HINT = {
  2: '客户端登录态失效。在 WorkBuddy 桌面端重新登录后重试',
  3: '另一个更新进程正在跑。等它结束后重跑这条查询即可',
  4: '条数护栏触发（新数据与上次偏差过大）。确认市场确实有变动后跑 update.mjs --force',
  5: '网络或接口失败，脚本已进入 1 小时退避。跑 update.mjs --force 可立即重试',
};
// 硬失败：本地没有可用数据，而实时接口同样救不了（凭证失效/网络不通），
// 或者本地状态得人工处理（并发锁/护栏）。这时降级只是换个地方报同一个错，直接退出。
function hardFail(code, prefix) {
  console.error(`${prefix}（退出码 ${code}）：${HINT[code] || '详见 update.mjs 的日志'}`);
  process.exit(code || 1);
}

// 首次使用自愈：没有索引就抓一次，不问用户（问了也只有「是」一个选项）
let degraded = null;
if (!fs.existsSync(IDX)) {
  const code = runUpdate();
  if (code === 6) degraded = '本地索引不存在，update.mjs 处于失败退避期';
  else if (code !== 0) hardFail(code, '索引初始化失败');
}

if (opts.refresh) {
  const code = runUpdate(['--force']);
  if (code === 0) degraded = null;   // 刷新成功，降级解除
  else say(`[update] 刷新失败（退出码 ${code}）${degraded ? '，继续降级' : '，继续使用现有数据'}`);
}

// 索引读不出来先尝试自修：损坏的索引重建一次就好了，不该直接退到实时接口。
// 只有重建也被挡住（退避期）、或重建后仍读不出，才降级。
let idx = null;
if (!degraded) {
  try {
    idx = readIndex(IDX);
  } catch (e) {
    say(`[index] ${e.message}，尝试重建`);
    const code = runUpdate();
    if (code === 0) {
      try {
        idx = readIndex(IDX);
      } catch (e2) {
        degraded = `索引重建后仍读不出来（${e2.message}）`;
      }
    } else if (code === 6) {
      degraded = `本地索引损坏，且 update.mjs 处于失败退避期（${e.message}）`;
    } else {
      hardFail(code, '索引损坏后重建失败');
    }
  }
}

if (degraded) {
  say(`[degraded] ${degraded}，改走实时接口兜底`);
  let live;
  try {
    const { searchLive } = await import('./lib/fetch.mjs');
    live = await searchLive(query, { top: opts.top, type: opts.type, log: say });
  } catch (e) {
    const auth = e?.constructor?.name === 'AuthError';
    console.error(`${degraded}；实时兜底也失败：${e.message}`);
    console.error(auth
      ? '客户端登录态失效，在 WorkBuddy 桌面端重新登录后重试'
      : '网络不可用，稍后重试；网络恢复后跑 update.mjs --force 重建索引');
    process.exit(auth ? 2 : 5);
  }
  // 服务端 keyword 排序不可信（实测搜「AI短剧制作」正确那条排第 7），本地只做一次粗排：
  // 名字命中优先，其次使用量。不做 BM25，因为手里没有倒排表。
  const scored = live.docs.map((d) => ({
    d,
    s: titleBoost(d.title) * ucBoost(d.uc),
  }));
  scored.sort((x, y) => y.s - x.s);
  const dhits = scored.slice(0, opts.top).map(({ d }, n) => hitOf(d, n + 1, { score: null, id: d.id, icon: d.icon }));

  const dout = {
    query,
    total: live.total,
    degraded: true,
    degraded_reason: degraded,
    note: '本地索引不可用，以下是实时接口的结果：只按名字命中和使用量粗排，没有走字段加权 BM25，'
      + '排名不代表匹配度，可能漏掉更合适的专家。网络恢复后跑 update.mjs --force 重建索引即可恢复完整检索。',
    hits: dhits,
  };
  if (opts.json) {
    console.log(JSON.stringify(dout, null, 2));
  } else {
    console.log(`需求：${query}`);
    console.log(`⚠ 降级模式：${degraded}`);
    console.log(`  接口返回 ${live.total} 条，未精排。恢复方式：node update.mjs --force\n`);
    for (const h of dhits) {
      console.log(`#${h.rank}  ${h.title}   [${h.type}]`);
      console.log(`    账号 ${h.account || '-'} · ${h.id}`);
      console.log(`    ${(h.desc || '').slice(0, 80)}`);
      console.log();
    }
    if (!dhits.length) console.log('实时接口也没匹配到——换个说法，或恢复索引后再查。');
  }
  process.exit(0);
}

const meta = readMeta() || {};
const updatedAt = meta.data_updated_at ? new Date(meta.data_updated_at) : null;
const ageDays = updatedAt ? Math.floor((Date.now() - updatedAt.getTime()) / 86400000) : null;
const stale = ageDays === null || ageDays >= TTL_DAYS;

const t0 = Date.now();
const { N, avgLen, lens, offsets, docIds, tfs, vocabMap } = idx;
const { k1, b } = BM25;
const scores = new Float64Array(N);
const qg = queryGrams(query);

let matchedGrams = 0;
for (const g of qg) {
  const gi = vocabMap.get(g);
  if (gi === undefined) continue;
  matchedGrams++;
  const start = offsets[gi];
  const end = offsets[gi + 1];
  const n = end - start;
  const idf = Math.log(1 + (N - n + 0.5) / (n + 0.5));
  for (let i = start; i < end; i++) {
    const d = docIds[i];
    const f = tfs[i];
    scores[d] += (idf * (f * (k1 + 1))) / (f + k1 * (1 - b + (b * lens[d]) / avgLen));
  }
}

// 先按 BM25 取较宽的候选，再叠加 use_count 与过滤条件
const rough = [];
for (let i = 0; i < N; i++) if (scores[i] > 0) rough.push(i);
rough.sort((x, y) => scores[y] - scores[x]);
const pool = rough.slice(0, Math.max(opts.top * 15, 200));

const catHit = (cats) => !opts.cat || cats.some((c) => c.includes(opts.cat) || opts.cat.includes(c));

const final = [];
for (const i of pool) {
  const d = idx.doc(i);
  if (opts.type && d.type !== opts.type) continue;
  if (!catHit(d.cats || [])) continue;
  final.push({ i, d, score: scores[i] * ucBoost(d.uc) * titleBoost(d.title) });
}
final.sort((x, y) => (y.score - x.score) || ((x.d.rr || 1e9) - (y.d.rr || 1e9)));

const hits = final.slice(0, opts.top).map((h, n) => {
  const text = [h.d.title, h.d.account, (h.d.tags || []).join(' '), h.d.desc, (h.d.qp || []).join(' ')]
    .join(' ')
    .replace(/[\s\p{P}]/gu, '');
  const matched = [...new Set(qg.filter((g) => text.includes(g)))].slice(0, 8);
  return hitOf(h.d, n + 1, {
    score: Math.round(h.score * 100) / 100,
    id: h.d.id,
    rr: h.d.rr,
    qp: h.d.qp,
    icon: h.d.icon,
    matched: opts.explain ? matched : undefined,
  });
});

const out = {
  query,
  total: N,
  data_updated: updatedAt ? updatedAt.toISOString().slice(0, 10) : null,
  age_days: ageDays,
  stale,
  status: meta.status || 'unknown',
  warning: meta.status && meta.status !== 'ok'
    ? `数据刷新失败（${meta.status}），当前为 ${meta.data_updated_at ? meta.data_updated_at.slice(0, 10) : '更早'} 的快照`
    : null,
  // 凭证剩余天数：token 刷新要靠外部解密模块，模块不在时只有到期才暴露。
  // 低于 7 天提前说，别等数据停更了才发现。
  token_days_left: typeof meta.token_days_left === 'number' ? meta.token_days_left : null,
  token_warning: (typeof meta.token_days_left === 'number' && meta.token_days_left < 7)
    ? `登录凭证 ${meta.token_days_left} 天后过期，过期后数据无法刷新（检索仍可用）。续期方式见 SKILL.md「凭证从哪来」`
    : null,
  matched_grams: matchedGrams,
  elapsed_ms: Date.now() - t0,
  // hits 为空时给个可执行的原因。--cat 是精确匹配，写错一个字就整批筛空，
  // 光看 hits:[] 会误判成「这个词搜不到」。
  hint: hits.length ? null : catHint(),
  hits,
};

function catHint() {
  const cats = Object.keys((meta && meta.cats) || {});
  if (opts.cat && cats.length && !cats.includes(opts.cat)) {
    return `分类「${opts.cat}」不在市场里。可选：${cats.join(' / ')}`;
  }
  return '没有召回结果，换更贴近业务的说法，或放宽 --cat / --type';
}

if (opts.json) {
  console.log(JSON.stringify(out, null, 2));
} else {
  console.log(`需求：${query}`);
  console.log(`库：${N} 条 | 数据日期：${out.data_updated}${stale ? `（已 ${ageDays} 天，建议刷新）` : ''}`
    + ` | 命中 gram ${matchedGrams}/${qg.length} | ${out.elapsed_ms}ms\n`);
  if (out.warning) console.log(`⚠ ${out.warning}\n`);
  if (out.token_warning) console.log(`⚠ ${out.token_warning}\n`);
  for (const h of hits) {
    console.log(`#${h.rank}  ${h.title}   [${h.type}]  ${h.score}`);
    console.log(`    账号 ${h.account || '-'} · ${h.id}`);
    console.log(`    ${h.desc.slice(0, 80)}`);
    if (h.tags && h.tags.length) console.log(`    标签：${h.tags.join(' / ')} | 使用 ${h.uc}`);
    console.log();
  }
  if (!hits.length) console.log(out.hint);
}
