// text.mjs  中文字符 bigram 分词 + 字段权重
// 不引分词词典：免依赖、抗新词，专有名词（小红书/拼豆/极狐）靠 bigram 天然覆盖。
// 无意义组合靠 BM25 的 IDF 自然压掉。

// 实测最优权重（调参记录见仓库 references/tuning.md，不随包发布）
// title = 市场卡片主标题（用户拿来搜专家的名字），最强信号
// account = 创作者/账号名，含品牌词，单独降权，避免「仁者爱」「Flova」这类词把同账号一堆专家顶上来
export const W = { title: 3.2, account: 1.8, tags: 2.0, desc: 1.0, qp: 1.2 };

export const BM25 = { k1: 1.2, b: 0.75 };

// use_count 的温和加成。log10 压缩量级差，除以 7 把最大加成控制在 ~1.2 倍，
// 目的是压长尾噪声，不主导语义排序。
export const UC_WEIGHT = 0.12;

export function ucBoost(uc) {
  return 1 + (UC_WEIGHT * Math.log10(1 + (uc || 0))) / 7;
}

// 名字直达先验：用户常常照着市场里的标题输进来。
// 纯 BM25 会被「AI 短剧制作总监」这种更长的近似标题压一头（实测搜「AI短剧制作」时
// 真正那条只排第 2），所以给标题命中一个强系数。不碰索引，只在打分阶段乘。
export function normTitle(s) {
  return String(s || '').replace(/[\s\p{P}]/gu, '').toLowerCase();
}

export function makeTitleBoost(query) {
  const qn = normTitle(query);
  return (title) => {
    const tn = normTitle(title);
    if (!tn || !qn) return 1;
    if (tn === qn) return 3;                             // 输入的就是这个标题
    if (qn.length >= 2 && tn.includes(qn)) return 1.6;   // 标题包含整段输入
    if (tn.length >= 2 && qn.includes(tn)) return 1.3;   // 标题是输入里的一部分
    return 1;
  };
}

export function grams(s) {
  // 只去空白与标点，保留 C++ / C# 这类有意义符号
  const t = String(s || '').replace(/[\s\p{P}]/gu, '');
  const out = [];
  if (t.length === 1) return [t];
  for (let i = 0; i < t.length - 1; i++) out.push(t.slice(i, i + 2));
  return out;
}

// 去重后的查询 gram（查询侧不需要词频）
export function queryGrams(q) {
  return [...new Set(grams(q))];
}

// 单条文档 -> { tf: Map<gram, 加权词频>, len }
export function docTerms(doc) {
  const tf = new Map();
  const add = (text, w) => {
    for (const g of grams(text)) tf.set(g, (tf.get(g) || 0) + w);
  };
  add(doc.title || '', W.title);
  add(doc.account || '', W.account);
  add((doc.tags || []).join(' '), W.tags);
  add(doc.desc || '', W.desc);
  add((doc.qp || []).join('。'), W.qp);
  let len = 0;
  for (const v of tf.values()) len += v;
  return { tf, len };
}
