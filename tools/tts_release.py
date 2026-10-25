"""Byte-preserving TTS release manifests/backups. No process control or directory mirroring."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASELINE = "1ec2f80e329d923bb300fb6764b599111c031cb3"


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()


def safe(root, rel):
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "tts":
        raise ValueError("not an approved TTS source path")
    path = (root/p).resolve()
    path.relative_to(root.resolve())
    return path


def hash_file(p):
    return digest(p.read_bytes()) if p.is_file() else None


def write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf8")


def plan(source, target):
    paths = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z", "tts"]).decode("utf8").rstrip("\0").split("\0")
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = []
    for rel in paths:
        src, dst = safe(source, rel), safe(target, rel)
        if (src.suffix.lower() not in (".py", ".json", ".md", ".bat", ".toml", ".txt", ".yaml", ".yml", ".lock", ".ps1", ".sh")
                and src.name not in (".gitignore", ".python-version", "LICENSE")):
            raise ValueError("unexpected source extension: " + rel)
        data = src.read_bytes()
        disk = dst.read_bytes() if dst.is_file() else None
        # Keep existing encodings/CRLF when the only difference is checkout newline policy.
        desired = disk if disk is not None and disk.replace(b"\r\n", b"\n") == data.replace(b"\r\n", b"\n") else data
        files.append(dict(path=rel, sha256=digest(desired), md5=digest(desired, "md5"),
                          source_sha256=digest(data), before=hash_file(dst)))
    return dict(candidate_sha=sha, baseline_sha=BASELINE, files=files)


def backup(source, target, dest, manifest, baseline):
    dest.resolve().relative_to((target/"backup/code").resolve())
    if dest.exists():
        raise FileExistsError("backup destination already exists")
    # Validate all inputs and reconstruct all historical bytes BEFORE creating a backup.
    old = {}
    for rel, md5 in baseline.items():
        safe(target, rel)
        raw = subprocess.check_output(["git", "-C", str(source), "show", BASELINE+":"+rel])
        crlf = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        data = next((d for d in (raw, crlf) if digest(d, "md5") == md5), None)
        if data is None:
            raise ValueError("historical baseline mismatch: " + rel)
        old[rel] = data
    for entry in manifest["files"]:
        if hash_file(safe(target, entry["path"])) != entry["before"]:
            raise ValueError("target changed since plan: " + entry["path"])
    dest.mkdir(parents=True)
    for entry in manifest["files"]:
        if entry["before"] is not None:
            rel = entry["path"]
            dst = safe(dest/"disk", rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(safe(target, rel).read_bytes())
            assert hash_file(dst) == entry["before"]
    for rel, data in old.items():
        dst = safe(dest/"v4", rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    write_json(dest/"baseline-md5.json", baseline)
    write_json(dest/"manifest.json", manifest)
    return dict(disk_files=sum(e["before"] is not None for e in manifest["files"]), v4_matched=len(old))


def deploy(source, target, dest, manifest):
    saved = json.loads((dest/"manifest.json").read_text("utf8"))
    if saved != manifest:
        raise ValueError("backup manifest mismatch")
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if sha != manifest["candidate_sha"]:
        raise ValueError("candidate commit changed")
    for e in manifest["files"]:
        if hash_file(safe(source, e["path"])) != e.get("source_sha256", e["sha256"]):
            raise ValueError("candidate bytes changed: " + e["path"])
        if hash_file(safe(target, e["path"])) != e["before"]:
            raise ValueError("target bytes changed: " + e["path"])
    for e in manifest["files"]:
        dst = safe(target, e["path"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        if hash_file(dst) != e["sha256"]:
            dst.write_bytes(safe(source, e["path"]).read_bytes())
        assert hash_file(dst) == e["sha256"]
    write_json(dest/"deployed.json", manifest)
    return dict(deployed=len(manifest["files"]))


def rollback(target, dest, manifest):
    baseline = json.loads((dest/"baseline-md5.json").read_text("utf8"))
    # Validate every destination before touching any file. Never overwrite a later user's change.
    for e in manifest["files"]:
        current = hash_file(safe(target, e["path"]))
        oldpath = safe(dest/"v4", e["path"])
        allowed = {e["sha256"], e["before"], hash_file(oldpath)}
        if current not in allowed:
            raise ValueError("rollback target changed: " + e["path"])
    for rel, md5 in baseline.items():
        if digest(safe(dest/"v4", rel).read_bytes(), "md5") != md5:
            raise ValueError("v4 backup corrupt: " + rel)
    for rel, md5 in baseline.items():
        dst = safe(target, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if hash_file(dst) != hash_file(safe(dest/"v4", rel)):
            dst.write_bytes(safe(dest/"v4", rel).read_bytes())
        assert digest(dst.read_bytes(), "md5") == md5
    removed = 0
    for e in manifest["files"]:
        if e["path"] not in baseline and e["before"] is None:
            dst = safe(target, e["path"])
            if hash_file(dst) == e["sha256"]:
                dst.unlink()
                removed += 1
    return dict(v4_matched=len(baseline), new_files_removed=removed)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["Plan", "Backup", "Deploy", "Rollback"])
    p.add_argument("--release-root", type=Path, required=True)
    p.add_argument("--target-root", type=Path, default=Path("D:/AI/Wife"))
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--backup-root", type=Path)
    a = p.parse_args()
    source, target = a.release_root.resolve(), a.target_root.resolve()
    if a.mode == "Plan":
        m = plan(source, target)
        write_json(a.manifest, m)
        result = dict(files=len(m["files"]), candidate_sha=m["candidate_sha"])
    else:
        m = json.loads(a.manifest.read_text("utf8"))
        if a.backup_root is None:
            p.error("--backup-root required")
        dest = a.backup_root.resolve()
        dest.relative_to((target/"backup/code").resolve())
        if a.mode == "Backup":
            baseline = json.loads((target/"tests/output/tts-refactor/production-baseline.json").read_text("utf8"))
            result = backup(source, target, dest, m, baseline)
        elif a.mode == "Deploy":
            result = deploy(source, target, dest, m)
        else:
            result = rollback(target, dest, m)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
