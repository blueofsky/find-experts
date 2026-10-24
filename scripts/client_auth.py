"""WorkBuddy 客户端登录态解密。

来源：totorosir-workbuddy-score/scripts/buddy_station.py 的精确子集（MIT-0）。
用 AST 按函数名抽取 [lineno, end_lineno] 区间，未改动任何实现。

保留 decrypt_access_token_field 的调用闭包：
  DPAPI 解 atRestSecretKey -> 信封解析 -> 派生 key -> AES-256-GCM 解密
  以及密钥发现的三条途径：客户端数据目录、macOS Keychain、进程内存、CDP 调试端口。

裁掉：core dump 与 Linux /proc 两条上游自注「未在本项目环境实测」的途径。
CDP 那条是上游注释里写明「macOS 上唯一被实测可行的自动途径」，Hardened Runtime
会让进程内存与核心转储都不可用，所以 macOS 靠它。

上游改了客户端加密格式时要同步更新本文件，否则解密报错会指向这里。
本文件不含任何密钥或 token，密钥只在内存中校验。
"""

import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import shutil
import urllib.parse
import urllib.request


AUTH_FILENAMES = ["workbuddy-desktop.info"]


EXTENSION_DIRS = [os.path.join("CodeBuddyExtension", "Data", "Public", "auth")]


USER_AUTH_DIR = os.path.join(".workbuddy", "auth")


_AAD_DOMAIN = b"WB-AAD\x00"


_FRAMING_CODE = {"file": 1, "field": 2, "record": 3, "stream": 4}


_STANDARD_FORMAT_ID = {"file": "WBEF1", "field": "WBEV1", "record": "WBER1", "stream": "WBES1"}


def _b64_canonical_decode(value, field):
    """复刻 decodeCanonicalBase64：标准 base64、末尾补 =，且与重编码一致才算合法。"""
    if not isinstance(value, str):
        raise ValueError("%s 不是字符串" % field)
    decoded = base64.b64decode(value)
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("%s 不是规范 base64" % field)
    return decoded


def _derive_at_rest_key(at_rest_secret_key):
    """复刻 normalizeAtRestKeyPayload：key=SHA256(secret字符串UTF-8)，keyId=SHA256(key)[:16]。

    返回 (key_32bytes, key_id_16hex)。
    """
    if not isinstance(at_rest_secret_key, str) or len(at_rest_secret_key) != 44:
        raise ValueError("atRestSecretKey 必须是 44 字符规范 base64（32 字节）")
    # 校验是规范 base64 且解码为 32 字节
    raw = _b64_canonical_decode(at_rest_secret_key, "atRestSecretKey")
    if len(raw) != 32:
        raise ValueError("atRestSecretKey 解码后必须是 32 字节")
    key = hashlib.sha256(at_rest_secret_key.encode("utf-8")).digest()
    key_id = hashlib.sha256(key).hexdigest()[:16]
    return key, key_id


def _build_field_aad(key_id):
    """复刻 buildAuthenticatedContextAad(keyId, suite=1, {framing:'field'}, scheme='sym-v1')。"""
    def encode_uint32(v):
        return v.to_bytes(4, "big")
    def encode_length_prefixed(s):
        b = s.encode("utf-8")
        return encode_uint32(len(b)) + b
    return b"".join([
        _AAD_DOMAIN,
        bytes([1]),
        encode_length_prefixed(_STANDARD_FORMAT_ID["field"]),  # "WBEV1"
        encode_length_prefixed("sym-v1"),
        encode_uint32(1),                                       # suite
        encode_length_prefixed(key_id),                          # 16 hex 字符
        bytes([_FRAMING_CODE["field"]]),                         # 2
        bytes([0]),                                              # sequence 缺失
        bytes([0]),                                              # final 缺失
    ])


def _aes_gcm_decrypt(key, nonce, ciphertext, aad, auth_tag):
    """AES-256-GCM 解密，返回明文 bytes；校验失败抛 IntegrityError。"""
    # 1) cryptography（WorkBuddy 托管 Python 自带，优先）
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aes = AESGCM(key)
        return aes.decrypt(nonce, ciphertext + auth_tag, aad)
    except ImportError:
        pass
    # 2) PyCryptodome（常见纯 Python 可安装包）
    try:
        from Crypto.Cipher import AES  # type: ignore
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(aad)
        return cipher.decrypt_and_verify(ciphertext, auth_tag)
    except ImportError:
        pass
    # 3) 纯 Python 兜底（零依赖，已与 cryptography 对拍校验）
    return _aes_gcm_decrypt_pure(key, nonce, ciphertext, aad, auth_tag)


def _aes_gcm_decrypt_pure(key, nonce, ciphertext, aad, auth_tag):
    pt = _aes_gcm_pure(key, nonce, ciphertext, aad, auth_tag, decrypt=True)
    return pt


