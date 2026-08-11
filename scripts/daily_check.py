#!/usr/bin/env python3
"""每日批量检测已收录应用的新版本（ETag 条件请求，避免 API 限流）

流程：
1. 扫描 metadata/*/info.json 获取全部已收录应用
2. 对每个 repo 调 /repos/{owner}/{repo}/releases/latest（带 If-None-Match）
   - 304：未变更（GitHub 不计入速率配额）
   - 200：release 标签与 info.json 的 sourceTag 不同 → 下载 APK 提取 → 更新元数据/图标
   - 404：无 release，跳过
   - 403：限流 → 立即停止，剩余下次继续
3. 200 响应达预算（防 5000/小时限流）→ 停止，剩余下次继续
4. ETag 持久化到 metadata/_etags.json（写回仓库，跨 run 生效）

用法:
    daily_check.py [rate_budget]

退出码:
    0 完成（可能部分应用因预算/限流未检测）
    1 遇 403 限流或达预算提前停止（正常，下次继续）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from glob import glob

from extract_metadata import log, process_app

API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ETAGS_FILE = "metadata/_etags.json"
DEFAULT_RATE_BUDGET = 3500  # 每次运行 200 响应预算（5000/小时限流留余量）
SLEEP_BETWEEN = 0.12  # 温和请求节奏（~8 请求/秒）


def load_etags():
    try:
        with open(ETAGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_etags(etags):
    with open(ETAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(etags, f, ensure_ascii=False, indent=2, sort_keys=True)


def list_apps():
    """扫描 metadata/ 下所有已收录应用，返回 [(owner, repo), ...]"""
    apps = []
    for d in sorted(glob("metadata/*")):
        if not os.path.isdir(d):
            continue
        info_file = os.path.join(d, "info.json")
        if not os.path.exists(info_file):
            continue
        try:
            with open(info_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            owner = data.get("owner", "")
            repo = data.get("repo", "")
            if owner and repo:
                apps.append((owner, repo))
        except Exception:
            continue
    return apps


def latest_release(owner, repo, etag):
    """调 releases/latest（带 If-None-Match）
    返回 (status, release dict 或 None, 新 etag 或 None)
    status: 200 / 304 / 404 / 403 / 其他 HTTP 码 / 'error'
    """
    req = urllib.request.Request(
        f"{API}/repos/{owner}/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "GStore-DailyCheck/1.0",
        },
    )
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200, json.load(resp), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, etag
        if e.code == 404:
            return 404, None, None
        if e.code == 403:
            return 403, None, None
        return e.code, None, None
    except Exception:
        return "error", None, None


def current_source_tag(key):
    info_file = f"metadata/{key}/info.json"
    try:
        with open(info_file, "r", encoding="utf-8") as f:
            return json.load(f).get("sourceTag", "")
    except Exception:
        return ""


def main():
    rate_budget = DEFAULT_RATE_BUDGET
    if len(sys.argv) > 1:
        try:
            rate_budget = int(sys.argv[1])
        except ValueError:
            pass

    apps = list_apps()
    if not apps:
        log("没有已收录应用，退出")
        return 0
    log(f"共 {len(apps)} 个已收录应用")

    etags = load_etags()
    changed = []
    failed = []
    used_200 = 0
    early_stop = False

    for owner, repo in apps:
        key = f"{owner}@{repo}"
        etag = etags.get(key)
        status, release, new_etag = latest_release(owner, repo, etag)

        if status == 200:
            used_200 += 1
            if new_etag:
                etags[key] = new_etag
            if release is None:
                log(f"[empty]  {key}")
                continue
            tag = release.get("tag_name") or release.get("name") or ""
            old_tag = current_source_tag(key)
            if tag and tag != old_tag:
                log(f"[update] {key}: {old_tag or '无'} -> {tag}")
                ok, data, err = process_app(owner, repo)
                if ok:
                    changed.append(key)
                else:
                    failed.append((key, err))
            else:
                log(f"[same]   {key}: {tag}")
        elif status == 304:
            log(f"[304]    {key}")
        elif status == 404:
            log(f"[no-rel] {key}")
        elif status == 403:
            log(f"[限流] 403 于 {key}，停止本次运行（剩余下次继续）")
            early_stop = True
            break
        elif status == "error":
            log(f"[error]  {key}")
        else:
            log(f"[{status}] {key}")

        if used_200 >= rate_budget:
            log(f"已达 200 响应预算 {rate_budget}，停止（剩余下次继续）")
            early_stop = True
            break

        time.sleep(SLEEP_BETWEEN)

    save_etags(etags)
    log(f"检测完成：{len(apps)} 个应用，更新 {len(changed)} 个，失败 {len(failed)} 个"
        f"{'（提前停止）' if early_stop else ''}")
    for key, err in failed:
        log(f"  ✗ {key}: {err}")

    # 输出到 GitHub Actions summary
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and changed:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## 每日检测结果\n\n更新 {len(changed)} 个应用：\n")
            for key in changed:
                f.write(f"- {key}\n")
    return 1 if early_stop else 0


if __name__ == "__main__":
    sys.exit(main())
