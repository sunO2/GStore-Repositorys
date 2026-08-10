#!/usr/bin/env python3
"""从 GitHub release 下载 APK 并用 androguard 提取应用元数据。

用法:
    extract_metadata.py <owner> <repo> [keyword]

环境变量:
    GITHUB_TOKEN    GitHub Token（Actions 中为自动提供的 token，用于获取 release）

输出:
    metadata/<owner>@<repo>/info.json   元数据（JSON）
    metadata/<owner>@<repo>/icon.png    应用图标（自适应图标取最大可用 PNG）

退出码:
    0 成功（含"无可用 APK 元数据"的明确失败，已写原因文件）
    2 参数错误 / 3 内部异常
"""
import json
import logging
import os
import re
import struct
import sys
import urllib.error
import urllib.request
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_APK_SIZE = 200 * 1024 * 1024  # 200MB
API = "https://api.github.com"


def log(msg):
    print(f"[extract_metadata] {msg}", flush=True)


def api_get(path):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "GStore-AppMetadata/1.0",
        },
    )
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API {path} 返回 {e.code}") from e


def download(url, dest):
    req = urllib.request.Request(
        url, headers={"User-Agent": "GStore-AppMetadata/1.0"}
    )
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        size = int(resp.headers.get("Content-Length") or 0)
        if size > MAX_APK_SIZE:
            raise RuntimeError(f"APK 大小 {size} 超过限制 {MAX_APK_SIZE} 字节")
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    return size


def pick_apk_asset(assets, keyword):
    apks = [a for a in assets if a["name"].lower().endswith(".apk")]
    if not apks:
        raise RuntimeError("最新 release 中没有 .apk 资产")
    if keyword:
        kw = keyword.lower()
        for a in apks:
            if kw in a["name"].lower():
                return a
        log(f"未找到含 '{keyword}' 的资产，回退取第一个 .apk")
    return apks[0]


def png_size(data):
    """解析 PNG IHDR 获取宽高，非 PNG 返回 None。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height)


def extract_icon(a, icon_path, dest):
    """提取应用图标。

    优先 get_app_icon() 指向的资源；若为自适应图标 XML 或缺失，
    则扫描 APK 内所有 ic_launcher PNG 候选，选分辨率最大的有效 PNG。
    """
    candidates = []
    try:
        primary = a.get_app_icon()
        if primary:
            candidates.append(primary)
    except Exception:
        pass
    for path in a.get_files():
        if re.search(r"(ic_launcher|app_icon).*\.png$", path, re.IGNORECASE):
            candidates.append(path)
        # Compose Multiplatform 应用：图标通常打包在 composeResources 下（资源名被混淆，
        # 但 assets 目录结构保留，常见如 ic_keyguard.png / ic_launcher.png）
        if (
            re.search(r"^assets/composeResources/", path, re.IGNORECASE)
            and re.search(r"(/|^)ic_.*\.png$", path, re.IGNORECASE)
        ):
            candidates.append(path)

    best = None
    best_size = (0, 0)
    for path in dict.fromkeys(candidates):  # 去重保序
        try:
            data = a.get_file(path)
        except Exception:
            continue
        size = png_size(data)
        if size is None:
            log(f"跳过非 PNG 资源: {path}")
            continue
        if size[0] * size[1] > best_size[0] * best_size[1]:
            best_size = size
            best = (path, data)
            log(f"候选图标: {path} {size[0]}x{size[1]}")

    if best is None:
        log("未找到有效 PNG 图标")
        return None

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(best[1])
    log(f"图标已写入: {dest} ({best_size[0]}x{best_size[1]})")
    return dest


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    owner, repo = sys.argv[1], sys.argv[2]
    keyword = sys.argv[3] if len(sys.argv) > 3 else ""

    try:
        import androguard  # noqa: F401
        from androguard.core.apk import APK
    except ImportError:
        print("androguard 未安装，请先执行: pip install androguard")
        return 2
    # 关闭 androguard 的 DEBUG 日志噪音
    logging.getLogger("androguard").setLevel(logging.ERROR)

    project_dir = f"metadata/{owner}@{repo}"
    os.makedirs(project_dir, exist_ok=True)
    meta_file = f"{project_dir}/info.json"
    icon_file = f"{project_dir}/icon.png"

    # 清理旧失败标记
    fail_file = f"{project_dir}/.failed"
    if os.path.exists(fail_file):
        os.remove(fail_file)

    apk_path = None
    try:
        local_apk = os.environ.get("GSTORE_LOCAL_APK")
        if local_apk:
            # 本地调试钩子：跳过 GitHub API 与下载，直接解析本地 APK
            log(f"使用本地 APK: {local_apk}")
            apk_path = local_apk
            tag = "local"
            asset_name = os.path.basename(local_apk)
            published_at = ""
        else:
            log(f"获取仓库信息: {owner}/{repo}")
            repo_info = api_get(f"/repos/{owner}/{repo}")
            if not repo_info.get("permissions", {}).get("pull", True) and repo_info.get("private"):
                raise RuntimeError("仓库为私有仓库，无法访问")

            log(f"获取最新 release")
            try:
                releases = api_get(f"/repos/{owner}/{repo}/releases?per_page=1")
            except RuntimeError as e:
                if "404" in str(e):
                    raise RuntimeError("仓库没有 release")
                raise
            if not releases:
                raise RuntimeError("仓库没有任何 release")

            latest = releases[0]
            tag = latest.get("tag_name") or latest.get("name") or ""
            log(f"最新 release 标签: {tag}")

            asset = pick_apk_asset(latest.get("assets") or [], keyword)
            url = asset.get("browser_download_url") or asset.get("url") or ""
            if not url:
                raise RuntimeError("release 资产缺少下载地址")
            if not url.startswith("https://github.com/") and not url.startswith(f"{API}/"):
                raise RuntimeError(f"拒绝非 GitHub 来源的下载地址: {url}")

            apk_path = f"/tmp/{owner}@{repo}.apk"
            size = download(url, apk_path)
            log(f"APK 下载完成: {asset['name']} ({size} 字节)")
            asset_name = asset["name"]
            published_at = latest.get("published_at") or ""

        a = APK(apk_path)
        package_name = a.get_package()
        version_name = a.get_androidversion_name()
        version_code = a.get_androidversion_code()
        app_name = a.get_app_name()
        log(f"包名={package_name} 版本名={version_name} 版本号={version_code} 应用名={app_name}")

        icon_path = extract_icon(a, a.get_app_icon(), icon_file)

        data = {
            "owner": owner,
            "repo": repo,
            "packageName": package_name or "",
            "appName": app_name or "",
            "versionName": version_name or "",
            "versionCode": version_code if version_code is not None else "",
            "icon": icon_path or "",
            "sourceTag": tag,
            "sourceApk": asset_name,
            "generatedAt": published_at,
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"元数据已写入: {meta_file}")
        print(f"RESULT=ok", flush=True)
        return 0
    except RuntimeError as e:
        log(f"失败: {e}")
        with open(fail_file, "w", encoding="utf-8") as f:
            f.write(str(e))
        print(f"RESULT=fail:{e}", flush=True)
        return 0  # 已知失败场景：正常返回，由 workflow 评论后关闭
    except Exception as e:  # noqa: BLE001
        log(f"异常: {e}")
        with open(fail_file, "w", encoding="utf-8") as f:
            f.write(f"内部异常: {e}")
        return 3
    finally:
        if apk_path and os.path.exists(apk_path):
            os.remove(apk_path)


if __name__ == "__main__":
    sys.exit(main())