def _aes_gcm_pure(key, nonce, data, aad, auth_tag_in, decrypt):
    # AES 密钥扩展
    Nk = len(key) // 4
    Nr = Nk + 6
    RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
    SBOX = _aes_sbox()
    w = [list(key[4 * i:4 * i + 4]) for i in range(Nk)]
    for i in range(Nk, 4 * (Nr + 1)):
        temp = list(w[i - 1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // Nk - 1]
        elif Nk > 6 and i % Nk == 4:
            temp = [SBOX[b] for b in temp]
        w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
    round_keys = [[w[i][j] for i in range(4 * (Nr + 1))] for j in range(4)]

    def xtime(a):
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
        return a & 0xFF

    def gmul(a, b):
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi:
                a ^= 0x1B
            b >>= 1
        return p & 0xFF

    def add_round_key(state, rnd):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= round_keys[r][4 * rnd + c]

    def sub_bytes(state):
        for r in range(4):
            for c in range(4):
                state[r][c] = SBOX[state[r][c]]

    def shift_rows(state):
        state[1] = state[1][1:] + state[1][:1]
        state[2] = state[2][2:] + state[2][:2]
        state[3] = state[3][3:] + state[3][:3]

    def mix_columns(state):
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = gmul(a[0], 2) ^ gmul(a[1], 3) ^ a[2] ^ a[3]
            state[1][c] = a[0] ^ gmul(a[1], 2) ^ gmul(a[2], 3) ^ a[3]
            state[2][c] = a[0] ^ a[1] ^ gmul(a[2], 2) ^ gmul(a[3], 3)
            state[3][c] = gmul(a[0], 3) ^ a[1] ^ a[2] ^ gmul(a[3], 2)

    def cipher_block(block):
        state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
        add_round_key(state, 0)
        for rnd in range(1, Nr):
            sub_bytes(state)
            shift_rows(state)
            mix_columns(state)
            add_round_key(state, rnd)
        sub_bytes(state)
        shift_rows(state)
        add_round_key(state, Nr)
        return bytes(state[r][c] for c in range(4) for r in range(4))

    # GCM
    H = cipher_block(b"\x00" * 16)
    # GHASH
    def gf_mult(x, y):
        R = 0xE1000000000000000000000000000000
        z = 0
        v = y
        for i in range(128):
            if (x >> (127 - i)) & 1:
                z ^= v
            if v & 1:
                v = (v >> 1) ^ R
            else:
                v >>= 1
        return z

    def ghash_blocks(blocks):
        y = 0
        for blk in blocks:
            x = int.from_bytes(blk, "big")
            y = gf_mult(y ^ x, int.from_bytes(H, "big"))
        return y.to_bytes(16, "big")

    # 构造计数器块：nonce(12) + 32位计数（从 1 开始）
    def ctr_block(counter):
        cb = nonce + counter.to_bytes(4, "big")
        return cipher_block(cb)

    # 分块（16 字节，末尾补零），空输入返回 []
    def split_blocks(b):
        if not b:
            return []
        pad = b + b"\x00" * ((16 - len(b) % 16) % 16)
        return [pad[i:i + 16] for i in range(0, len(pad), 16)]

    # CTR 加/解密（对称：明文 ^ 密钥流 = 密文）
    # 关键：数据计数器从 inc32(J0) 开始，即 nonce||0x00000002（J0=nonce||0x00000001 只用于给 tag 做 E_K）
    aad_blocks = split_blocks(aad)
    if decrypt:
        ct_blocks = split_blocks(data)          # data = 密文
        out = bytearray()
        for i, blk in enumerate(ct_blocks):
            ks = ctr_block(i + 2)
            out.extend(bytes(blk[j] ^ ks[j] for j in range(len(blk))))
        out = bytes(out[:len(data)])
        ct_for_ghash = data
    else:
        pt_blocks = split_blocks(data)          # data = 明文
        ct = bytearray()
        for i, blk in enumerate(pt_blocks):
            ks = ctr_block(i + 2)
            ct.extend(bytes(blk[j] ^ ks[j] for j in range(len(blk))))
        ct = bytes(ct[:len(data)])
        out = ct
        ct_for_ghash = ct

    # GHASH 必须基于"密文"计算（加密时即 CTR 输出；解密时即输入密文）
    ghash_input = aad_blocks + split_blocks(ct_for_ghash)
    ghash_input.append((len(aad) * 8).to_bytes(8, "big") + (len(ct_for_ghash) * 8).to_bytes(8, "big"))
    S = ghash_blocks(ghash_input)

    # J0：12 字节 nonce 时 J0 = nonce || 0x00000001；其它长度按 GHASH(nonce) 处理
    if len(nonce) == 12:
        j0 = nonce + b"\x00\x00\x00\x01"
    else:
        j0 = ghash_blocks(split_blocks(nonce) + [(len(nonce) * 8).to_bytes(16, "big")])
    ek_j0 = cipher_block(j0)
    tag = bytes(S[k] ^ ek_j0[k] for k in range(16))

    if decrypt:
        if auth_tag_in is not None and int.from_bytes(tag, "big") != int.from_bytes(auth_tag_in, "big"):
            raise ValueError("ciphertext authentication failed (GCM tag mismatch)")
        return out
    else:
        return out, tag


def _aes_sbox():
    # 纯 Python 兜底用的标准 AES S-Box（公开常量，已验证）。
    return _AES_STD_SBOX


def _rotl(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _xtime_lite(x):
    x <<= 1
    if x & 0x100:
        x ^= 0x11B
    return x & 0xFF


def _gmul_lookup(a, b):
    # 仅用于扩展欧几里得，简单实现
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p & 0xFF


_AES_STD_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]


def _parse_suite1_envelope(envelope_bytes):
    try:
        env = json.loads(envelope_bytes.decode("utf-8"))
    except Exception as e:
        raise ValueError("信封不是合法 JSON: %s" % e)
    if not isinstance(env, dict):
        raise ValueError("信封必须是对象")
    if env.get("suite") != 1:
        raise ValueError("只支持 suite=1，实际 %r" % env.get("suite"))
    key_id = env.get("keyId")
    if not isinstance(key_id, str) or not _re_hex16(key_id):
        raise ValueError("envelope.keyId 非法")
    nonce = _b64_canonical_decode(env["nonce"], "nonce")
    auth_tag = _b64_canonical_decode(env["authTag"], "authTag")
    ct = env.get("ciphertext")
    ciphertext = b"" if (isinstance(ct, str) and ct == "") else _b64_canonical_decode(ct, "ciphertext")
    if len(nonce) != 12 or len(auth_tag) != 16:
        raise ValueError("nonce(12)/authTag(16) 长度错误")
    return key_id, nonce, ciphertext, auth_tag


def _re_hex16(s):
    import re
    return bool(re.fullmatch(r"[0-9a-f]{16}", s))


def _dpapi_unprotect(blob):
    """调用 crypt32.CryptUnprotectData（当前用户）解开 DPAPI blob，返回原文 bytes。"""
    if not sys.platform.startswith("win"):
        raise OSError("DPAPI 仅在 Windows 可用")
    import ctypes
    from ctypes import wintypes
    crypt32 = ctypes.windll.crypt32  # type: ignore
    blob_len = len(blob)
    blob_buf = ctypes.create_string_buffer(blob, blob_len)
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    in_blob = DATA_BLOB(blob_len, blob_buf)
    out_blob = DATA_BLOB()
    res = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not res:
        raise OSError("CryptUnprotectData 失败（可能不是当前用户/本机加密的 blob）")
    try:
        size = out_blob.cbData
        ptr = ctypes.cast(out_blob.pbData, ctypes.POINTER(ctypes.c_byte * size))
        data = bytes(ptr.contents[:size])
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)  # type: ignore
    return data


_B64_CANDIDATE_RE = r"[A-Za-z0-9+/]{43}="


_B64_CANDIDATE_TEXT_RE = r"[A-Za-z0-9+/]{43}="


_B64_CANDIDATE_RE_UTF16 = rb"(?:[A-Za-z0-9+/]\x00){43}=\x00"


_MAX_MEMORY_CANDIDATES = 200000


def _list_workbuddy_pids():
    """枚举运行中的 WorkBuddy 客户端进程 PID（Windows 走 Toolhelp32，其他平台走 ps）。"""
    names = _workbuddy_process_names()
    if not sys.platform.startswith("win"):
        # macOS 的 libproc 途径依赖上游未抽取的函数，这里统一退到 ps
        return _list_pids_posix(names)
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2
    INVALID_HANDLE_VALUE = ctypes.c_size_t(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),         # 注意是 LONG，不是指针
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if _normalized_process_name(entry.szExeFile) in names:
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def _iter_process_readable_regions(kernel32, handle, max_total_bytes=3 * 1024 * 1024 * 1024):
    """逐段产出目标进程可读已提交内存块（bytes）。只读，跳过不可读/守护页。"""
    import ctypes
    from ctypes import wintypes

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        # 严格对齐 Windows 官方 _MEMORY_BASIC_INFORMATION（64 位）：
        # BaseAddress(8) / AllocationBase(8) / AllocationProtect(4) / RegionSize(8) / State(4) / Protect(4) / Type(4)
        _fields_ = [("BaseAddress", ctypes.c_void_p),
                    ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD),
                    ("Type", wintypes.DWORD)]

    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    MEM_COMMIT = 0x1000
    PAGE_NOACCESS = 0x01
    PAGE_GUARD = 0x100
    CHUNK = 8 * 1024 * 1024
    total = 0
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION()
    while total < max_total_bytes:
        ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        base = mbi.BaseAddress or addr
        region_size = mbi.RegionSize or 0
        if region_size <= 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect != PAGE_NOACCESS and not (mbi.Protect & PAGE_GUARD):
            read = 0
            while read < region_size and total < max_total_bytes:
                n = min(CHUNK, region_size - read)
                buf = ctypes.create_string_buffer(n)
                got = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base + read),
                                                buf, n, ctypes.byref(got))
                if ok and got.value:
                    data = buf.raw[:got.value]
                    total += len(data)
                    yield data
                    read += got.value
                else:
                    # 该子段不可读，跳到区域尾
                    read = region_size
        nxt = base + region_size
        if nxt <= addr:
            break
        addr = nxt


