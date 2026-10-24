#!/usr/bin/env node
// update.mjs  刷新专家市场索引
//   1. 预检 token（不足 2 小时先刷新）
//   2. 翻页抓全量 agent + team
//   3. 护栏校验（条数突变则中止，保留旧索引）
//   4. 原子替换 index.bin / meta.json / history.json
//
// 用法：
//   node update.mjs                 正常刷新
//   node update.mjs --force         忽略护栏与失败退避
//   node update.mjs --rebuild       不联网，用现有数据重算索引（调权重/分词时用）
//   node update.mjs --from-file x.json  从接口返回的 JSON 文件导入（凭证不可用时的兜底）
//
// 退出码：0 成功 / 2 凭证失败 / 3 已有更新在跑 / 4 条数护栏 / 5 网络失败 / 6 失败退避中

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { fetchAll, slim, AuthError } from './fetch.mjs';
import { writeIndex, readIndex } from './index.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DATA = path.join(ROOT, 'data');
const IDX = path.join(DATA, 'index.bin');
const META = path.join(DATA, 'meta.json');
const HIST = path.join(DATA, 'history.json');
const LOCK = path.join(DATA, '.lock');

const LOCK_TTL_MS = 10 * 60 * 1000;
const BACKOFF_MS = 60 * 60 * 1000;
const SCHEMA_VERSION = 1;
const GUARD_LOW = 0.9;
const GUARD_HIGH = 1.3;
// 页级失败上限。条数护栏只看总量比例，232 页里挂 25 页（约 2500 条、10%）它照样放行，
// 所以这一条单独判，而且**不受 --force 影响**：条数突变可能是市场真变了，
// 页抓失败则是确定性错误，残缺索引没有任何价值。
const FAIL_PAGE_PCT = 0.03;
const HISTORY_KEEP_DAYS = 90;

const argv = process.argv.slice(2);
const has = (f) => argv.includes(f);
const opt = (f, d = null) => {
  const i = argv.indexOf(f);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d;
};
const FORCE = has('--force') || has('-f');
const FROM_FILE = opt('--from-file');
const REBUILD = has('--rebuild');
const QUIET = has('--quiet') || has('-q');

const log = (...a) => { if (!QUIET) console.error(...a); };
const nowIso = () => new Date().toISOString();
const localStamp = (d = new Date()) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

function readJson(p, dflt = null) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return dflt; }
}

function pidAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function acquireLock() {
  if (fs.existsSync(LOCK)) {
    const j = readJson(LOCK, {});
    const fresh = Date.now() - (j.ts || 0) < LOCK_TTL_MS;
    if (fresh && j.pid && pidAlive(j.pid)) return false;
    log('[lock] 发现过期锁，接管');
  }
  fs.mkdirSync(DATA, { recursive: true });
  fs.writeFileSync(LOCK, JSON.stringify({ pid: process.pid, ts: Date.now() }));
  return true;
}

const releaseLock = () => { try { fs.unlinkSync(LOCK); } catch { /* ignore */ } };

function writeMeta(meta) {
  const tmp = META + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(meta, null, 2), 'utf8');
  fs.renameSync(tmp, META);
}

// 失败时保留旧数据，只把状态写进 meta
function markFailure(oldMeta, status, error) {
  if (!oldMeta) return;
  writeMeta({
    ...oldMeta,
    status,
    last_attempt: nowIso(),
    last_error: String(error || '').slice(0, 400),
  });
}

function loadFromFile(file) {
  const j = JSON.parse(fs.readFileSync(file, 'utf8'));
  let arr = null;
  if (Array.isArray(j)) arr = j;
  else if (Array.isArray(j.experts)) arr = j.experts;
  else if (j.data && Array.isArray(j.data.experts)) arr = j.data.experts;
  if (!arr) throw new Error('文件里找不到 experts 数组（支持 {data:{experts:[]}} / {experts:[]} / []）');
  const seen = new Set();
  const docs = [];
  for (const e of arr) {
    if (!e || !e.expert_id || seen.has(e.expert_id)) continue;
    seen.add(e.expert_id);
    docs.push(slim(e));
  }
  return { docs, totals: { file: docs.length }, sourceCount: {}, tokenExp: null, failedPages: 0 };
}

