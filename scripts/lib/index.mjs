// index.mjs  单文件二进制索引：文档正文与倒排表打包进同一个文件，一次 rename 完成原子替换。
// 读方永远看到完整快照，不会出现「新正文 + 旧倒排」的错配。
//
// 布局（小端，header 40 字节固定）：
//   [0..4)   magic "EXF1"
//   u32 @4   version
//   u32 @8   docCount
//   u32 @12  vocabCount
//   u32 @16  postingsCount
//   f32 @20  avgLen
//   u32 @24  vocabLen
//   u32 @28  docsLen
//   u32 @32  metaLen
//   u32 @36  reserved
//   @40      docsBlob    JSONL，每行一条文档（展示字段）
//            metaBlob    JSON
//            vocabBlob   utf8，\n 分隔
//            pad 到 4 字节
//            lens      Float32Array(docCount)      文档加权长度
//            offsets   Uint32Array(vocabCount+1)   每 gram 的 postings 区间
//            docIds    Uint32Array(postingsCount)
//            tfs       Float32Array(postingsCount) 加权词频
//            docOff    Uint32Array(docCount+1)     每行在 docsBlob 中的偏移

import fs from 'node:fs';
import { docTerms } from './text.mjs';

// v3：文档字段新增 home / ver / plug（来源、版本、插件 id）。
// 字段变了必须 bump，否则旧索引能通过校验但读出来全是 undefined。
export const VERSION = 3;
export const HEADER = 40;
const MAGIC = 'EXF1';

export function writeIndex(file, docs, meta = {}) {
  const N = docs.length;
  const lens = new Float32Array(N);
  const inv = new Map();

  for (let i = 0; i < N; i++) {
    const { tf, len } = docTerms(docs[i]);
    lens[i] = len;
    for (const [g, f] of tf) {
      let a = inv.get(g);
      if (!a) inv.set(g, (a = []));
      a.push(i, f);
    }
  }

  const vocab = [...inv.keys()].sort();
  let postings = 0;
  for (const g of vocab) postings += inv.get(g).length / 2;

  const offsets = new Uint32Array(vocab.length + 1);
  const docIds = new Uint32Array(postings);
  const tfs = new Float32Array(postings);

  let k = 0;
  for (let v = 0; v < vocab.length; v++) {
    offsets[v] = k;
    const a = inv.get(vocab[v]);
    for (let j = 0; j < a.length; j += 2) {
      docIds[k] = a[j];
      tfs[k] = a[j + 1];
      k++;
    }
    inv.delete(vocab[v]); // 及时释放，降低峰值内存
  }
  offsets[vocab.length] = k;

  // 文档正文 + 行偏移
  const lines = docs.map((d) => JSON.stringify(d));
  const docsBlob = Buffer.from(lines.join('\n') + '\n', 'utf8');
  const docOff = new Uint32Array(N + 1);
  let pos = 0;
  for (let i = 0; i < N; i++) {
    docOff[i] = pos;
    pos += Buffer.byteLength(lines[i], 'utf8') + 1;
  }
  docOff[N] = docsBlob.length;

  let sum = 0;
  for (let i = 0; i < N; i++) sum += lens[i];
  const avgLen = N ? sum / N : 0;

  const vocabBlob = Buffer.from(vocab.join('\n'), 'utf8');
  const metaBlob = Buffer.from(JSON.stringify({ ...meta, avgLen, N, postings }), 'utf8');
  const pad = (4 - ((HEADER + docsBlob.length + metaBlob.length + vocabBlob.length) % 4)) % 4;

  const total = HEADER + docsBlob.length + metaBlob.length + vocabBlob.length + pad
    + lens.byteLength + offsets.byteLength + docIds.byteLength + tfs.byteLength + docOff.byteLength;

  const buf = Buffer.allocUnsafe(total);
  buf.write(MAGIC, 0, 'ascii');
  buf.writeUInt32LE(VERSION, 4);
  buf.writeUInt32LE(N, 8);
  buf.writeUInt32LE(vocab.length, 12);
  buf.writeUInt32LE(postings, 16);
  buf.writeFloatLE(avgLen, 20);
  buf.writeUInt32LE(vocabBlob.length, 24);
  buf.writeUInt32LE(docsBlob.length, 28);
  buf.writeUInt32LE(metaBlob.length, 32);
  buf.writeUInt32LE(0, 36);

  let off = HEADER;
  docsBlob.copy(buf, off); off += docsBlob.length;
  metaBlob.copy(buf, off); off += metaBlob.length;
  vocabBlob.copy(buf, off); off += vocabBlob.length;
  off += pad;

  const put = (arr) => {
    Buffer.from(arr.buffer, arr.byteOffset, arr.byteLength).copy(buf, off);
    off += arr.byteLength;
  };
  put(lens);
  put(offsets);
  put(docIds);
  put(tfs);
  put(docOff);

  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, buf);
  try {
    fs.renameSync(tmp, file);
  } catch {
    // Windows 下若目标被短暂占用，退化为删除后重建（仍有极短窗口期）
    try { fs.unlinkSync(file); } catch { /* ignore */ }
    fs.renameSync(tmp, file);
  }

  return { vocab: vocab.length, postings, avgLen, N, bytes: total };
}

