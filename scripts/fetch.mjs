// fetch.mjs  专家市场分页抓取（只读，Bearer 鉴权，page_size 服务端硬截 100）

import { ensureToken } from './token.mjs';

// 域名跟随登录态（workbuddy.cn / codebuddy.cn 都通，同一套 portal 接口）
const DEFAULT_BASE = 'https://www.workbuddy.cn';
const EP = '/portal/operation-platform/market/expert/list';
const EP_BY_IDS = '/portal/operation-platform/market/expert/get-by-ids';
const PAGE_SIZE = 100;
const CONCURRENCY = 6;

export class AuthError extends Error {}
export class NetworkError extends Error {}

// 字段语义（2026-10-23 核对原始接口后修正）：
//   profession_zh    -> title   ：市场卡片主标题，也是搜索框里"专家名称"显示的那个，用户按它找人
//   display_name_zh  -> account ：创作者/账号名，界面右上角灰字
// 之前把 display_name_zh 当 name 输出，导致推荐里给的是「仁者爱短剧导演」而不是「AI短剧制作」。
export function slim(e) {
  return {
    id: e.expert_id,
    title: e.profession_zh || '',
    account: e.display_name_zh || e.agent_name || '(未命名)',
    agent: e.agent_name || '',
    type: e.expert_type === 'team' ? 'team' : 'agent',
    src: e.source || '',
    cats: Array.isArray(e.categories_detail) ? e.categories_detail.map((c) => c.name_zh) : [],
    tags: Array.isArray(e.tags_zh) ? e.tags_zh : [],
    desc: e.description_zh || e.description_short || '',
    qp: (e.quick_prompts || []).map((q) => q.zh).filter(Boolean),
    uc: e.use_count || 0,
    rr: e.reco_rank || 0,
    icon: e.icon || '',
    upd: e.updated_at || '',
    // 以下三个列表接口就返回，不是额外请求换来的。存进索引是为了回答
    // 「去哪看源码 / 什么版本 / 多久没更新」，不用再打一次 get-by-ids。
    home: e.homepage || '',
    ver: e.version || e.plugin_version || '',
    plug: e.plugin_id || '',
  };
}

function makeRequester(token, base, ep = EP) {
  const headers = {
    accept: 'application/json',
    'content-type': 'application/json',
    origin: base,
    referer: base + '/app/experts',
    authorization: 'Bearer ' + token,
  };

  return async function req(body, tries = 3) {
    let lastErr;
    for (let i = 0; i < tries; i++) {
      let r;
      try {
        r = await fetch(base + ep, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(30000),
        });
      } catch (e) {
        lastErr = new NetworkError(e.message);
        await sleep(700 * (i + 1));
        continue;
      }
      if (r.status === 401 || r.status === 403) {
        throw new AuthError(`HTTP ${r.status}：凭证被拒，需要重新登录客户端后刷新 token`);
      }
      if (r.status === 429 || r.status >= 500) {
        lastErr = new NetworkError(`HTTP ${r.status}`);
        await sleep(800 * (i + 1));
        continue;
      }
      if (!r.ok) throw new NetworkError(`HTTP ${r.status}`);
      const j = await r.json();
      if (j.code !== 0) throw new NetworkError(`业务码 ${j.code}: ${j.msg || ''}`);
      return j.data;
    }
    throw lastErr || new NetworkError('请求失败');
  };
}

const sleep = (ms) => new Promise((s) => setTimeout(s, ms));

// 返回 { docs, totals, sourceCount, tokenExp, base }
//
// 401 会自愈一次：本地只看 JWT 的 exp，但服务端可能因为「客户端重新登录」提前作废旧 token，
// 这时 exp 还没到、本地判定有效，请求却被拒。所以遇到 AuthError 先强制重导出一次再重试，
// 仍失败才向上抛。只重试一次，避免登录态真的失效时打转。
export async function fetchAll({ log = console.error, types = ['agent', 'team'] } = {}) {
  const tk = ensureToken({ log });
  if (!tk.ok) throw new AuthError(tk.error);

  try {
    return await runFetch(tk, { log, types });
  } catch (e) {
    if (!(e instanceof AuthError)) throw e;
    log(`[token] 服务端拒绝（${e.message}），强制重新导出后重试一次`);
    const tk2 = ensureToken({ force: true, log });
    if (!tk2.ok) throw new AuthError(`凭证失效，且重新导出失败：${tk2.error}`);
    return runFetch(tk2, { log, types });
  }
}

