"""
Proof-of-Work solver + AES decryptor for embed69.org / FlixLatam video player.

Algorithm extracted from SoloLatinoProvider.kt (decompiled):
  1. solvePoW(challenge, difficulty, salt):
       Find i such that SHA-256(challenge + str(i)) hex starts with `difficulty` zeros.
       Return SHA-256(challenge + str(i) + salt) as the AES key.

  2. decryptAES(encrypted_b64, aes_key):
       raw = base64decode(encrypted_b64)
       iv  = raw[0:16]
       ct  = raw[16:]
       return AES-CBC-PKCS7(ct, key=aes_key, iv=iv).decode('utf-8')

  3. decodeBase64Link(link_with_dots):
       parts = link.split('.')
       if len(parts) != 3: return None
       s = parts[1]; pad to multiple of 4
       decoded = base64decode(s).decode('utf-8')
       extract substring between '"link":"' and '"'
"""

import base64
import hashlib
import re
from typing import Optional


def solve_pow(challenge: str, difficulty: int, salt: str) -> bytes:
    """
    Solve the proof-of-work challenge.

    Find smallest i >= 0 such that:
        sha256((challenge + str(i)).encode()).hex()
        starts with `difficulty` zero chars.

    Then return:
        sha256((challenge + str(i) + salt).encode()).digest()
    """
    target = "0" * difficulty
    i = 0
    while True:
        candidate = f"{challenge}{i}".encode("utf-8")
        h = hashlib.sha256(candidate).hexdigest()
        if h.startswith(target):
            # Found - derive AES key
            key_src = f"{challenge}{i}{salt}".encode("utf-8")
            return hashlib.sha256(key_src).digest()
        i += 1


def decrypt_aes(encrypted_b64: str, aes_key: bytes) -> Optional[str]:
    """
    Decrypt an AES-CBC-PKCS7 encrypted base64 payload.

    Layout after base64 decode:
        bytes[0:16]  = IV
        bytes[16:]   = ciphertext
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        # Fallback to pycryptodome
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad
            raw = base64.b64decode(encrypted_b64)
            iv, ct = raw[:16], raw[16:]
            cipher = AES.new(aes_key, AES.MODE_CBC, iv)
            pt = unpad(cipher.decrypt(ct), 16)
            return pt.decode("utf-8")
        except Exception:
            return None

    try:
        raw = base64.b64decode(encrypted_b64)
        iv, ct = raw[:16], raw[16:]
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        pt = unpadder.update(padded) + unpadder.finalize()
        return pt.decode("utf-8")
    except Exception:
        return None


def decode_base64_link(link_with_dots: str) -> Optional[str]:
    """
    Decode a 'something.base64payload.something' style link.

    The middle part is base64 (urlsafe-ish, with padding fix) of a JSON
    snippet like {"link":"https://actual-url","...":"..."}.
    """
    try:
        parts = link_with_dots.split(".")
        if len(parts) != 3:
            return None
        s = parts[1]
        # Pad to multiple of 4
        rem = len(s) % 4
        if rem:
            s = s + "=" * (4 - rem)
        decoded = base64.b64decode(s).decode("utf-8")
        # Extract "link":"..."
        m = re.search(r'"link":"([^"]+)"', decoded)
        return m.group(1) if m else None
    except Exception:
        return None


def parse_player_page(html: str) -> dict:
    """
    Parse the embed69.org / flixlatam player page.

    Returns:
        {
            "pow": {"challenge": "...", "difficulty": int, "salt": "..."} or None,
            "data_link": [ list of items ],
        }
    """
    out = {"pow": None, "data_link": []}

    # POW_CHALLENGE = 'xxx'; POW_DIFFICULTY = N; POW_SALT = 'yyy';
    c = re.search(r"const\s+POW_CHALLENGE\s*=\s*'([^']+)';", html)
    d = re.search(r"const\s+POW_DIFFICULTY\s*=\s*(\d+);", html)
    s = re.search(r"const\s+POW_SALT\s*=\s*'([^']+)';", html)
    if c and d and s:
        out["pow"] = {
            "challenge": c.group(1),
            "difficulty": int(d.group(1)),
            "salt": s.group(1),
        }

    # dataLink = [{...}];
    m = re.search(r"dataLink\s*=\s*(\[.+?\]);", html, re.DOTALL)
    if m:
        import json
        try:
            out["data_link"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            # Try a more greedy regex
            m2 = re.search(r"dataLink\s*=\s*(\[[\s\S]+?\]);\s*\n", html)
            if m2:
                try:
                    out["data_link"] = json.loads(m2.group(1))
                except json.JSONDecodeError:
                    pass

    return out


def resolve_servers(html: str) -> list[dict]:
    """
    Given the player page HTML, return a list of resolved stream URLs:
        [
            {"url": "https://...", "name": "vidhide [LAT]", "language": "LAT"},
            ...
        ]
    """
    parsed = parse_player_page(html)
    pow_info = parsed["pow"]
    data_link = parsed["data_link"]

    # Derive AES key if PoW present
    aes_key: Optional[bytes] = None
    if pow_info:
        try:
            aes_key = solve_pow(
                pow_info["challenge"],
                pow_info["difficulty"],
                pow_info["salt"],
            )
        except Exception:
            aes_key = None

    lang_label = {"LAT": "[LAT]", "ESP": "[CAST]", "SUB": "[SUB]", "JAP": "[JAP]"}

    servers = []
    for item in data_link:
        lang = item.get("video_language", "")
        label = lang_label.get(lang, f"[{lang}]")
        for embed in item.get("sortedEmbeds", []):
            link = embed.get("link", "")
            servername = embed.get("servername", "?")
            if not link:
                continue
            # Skip download embeds
            if servername.lower() == "download":
                continue

            # Two link formats:
            #  1) "a.b.c"  (3 parts separated by '.') -> decodeBase64Link
            #  2) AES-encrypted base64 -> decryptAES with PoW key
            if link.count(".") == 2 and len(link.split(".")) == 3:
                url = decode_base64_link(link)
            elif aes_key:
                url = decrypt_aes(link, aes_key)
            else:
                url = None

            if url:
                servers.append({
                    "url": url,
                    "name": f"{servername} {label}".strip(),
                    "language": lang,
                    "host": servername,
                })

    return servers
