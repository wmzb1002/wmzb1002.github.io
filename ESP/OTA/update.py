#!/usr/bin/env python3
"""
扫描 ESP/OTA/ClientC 和 ESP/OTA/ClientL 下的 .bin 固件，增量更新 version.json。

行为：
  - 新固件（JSON 中不存在的 key+version）：交互式要求输入更新说明
  - 已有固件且 sha256 相同：跳过，不打扰
  - 已有固件但 sha256 变了（重新编译）：保留原 notes，不询问
  - 非交互环境（CI/管道）：新固件 notes 默认留空，或读同名 .txt 兜底
"""
import re
import sys
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Optional

# ====== 配置 ======
OTA_ROOT   = Path(".")
JSON_PATH  = OTA_ROOT / "version.json"
BASE_URL   = "https://ota.wmzb.us.ci/"
FIRMWARE_DIRS = ["ClientC", "ClientL"]

NAME_RE = re.compile(
    r"^(S3|C3)\s*[_\- ]?\s*[vV]?(\d+\.\d+\.\d+(?:[.\-][A-Za-z0-9]+)*)\.bin$",
    re.IGNORECASE
)

# ====== 工具 ======
def dir_to_device_id(dirname: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", dirname)
    return s.upper()

def version_key(v: str):
    parts = re.split(r"[._\-]", v)
    return [int(p) if p.isdigit() else p for p in parts]

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_existing() -> dict:
    if JSON_PATH.exists():
        try:
            return json.loads(JSON_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] 现有 JSON 解析失败: {e}，将从头生成")
    return {"schema": 1, "updated_at": "", "firmwares": {}}

def load_notes_txt(bin_path: Path) -> str:
    """同名 .txt 作为默认值（仅非交互或用户回车时使用）"""
    txt = bin_path.with_suffix(".txt")
    if txt.exists():
        return txt.read_text(encoding="utf-8").strip()
    return ""

def build_url(device_dir: str, filename: str) -> str:
    return f"{BASE_URL}/{device_dir}/{quote(filename)}"

# ====== 交互输入 ======
def ask_notes(key: str, version: str, default: str = "") -> str:
    """新固件时询问更新说明；非 TTY 环境直接返回 default"""
    if not sys.stdin.isatty():
        return default
    prompt = f"  更新说明 [{key} v{version}]"
    if default:
        prompt += f"（回车保留: {default}）"
    prompt += ": "
    try:
        s = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return s if s else default

# ====== 扫描 ======
def scan_all() -> list:
    results = []
    for dname in FIRMWARE_DIRS:
        d = OTA_ROOT / dname
        if not d.exists():
            print(f"[skip] 目录不存在: {d}")
            continue
        device_id = dir_to_device_id(dname)
        for bin_path in sorted(d.glob("*.bin")):
            m = NAME_RE.match(bin_path.name)
            if not m:
                print(f"[skip] 文件名不符格式: {bin_path.name}")
                continue
            chip    = m.group(1).upper()
            version = m.group(2)
            results.append({
                "key":      f"{device_id}_{chip}",
                "chip":     chip,
                "version":  version,
                "filename": bin_path.name,
                "path":     bin_path,
                "size":     bin_path.stat().st_size,
                "sha256":   sha256_of(bin_path),
                "url":      build_url(dname, bin_path.name),
                "notes":    load_notes_txt(bin_path),   # 默认值，交互时可能被覆盖
            })
    # 去重
    seen = {}
    for info in results:
        k = (info["key"], info["version"])
        if k in seen:
            print(f"[warn] 重复版本 {k}: {seen[k]['filename']} vs {info['filename']}，保留前者")
            continue
        seen[k] = info
    return list(seen.values())

# ====== 现有索引 ======
def build_lookup(existing: dict) -> dict:
    lookup = {}
    for key, block in existing.get("firmwares", {}).items():
        if isinstance(block.get("latest"), dict):
            lookup[(key, block["latest"]["version"])] = block["latest"]
        for e in block.get("history", []):
            lookup[(key, e["version"])] = e
    return lookup

# ====== 合并 ======
def build_entry(info: dict, existing: Optional[dict]) -> Optional[dict]:
    if existing and existing.get("sha256") == info["sha256"]:
        return None
    return {
        "version":     info["version"],
        "chip":        info["chip"],
        "url":         info["url"],
        "size":        info["size"],
        "sha256":      info["sha256"],
        "uploaded_at": existing["uploaded_at"] if existing else iso_now(),
        "notes":       info["notes"] or (existing.get("notes", "") if existing else ""),
    }

def merge(existing: dict, scanned: list):
    lookup = build_lookup(existing)
    grouped = {}
    added, updated, skipped = [], [], []

    for info in scanned:
        k = (info["key"], info["version"])
        old = lookup.get(k)
        entry = build_entry(info, old)

        if entry is None:
            skipped.append(f"{info['key']} v{info['version']}")
            grouped.setdefault(info["key"], []).append(old)
        else:
            if old:
                updated.append(f"{info['key']} v{info['version']}  {entry['notes']}")
            else:
                added.append(f"{info['key']} v{info['version']}  {entry['notes']}")
            grouped.setdefault(info["key"], []).append(entry)

    # 保留 JSON 里已有但目录里没了的条目（历史不丢）
    for (key, ver), entry in lookup.items():
        grouped.setdefault(key, [])
        if not any(e["version"] == ver for e in grouped[key]):
            grouped[key].append(entry)

    new_firmwares = {}
    for key, entries in grouped.items():
        entries.sort(key=lambda e: version_key(e["version"]), reverse=True)
        new_firmwares[key] = {
            "latest":  entries[0],
            "history": entries[1:],
        }
    return new_firmwares, added, updated, skipped

# ====== 主流程 ======
def main():
    print(f"扫描: {', '.join(str(OTA_ROOT / d) for d in FIRMWARE_DIRS)}")
    existing = load_existing()
    scanned = scan_all()
    print(f"发现 {len(scanned)} 个 bin\n")

    # ---- 先判断哪些是新增 ----
    lookup = build_lookup(existing)
    new_items = [i for i in scanned if (i["key"], i["version"]) not in lookup]

    if new_items:
        print(f"发现 {len(new_items)} 个新固件，请输入更新说明（直接回车留空）：\n")
        for info in new_items:
            info["notes"] = ask_notes(info["key"], info["version"], info["notes"])
        print()
    else:
        print("没有新增固件，跳过输入。\n")

    # ---- 合并写 JSON ----
    new_firmwares, added, updated, skipped = merge(existing, scanned)

    print("=== 新增 ===")
    for x in (added or ["(无)"]):   print(f"  + {x}")
    print("\n=== 更新（sha256 变化）===")
    for x in (updated or ["(无)"]): print(f"  ~ {x}")
    print("\n=== 跳过（无变化）===")
    for x in (skipped or ["(无)"]): print(f"  = {x}")

    output = {
        "schema": 1,
        "updated_at": iso_now(),
        "firmwares": new_firmwares,
    }
    JSON_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )
    print(f"\n[written] {JSON_PATH}")

    print("\n=== 当前最新版本 ===")
    for key, block in sorted(new_firmwares.items()):
        l = block["latest"]
        note = f"  // {l['notes']}" if l.get("notes") else ""
        print(f"  {key:<16} v{l['version']:<10} {l['size']:>8}B  {l['sha256'][:12]}...{note}")

if __name__ == "__main__":
    main()
