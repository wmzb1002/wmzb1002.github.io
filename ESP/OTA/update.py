#!/usr/bin/env python3
"""
扫描 ESP/OTA/ClientC 和 ESP/OTA/ClientL 下的 .bin 固件，增量更新 version.json。

latest 判定规则（不再按版本号最高）：
  1. 本次新出现的条目（JSON 里原本没有 key+version）→ 作为 latest
     多个新出现时，取 mtime 最新的；mtime 相同则版本字符串最大的
  2. 本次没有新出现 → 保持原 JSON 的 latest 不变
  3. 兜底：原 JSON 无该 key 且本次无新出现（理论上不会发生）→ 版本号最高

force 字段：
  - 新固件时交互式询问是否强制更新，默认 N（非交互环境默认 False）
  - 已有固件重新编译（sha256 变化）时沿用旧的 force 值
  - force:true 时设备检测到新版本会静默后台下载，不弹 OLED/TJC 提示

其它行为：
  - 新固件（本次新出现）：交互式要求输入更新说明
  - 已有固件且 sha256 相同：跳过
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

def ask_force(key: str, version: str, default: bool = False) -> bool:
    """新固件时询问是否强制更新；非 TTY 环境返回 default（False 更安全）"""
    if not sys.stdin.isatty():
        return default
    hint = "Y/n" if default else "y/N"
    prompt = f"  强制更新？ [{key} v{version}] ({hint}): "
    try:
        s = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if s in ("y", "yes"):
        return True
    if s in ("n", "no"):
        return False
    return default

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
            stat    = bin_path.stat()
            results.append({
                "key":      f"{device_id}_{chip}",
                "chip":     chip,
                "version":  version,
                "filename": bin_path.name,
                "path":     bin_path,
                "size":     stat.st_size,
                "mtime":    stat.st_mtime,
                "sha256":   sha256_of(bin_path),
                "url":      build_url(dname, bin_path.name),
                "notes":    load_notes_txt(bin_path),
                "force":    False,  # 仅新出现的固件会在 main 里被 ask_force 覆盖
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

def build_existing_latest(existing: dict) -> dict:
    """原 JSON 里每个 key 的 latest 版本"""
    out = {}
    for key, block in existing.get("firmwares", {}).items():
        if isinstance(block.get("latest"), dict):
            out[key] = block["latest"]["version"]
    return out

# ====== 合并 ======
def build_entry(info: dict, existing: Optional[dict]) -> Optional[dict]:
    if existing and existing.get("sha256") == info["sha256"]:
        return None
    # force 策略：
    #   - 已有固件重新编译（sha256 变化）：沿用旧 force 值
    #   - 新出现的固件：用 main 里 ask_force 设置的 info["force"]
    force = existing.get("force", False) if existing else info.get("force", False)
    return {
        "version":     info["version"],
        "chip":        info["chip"],
        "url":         info["url"],
        "size":        info["size"],
        "sha256":      info["sha256"],
        "force":       force,
        "uploaded_at": existing["uploaded_at"] if existing else iso_now(),
        "notes":       info["notes"] or (existing.get("notes", "") if existing else ""),
    }

def merge(existing: dict, scanned: list):
    lookup = build_lookup(existing)
    existing_latest = build_existing_latest(existing)

    grouped = {}
    added, updated, skipped = [], [], []
    new_appeared = {}   # key -> [(version, mtime)]

    for info in scanned:
        k = (info["key"], info["version"])
        old = lookup.get(k)
        entry = build_entry(info, old)

        if entry is None:
            skipped.append(f"{info['key']} v{info['version']}")
            grouped.setdefault(info["key"], []).append(old)
        else:
            if old is None:
                added.append(f"{info['key']} v{info['version']}  force={entry['force']}  {entry['notes']}")
                new_appeared.setdefault(info["key"], []).append(
                    (info["version"], info["mtime"])
                )
            else:
                updated.append(f"{info['key']} v{info['version']}  force={entry['force']}  {entry['notes']}")
            grouped.setdefault(info["key"], []).append(entry)

    # 保留 JSON 里已有但目录里没有的条目（历史不丢）
    for (key, ver), entry in lookup.items():
        grouped.setdefault(key, [])
        if not any(e["version"] == ver for e in grouped[key]):
            grouped[key].append(entry)

    new_firmwares = {}
    for key, entries in grouped.items():
        latest_entry = None

        if key in new_appeared and new_appeared[key]:
            candidates = new_appeared[key]
            max_mtime = max(c[1] for c in candidates)
            top = [c for c in candidates if c[1] == max_mtime]
            top.sort(key=lambda x: x[0], reverse=True)
            target_ver = top[0][0]
            for e in entries:
                if e["version"] == target_ver:
                    latest_entry = e
                    break

        if latest_entry is None and key in existing_latest:
            old_ver = existing_latest[key]
            for e in entries:
                if e["version"] == old_ver:
                    latest_entry = e
                    break

        if latest_entry is None:
            sorted_entries = sorted(entries, key=lambda e: version_key(e["version"]), reverse=True)
            if sorted_entries:
                latest_entry = sorted_entries[0]

        if latest_entry is None:
            continue

        history = [e for e in entries if e["version"] != latest_entry["version"]]
        history.sort(key=lambda e: version_key(e["version"]), reverse=True)

        new_firmwares[key] = {
            "latest":  latest_entry,
            "history": history,
        }

    return new_firmwares, added, updated, skipped

# ====== 主流程 ======
def main():
    print(f"扫描: {', '.join(str(OTA_ROOT / d) for d in FIRMWARE_DIRS)}")
    existing = load_existing()
    scanned = scan_all()
    print(f"发现 {len(scanned)} 个 bin\n")

    # ---- 先判断哪些是新增（用于交互询问 notes / force）----
    lookup = build_lookup(existing)
    new_items = [i for i in scanned if (i["key"], i["version"]) not in lookup]

    if new_items:
        print(f"发现 {len(new_items)} 个新固件，请输入更新说明与强制策略（直接回车留空/默认）：\n")
        for info in new_items:
            info["notes"] = ask_notes(info["key"], info["version"], info["notes"])
            info["force"] = ask_force(info["key"], info["version"], False)
        print()
    else:
        print("没有新增固件，跳过输入。\n")

    # ---- 合并写 JSON ----
    new_firmwares, added, updated, skipped = merge(existing, scanned)

    print("=== 新增（本次新出现，latest 可能变化）===")
    for x in (added or ["(无)"]):   print(f"  + {x}")
    print("\n=== 更新（sha256 变化，latest 不因此改变）===")
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

    print("\n=== 当前最新版本（latest）===")
    for key, block in sorted(new_firmwares.items()):
        l = block["latest"]
        force_flag = " [FORCE]" if l.get("force") else ""
        note = f"  // {l['notes']}" if l.get("notes") else ""
        print(f"  {key:<16} v{l['version']:<10} {l['size']:>8}B{force_flag}  {l['sha256'][:12]}...{note}")

    print("\n=== 历史版本（history）===")
    for key, block in sorted(new_firmwares.items()):
        if block["history"]:
            for h in block["history"]:
                ff = " [FORCE]" if h.get("force") else ""
                print(f"  {key:<16} v{h['version']:<10} {h['size']:>8}B{ff}")

if __name__ == "__main__":
    main()