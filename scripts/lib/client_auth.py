"""WorkBuddy 客户端登录态解密（Windows 最小子集）。

来源：totorosir-workbuddy-score/scripts/buddy_station.py 的精确子集。
用 AST 按函数名抽取 [lineno, end_lineno] 区间，未改动任何实现。

保留 decrypt_access_token_field 的调用闭包：
  DPAPI 解 atRestSecretKey -> 信封解析 -> 派生 key -> AES-256-GCM 解密
  以及 Windows 进程内存密钥发现（客户端 5.6.2+ 密钥只驻留内存时用）。
裁掉：macOS 的 Keychain / CDP、Linux 的 /proc、core dump 途径。

上游改了客户端加密格式时要同步更新本文件，否则解密报错会指向这里。
本文件不含任何密钥或 token，密钥只在内存中校验。
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys


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
    """本脚本默认查找的 atRestSecretKey 文件（由 --set-key 写入，权限 0600）。

    放在 ~/.workbuddy/scripts/ 下而不是环境变量，是为了让「WorkBuddy 自带自动化」
    （不继承自定义环境变量）也能拿到密钥 —— 用户在终端里 --set-key 一次即可长期生效。
    """
    config_dir = os.environ.get("CODEBUDDY_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".workbuddy")
    return _dedupe([
        os.path.join(config_dir, "scripts", "atrest.key"),
        os.path.join(config_dir, "atrest.key"),
    ])


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


# ---- 精简版密钥获取（上游同名函数的 Windows 子集）----
# 上游还包含 macOS Keychain、CDP 调试端口、Linux /proc、core dump 四条途径。
# 这里只留 Windows 需要的两条：客户端数据目录的 DPAPI 密钥文件、运行中进程内存。
def _discover_at_rest_key(target_key_id):
    """定位并解开 atRestSecretKey（44 字符规范 base64），校验派生 keyId 一致。

    顺序：环境变量 -> 环境变量指定的文件 -> 本地密钥文件 -> 客户端数据目录
          -> 客户端进程内存。

    返回 atRestSecretKey 字符串；找不到抛 RuntimeError（带诊断记录）。
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

    if sys.platform.startswith("win"):
        try:
            mem_key = _discover_at_rest_key_from_memory_windows(target_key_id)
        except Exception:
            mem_key = None
        if mem_key:
            _diag_record("process.memory_scan", "ok", "由进程内存命中密钥")
            return mem_key
        if "process.memory_scan" not in _KEY_ACQ_DIAG:
            _diag_record("process.memory_scan", "failed", "未命中")
    else:
        _diag_record("process.memory_scan", "skipped", "非 Windows 平台，本子集不提供该途径")

    raise RuntimeError(
        "无法在本机定位 WorkBuddy 5.6.2+ 的 atRestSecretKey（平台：%s）。\n"
        "已尝试：环境变量、本地密钥文件、客户端数据目录（命中 %d 个候选）、客户端进程内存。\n%s\n"
        "可选项：\n"
        "  (a) 确认 WorkBuddy 客户端已启动并登录后重跑；\n"
        "  (b) 设置环境变量 WORKBUDDY_ATREST_KEY=<44字符规范base64>；\n"
        "  (c) 直接提供现成 JWT：设置 WB_ACCESS_TOKEN，绕过解密。\n"
        % (_platform_tag(), dpapi_tried, "\n".join(_diag_lines()) or "（无诊断记录）")
    )