async function runFetch(tk, { log, types }) {
  const base = tk.domain ? `https://${tk.domain}` : DEFAULT_BASE;
  const req = makeRequester(tk.token, base);
  const docs = [];
  const seen = new Set();
  const totals = {};
  let failedPages = 0;
  let pagesTotal = 0;

  for (const type of types) {
    const first = await req({ page: 1, page_size: PAGE_SIZE, expert_type: type, sort_by: 'reco_rank', sort_order: 'desc' });
    const total = first.total_count ?? 0;
    totals[type] = total;
    const pages = Math.ceil(total / PAGE_SIZE);
    pagesTotal += pages;

    const push = (arr) => {
      for (const e of arr || []) {
        if (!e || !e.expert_id || seen.has(e.expert_id)) continue;
        seen.add(e.expert_id);
        docs.push(slim(e));
      }
    };
    push(first.experts);

    let next = 2;
    await Promise.all(
      Array.from({ length: CONCURRENCY }, async () => {
        while (true) {
          const p = next++;
          if (p > pages) return;
          try {
            push((await req({ page: p, page_size: PAGE_SIZE, expert_type: type, sort_by: 'reco_rank', sort_order: 'desc' })).experts);
          } catch (e) {
            if (e instanceof AuthError) throw e;
            failedPages++;
            log(`[fetch] ${type} 第 ${p} 页失败：${e.message}`);
          }
        }
      })
    );
    log(`[fetch] ${type}: ${pages} 页 / 声明总数 ${total} / 实际收 ${docs.filter((d) => d.type === type).length}`);
  }

  if (failedPages) log(`[fetch] 共 ${failedPages}/${pagesTotal} 页失败，结果不完整`);

  const sourceCount = {};
  for (const d of docs) sourceCount[d.src || 'unknown'] = (sourceCount[d.src || 'unknown'] || 0) + 1;

  return { docs, totals, sourceCount, tokenExp: tk.exp, failedPages, pagesTotal };
}

// 实时关键字检索：不是「另一种搜索方式」，只在本地索引不可用时兜底。
// 官方 keyword 是服务端字符串模糊匹配，无字段加权，实测排序明显差于本地 BM25
// （搜「AI短剧制作」正确的一条排第 7），所以调用方必须标注结果未精排。
export async function searchLive(keyword, { top = 20, type = null, log = console.error } = {}) {
  const tk = ensureToken({ log });
  if (!tk.ok) throw new AuthError(tk.error);
  const base = tk.domain ? `https://${tk.domain}` : DEFAULT_BASE;
  const req = makeRequester(tk.token, base);
  const body = { keyword, page: 1, page_size: Math.min(PAGE_SIZE, Math.max(top, 20)) };
  if (type) body.expert_type = type;
  const data = await req(body);
  const arr = data.experts || [];
  return { docs: arr.map(slim), total: data.total_count ?? arr.length };
}

// 按 id 取详情。比 list 接口多几个字段，但不保证都有：
// download_url（安装包，常有）、developer（作者）、fingerprint。
// 实测 openapi 来源 32 字段三个都有，official_sync 来源 31 字段缺 developer/fingerprint。
// 其余 29 个字段与 list 完全相同，所以这不是「查详情」，是「查作者和安装包」。
export async function getByIds(ids, { log = console.error } = {}) {
  const tk = ensureToken({ log });
  if (!tk.ok) throw new AuthError(tk.error);
  const base = tk.domain ? `https://${tk.domain}` : DEFAULT_BASE;
  const req = makeRequester(tk.token, base, EP_BY_IDS);
  const data = await req({ expert_ids: ids });
  return { raw: data.experts || [], total: data.total_count ?? 0 };
}
