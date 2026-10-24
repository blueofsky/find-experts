#!/usr/bin/env node
// deploy.mjs  开发环境 → 生产环境单向同步
//
// 只在仓库里用，不进发布包（pack.mjs 的 SKIP 里有它）。
//
// 同步范围（与 pack.mjs 的 RELEASE 一致）：
//   SKILL.md, LICENSE, references/, scripts/   —— pack.mjs、deploy.mjs 跳过
// 绝不触碰目标端的：
//   data/（索引 + token）、dist/、.git、.workbuddy
//
// 用法：
//   node scripts/deploy.mjs                 # 同步到 ~/.workbuddy/skills/find-experts
//   node scripts/deploy.mjs --dry-run       # 只列差异，不动文件
//   node scripts/deploy.mjs --to <目录>     # 指定目标（必须是已存在的 skill 目录）
//   node scripts/deploy.mjs --verify        # 同步后跑一次真实检索冒烟

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(HERE, '..');

const DEFAULT_TARGET = path.join(os.homedir(), '.workbuddy', 'skills', 'find-experts');
const ROOT_FILES = ['SKILL.md', 'LICENSE'];
const DIRS = ['references', 'scripts'];
const SKIP_NAMES = new Set(['pack.mjs', 'deploy.mjs']);

const args = process.argv.slice(2);
const DRY = args.includes('--dry-run');
const VERIFY = args.includes('--verify');
const toIdx = args.indexOf('--to');
const TARGET = toIdx >= 0 && args[toIdx + 1] ? path.resolve(args[toIdx + 1]) : DEFAULT_TARGET;

function die(msg) {
  console.error(`\n✗ ${msg}\n`);
  process.exit(1);
}

function skillVersion(dir) {
  try {
    const head = fs.readFileSync(path.join(dir, 'SKILL.md'), 'utf8').slice(0, 800);
    const m = head.match(/^version:\s*(\S+)/m);
    return m ? m[1] : '?';
  } catch {
    return '?';
  }
}

