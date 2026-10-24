// token.mjs  凭证读取与刷新
// 凭证只存一份：<skill>/data/wb-token.env（由同目录的 export-token.py 从客户端登录态解出）。
// 客户端是滑动续期，只要保持登录，重跑导出脚本拿到的就是最新 token。

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SKILL_ROOT = path.resolve(HERE, '..');
const ENV_FILE = path.join(SKILL_ROOT, 'data', 'wb-token.env');
const PY_SCRIPT = path.join(HERE, 'export-token.py');

// 刷新阈值：剩余有效期低于 2 小时就先刷
export const REFRESH_BEFORE_SEC = 2 * 3600;

function parseEnvFile(file) {
  if (!fs.existsSync(file)) return null;
  const txt = fs.readFileSync(file, 'utf8');
  const m = txt.match(/WB_ACCESS_TOKEN\s*=\s*"?([^"\r\n]+)"?/);
  if (!m) return null;
  const token = m[1].trim();
  const dm = txt.match(/WB_DOMAIN\s*=\s*"?([^"\r\n]+)"?/);
  return { token, domain: dm ? dm[1].trim() : 'www.workbuddy.cn', file, exp: jwtExp(token) };
}

export function readTokenEnv() {
  return parseEnvFile(ENV_FILE);
}

export function jwtExp(token) {
  try {
    const p = token.split('.')[1];
    const json = Buffer.from(p.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
    const obj = JSON.parse(json);
    return obj.exp ? new Date(obj.exp * 1000) : null;
  } catch {
    return null;
  }
}

function pythonCandidates() {
  const c = [];
  if (process.env.WB_PYTHON) c.push(process.env.WB_PYTHON);
  // 托管运行时（优先）
  const managed = path.join(os.homedir(), '.workbuddy', 'binaries', 'python', 'versions');
  if (fs.existsSync(managed)) {
    for (const v of fs.readdirSync(managed).sort().reverse()) {
      const p = path.join(managed, v, process.platform === 'win32' ? 'python.exe' : 'bin/python3');
      if (fs.existsSync(p)) c.push(p);
    }
  }
  c.push(process.platform === 'win32' ? 'python' : 'python3');
  return c;
}

// 环境变量直供：`WB_ACCESS_TOKEN` 已设且未过期时，完全不碰 python / 客户端登录态。
// 这条路给「不想装解密模块」或「CI/无头环境」用。
function tokenFromEnv() {
  const t = (process.env.WB_ACCESS_TOKEN || '').trim();
  if (!t || t.split('.').length !== 3) return null;
  const exp = jwtExp(t);
  if (exp && exp.getTime() <= Date.now()) return null;
  return { token: t, domain: process.env.WB_DOMAIN || 'www.workbuddy.cn', exp };
}

// 返回 { ok, exp, detail }
export function refreshToken() {
  for (const py of pythonCandidates()) {
    try {
      const out = execFileSync(py, [PY_SCRIPT], {
        encoding: 'utf8',
        timeout: 60000,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      const line = out.trim().split('\n').pop();
      const j = JSON.parse(line);
      if (!j.ok) return { ok: false, detail: j.error };
      const t = readTokenEnv();
      return { ok: true, detail: `token 长度 ${j.length}`, exp: t && t.exp };
    } catch (e) {
      const msg = (e.stderr || e.message || '').toString().trim().split('\n').pop();
      if (e.code === 'ENOENT') continue; // 换下一个解释器
      return { ok: false, detail: msg.slice(0, 300) };
    }
  }
  return { ok: false, detail: '没有可用的 python 解释器（可设 WB_PYTHON 指定）' };
}

// 保证拿到一个有效期足够的 token；必要时自动刷新
export function ensureToken({ force = false, log = console.error } = {}) {
  const now = Date.now();

  // 优先级 0：环境变量直供，不读文件、不启动子进程
  if (!force) {
    const envTk = tokenFromEnv();
    if (envTk) return { ok: true, token: envTk.token, domain: envTk.domain, exp: envTk.exp, refreshed: false, source: 'env' };
  }

  let env = readTokenEnv();

  if (env && !force && env.exp && env.exp.getTime() - now > REFRESH_BEFORE_SEC * 1000) {
    return { ok: true, token: env.token, domain: env.domain, exp: env.exp, refreshed: false, source: 'file' };
  }

  log('[token] 有效期不足或缺失，尝试从客户端登录态刷新…');
  const r = refreshToken();
  if (!r.ok) {
    // 刷新失败但旧 token 还没过期，仍可用
    if (env && env.exp && env.exp.getTime() > now) {
      log(`[token] 刷新失败（${r.detail}），沿用现有 token 至 ${env.exp.toLocaleString('zh-CN')}`);
      return { ok: true, token: env.token, domain: env.domain, exp: env.exp, refreshed: false, warn: r.detail };
    }
    return { ok: false, error: r.detail };
  }

  env = readTokenEnv();
  if (!env || !env.token) return { ok: false, error: '刷新后未能读到 token' };
  log(`[token] 刷新成功，到期 ${env.exp ? env.exp.toLocaleString('zh-CN') : '未知'}`);
  return { ok: true, token: env.token, domain: env.domain, exp: env.exp, refreshed: true, source: 'refreshed' };
}
