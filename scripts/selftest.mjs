#!/usr/bin/env node
// selftest.mjs  回归用例：改权重/改分词后跑一遍，防止悄悄退化。
// 用法: node selftest.mjs [--verbose]

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readIndex } from './lib/index.mjs';
import { queryGrams, BM25, ucBoost, makeTitleBoost } from './lib/text.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA = path.resolve(HERE, '..', 'data');
const verbose = process.argv.includes('--verbose');

// gold 用正则匹配「主标题 + 账号名」，命中即算对
const CASES = [
  { q: '我这堆访谈录音和扫描件，想整理成一本二十万字的书', gold: /长文档|写作与改稿|长卷/ },
  { q: '公司想卖一部分老股套现，对方报价怎么判断合不合理', gold: /并购|股权|估值/ },
  { q: '孩子明年高考，600分能上什么学校', gold: /志愿/ },
  { q: '小红书写了半年没起色，怎么涨粉', gold: /小红书/ },
  { q: '帮我审一下这段代码有没有安全漏洞', gold: /代码审计|安全审计|代码审查|安全工程/ },
  { q: '我们公司要申请政府补贴，材料不知道怎么写', gold: /补贴|政策|申报|政府/ },
  { q: '我做了个拼豆图纸想做成一幅画，怎么弄', gold: /拼豆|像素/ },
  // 按名字直达：标题完全等于查询时必须排第一。这条专门守 titleBoost，
  // 没有它的话先验退化或被误删，上面 7 条口语用例照样全过。
  { q: 'AI短剧制作', exactTitle: true },
];

const idx = readIndex(path.join(DATA, 'index.bin'));
const { N, avgLen, lens, offsets, docIds, tfs, vocabMap } = idx;
const { k1, b } = BM25;

function search(query, topk = 30) {
  const scores = new Float64Array(N);
  for (const g of queryGrams(query)) {
    const gi = vocabMap.get(g);
    if (gi === undefined) continue;
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
  const out = [];
  // 打分和 find.mjs 共用 lib/text.mjs 的 ucBoost / makeTitleBoost，
  // 否则调了权重或先验之后，测试还在跑旧逻辑，指标看着正常但和线上不一致。
  const tb = makeTitleBoost(query);
  for (let i = 0; i < N; i++) {
    if (scores[i] <= 0) continue;
    const d = idx.doc(i);
    out.push({ s: scores[i] * ucBoost(d.uc) * tb(d.title), d });
  }
  out.sort((x, y) => y.s - x.s);
  return out.slice(0, topk);
}

let r5 = 0;
let r10 = 0;
let top1 = 0;
console.log(`语料 ${N} 条\n`);
for (const c of CASES) {
  const top = search(c.q, 30);
  const rank = (c.exactTitle
    ? top.findIndex((h) => (h.d.title || '') === c.q)
    : top.findIndex((h) => c.gold.test((h.d.title || '') + (h.d.account || '')))) + 1;
  if (rank === 1) top1++;
  if (rank > 0 && rank <= 5) r5++;
  if (rank > 0 && rank <= 10) r10++;
  console.log(`[${rank > 0 ? '#' + rank : 'miss'}] ${c.q}`);
  if (verbose || rank !== 1) {
    top.slice(0, 3).forEach((h, i) => console.log(`        ${i + 1}. ${h.d.title} | ${h.d.account} (${h.s.toFixed(1)})`));
  }
}
const n = CASES.length;
console.log(`\nTop1 ${top1}/${n}   Recall@5 ${r5}/${n}   Recall@10 ${r10}/${n}`);

// 字段映射守卫：title/account 曾因接口语义理解反了而整批错位，
// 排序指标看不出来（照样出结果），但报给用户的名字是错的。这里卡住空值率。
let emptyTitle = 0;
let emptyAccount = 0;
let emptyDesc = 0;
let emptyVer = 0;
let emptyPlug = 0;
for (let i = 0; i < N; i++) {
  const d = idx.doc(i);
  if (!d.title) emptyTitle++;
  if (!d.account) emptyAccount++;
  if (!d.desc) emptyDesc++;
  if (!d.ver) emptyVer++;
  if (!d.plug) emptyPlug++;
}
const pct = (x) => ((x / N) * 100).toFixed(2) + '%';
console.log(`字段完整性：title 空 ${pct(emptyTitle)} / account 空 ${pct(emptyAccount)} / desc 空 ${pct(emptyDesc)}`
  + ` / ver 空 ${pct(emptyVer)} / plug 空 ${pct(emptyPlug)}`);
// ver/plug 是索引 v3 新增字段。跑 --rebuild 从旧索引重算、或字段映射改名时会整批空掉，
// 而排序指标照样正常，所以一并卡住。
const fieldsOk = emptyTitle / N <= 0.02 && emptyVer / N <= 0.05 && emptyPlug / N <= 0.05;
if (emptyTitle / N > 0.02) console.log('✗ title 空值率过高，检查 fetch.mjs 的 slim() 字段映射');
if (!fieldsOk && emptyTitle / N <= 0.02) console.log('✗ ver/plug 大面积为空，索引可能是旧版本，跑一次 node update.mjs');

process.exit(r10 >= n - 1 && fieldsOk ? 0 : 1);
