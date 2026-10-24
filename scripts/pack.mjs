#!/usr/bin/env node
/**
 * 发版打包：生成一份可以直接上传开放平台的目录。
 *
 * 只放 SKILL.md、LICENSE、scripts/。
 * references/ 是维护者笔记（排障记录、客户端内部实现、调参实验），
 * 使用者用不到，公开还会扩大审核面，所以不进包。
 * data/ 更不能进：里面有真的 accessToken 和 51 MB 索引。
 *
 * 用法：node scripts/pack.mjs
 * 产物：dist/find-experts/  —— 把这个目录压缩成 zip 上传
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const OUT = path.join(DIST, 'find-experts');

const RELEASE = ['SKILL.md', 'LICENSE', 'scripts'];
// 打包器自己和 Python 编译缓存不进包
const SKIP = new Set(['pack.mjs', '__pycache__']);
const SKIP_FILE = /\.pyc$/i;
// 命中任何一个就中止：这类文件一旦进包，等于把凭证或全量数据公开
const FORBIDDEN = /(^|[\\/])(wb-token\.env|atrest\.key|index\.bin|history\.json|meta\.json)$|\.env$/i;

function copyTree(src, dest) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      if (SKIP.has(name) || SKIP_FILE.test(name)) continue;
      copyTree(path.join(src, name), path.join(dest, name));
    }
    return;
  }
  fs.copyFileSync(src, dest);
}

function version() {
  const text = fs.readFileSync(path.join(ROOT, 'SKILL.md'), 'utf8');
  const m = text.match(/^version:\s*(.+)$/m);
  return m ? m[1].trim() : '?';
}

function walk(dir, base = dir, out = []) {
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name);
    const st = fs.statSync(p);
    if (st.isDirectory()) walk(p, base, out);
    else out.push({ rel: path.relative(base, p).replace(/\\/g, '/'), size: st.size });
  }
  return out;
}

fs.rmSync(DIST, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

for (const item of RELEASE) {
  const src = path.join(ROOT, item);
  if (!fs.existsSync(src)) {
    console.error(`[pack] 缺文件：${item}`);
    process.exit(1);
  }
  copyTree(src, path.join(OUT, item));
}

const files = walk(OUT);
const leaked = files.filter((f) => FORBIDDEN.test(f.rel));
if (leaked.length) {
  fs.rmSync(DIST, { recursive: true, force: true });
  console.error('[pack] 中止：产物里出现不该发布的文件');
  for (const f of leaked) console.error(`  ${f.rel}  ${f.size} B`);
  process.exit(1);
}

const total = files.reduce((n, f) => n + f.size, 0);
console.log(`v${version()}  ->  ${path.relative(ROOT, OUT).replace(/\\/g, '/')}/`);
console.log(`共 ${files.length} 个文件，${(total / 1024).toFixed(0)} KB\n`);
for (const f of files.sort((a, b) => a.rel.localeCompare(b.rel))) {
  console.log(`  ${String((f.size / 1024).toFixed(1)).padStart(7)} KB  ${f.rel}`);
}
console.log('\n把这个目录压缩成 zip 上传。references/ 与 data/ 未包含。');