def _key_candidate_matches(candidate_text, target_key_id):
    """校验 44 字符 base64 候选是否派生出目标 keyId。"""
    try:
        _, key_id = _derive_at_rest_key(candidate_text)
        return key_id == target_key_id
    except Exception:
        return False


def _extract_key_near_hits(data, hit_indexes, pattern_len, target_key_id, utf16=False):
    """在 keyId 命中点附近 ±4KB 窗口提取 base64 候选并校验。"""
    import re
    win = 4096
    checked = set()
    for h in hit_indexes:
        lo = max(0, h - win)
        hi = min(len(data), h + pattern_len + win)
        seg = data[lo:hi]
        if utf16:
            for m in re.finditer(_B64_CANDIDATE_TEXT_RE, seg.decode("utf-16-le", "ignore")):
                cand = m.group(0)
                if cand in checked:
                    continue
                checked.add(cand)
                if _key_candidate_matches(cand, target_key_id):
                    return cand
        else:
            for m in re.finditer(_B64_CANDIDATE_RE.encode("ascii", "ignore"), seg):
                cand = m.group(0).decode("ascii")
                if cand in checked:
                    continue
                checked.add(cand)
                if _key_candidate_matches(cand, target_key_id):
                    return cand
    return None


def _discover_at_rest_key_from_memory_windows(target_key_id):
    """扫描运行中 WorkBuddy 客户端进程内存寻找 atRestSecretKey（仅 Windows）。

    策略：
      Pass 1（定向，快速路径）：搜索信封 keyId 的 ASCII / UTF-16LE 串，
                                在命中点 ±4KB 窗口内提取 44 字符 canonical base64 候选并校验；
      Pass 2（兜底）：对全部可读内存做 base64 候选（ASCII 与 UTF-16LE）扫描逐个校验。

    ⚠️ 设计要点：Pass 2 必须无条件执行，不能用「Pass 1 是否命中 keyId」来门控。
      客户端内存中 keyId 的 ASCII 串会因 AAD / 日志等上下文大量出现，而密钥本体未必
      落在这些命中点的 ±4KB 邻域内；若以「是否命中」作门控，就会出现「命中 keyId 但
      取不到密钥 → 跳过全内存兜底 → 误报无法定位」的失败。故 Pass 1 失败后无条件继续
      Pass 2，并补齐 UTF-16LE 候选（V8 字符串可能以宽字符驻留），二者共用去重集合与上限。

    找到返回密钥字符串；找不到返回 None。任何异常都不向外抛（签到主流程不应因扫描崩溃）。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        import re as _re
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        pattern = target_key_id.encode("ascii", "ignore")
        if len(pattern) != 16:
            return None
        utf16_pattern = target_key_id.encode("utf-16-le")
        b64_ascii_re = _re.compile(_B64_CANDIDATE_RE.encode("ascii", "ignore"))
        b64_utf16_re = _re.compile(_B64_CANDIDATE_RE_UTF16)
        pids = _list_workbuddy_pids()
        if not pids:
            return None
        seen_candidates = set()
        for pid in pids:
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if not handle:
                continue
            try:
                for data in _iter_process_readable_regions(kernel32, handle):
                    # ---- Pass 1：keyId 邻域快速路径（ASCII 与 UTF-16LE 各试一次）----
                    for pat, is_u16 in ((pattern, False), (utf16_pattern, True)):
                        hits = []
                        start = 0
                        while True:
                            i = data.find(pat, start)
                            if i < 0:
                                break
                            hits.append(i)
                            start = i + 1
                        if hits:
                            key = _extract_key_near_hits(
                                data, hits, len(pat), target_key_id, utf16=is_u16)
                            if key:
                                return key
                    # ---- Pass 2：全内存候选兜底（不再被 Pass 1 命中与否门控）----
                    for m in b64_ascii_re.finditer(data):
                        cand = m.group(0).decode("ascii")
                        if cand in seen_candidates:
                            continue
                        seen_candidates.add(cand)
                        if len(seen_candidates) > _MAX_MEMORY_CANDIDATES:
                            return None
                        if _key_candidate_matches(cand, target_key_id):
                            return cand
                    for m in b64_utf16_re.finditer(data):
                        cand = m.group(0).decode("utf-16-le")
                        if cand in seen_candidates:
                            continue
                        seen_candidates.add(cand)
                        if len(seen_candidates) > _MAX_MEMORY_CANDIDATES:
                            return None
                        if _key_candidate_matches(cand, target_key_id):
                            return cand
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    except Exception:
        return None


_KEY_ACQ_DIAG = {}


def _platform_tag():
    """归一化平台标识：windows / macos / linux。"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _dedupe(items):
    """去重保序。"""
    seen = set()
    out = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _normalized_process_name(raw):
    """进程名归一化：小写并去掉 .exe 后缀，便于跨平台比较。"""
    n = (raw or "").strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n


