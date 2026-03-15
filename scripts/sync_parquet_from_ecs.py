#!/usr/bin/env python3
"""
本地 parquet 同步脚本：从 ECS 下载完整 parquet 文件到本地，校验后删除云端。

- 仅同步「已完成」文件：文件名日期早于今天的（当天文件仍在写入）
- 下载后校验：本地文件大小与云端一致
- 校验通过后才删除云端文件

用法: python scripts/sync_parquet_from_ecs.py
从项目根目录 .env 读取 DEPLOY_PASSWORD 等，无需每次输入
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import pexpect
except ImportError:
    print("请安装 pexpect: pip install pexpect", file=sys.stderr)
    sys.exit(1)


def _load_dotenv() -> None:
    """从项目根目录 .env 加载配置到 os.environ"""
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"").strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# 配置（环境变量或 .env）
SERVER = os.environ.get("SYNC_ECS_SERVER", "47.238.152.210")
USER = os.environ.get("SYNC_ECS_USER", "root")
REMOTE_DATA_DIR = os.environ.get("SYNC_REMOTE_DIR", "/root/btc_collector/collector_data")
LOCAL_DATA_DIR = os.environ.get("SYNC_LOCAL_DIR", "")
PASSWORD = os.environ.get("DEPLOY_PASSWORD", "")

# parquet 文件名格式: prefix_YYYYMMDD.parquet
PARQUET_PATTERN = re.compile(r"^(.+)_(\d{8})\.parquet$")


def get_today_utc() -> str:
    """今日日期 YYYYMMDD (UTC)，用于过滤「未完成」文件"""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def parse_parquet_filename(name: str) -> tuple[str, str] | None:
    """解析文件名，返回 (prefix, YYYYMMDD) 或 None"""
    m = PARQUET_PATTERN.match(name)
    return (m.group(1), m.group(2)) if m else None


def is_complete_file(name: str) -> bool:
    """仅当文件日期早于今天时视为已完成（当天文件仍在写入）"""
    parsed = parse_parquet_filename(name)
    if not parsed:
        return False
    _, date_str = parsed
    return date_str < get_today_utc()


def run_ssh(cmd: str, password: str) -> tuple[int, str]:
    """执行 SSH 命令，返回 (returncode, stdout)"""
    c = pexpect.spawn(
        f'ssh -o StrictHostKeyChecking=no {USER}@{SERVER} "{cmd}"',
        timeout=120,
        encoding="utf-8",
    )
    c.expect("password:")
    c.sendline(password)
    c.expect(pexpect.EOF)
    c.close()
    return (c.exitstatus if c.exitstatus is not None else 0), (c.before or "")


def run_scp(remote_path: str, local_path: str, password: str) -> int:
    """执行 SCP 下载，返回 returncode"""
    c = pexpect.spawn(
        f"scp -o StrictHostKeyChecking=no {USER}@{SERVER}:{remote_path} {local_path}",
        timeout=600,
        encoding="utf-8",
    )
    c.expect("password:")
    c.sendline(password)
    c.expect(pexpect.EOF)
    c.close()
    return c.exitstatus if c.exitstatus is not None else 0


def get_remote_file_list(password: str) -> list[tuple[str, int]]:
    """获取远端 parquet 文件列表及大小：(filename, size_bytes)"""
    cmd = f"ls -l {REMOTE_DATA_DIR}/*.parquet 2>/dev/null | awk '{{print $NF, $5}}'"
    code, out = run_ssh(cmd, password)
    if code != 0:
        return []
    result = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        path, size_str = parts
        name = Path(path).name
        try:
            size = int(size_str)
            result.append((name, size))
        except ValueError:
            pass
    return result


def main() -> int:
    if not PASSWORD:
        print("请设置 DEPLOY_PASSWORD 环境变量", file=sys.stderr)
        return 1

    project_root = Path(__file__).resolve().parent.parent
    local_dir = Path(LOCAL_DATA_DIR) if LOCAL_DATA_DIR else project_root / "data" / "collector_data"
    local_dir.mkdir(parents=True, exist_ok=True)

    # 获取远端文件列表
    files = get_remote_file_list(PASSWORD)
    complete = [(n, s) for n, s in files if is_complete_file(n)]
    if not complete:
        print("无需要同步的完整 parquet 文件（仅同步昨日及更早的文件）")
        return 0

    print(f"待同步 {len(complete)} 个文件")
    synced = 0
    failed = []

    for name, remote_size in complete:
        remote_path = f"{REMOTE_DATA_DIR}/{name}"
        local_path = local_dir / name

        # 1. 下载
        print(f"  下载: {name} ({remote_size} bytes)...", end=" ", flush=True)
        code = run_scp(remote_path, str(local_path), PASSWORD)
        if code != 0:
            print("下载失败")
            failed.append(name)
            continue

        # 2. 校验大小
        if not local_path.exists():
            print("本地文件不存在")
            failed.append(name)
            continue
        local_size = local_path.stat().st_size
        if local_size != remote_size:
            print(f"校验失败: 本地 {local_size} != 远端 {remote_size}")
            local_path.unlink(missing_ok=True)
            failed.append(name)
            continue

        # 3. 删除云端
        rm_cmd = f"rm -f {remote_path}"
        code, _ = run_ssh(rm_cmd, PASSWORD)
        if code != 0:
            print("下载成功，但删除云端失败（请手动检查）")
            failed.append(name)
            continue

        print("OK (已删除云端)")
        synced += 1

    print(f"\n完成: 同步 {synced} 个，失败 {len(failed)} 个")
    if failed:
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
