"""Verified Downloads intake. Never choose a file by recency or rename originals.

The queue matches real single-record TXT contents to its SA record. The standalone
command only copies exports into an UNLINKED folder; it never uploads, edits Excel
or invents an SA association. Run without --apply for a read-only preview.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from automation import MAX_TXT, doi, norm, parse_wos, wos
from core import SafetyStop
from wos_policy import affiliation_status


def record_ut(url):
    """Only an exact core-collection record URL can supply an expected UT."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc not in
                ("www.webofscience.com", "webofscience.clarivate.cn")
                or parsed.query or parsed.fragment or re.search(r"%(?:2f|5c)", parsed.path, re.I)):
            raise ValueError
        matched = re.fullmatch(r"/wos/woscc/full-record/(WOS:\d{15})/?", unquote(parsed.path))
        if not matched:
            raise ValueError
        return matched[1]
    except (TypeError, ValueError):
        raise SafetyStop("导出关联的 WOS 单篇网址无效。") from None


def read_export(path):
    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != ".txt" or path.is_symlink():
        raise SafetyStop("TXT 路径不安全或不是完整下载文件。")
    try:
        before = path.stat()
        if not path.is_file() or not 1 <= before.st_size <= MAX_TXT:
            raise SafetyStop("下载未完成或文件大小异常。")
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise SafetyStop("无法读取 WOS TXT；请等待下载完成。") from exc
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise SafetyStop("WOS TXT 正在写入，暂不采纳。")
    return raw, parse_wos(raw)


def matches(record, candidate, expected_url=""):
    identifiers = {"doi": doi(record.doi), "wos": wos(record.wos)}
    if expected_url and candidate["wos"] != record_ut(expected_url):
        return False
    return bool((identifiers["doi"] or identifiers["wos"])
                and norm(record.title) == norm(candidate["title"])
                and all(not value or candidate[key] == value for key, value in identifiers.items()))


def download_paths(folder):
    """Nonrecursive, fixed WOS export names only. Partial downloads are excluded."""
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink():
        return []
    return sorted((p for p in folder.iterdir() if
                   re.fullmatch(r"(?:savedrecs(?: ?\(\d+\))?|wos[-_].+)\.txt", p.name, re.I)
                   and p.is_file() and not p.is_symlink()), key=lambda p: p.name.casefold())


def find_export(record, folders, expected_url=""):
    """Conflicting Full Records stop; equivalent copies do not cause re-export."""
    if expected_url:
        record_ut(expected_url)
    if not (doi(record.doi) or wos(record.wos)):
        return None  # A title alone never adopts an unrelated historical download.
    found = []
    for folder in folders:
        for path in download_paths(folder):
            try:
                raw, candidate = read_export(path)
            except SafetyStop:
                continue
            if matches(record, candidate, expected_url):
                found.append((path, raw, candidate))
    if not found:
        return None
    # Compare all original fields, including those not used by parse_wos (e.g.
    # funding/abstract), rather than silently choosing between different exports.
    if any(raw != found[0][1] for _, raw, _ in found[1:]):
        raise SafetyStop("下载目录中同一论文存在内容不同的完整记录；请核验，未自动选择最新版。")
    return found[0]


def _copy_exact(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SafetyStop("存档目标是链接，拒绝覆盖。")
    if path.exists():
        if path.read_bytes() != content:
            raise SafetyStop("已保存的 TXT 或关联说明被修改，拒绝覆盖。")
        return
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def archive_export(store, path, raw, candidate, record=None):
    """Hash archive + readable copy + immutable association. Original stays put."""
    if parse_wos(raw) != candidate:
        raise SafetyStop("TXT 内容与待保存的元数据不一致。")
    ut = wos(candidate["wos"]).removeprefix("WOS:")
    prefix = ""
    folder = "未关联下载"
    if record is not None:
        if record.owner != "谭勋策" or record.done or record.matches != 0:
            raise SafetyStop("不为其他负责人、已完成或非零匹配任务建立导入关联。")
        # Identifier is a filename component, never a caller-supplied path.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record.sa_id):
            raise SafetyStop("名单编号不能安全用作存档文件名。")
        if norm(record.title) != norm(candidate["title"]) or any(
                value and candidate[key] != value for key, value in
                (("doi", doi(record.doi)), ("wos", wos(record.wos)))):
            raise SafetyStop("TXT 与名单题名或标识冲突，拒绝建立关联。")
        prefix, folder = record.sa_id + "__", "按名单编号"
    store.archive(raw)
    target = store.root / folder / f"{prefix}WOS-{ut}__{candidate['sha256'][:12]}.txt"
    if target.parent.is_symlink():
        raise SafetyStop("存档目录是链接，拒绝写入。")
    metadata = {"schema": 1, "sa_id": record.sa_id if record else None,
                "record_key": record.key if record else None,
                "title": candidate["title"], "doi": candidate["doi"], "wos": candidate["wos"],
                "sha256": candidate["sha256"], "affiliation_status": affiliation_status(candidate),
                "linked_to_roster": record is not None,
                "identity_confirmed": bool(record and (doi(record.doi) or wos(record.wos)))}
    _copy_exact(target, raw)
    _copy_exact(target.with_suffix(".json"), (json.dumps(metadata, ensure_ascii=False,
                indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return str(target.resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description="核验并复制 WOS TXT；不上传、不改名单，下载原件保留。")
    parser.add_argument("--source-dir", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--apply", action="store_true", help="复制有效单篇 TXT 到 code/runtime/wos-downloads/未关联下载")
    args = parser.parse_args(argv)
    if args.apply:
        from wos_batch import default_store
        store = default_store()
    results = []
    for path in download_paths(args.source_dir):
        try:
            raw, candidate = read_export(path)
            entry = {"file": path.name, "title": candidate["title"], "doi": candidate["doi"],
                     "wos": candidate["wos"], "affiliation": affiliation_status(candidate),
                     "status": "未关联名单，不直接入库"}
            if args.apply:
                entry["saved_to"] = archive_export(store, path, raw, candidate)
            results.append(entry)
        except SafetyStop as exc:
            results.append({"file": path.name, "status": "拒绝", "reason": str(exc)})
    print(json.dumps({"mode": "copy" if args.apply else "preview", "files": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