export function readIndex(file, { checkVersion = true } = {}) {
  const raw = fs.readFileSync(file);
  if (raw.length < HEADER || raw.toString('ascii', 0, 4) !== MAGIC) {
    throw new Error('索引文件损坏，请重新执行 update.mjs');
  }
  const version = raw.readUInt32LE(4);
  if (checkVersion && version !== VERSION) {
    throw new Error(`索引版本 ${version} 与代码 ${VERSION} 不一致，请重新执行 update.mjs --rebuild 或 update.mjs`);
  }
  const N = raw.readUInt32LE(8);
  const vocabCount = raw.readUInt32LE(12);
  const postings = raw.readUInt32LE(16);
  const avgLen = raw.readFloatLE(20);
  const vocabLen = raw.readUInt32LE(24);
  const docsLen = raw.readUInt32LE(28);
  const metaLen = raw.readUInt32LE(32);

  // 长度校验：被截断的文件仍能通过 magic 和 version 检查（头部完好），
  // 但后面会一路读到文件尾，最后在 JSON.parse 抛一个看不懂的 "Unexpected end of JSON input"。
  const need = HEADER + docsLen + metaLen + vocabLen
    + N * 4 + (vocabCount + 1) * 4 + postings * 4 + postings * 4 + (N + 1) * 4;
  if (raw.length < need) {
    throw new Error(`索引文件不完整（${raw.length} / ${need} 字节），请重新执行 update.mjs`);
  }

  let off = HEADER;
  const docsBlob = raw.subarray(off, off + docsLen); off += docsLen;
  let meta;
  try {
    meta = JSON.parse(raw.toString('utf8', off, off + metaLen));
  } catch {
    throw new Error('索引文件损坏（meta 段解析失败），请重新执行 update.mjs');
  }
  off += metaLen;
  const vocabBlob = raw.toString('utf8', off, off + vocabLen); off += vocabLen;
  off += (4 - (off % 4)) % 4;

  // 零拷贝：直接复用 Buffer 底层的 ArrayBuffer（省一次 46MB 复制）。
  // 要求视图起点 4 字节对齐；Buffer 池化的小文件 byteOffset 可能不对齐，那就退回复制。
  let ab = raw.buffer;
  let vbase = raw.byteOffset;
  if (vbase % 4 !== 0 || (vbase + off) % 4 !== 0) {
    ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.length);
    vbase = 0;
  }
  const lens = new Float32Array(ab, vbase + off, N); off += N * 4;
  const offsets = new Uint32Array(ab, vbase + off, vocabCount + 1); off += (vocabCount + 1) * 4;
  const docIds = new Uint32Array(ab, vbase + off, postings); off += postings * 4;
  const tfs = new Float32Array(ab, vbase + off, postings); off += postings * 4;
  const docOff = new Uint32Array(ab, vbase + off, N + 1);

  const vocabMap = new Map();
  if (vocabCount) {
    const vocab = vocabBlob.split('\n');
    for (let i = 0; i < vocab.length; i++) vocabMap.set(vocab[i], i);
  }
  const doc = (i) => JSON.parse(docsBlob.toString('utf8', docOff[i], docOff[i + 1] - 1));
  return { version, N, avgLen, lens, offsets, docIds, tfs, meta, vocabMap, doc };
}
