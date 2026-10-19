"""OSNMA keyset persistence (master secret, ECDSA key, Merkle leaves) and receiver exports."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from backend import config
from backend.analysis.osnma import merkle, p256

DEFAULT_PATH = config.DATA_DIR / "osnma" / "keyset.json"
N_LEAVES = 16


def _mac(master: bytes, msg: bytes) -> bytes:
    return hmac.new(master, msg, hashlib.sha256).digest()


def _scalar(master: bytes, msg: bytes) -> int:
    return 1 + int.from_bytes(_mac(master, msg), "big") % (p256.N - 1)


@dataclass(frozen=True)
class Keyset:
    master_secret: bytes
    d: int
    pkid: int
    leaf_index: int
    points: tuple[bytes, ...]

    @cached_property
    def pub(self) -> tuple[int, int]:
        return p256.pubkey(self.d)

    @property
    def npk(self) -> bytes:
        return p256.compress(self.pub)

    @cached_property
    def merkle_levels(self) -> list[list[bytes]]:
        return merkle.levels([merkle.leaf(1, i + 1, pt) for i, pt in enumerate(self.points)])

    @property
    def merkle_root(self) -> bytes:
        return self.merkle_levels[-1][0]

    def _doc(self) -> dict:
        return {"version": 1, "master_secret": self.master_secret.hex(), "d": f"{self.d:064x}",
                "pkid": self.pkid, "leaf_index": self.leaf_index,
                "points": [p.hex() for p in self.points]}

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self._doc(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def create() -> Keyset:
    master = secrets.token_bytes(32)
    d = _scalar(master, b"osnma-ecdsa")
    pts = [p256.compress(p256.pubkey(_scalar(master, b"osnma-leaf" + bytes([i]))))
           for i in range(N_LEAVES)]
    pts[0] = p256.compress(p256.pubkey(d))
    return Keyset(master, d, 1, 0, tuple(pts))


def load(path: Path = DEFAULT_PATH) -> Keyset:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("version") != 1:
        raise ValueError("unsupported keyset version")
    return Keyset(bytes.fromhex(doc["master_secret"]), int(doc["d"], 16), doc["pkid"],
                  doc["leaf_index"], tuple(bytes.fromhex(p) for p in doc["points"]))


def load_or_create(path: Path = DEFAULT_PATH) -> Keyset:
    path = Path(path)
    try:
        return load(path)
    except FileNotFoundError:
        pass
    ks = create()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keyset", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ks._doc(), f, indent=1)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return load(path)
    finally:
        os.unlink(tmp)
    return ks


def pubkey_xml(ks: Keyset, issue_date: str) -> str:
    pt = ks.npk.hex().upper()
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?><signalData><header>'
        f'<GAL-header><issueDate>{issue_date}</issueDate><signalVersion>1.0</signalVersion>'
        '<dataVersion>1.0</dataVersion></GAL-header></header><body><PublicKey>'
        f'<PKID>{ks.pkid}</PKID><lengthInBits>264</lengthInBits><point>{pt}</point>'
        '<PKType>ECDSA P-256/SHA-256</PKType></PublicKey></body></signalData>')


def septentrio_commands(ks: Keyset, pem_path: Path) -> str:
    return (
        "# TEMPLATE: confirm exact setGalOSNMAPublicKeys / setGalOSNMAUsage syntax against the\n"
        "# mosaic-X5 Firmware Reference Guide 4.14 at the bench.\n"
        f"# PKID {ks.pkid}, Merkle root {ks.merkle_root.hex().upper()}\n"
        f"# public key PEM: {pem_path}\n"
        f"setGalOSNMAPublicKeys, Config1, {pem_path}\n"
        "setGalOSNMAUsage, Config1, on\n")


def gnss_sdr_snippet(ks: Keyset, paths: dict[str, Path]) -> str:
    return (
        "Galileo.osnma_enable=true\n"
        f"Galileo.osnma_public_key={paths['pem']}\n"
        f"Galileo.osnma_merkletree={paths['merkle_xml']}\n"
        "Galileo.osnma_mode=strict\n")


def export_all(ks: Keyset, out_dir: Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    issue = "2026-01-01T00:00:00Z"
    paths = {
        "pem": out / f"pubkey_{ks.pkid}.pem",
        "pubkey_xml": out / f"OSNMA_PublicKey_{ks.pkid}.xml",
        "merkle_xml": out / "OSNMA_MerkleTree.xml",
        "septentrio": out / "septentrio_osnma.txt",
        "gnss_sdr": out / "gnss-sdr-osnma.conf",
        "root": out / "merkle_root.txt",
    }
    meta = [{"pkid": i + 1, "point": pt.hex().upper()} for i, pt in enumerate(ks.points)]
    paths["pem"].write_text(p256.pem_public(ks.pub), encoding="utf-8")
    paths["pubkey_xml"].write_text(pubkey_xml(ks, issue), encoding="utf-8")
    paths["merkle_xml"].write_text(merkle.gsc_xml(ks.merkle_levels, meta, issue), encoding="utf-8")
    paths["septentrio"].write_text(septentrio_commands(ks, paths["pem"]), encoding="utf-8")
    paths["gnss_sdr"].write_text(gnss_sdr_snippet(ks, paths), encoding="utf-8")
    paths["root"].write_text(ks.merkle_root.hex().upper() + "\n", encoding="utf-8")
    return paths
