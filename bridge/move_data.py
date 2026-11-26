"""Move the bridge data (config.json with all keys and tokens) to another machine, encrypted end to end.

The data never crosses a network or a helper (a laptop, a chat) readable: the new machine makes a key pair, only
the public key goes to the old machine, the old machine encrypts, the file goes back, only the new machine can open it.

  new machine:  python move_data.py keygen <dir>                       -> <dir>/move-key.pem (private, 0600),
                                                                          prints the public key
  old machine:  python move_data.py export <public.pem> <data dir> <out.enc>
  new machine:  python move_data.py import <dir>/move-key.pem <in.enc> <data dir> [--force]
  or receive:   python move_data.py receive <dir>/move-key.pem <data dir> <port> <allowed net> [--public-url URL]
                (waits for one POST /move from the allowed network, imports it, exits 0; the old machine's
                move-remote.bat sends it there, so nothing has to be copied by hand while the old bridge is off)

AES-256-GCM with a random key, that key wrapped with RSA-OAEP (SHA-256, 3072 bit). Only names and a SHA-256 of the
encrypted file are printed, never contents. Uses only the `cryptography` package the bridge already needs.

Stop the old bridge before export and never start both: the Electrolux refresh token rotates, a second bridge with
the same config.json would break the login of the first (and use the Skoda call limit twice).
"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"STBMOVE1"
# what belongs to the data of a bridge; code, venv, logs, update leftovers and run flags stay behind
TOP_FILES = ("config.json", "claude-tokens.json", "setup-cert.pem", "setup-key.pem", "setup-key.txt")
TOP_DIRS = ("homeconnect", "samsung", "echo")
SKIP_SUFFIX = (".log", ".py", ".pyc", ".bat", ".cmd", ".ps1", ".flag", ".tmp", ".enc")
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def keygen(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    p = d / "move-key.pem"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    (d / "move-pub.pem").write_bytes(pub)
    sys.stdout.write(pub.decode())


def collect(data: Path) -> list[Path]:
    out = [data / n for n in TOP_FILES if (data / n).is_file()]
    for n in TOP_DIRS:
        if (data / n).is_dir():
            out += sorted(p for p in (data / n).rglob("*") if p.is_file() and not p.name.endswith(SKIP_SUFFIX))
    return out


def export(pub_pem: Path, data: Path, out: Path) -> None:
    files = collect(data)
    if not any(p.name == "config.json" and p.parent == data for p in files):
        raise SystemExit(f"no config.json in {data}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(data).as_posix())
    pub = serialization.load_pem_public_key(pub_pem.read_bytes())
    if not isinstance(pub, rsa.RSAPublicKey) or pub.key_size < 3072:
        raise SystemExit("public key must be RSA, 3072 bit or more")
    k, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    wrapped = pub.encrypt(k, OAEP)
    blob = (MAGIC + len(wrapped).to_bytes(2, "big") + wrapped + nonce
            + AESGCM(k).encrypt(nonce, buf.getvalue(), MAGIC))
    out.write_bytes(blob)
    print(f"{len(files)} files, {len(blob)} bytes, sha256 {hashlib.sha256(blob).hexdigest()}")
    for p in files:
        print("  " + p.relative_to(data).as_posix())


def _safe(name: str) -> bool:
    parts = Path(name).parts
    return bool(parts) and not name.startswith(("/", "\\")) and ".." not in parts and ":" not in name


def import_(key_pem: Path, enc: Path, data: Path, force: bool) -> None:
    blob = enc.read_bytes()
    print(f"sha256 {hashlib.sha256(blob).hexdigest()}")
    if not blob.startswith(MAGIC):
        raise SystemExit("not a bridge move file")
    n = int.from_bytes(blob[8:10], "big")
    wrapped, nonce, ct = blob[10:10 + n], blob[10 + n:22 + n], blob[22 + n:]
    key = serialization.load_pem_private_key(key_pem.read_bytes(), password=None)
    plain = AESGCM(key.decrypt(wrapped, OAEP)).decrypt(nonce, ct, MAGIC)
    data.mkdir(parents=True, exist_ok=True)
    if (data / "config.json").exists() and not force:
        raise SystemExit(f"{data}/config.json exists: add --force to replace it")
    with zipfile.ZipFile(io.BytesIO(plain)) as z:
        names = z.namelist()
        bad = [x for x in names if not _safe(x)]
        if bad:
            raise SystemExit(f"refused names in the archive: {bad}")
        for name in names:
            dest = data / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
            os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
            print("  " + name)
    print(f"{len(names)} files in {data}")


def set_public_url(data: Path, url: str) -> None:
    """The address Claude reaches the bridge on (OAuth metadata): changes with the move from Funnel to own domain."""
    import json
    p = data / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg.setdefault("claude", {})["public_url"] = url.rstrip("/")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    print(f"claude.public_url = {url}")


def receive(key_pem: Path, data: Path, port: int, allow: str, public_url: str | None) -> None:
    import ipaddress
    from http.server import BaseHTTPRequestHandler, HTTPServer
    net = ipaddress.ip_network(allow, strict=False)
    if (data / "config.json").exists():
        raise SystemExit(f"{data}/config.json exists: this machine already has a bridge")
    done: dict[str, str] = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D401, ANN001
            print("  " + self.client_address[0] + " " + (fmt % args), flush=True)

        def do_POST(self):  # noqa: N802
            if ipaddress.ip_address(self.client_address[0]) not in net or self.path != "/move" or done:
                self.send_error(403)
                return
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 64 * 1024 * 1024:
                self.send_error(413)
                return
            tmp = data / ".move.enc"
            data.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(self.rfile.read(n))
            try:
                import_(key_pem, tmp, data, force=False)
                if public_url:
                    set_public_url(data, public_url)
            except (SystemExit, ValueError, OSError) as e:  # wrong file: keep waiting for the right one
                self.send_error(400, str(e)[:200])
                return
            finally:
                tmp.unlink(missing_ok=True)
            done["ok"] = "1"
            body = b"imported\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("0.0.0.0", port), H)
    print(f"waiting for POST /move on port {port} from {net}", flush=True)
    while not done:
        srv.handle_request()
    srv.server_close()


def main(argv: list[str]) -> None:
    if len(argv) >= 2 and argv[0] == "keygen":
        keygen(Path(argv[1]))
    elif len(argv) == 4 and argv[0] == "export":
        export(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    elif len(argv) in (5, 7) and argv[0] == "receive":
        url = argv[6] if len(argv) == 7 and argv[5] == "--public-url" else None
        receive(Path(argv[1]), Path(argv[2]), int(argv[3]), argv[4], url)
    elif len(argv) in (4, 5) and argv[0] == "import":
        import_(Path(argv[1]), Path(argv[2]), Path(argv[3]), force=argv[4:] == ["--force"])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
