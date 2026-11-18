"""In GitHub Actions the planner's memory (trip log, learned times, prediction accuracy, plans)
is saved back into the repo between runs, encrypted, so a public repo reveals nothing."""
import io
import pathlib
import tarfile

FILES = ["commute.sqlite", "state.json", "resolved_stops.json"]
OUT = pathlib.Path("state/state.enc")


def _fernet(key):
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def save(key):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in FILES:
            if pathlib.Path(f).exists():
                tar.add(f)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(_fernet(key).encrypt(buf.getvalue()))


def load(key):
    if not OUT.exists():
        return False
    data = _fernet(key).decrypt(OUT.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name in FILES:
                tar.extract(m, filter="data")
    return True
