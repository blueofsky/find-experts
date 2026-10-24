#!/usr/bin/env node
/**
 * 发版打包：生成一份可以直接上传开放平台的目录，并压成 zip。
 *
 * 只放 SKILL.md、LICENSE、scripts/。
 * references/ 是维护者笔记（排障记录、客户端内部实现、调参实验），
 * 使用者用不到，公开还会扩大审核面，所以不进包。
 * data/ 更不能进：里面有真的 accessToken 和 51 MB 索引。
 *
 * 用法：node scripts/pack.mjs
 * 产物：dist/find-experts/            解压后的目录，可先检查
 *       dist/find-experts-v<版本>.zip 上传这个
 *
 * zip 自己写，不用 PowerShell 的 Compress-Archive：那个 5.1 版本把路径写成
 * 反斜杠（find-experts\SKILL.md），ZIP 规范要求正斜杠，服务端解析容易挂。
 */
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const NAME = 'find-experts';
const OUT = path.join(DIST, NAME);

const RELEASE = ['SKILL.md', 'LICENSE', 'scripts'];
// 打包器自己和 Python 编译缓存不进包
const SKIP = new Set(['pack.mjs', '__pycache__']);
const SKIP_FILE = /\.pyc$/i;
// 命中任何一个就中止：这类文件一旦进包，等于把凭证或全量数据公开
const FORBIDDEN = /(^|[\\/])(wb-token\.env|atrest\.key|index\.bin|history\.json|meta\.json)$|\.env$/i;

// ---- CRC32 ----
const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[i] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function dosTime(d) {
  return {
    time: (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1),
    date: ((Math.max(d.getFullYear(), 1980) - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate(),
  };
}

function zipDir(srcDir, prefix, zipPath) {
  const entries = [];
  (function collect(dir, rel) {
    for (const name of fs.readdirSync(dir).sort()) {
      const full = path.join(dir, name);
      const st = fs.statSync(full);
      const nameInZip = `${rel}/${name}`;
      if (st.isDirectory()) {
        entries.push({ name: `${nameInZip}/`, dir: true, mtime: st.mtime });
        collect(full, nameInZip);
      } else {
        entries.push({ name: nameInZip, dir: false, mtime: st.mtime, data: fs.readFileSync(full) });
      }
    }
  })(srcDir, prefix);

  const chunks = [];
  const central = [];
  let offset = 0;

  for (const e of entries) {
    const nameBuf = Buffer.from(e.name, 'utf8');
    const raw = e.dir ? Buffer.alloc(0) : e.data;
    const body = e.dir ? raw : zlib.deflateRawSync(raw, { level: 9 });
    const crc = crc32(raw);
    const { time, date } = dosTime(e.mtime);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6); // UTF-8 文件名
    local.writeUInt16LE(e.dir ? 0 : 8, 8); // 0=store 8=deflate
    local.writeUInt16LE(time, 10);
    local.writeUInt16LE(date, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(raw.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);

    chunks.push(local, nameBuf, body);

    const cd = Buffer.alloc(46);
    cd.writeUInt32LE(0x02014b50, 0);
    cd.writeUInt16LE(20, 4);
    cd.writeUInt16LE(20, 6);
    cd.writeUInt16LE(0x0800, 8);
    cd.writeUInt16LE(e.dir ? 0 : 8, 10);
    cd.writeUInt16LE(time, 12);
    cd.writeUInt16LE(date, 14);
    cd.writeUInt32LE(crc, 16);
    cd.writeUInt32LE(body.length, 20);
    cd.writeUInt32LE(raw.length, 24);
    cd.writeUInt16LE(nameBuf.length, 28);
    cd.writeUInt16LE(0, 30); // extra
    cd.writeUInt16LE(0, 32); // comment
    cd.writeUInt16LE(0, 34); // disk
    cd.writeUInt16LE(0, 36); // internal attr
    cd.writeUInt32LE(e.dir ? 0x10 : 0, 38); // external attr：目录置位
    cd.writeUInt32LE(offset, 42);

    central.push(cd, nameBuf);
    offset += local.length + nameBuf.length + body.length;
  }

  const cdBuf = Buffer.concat(central);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(cdBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  eocd.writeUInt16LE(0, 20);

  fs.writeFileSync(zipPath, Buffer.concat([...chunks, cdBuf, eocd]));
  return { count: entries.filter((e) => !e.dir).length, bytes: fs.statSync(zipPath).size };
}

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

// ---- 主流程 ----
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

const ver = version();
const zipPath = path.join(DIST, `${NAME}-v${ver}.zip`);
const zip = zipDir(OUT, NAME, zipPath);

const total = files.reduce((n, f) => n + f.size, 0);
console.log(`v${ver}  ->  dist/${NAME}/`);
console.log(`共 ${files.length} 个文件，${(total / 1024).toFixed(0)} KB\n`);
for (const f of files.sort((a, b) => a.rel.localeCompare(b.rel))) {
  console.log(`  ${String((f.size / 1024).toFixed(1)).padStart(7)} KB  ${f.rel}`);
}
console.log(`\n上传用：dist/${path.basename(zipPath)}  （${zip.count} 个文件，${(zip.bytes / 1024).toFixed(0)} KB）`);