// 收集源端要发布的文件（相对路径，正斜杠）
function collectSource() {
  const out = [];
  for (const f of ROOT_FILES) {
    const p = path.join(SRC, f);
    if (fs.existsSync(p)) out.push(f);
  }
  for (const d of DIRS) {
    const base = path.join(SRC, d);
    if (!fs.existsSync(base)) continue;
    const walk = (abs, rel) => {
      for (const e of fs.readdirSync(abs, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        if (SKIP_NAMES.has(e.name)) continue;
        if (e.name === '.DS_Store' || e.name === '__pycache__' || e.name.endsWith('.pyc')) continue;
        const a = path.join(abs, e.name);
        const r = `${rel}/${e.name}`;
        if (e.isDirectory()) walk(a, r);
        else if (e.isFile()) out.push(r);
      }
    };
    walk(base, d);
  }
  return out;
}

// 收集目标端在发布范围内、但源端没有的文件（要被删）
// 注意 SKIP_NAMES 里的开发工具在这里**不跳过**：它们不进发布包，
// 生产端也就不该留着旧副本，否则以后 diff 永远对不齐。
function collectStale(srcSet) {
  const stale = [];
  for (const d of DIRS) {
    const base = path.join(TARGET, d);
    if (!fs.existsSync(base)) continue;
    const walk = (abs, rel) => {
      for (const e of fs.readdirSync(abs, { withFileTypes: true })) {
        if (e.name === '__pycache__' || e.name.endsWith('.pyc')) continue;
        const a = path.join(abs, e.name);
        const r = `${rel}/${e.name}`;
        if (e.isDirectory()) walk(a, r);
        else if (e.isFile() && !srcSet.has(r)) stale.push(r);
      }
    };
    walk(base, d);
  }
  return stale;
}

function sameBytes(a, b) {
  const sa = fs.statSync(a);
  const sb = fs.statSync(b);
  if (sa.size !== sb.size) return false;
  return Buffer.compare(fs.readFileSync(a), fs.readFileSync(b)) === 0;
}

// ── 前置校验 ────────────────────────────────────────────────
if (!fs.existsSync(path.join(SRC, 'SKILL.md'))) die(`源端不是 skill 目录：${SRC}`);
if (SRC === TARGET) die('源端与目标端是同一个目录，别在生产目录里跑 deploy');

if (!fs.existsSync(TARGET)) {
  die(`目标目录不存在：${TARGET}\n  首次部署请手动建目录并把 skill 装好（或用 WorkBuddy 装一遍），本工具只做增量同步。`);
}
if (!fs.existsSync(path.join(TARGET, 'SKILL.md'))) {
  die(`目标目录里没有 SKILL.md，不像 skill 目录，拒绝写入：${TARGET}`);
}

const files = collectSource();
const srcSet = new Set(files);
const stale = collectStale(srcSet);

const changed = [];
const added = [];
for (const rel of files) {
  const s = path.join(SRC, rel);
  const t = path.join(TARGET, rel);
  if (!fs.existsSync(t)) added.push(rel);
  else if (!sameBytes(s, t)) changed.push(rel);
}

// ── 报告 ────────────────────────────────────────────────────
console.log(`源端  ${SRC}  (v${skillVersion(SRC)})`);
console.log(`目标  ${TARGET}  (v${skillVersion(TARGET)})`);
console.log(`范围  ${files.length} 个文件\n`);

if (!added.length && !changed.length && !stale.length) {
  console.log('✓ 已是最新，无需部署');
  if (VERIFY) runVerify();
  process.exit(0);
}

for (const r of added) console.log(`  + ${r}`);
for (const r of changed) console.log(`  ~ ${r}`);
for (const r of stale) console.log(`  - ${r}`);

if (DRY) {
  console.log(`\n(dry-run) 将要 新增 ${added.length}、覆盖 ${changed.length}、删除 ${stale.length}`);
  process.exit(0);
}

// ── 执行 ────────────────────────────────────────────────────
for (const rel of [...added, ...changed]) {
  const t = path.join(TARGET, rel);
  fs.mkdirSync(path.dirname(t), { recursive: true });
  fs.copyFileSync(path.join(SRC, rel), t);
}
for (const rel of stale) {
  fs.rmSync(path.join(TARGET, rel), { force: true });
}

// 删掉空的 references/ 之类残留目录
for (const d of DIRS) {
  const base = path.join(TARGET, d);
  if (fs.existsSync(base) && fs.readdirSync(base).length === 0) fs.rmdirSync(base);
}

console.log(`\n✓ 部署完成：新增 ${added.length}、覆盖 ${changed.length}、删除 ${stale.length}`);
console.log(`  v${skillVersion(TARGET)}  下一步：node ${TARGET.replace(/\\/g, '/')}/scripts/selftest.mjs`);

if (VERIFY) runVerify();

function runVerify() {
  const find = path.join(TARGET, 'scripts', 'find.mjs');
  if (!fs.existsSync(find)) {
    console.log('\n(跳过冒烟：目标端没有 find.mjs)');
    return;
  }
  console.log('\n冒烟测试：');
  try {
    const out = execFileSync(process.execPath, [find, '做公众号文章', '--top', '3', '--json'], {
      encoding: 'utf8',
      timeout: 120000,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const j = JSON.parse(out);
    const hits = j.hits || [];
    console.log(`  status=${j.status}  hits=${hits.length}  stale=${j.stale}`);
    for (const h of hits) console.log(`  · ${h.title}  (uc ${h.uc})`);
    if (!hits.length) process.exitCode = 1;
  } catch (e) {
    console.error(`  ✗ 冒烟失败：${(e.stderr || e.message || '').toString().trim().slice(0, 400)}`);
    process.exitCode = 1;
  }
}