def _workbuddy_process_names():
    """客户端进程名集合（归一化形式；可用 WORKBUDDY_PROCESS_NAMES 追加）。"""
    names = {"workbuddy"}
    for raw in os.environ.get("WORKBUDDY_PROCESS_NAMES", "").split(","):
        n = _normalized_process_name(raw)
        if n:
            names.add(n)
    return names


def _list_pids_posix(names):
    """Linux 等：用 ps 列出进程并按归一化名称匹配客户端主进程。"""
    try:
        raw = subprocess.run(
            ["ps", "-A", "-o", "pid=,comm="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for line in raw.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, comm = parts
        if _normalized_process_name(os.path.basename(comm.strip())) in names:
            try:
                pids.append(int(pid_str))
            except ValueError:
                continue
    return pids


_KEY_ACQ_DIAG = {}


def _diag_record(source, status, detail=None):
    """记录一次密钥获取尝试的结果，供失败提示与 --diagnose 使用。"""
    _KEY_ACQ_DIAG[source] = {"status": status, "detail": detail}


def _diag_snapshot():
    return {k: dict(v) for k, v in _KEY_ACQ_DIAG.items()}


def _diag_lines():
    """把诊断记录整理成人类可读的若干行（用于失败提示）。"""
    if not _KEY_ACQ_DIAG:
        return []
    lines = ["密钥获取过程记录："]
    for name in sorted(_KEY_ACQ_DIAG):
        item = _KEY_ACQ_DIAG[name]
        detail = item.get("detail")
        suffix = ("（%s）" % detail) if detail else ""
        lines.append("  - %s: %s%s" % (name, item.get("status", "unknown"), suffix))
    return lines


def _default_key_file_paths():
    """atRestSecretKey 的本地缓存路径（写入权限 0600）。

    第一个是本 skill 自己的 data/atrest.key —— 只在 skill 目录内落盘，
    不碰客户端和其他 skill 的位置。
    第二个是上游 skill 的缓存位置，只读兜底：用户装过那个 skill 时可以直接
    复用同一把密钥，省一次 CDP 启动。
    """
    home = os.path.expanduser("~")
    upstream = os.environ.get("CODEBUDDY_CONFIG_DIR") or os.path.join(home, ".workbuddy")
    return _dedupe([
        os.path.join(_skill_data_dir(), "atrest.key"),
        os.path.join(upstream, "scripts", "atrest.key"),
        os.path.join(upstream, "atrest.key"),
    ])


def _skill_data_dir():
    """本 skill 的 data/ 目录（client_auth.py 位于 <skill>/scripts/）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "data")


def _candidate_to_at_rest_key(blob_or_text, target_key_id):
    """尝试把一段字节变成 atRestSecretKey 并校验 keyId。失败抛异常。"""
    # 先尝试作为 DPAPI 密文解开
    text = None
    try:
        plain = _dpapi_unprotect(blob_or_text)
        text = plain.decode("utf-8", "replace").strip()
    except Exception:
        # 也可能就是明文串（无 DPAPI 包裹）
        try:
            text = blob_or_text.decode("utf-8", "replace").strip()
        except Exception:
            text = None
    if not text:
        raise ValueError("无法解析候选密钥")
    # 去掉可能的引号/空白
    text = text.strip().strip('"').strip("'")
    try:
        key, key_id = _derive_at_rest_key(text)
    except Exception:
        raise ValueError("候选不是合法 atRestSecretKey")
    if key_id != target_key_id:
        raise ValueError("keyId 不匹配（%s != %s）" % (key_id, target_key_id))
    return text


def _at_rest_scan_roots():
    """密钥文件候选扫描根：各客户端数据目录下的 Data 子目录，以及 ~/.workbuddy。"""
    roots = []
    home = os.path.expanduser("~")
    for env in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env, "")
        if base:
            roots.append(os.path.join(base, "CodeBuddyExtension", "Data"))
            roots.append(os.path.join(base, "WorkBuddyExtension", "Data"))
    # macOS
    roots.append(os.path.join(home, "Library", "Application Support", "CodeBuddyExtension", "Data"))
    roots.append(os.path.join(home, "Library", "Application Support", "WorkBuddyExtension", "Data"))
    # Linux
    roots.append(os.path.join(home, ".config", "CodeBuddyExtension", "Data"))
    roots.append(os.path.join(home, ".config", "WorkBuddyExtension", "Data"))
    roots.append(os.path.join(home, ".local", "share", "CodeBuddyExtension", "Data"))
    roots.append(os.path.join(home, ".workbuddy"))
    # 去重保序
    seen = set()
    out = []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


_SKIP_DIRS = {
    "cache", "code cache", "gpucache", "file-tree", "extensions", "blob_storage",
    "indexeddb", "service worker", "logs", "crashpad", "swreporter", "network",
    "application cache", "shadercache", "webrtc", "videoDecodeStats",
}


def _iter_candidate_files(root):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except Exception:
                continue
            yield p, sz
            count += 1
            if count > 4000:
                return


def decrypt_access_token_field(raw_value):
    """复刻 ProtectedFieldCodec.decodeString：字符串直接返回；信封则解 AES-GCM。

    raw_value: auth.accessToken 字段（可能是 str，也可能是 {$wbEncrypted:1, envelope}）
    返回明文 JWT 字符串。
    """
    if isinstance(raw_value, str):
        return raw_value  # 明文 JWT（5.5.x 及更早 / 加密关闭时）
    if isinstance(raw_value, dict) and raw_value.get("$wbEncrypted") == 1:
        envelope_b64 = raw_value.get("envelope")
        if not isinstance(envelope_b64, str):
            raise ValueError("加密信封缺少 envelope 字段")
        envelope_bytes = base64.b64decode(envelope_b64)
        if base64.b64encode(envelope_bytes).decode("ascii") != envelope_b64:
            raise ValueError("envelope 不是规范 base64")
        key_id, nonce, ciphertext, auth_tag = _parse_suite1_envelope(envelope_bytes)
        at_rest_secret_key = _discover_at_rest_key(key_id)
        key, _ = _derive_at_rest_key(at_rest_secret_key)
        aad = _build_field_aad(key_id)
        plaintext = _aes_gcm_decrypt(key, nonce, ciphertext, aad, auth_tag)
        return plaintext.decode("utf-8")
    raise ValueError("accessToken 既不是明文也不是加密信封")


def find_login_state():
    candidates = []
    for env_key in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env_key)
        if base:
            for ext in EXTENSION_DIRS:
                candidates.append(os.path.join(base, ext))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, "Library", "Application Support",
                                   "CodeBuddyExtension", "Data", "Public", "auth"))
    candidates.append(os.path.join(home, ".config", "CodeBuddyExtension",
                                   "Data", "Public", "auth"))
    candidates.append(os.path.join(home, USER_AUTH_DIR))
    for d in candidates:
        for name in AUTH_FILENAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


# ---- 密钥获取：平台相关的补充途径 ----
# 上游 buddy_station.py 有六条途径：env / 本地密钥文件 / 客户端数据目录 /
# macOS Keychain / 进程内存 / CDP 调试端口。这里补上 Keychain 与 CDP，裁掉
# core dump 与 Linux /proc —— 上游对后两者自注「未在本项目环境实测」。
# CDP 那条是上游注释里写明的「macOS 上唯一被实测可行的自动途径」。

_CDP_EXPR = (
    "(()=>{"
    "try{const b=process._linkedBinding('electron_browser_workbuddy_storage');"
    "if(b&&b.loggerGet)return 'BINDING:'+b.loggerGet();}catch(e){}"
    "try{const r=(typeof require!=='undefined')?require:"
    "(process.mainModule&&process.mainModule.require);"
    "return 'MOD:'+r('electron').workbuddyStorage.loggerGet();}"
    "catch(e){return 'ERR:'+e.message}})()"
)
_CDP_STARTUP_WAIT = 60      # 等待调试端口就绪的最长时间（秒）
_CDP_EVAL_TIMEOUT = 30      # 等待求值返回的最长时间（秒）

_KEYCHAIN_SERVICE_CANDIDATES = (
    "WorkBuddy Safe Storage",
    "WorkBuddy",
    "CodeBuddy Safe Storage",
    "CodeBuddy",
    "CodeBuddyExtension Safe Storage",
    "CodeBuddyExtension",
    "workbuddy-desktop",
)


def _cdp_probe_enabled():
    """CDP 途径开关（默认开启；WORKBUDDY_ATREST_CDP=0 关闭）。"""
    v = os.environ.get("WORKBUDDY_ATREST_CDP", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _client_hardened_hint():
    """macOS：探测 SIP 状态，用于解释「为什么进程内存 / 核心转储走不通」（只读）。"""
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(["/usr/bin/csrutil", "status"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        text = p.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return None
    if "enabled" in text.lower():
        return ("系统完整性保护（SIP）已启用，客户端以 Hardened Runtime 签名时"
                "会拒绝调试器附加与内存读取，故 macOS 改走 CDP 调试端口途径")
    return text or None


def _client_executable_candidates():
    """本机客户端可执行文件候选（按平台）。

    macOS 段与上游逐字一致。Windows / Linux 段做了精简：这两条平台默认靠本地
    登录态和进程内存就能拿到密钥，CDP 只是最后的兜底，没必要扫全盘。
    可用 WORKBUDDY_CLIENT_PATH 直接指定。
    """
    override = os.environ.get("WORKBUDDY_CLIENT_PATH", "").strip().strip('"')
    if override:
        return [override] if os.path.isfile(override) else []
    home = os.path.expanduser("~")
    cands = []
    if sys.platform == "darwin":
        for app in ("WorkBuddy", "CodeBuddy"):
            for exe in ("Electron", app, "WorkBuddy", "CodeBuddy"):
                cands.append("/Applications/%s.app/Contents/MacOS/%s" % (app, exe))
                cands.append(os.path.join(home, "Applications", "%s.app" % app,
                                          "Contents", "MacOS", exe))
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        pf = os.environ.get("ProgramFiles") or "C:\\Program Files"
        for name in ("WorkBuddy", "CodeBuddy"):
            for base in (os.path.join(local, "Programs", name),
                         os.path.join(local, name),
                         os.path.join(pf, name)):
                for exe in ("%s.exe" % name, "%s.exe" % name.lower()):
                    cands.append(os.path.join(base, exe))
    else:
        for n in ("workbuddy", "codebuddy", "WorkBuddy", "CodeBuddy"):
            p = shutil.which(n)
            if p:
                cands.append(p)
        # POSIX 兜底：直接读运行中的进程（macOS 的 ps comm 给的是完整路径，
        # 自定义安装位置、改名后的包都能命中，比猜 /Applications 可靠）
        try:
            raw = subprocess.run(["ps", "-Ao", "comm="],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=10).stdout.decode("utf-8", "replace")
            for line in raw.splitlines():
                p = line.strip()
                base = os.path.basename(p).lower()
                if not p or not os.path.isabs(p):
                    continue
                if "workbuddy" in base or "codebuddy" in base:
                    cands.append(p)
        except Exception:
            pass

    seen = set()
    out = []
    for c in cands:
        c = os.path.abspath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            out.append(c)
    return out


def _pick_free_port():
    """向系统申请一个空闲端口（用于 --inspect）。失败返回 None。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port
    except Exception:
        return None


class _WSClient(object):
    """极简 WebSocket 客户端（仅 CDP 所需的最小子集：握手 / 文本帧 / 分片）。"""

    def __init__(self, sock, timeout=10):
        self.sock = sock
        self.sock.settimeout(timeout)
        self.buf = b""

    def _fill(self, need):
        while len(self.buf) < need:
            try:
                d = self.sock.recv(65536)
            except socket.timeout:
                raise IOError("读取超时")
            if not d:
                raise IOError("连接已关闭")
            self.buf += d

    def _take(self, n):
        self._fill(n)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            try:
                d = self.sock.recv(4096)
            except socket.timeout:
                raise IOError("握手超时")
            if not d:
                raise IOError("握手阶段连接关闭")
            head += d
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if "101" not in status:
            raise IOError("握手失败：%s" % status.strip())
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send_text(self, text):
        data = text.encode("utf-8")
        n = len(data)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(bytearray(data)))
        self.sock.sendall(bytes(header) + mask + bytes(masked))

    def read_message(self):
        """读取一条完整消息（自动合并分片）。返回 (opcode, payload_bytes)。"""
        out = b""
        opcode = None
        while True:
            b1, b2 = bytearray(self._take(2))
            fin = bool(b1 & 0x80)
            op = b1 & 0x0F
            masked = bool(b2 & 0x80)
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._take(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._take(8))[0]
            if masked:
                mask = self._take(4)
            payload = self._take(ln)
            if masked:
                payload = bytes(bytearray(x ^ mask[i % 4]
                                          for i, x in enumerate(bytearray(payload))))
            if op == 0x8:
                return 0x8, out
            if op in (0x9, 0xA):
                continue
            if opcode is None:
                opcode = op
            out += payload
            if fin:
                return (opcode or 0x1), out


def _cdp_evaluate(host, port, path, expression, timeout=_CDP_EVAL_TIMEOUT):
    """经 CDP 执行一次 Runtime.evaluate，返回字符串结果。失败抛异常。"""
    sock = socket.create_connection((host, port), timeout=timeout)
    ws = _WSClient(sock, timeout=timeout)
    try:
        ws.handshake(host, port, path)
        ws.send_text(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.read_message()
        ws.send_text(json.dumps({
            "id": 2, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": False},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            op, payload = ws.read_message()
            if op == 0x8:
                break
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("id") == 2:
                result = (msg.get("result") or {}).get("result") or {}
                if "value" in result:
                    return result.get("value")
                if msg.get("error"):
                    raise IOError("CDP 错误：%s" % (msg["error"].get("message") or msg["error"]))
                raise IOError("求值未返回值：%s" % json.dumps(msg)[:200])
        raise IOError("等待 CDP 求值返回超时")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _parse_cdp_key_payload(value):
    """从 loggerGet() 返回内容中取出 atRestSecretKey。"""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    text = value.strip()
    for prefix in ("BINDING:", "MOD:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if text.startswith("ERR:"):
        raise IOError(text[4:])
    try:
        obj = json.loads(text)
    except Exception:
        raise IOError("调试端口返回内容不是 JSON：%s" % text[:120])
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("atRestSecretKey", "secretKey", "key", "at_rest_secret_key"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
    raise IOError("返回内容中未找到 atRestSecretKey：%s" % text[:120])


def _discover_at_rest_key_from_cdp(target_key_id, auto_save=True):
    """经客户端主进程 Node 调试端口（CDP）取 atRestSecretKey。

    做法：用临时 user-data-dir 再起一个客户端实例并带 --inspect，抢在单例锁
    把它踢掉之前连上 Node Inspector，让客户端自己的原生模块交出密钥。
    macOS 上这是唯一实测可行的自动途径。

    取得后按 keyId 严格校验；通过且 auto_save 时缓存到 data/atrest.key（0600）。
    返回密钥字符串；失败返回 None（原因写进诊断记录）。
    """
    if not _cdp_probe_enabled():
        _diag_record("cdp.inspector", "skipped", "已由 WORKBUDDY_ATREST_CDP=0 关闭")
        return None
    exes = _client_executable_candidates()
    if not exes:
        _diag_record("cdp.inspector", "failed",
                     "未找到客户端可执行文件（可设 WORKBUDDY_CLIENT_PATH=绝对路径 指定）")
        return None
    tmpdir = tempfile.mkdtemp(prefix="wb-cdp-profile-")
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    proc = None
    try:
        # 候选可执行文件 × 2 轮：单例锁存在竞态，同一候选重试一次通常能抢到
        attempts = (exes[:2]) * 2 or []
        last_err = "未尝试"
        key = None
        for exe in attempts:
            port = _pick_free_port()
            if not port:
                _diag_record("cdp.inspector", "failed", "无法申请空闲调试端口")
                return None
            args = [exe, "--inspect=%d" % port, "--user-data-dir=%s" % tmpdir]
            if sys.platform.startswith("linux"):
                args.append("--no-sandbox")
            try:
                proc = subprocess.Popen(args, env=env,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL)
            except Exception as e:
                last_err = "启动客户端实例失败：%s" % e
                continue
            # 抢在「单例锁」之前抓调试目标：第二实例会在 1s 内因 single_instance_locked
            # 退出，但 Node Inspector 在启动早期就已监听 —— 所以 0.2s 密集轮询，
            # 且不因进程退出而放弃（退出后端口可能仍短暂可连）。
            target = None
            deadline = time.time() + _CDP_STARTUP_WAIT
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/json/list" % port, timeout=2) as resp:
                        listing = json.loads(resp.read().decode("utf-8", "replace"))
                except Exception:
                    time.sleep(0.2)
                    continue
                for t in listing or []:
                    if t.get("type") == "node" and t.get("webSocketDebuggerUrl"):
                        target = t
                        break
                if target:
                    break
                time.sleep(0.2)
            if not target:
                last_err = ("调试端口未在 %ds 内就绪（实例启动失败、被单例锁拦截，"
                            "或客户端可执行文件为 stub）" % _CDP_STARTUP_WAIT)
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc = None
                time.sleep(1.5)  # 给上一个实例留出释放锁的时间
                continue
            url = urllib.parse.urlparse(target["webSocketDebuggerUrl"])
            try:
                value = _cdp_evaluate(url.hostname or "127.0.0.1",
                                      url.port or port,
                                      url.path or "/", _CDP_EXPR)
                key = _parse_cdp_key_payload(value)
            except Exception as e:
                last_err = "调试求值失败：%s" % e
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc = None
                time.sleep(1.5)
                continue
            break

        if not key:
            _diag_record("cdp.inspector", "failed", last_err)
            return None
        if not _key_candidate_matches(key, target_key_id):
            _diag_record("cdp.inspector", "failed",
                         "取得的密钥与登录态信封 keyId 不匹配（可能已轮换）")
            return None
        note = "经客户端调试端口取得密钥"
        if auto_save:
            try:
                ok, _msg = save_at_rest_key(key)
                note += "并已缓存到 data/atrest.key（0600）" if ok else "（缓存失败，仅本次使用）"
            except Exception:
                pass
        _diag_record("cdp.inspector", "ok", note)
        return key
    except Exception as e:
        _diag_record("cdp.inspector", "failed", "异常：%s" % e)
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ---- macOS Keychain ----

def _keychain_service_names():
    """Keychain 探测的 service 名列表（可用 WORKBUDDY_KEYCHAIN_SERVICES 追加）。"""
    names = list(_KEYCHAIN_SERVICE_CANDIDATES)
    for raw in os.environ.get("WORKBUDDY_KEYCHAIN_SERVICES", "").split(","):
        s = raw.strip()
        if s and s not in names:
            names.append(s)
    return names


def _keychain_service_hit(service):
    """探测 Keychain 中是否存在该 service 的条目（只查属性、不读值，不触发授权弹窗）。"""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _keychain_read_value(service):
    """读取 Keychain 条目值；可能触发系统授权弹窗，故仅在条目已确认存在时调用。值绝不打印。"""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return None


def _extract_b64_candidates_from_text(text):
    """从一段文本中提取 44 字符 canonical base64 候选（含 JSON atRestSecretKey 字段）。"""
    if not isinstance(text, str) or not text:
        return []
    out = []
    for c in re.findall(r"[A-Za-z0-9+/]{43}=", text):
        if c not in out:
            out.append(c)
    try:
        d = json.loads(text)
        if isinstance(d, dict) and isinstance(d.get("atRestSecretKey"), str):
            if d["atRestSecretKey"] not in out:
                out.append(d["atRestSecretKey"])
    except Exception:
        pass
    return out


def _discover_at_rest_key_from_keychain(target_key_id):
    """macOS：在 Keychain 的 safeStorage 类条目中查找 atRestSecretKey。

    上游实测客户端 5.6.2（macOS）未把密钥写入 Keychain（由原生模块在运行时提供），
    本环节仅作兼容兜底：只在条目真实存在时读值，并按 keyId 严格校验。
    """
    if sys.platform != "darwin":
        return None
    try:
        for service in _keychain_service_names():
            if not _keychain_service_hit(service):
                continue
            value = _keychain_read_value(service)
            if not value:
                continue
            for cand in _extract_b64_candidates_from_text(value):
                if _key_candidate_matches(cand, target_key_id):
                    return cand
        return None
    except Exception:
        return None


def save_at_rest_key(raw_text):
    """校验并把 atRestSecretKey 缓存到 data/atrest.key（权限 0600）。

    只写本 skill 自己的目录，不碰客户端和别的 skill 的位置。
    返回 (ok, message)；message 里只出现 keyId，不出现密钥本体。
    """
    text = (raw_text or "").strip().strip('"').strip("'").strip()
    if not text:
        return False, "密钥为空。"
    try:
        _, key_id = _derive_at_rest_key(text)
    except Exception as e:
        return False, "不是合法的 44 字符规范 base64 密钥：%s" % e
    path = _default_key_file_paths()[0]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as e:
        return False, "写入失败：%s" % e
    return True, "密钥已缓存（keyId=%s）：%s" % (key_id, path)


def _discover_at_rest_key(target_key_id):
    """定位并解开 atRestSecretKey（44 字符规范 base64），要求派生 keyId 一致。

    顺序（与上游 _discover_at_rest_key 同序）：
      1) 环境变量 WORKBUDDY_ATREST_KEY
      2) 环境变量 WORKBUDDY_ATREST_KEY_FILE 指向的文件
      3) 本地密钥缓存（本 skill 的 data/atrest.key，及上游 skill 的缓存位置）
      4) 客户端数据目录里由平台密钥库加密的密钥文件（Windows 上是 DPAPI blob）
      5) macOS：Keychain safeStorage 类条目（兼容兜底）
      6) 进程内存：仅 Windows（ReadProcessMemory）
      7) 客户端调试端口 CDP（macOS 主途径，其他平台兜底）

    返回 atRestSecretKey 字符串；找不到抛 RuntimeError（带按平台区分的提示）。
    """
    _KEY_ACQ_DIAG.clear()

    env_key = os.environ.get("WORKBUDDY_ATREST_KEY")
    if env_key:
        try:
            _derive_at_rest_key(env_key)
            _diag_record("env.WORKBUDDY_ATREST_KEY", "ok", "由环境变量提供")
            return env_key
        except Exception as e:
            _diag_record("env.WORKBUDDY_ATREST_KEY", "failed", "格式非法：%s" % e)
    else:
        _diag_record("env.WORKBUDDY_ATREST_KEY", "unset")

    env_file = os.environ.get("WORKBUDDY_ATREST_KEY_FILE")
    if env_file:
        if os.path.isfile(env_file):
            try:
                key = _candidate_to_at_rest_key(open(env_file, "rb").read(), target_key_id)
                _diag_record("env.WORKBUDDY_ATREST_KEY_FILE", "ok", "由环境变量指定文件提供")
                return key
            except Exception as e:
                _diag_record("env.WORKBUDDY_ATREST_KEY_FILE", "failed", str(e))
        else:
            _diag_record("env.WORKBUDDY_ATREST_KEY_FILE", "failed", "文件不存在：%s" % env_file)
    else:
        _diag_record("env.WORKBUDDY_ATREST_KEY_FILE", "unset")

    for kf in _default_key_file_paths():
        if not os.path.isfile(kf):
            continue
        try:
            key = _candidate_to_at_rest_key(open(kf, "rb").read(), target_key_id)
            _diag_record("keyfile.%s" % os.path.basename(kf), "ok", "由本地密钥文件提供")
            return key
        except Exception as e:
            _diag_record("keyfile.%s" % os.path.basename(kf), "failed", str(e))

    # 客户端数据目录里由平台密钥库加密的密钥文件（Windows 上是 DPAPI blob）
    DPAPI_MAGIC = b"\x01\x00\x00\x00\xd0\x8c\x9d\x0a"
    dpapi_tried = 0
    for root in _at_rest_scan_roots():
        if not os.path.isdir(root):
            continue
        for path, size in _iter_candidate_files(root):
            if size > 256 * 1024:
                continue
            try:
                content = open(path, "rb").read()
            except Exception:
                continue
            idx = content.find(DPAPI_MAGIC)
            if idx < 0:
                continue
            dpapi_tried += 1
            try:
                key = _candidate_to_at_rest_key(content[idx:], target_key_id)
                if key:
                    _diag_record("client.keyfile_scan", "ok", "客户端密钥文件解出密钥")
                    return key
            except Exception:
                continue
    _diag_record("client.keyfile_scan", "failed",
                 "扫描客户端数据目录，命中 %d 个候选密钥文件" % dpapi_tried)

    # macOS：Keychain 兼容兜底
    try:
        kc_key = _discover_at_rest_key_from_keychain(target_key_id)
    except Exception:
        kc_key = None
    if kc_key:
        _diag_record("macos.keychain", "ok", "由 Keychain 条目解出密钥")
        return kc_key
    if sys.platform == "darwin":
        _diag_record("macos.keychain", "failed", "未发现 safeStorage 类条目命中")

    # 进程内存：本子集只实现 Windows
    if sys.platform.startswith("win"):
        try:
            mem_key = _discover_at_rest_key_from_memory_windows(target_key_id)
        except Exception:
            mem_key = None
        if mem_key:
            _diag_record("process.memory_scan", "ok", "由进程内存命中密钥")
            return mem_key
        _diag_record("process.memory_scan", "failed", "未命中")
    else:
        _diag_record("process.memory_scan", "skipped", "本子集仅实现 Windows 进程内存")

    # 客户端调试端口：macOS 主途径，其他平台兜底
    try:
        cdp_key = _discover_at_rest_key_from_cdp(target_key_id)
    except Exception:
        cdp_key = None
    if cdp_key:
        return cdp_key

    hint = _client_hardened_hint() if sys.platform == "darwin" else None
    raise RuntimeError(
        "无法在本机定位 WorkBuddy 5.6.2+ 的 atRestSecretKey（平台：%s）。\n"
        "已尝试：环境变量、本地密钥文件、%d 个候选密钥文件、%s、客户端调试端口。\n%s\n"
        "可选项：\n"
        "  (a) 确认 WorkBuddy 客户端已启动并登录后重跑；\n"
        "  (b) 设置环境变量 WORKBUDDY_ATREST_KEY=<44字符规范base64>；\n"
        "  (c) 直接提供现成 JWT：设置 WB_ACCESS_TOKEN，绕过解密；\n"
        "  (d) 设 WORKBUDDY_ATREST_CDP=1（默认已开）并确认客户端可执行文件路径，"
        "或用 WORKBUDDY_CLIENT_PATH 指定。\n"
        % (_platform_tag(), dpapi_tried,
           "客户端进程内存" if sys.platform.startswith("win")
           else "Keychain（本平台已试）",
           "\n".join(_diag_lines()) or "（无诊断记录）")
    )
