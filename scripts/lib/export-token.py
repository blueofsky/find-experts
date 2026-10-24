#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""取 WorkBuddy accessToken，写到 <skill>/data/wb-token.env（权限 600）。

来源按优先级：
  1) 环境变量 WB_ACCESS_TOKEN —— 已是合法 JWT 就直接用，不碰客户端登录态
  2) 内置 client_auth.py —— 解 5.6.2+ 的 AES-GCM 信封（DPAPI + 进程内存取密钥）
  3) 外部模块（可选，WB_BUDDY_STATION 指定）—— 内置失效时的应急通道

解密逻辑已内联在 scripts/lib/client_auth.py，删掉其他 skill 不影响本 skill。

用法：
  python export-token.py           导出并写盘
  python export-token.py --check   只诊断，不写盘、不打印 token

stdout 输出一行 JSON 供 node 消费，绝不打印 token 本身。
"""

import json
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(os.path.dirname(HERE))
ENV_OUT = os.path.join(SKILL_ROOT, "data", "wb-token.env")

sys.path.insert(0, HERE)


def fail(msg, hint=None):
    out = {"ok": False, "error": msg}
    if hint:
        out["hint"] = hint
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)


def is_jwt(s):
    return isinstance(s, str) and s.count(".") == 2 and s.startswith("ey")


def source_from_env():
    tok = (os.environ.get("WB_ACCESS_TOKEN") or "").strip()
    if not is_jwt(tok):
        return None
    return tok, os.environ.get("WB_DOMAIN") or "www.workbuddy.cn"


def load_module():
    """内置优先，失败再退回 WB_BUDDY_STATION 指定的外部模块。"""
    try:
        import client_auth
        return client_auth, "builtin"
    except Exception as exc:  # noqa: BLE001
        builtin_err = exc

    ext = os.environ.get("WB_BUDDY_STATION")
    if ext and os.path.isfile(ext):
        sys.path.insert(0, os.path.dirname(ext))
        try:
            import buddy_station
            return buddy_station, "external"
        except Exception:  # noqa: BLE001
            pass

    fail(
        "内置解密模块导入失败：%s" % builtin_err,
        "先试 (a) 设 WB_ACCESS_TOKEN=<JWT> 绕过解密；\n"
        "(b) 若内置模块报语法/依赖错误，见 SKILL.md「凭证从哪来」。",
    )


def source_from_client():
    bs, via = load_module()

    state = bs.find_login_state()
    if not state:
        fail("未找到客户端登录态文件，请确认 WorkBuddy 已登录")

    with open(state, "r", encoding="utf-8") as fh:
        auth = json.load(fh).get("auth", {})

    try:
        token = bs.decrypt_access_token_field(auth.get("accessToken"))
    except Exception as exc:  # noqa: BLE001
        diag = ""
        if hasattr(bs, "_diag_lines"):
            lines = bs._diag_lines()
            if lines:
                diag = "\n" + "\n".join(lines)
        fail("解密 accessToken 失败: %s%s" % (exc, diag))

    if not is_jwt(token):
        fail("解出的 token 结构异常")
    return token, auth.get("domain") or "www.workbuddy.cn", state, via


def main():
    check_only = "--check" in sys.argv

    env_src = source_from_env()
    if env_src:
        token, domain = env_src
        source, state, via = "env", None, None
    else:
        token, domain, state, via = source_from_client()
        source = "client"

    if check_only:
        print(json.dumps({
            "ok": True,
            "check_only": True,
            "source": source,
            "module": via,
            "login_state": state,
            "domain": domain,
            "length": len(token),
            "env_out": ENV_OUT,
            "env_out_exists": os.path.isfile(ENV_OUT),
        }, ensure_ascii=False))
        return

    os.makedirs(os.path.dirname(ENV_OUT), exist_ok=True)
    with open(ENV_OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 由 find-experts/scripts/lib/export-token.py 生成，token 过期后重跑即可\n")
        fh.write('export WB_ACCESS_TOKEN="%s"\n' % token)
        fh.write('export WB_DOMAIN="%s"\n' % domain)
    try:
        os.chmod(ENV_OUT, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    print(json.dumps({
        "ok": True,
        "source": source,
        "module": via,
        "path": ENV_OUT,
        "login_state": state,
        "domain": domain,
        "length": len(token),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