function mergeHistory(oldHist, docs) {
  const hist = oldHist || {};
  const today = localStamp();
  const seen = new Set();
  for (const d of docs) {
    seen.add(d.id);
    const cur = hist[d.id];
    hist[d.id] = cur ? [cur[0], today] : [today, today];
  }
  const cutoff = localStamp(new Date(Date.now() - HISTORY_KEEP_DAYS * 86400000));
  let pruned = 0;
  for (const [id, v] of Object.entries(hist)) {
    if (!seen.has(id) && v[1] < cutoff) { delete hist[id]; pruned++; }
  }
  const added = docs.filter((d) => {
    const v = hist[d.id];
    return v && v[0] === today && v[1] === today;
  }).length;
  return { hist, added, pruned, tracked: Object.keys(hist).length };
}

async function main() {
  fs.mkdirSync(DATA, { recursive: true });
  const t0 = Date.now();
  const oldMeta = readJson(META);
  const oldTotal = oldMeta && oldMeta.status === 'ok' ? oldMeta.total : 0;

  // 失败退避：刚失败过就别每次都重试，避免每次搜索都吃超时
  if (!FORCE && !FROM_FILE && !REBUILD && oldMeta && oldMeta.status !== 'ok' && oldMeta.last_attempt) {
    const since = Date.now() - new Date(oldMeta.last_attempt).getTime();
    if (since < BACKOFF_MS) {
      log(`[backoff] 上次更新失败于 ${Math.round(since / 60000)} 分钟前（${oldMeta.status}），1 小时内不再重试`);
      return 6;
    }
  }

  if (!acquireLock()) {
    log('[lock] 已有更新进程在跑，跳过');
    return 3;
  }

  try {
    let res;
    if (REBUILD) {
      // 只重算索引，不联网：改权重、改分词、改字段映射后用它快速验证，不用等一轮抓取
      log('[rebuild] 从现有 index.bin 重算，不联网');
      const old = readIndex(IDX, { checkVersion: false });
      const docs = [];
      for (let i = 0; i < old.N; i++) docs.push(old.doc(i));
      // 统计口径取自外层 meta.json，index 内嵌的 meta 只有 generated_at/total，
      // 早先从这里读会把 totals_declared 和 sources 清空。
      res = {
        docs,
        totals: (oldMeta && oldMeta.totals_declared) || {},
        sourceCount: (oldMeta && oldMeta.sources) || {},
        tokenExp: null,
        failedPages: 0,
      };
    } else if (FROM_FILE) {
      log(`[fetch] 从文件导入：${FROM_FILE}`);
      res = loadFromFile(FROM_FILE);
    } else {
      res = await fetchAll({ log });
    }

    const docs = res.docs;
    if (!docs.length) throw new Error('抓取结果为空');

    // rebuild 只能重算已有字段。索引版本升级新增字段时，旧索引里的文档没有这些 key，
    // rebuild 出来会是一份「版本新、字段空」的半残索引，find.mjs 会显示成空字符串。
    // 所以这里直接拦住，让人跑一次正常更新把字段补上。
    if (REBUILD && docs[0].home === undefined) {
      log('[rebuild] 索引是旧版本（缺 home/ver/plug 字段），rebuild 补不出这些值。先跑一次正常更新：node update.mjs');
      return 1;
    }

    // 页级失败护栏：先于条数护栏判。失败页是确定性错误，--force 也不放行。
    const failedPages = res.failedPages || 0;
    const pagesTotal = res.pagesTotal || 0;
    if (pagesTotal && failedPages / pagesTotal > FAIL_PAGE_PCT) {
      const pct = ((failedPages / pagesTotal) * 100).toFixed(1);
      const msg = `抓取不完整：${failedPages}/${pagesTotal} 页失败（${pct}%），超过 ${(FAIL_PAGE_PCT * 100).toFixed(0)}% 上限`;
      markFailure(oldMeta, 'incomplete', msg);
      log('[guard] ' + msg + '，已保留旧索引');
      return 4;
    }

    // 条数护栏：市场总量突变通常意味着接口异常或抓取中断
    if (!FORCE && oldTotal >= 1000) {
      const ratio = docs.length / oldTotal;
      if (ratio < GUARD_LOW || ratio > GUARD_HIGH) {
        const msg = `条数异常：新 ${docs.length} / 旧 ${oldTotal} = ${ratio.toFixed(3)}，超出 [${GUARD_LOW}, ${GUARD_HIGH}]`;
        markFailure(oldMeta, 'count_guard', msg);
        log('[guard] ' + msg);
        return 4;
      }
    }

    const agents = docs.filter((d) => d.type === 'agent').length;
    const teams = docs.length - agents;
    const cats = {};
    for (const d of docs) for (const c of d.cats) cats[c] = (cats[c] || 0) + 1;

    const h = mergeHistory(readJson(HIST), docs);
    const build = writeIndex(IDX, docs, { generated_at: nowIso(), total: docs.length });

    // 凭证到期时间：token 刷新依赖外部解密模块，模块被移除后只有到期才暴露问题，
    // 所以把剩余天数落进 meta，让 find.mjs 能提前预警。
    const expMs = res.tokenExp
      ? res.tokenExp.getTime()
      : (oldMeta && oldMeta.token_exp ? new Date(oldMeta.token_exp).getTime() : null);
    const daysLeft = expMs ? Math.round(((expMs - Date.now()) / 86400000) * 10) / 10 : null;

    // rebuild 不碰数据，只是重算索引，所以「数据日期」必须沿用旧的。
    // 否则连续 rebuild 会让 stale 永远为 false，数据过期了也不提醒。
    const dataUpdatedAt = REBUILD && oldMeta && oldMeta.data_updated_at
      ? oldMeta.data_updated_at
      : nowIso();

    const meta = {
      schema_version: SCHEMA_VERSION,
      // 少量页失败时照样写盘（丢 1~2 页不值得放弃整轮），但状态要如实标 partial，
      // find.mjs 看到非 ok 就会提示「本次刷新缺 N 页」。以前这里是硬编码 'ok'，
      // failed_pages 写进 meta 之后没有任何代码读它，等于白记。
      status: failedPages ? 'partial' : 'ok',
      generated_at: nowIso(),
      data_updated_at: dataUpdatedAt,
      total: docs.length,
      agents,
      teams,
      totals_declared: res.totals,
      failed_pages: failedPages,
      sources: res.sourceCount || {},
      cats,
      token_exp: res.tokenExp ? res.tokenExp.toISOString() : (oldMeta && oldMeta.token_exp) || null,
      token_days_left: daysLeft,
      last_attempt: nowIso(),
      last_error: null,
      build_ms: Date.now() - t0,
      index_bytes: build.bytes,
      vocab: build.vocab,
      postings: build.postings,
      avg_doc_len: Math.round(build.avgLen * 10) / 10,
      history: { tracked: h.tracked, added_today: h.added, pruned: h.pruned },
    };

    const htmp = HIST + '.tmp';
    fs.writeFileSync(htmp, JSON.stringify(h.hist), 'utf8');
    fs.renameSync(htmp, HIST);
    writeMeta(meta);

    log(`[done] ${docs.length} 条（agent ${agents} / team ${teams}），`
      + `词表 ${build.vocab}，倒排 ${build.postings}，`
      + `${(build.bytes / 1048576).toFixed(1)} MB，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    if (oldTotal) log(`[done] 较上次 ${oldTotal} 条：${docs.length - oldTotal >= 0 ? '+' : ''}${docs.length - oldTotal}`);

    if (has('--json')) console.log(JSON.stringify(meta));
    return 0;
  } catch (e) {
    const status = e instanceof AuthError ? 'auth_expired' : 'network_error';
    markFailure(oldMeta, status, e.message);
    log(`[fail] ${status}: ${e.message}`);
    return status === 'auth_expired' ? 2 : 5;
  } finally {
    releaseLock();
  }
}

main().then((code) => process.exit(code));
