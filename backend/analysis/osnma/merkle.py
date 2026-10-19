"""OSNMA Merkle tree over public-key leaves, ITN paths and GSC XML export."""
from __future__ import annotations

import hashlib


def _h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def leaf(npkt: int, npkid: int, npk: bytes) -> bytes:
    return bytes([npkt << 4 | npkid]) + npk


def levels(leaves: list[bytes]) -> list[list[bytes]]:
    n = len(leaves)
    if n < 1 or n & (n - 1):
        raise ValueError("leaf count must be a power of 2")
    lv = [[_h(x) for x in leaves]]
    while len(lv[-1]) > 1:
        cur = lv[-1]
        lv.append([_h(cur[i] + cur[i + 1]) for i in range(0, len(cur), 2)])
    return lv


def path(lv: list[list[bytes]], index: int) -> list[bytes]:
    out = []
    for level in lv[:-1]:
        out.append(level[index ^ 1])
        index >>= 1
    return out


def root_from_path(leaf_bytes: bytes, index: int, itn: list[bytes]) -> bytes:
    x = _h(leaf_bytes)
    for sib in itn:
        x = _h(sib + x) if index & 1 else _h(x + sib)
        index >>= 1
    return x


def gsc_xml(lv: list[list[bytes]], leaves_meta: list[dict], issue_date: str) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?><signalData><header>'
        f'<GAL-header><issueDate>{issue_date}</issueDate><signalVersion>1.0</signalVersion>'
        '<dataVersion>1.0</dataVersion></GAL-header></header><body><MerkleTree>'
        f'<N>{len(lv[0])}</N><HashFunction>SHA-256</HashFunction>']
    for i, m in enumerate(leaves_meta):
        pt = m["point"]
        parts.append(
            f'<PublicKey><i>{i}</i><PKID>{m["pkid"]}</PKID>'
            f'<lengthInBits>{len(pt) * 4}</lengthInBits><point>{pt}</point>'
            f'<PKType>{m.get("pktype", "ECDSA P-256/SHA-256")}</PKType></PublicKey>')
    for j in range(len(lv) - 1, -1, -1):
        for i, x in enumerate(lv[j]):
            parts.append(
                f'<TreeNode><j>{j}</j><i>{i}</i><lengthInBits>256</lengthInBits>'
                f'<x_ji>{x.hex().upper()}</x_ji></TreeNode>')
    parts.append('</MerkleTree></body></signalData>')
    return "".join(parts)
