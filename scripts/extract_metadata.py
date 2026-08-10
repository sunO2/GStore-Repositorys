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
import xml.etree.ElementTree as ET
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_APK_SIZE = 500 * 1024 * 1024  # 500MB
API = "https://api.github.com"

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def aget(elem, name):
    """AXMLPrinter 输出为 android: 前缀属性（非命名空间），兼容两种键"""
    v = elem.get("android:" + name)
    if v is None:
        v = elem.get(ANDROID_NS + name)
    return v


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
    """解析 PNG/WebP 图像尺寸，非图像返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24:
            return None
        width, height = struct.unpack(">II", data[16:24])
        return (width, height)
    # WebP：RIFF....WEBP
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            return img.size
        except Exception:
            return None
    return None


# ==================== 自适应图标渲染（VectorDrawable → SVG → PNG） ====================

def _resolve_color(a, rid):
    """从 resources.arsc 解析颜色资源（ARGB8=0x1C / RGB8=0x1D）"""
    try:
        from androguard.core.axml import ARSCResTableEntry
        res = a.get_android_resources()
        raw = None
        for e in res.packages[list(res.packages.keys())[0]]:
            if isinstance(e, ARSCResTableEntry) and e.mResId == rid:
                if raw is None:
                    raw = a.zip.read("resources.arsc")
                dtype = struct.unpack_from("<B", raw, e.start + 11)[0]
                if dtype in (0x1C, 0x1D):  # TYPE_INT_COLOR_ARGB8 / RGB8
                    v = struct.unpack_from("<I", raw, e.start + 12)[0]
                    if dtype == 0x1C:
                        al, r, g, b = (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
                    else:
                        al, r, g, b = 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
                    return "#%02X%02X%02X%02X" % (r, g, b, al)
    except Exception:
        pass
    return None


def _resolve_res_file(a, rid):
    """返回资源引用的实际文件路径（混淆 APK 的 get_key_data）"""
    try:
        from androguard.core.axml import ARSCResTableEntry
        res = a.get_android_resources()
        for e in res.packages[list(res.packages.keys())[0]]:
            if isinstance(e, ARSCResTableEntry) and e.mResId == rid:
                return e.get_key_data()
    except Exception:
        pass
    return None


def _argb_to_css(v):
    """Android #AARRGGBB → CSS rgba()（cairosvg 不支持 #RRGGBBAA）"""
    if re.match(r"^#[0-9A-Fa-f]{8}$", v):
        a, r, g, b = int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16), int(v[7:9], 16)
        return "rgba(%d,%d,%d,%.3f)" % (r, g, b, a / 255.0)
    return v


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _transform_path_data(d, s, tx, ty):
    """对 pathData 应用 scale(s) + translate(tx,ty)；相对命令只缩放不平移"""
    out = []
    i = 0
    prev_cmd = None
    while i < len(d):
        ch = d[i]
        if ch.isalpha():
            out.append(ch)
            prev_cmd = ch
            i += 1
            continue
        if ch in " ,":
            out.append(ch)
            i += 1
            continue
        m = _NUM.match(d, i)
        if not m:
            i += 1
            continue
        val = float(m.group(0))
        cmd = prev_cmd or "M"
        rel = cmd.islower()
        if cmd.upper() in "HV":
            nv = (val * s + (0 if rel else tx)) if cmd.upper() == "H" else (val * s + (0 if rel else ty))
            out.append("%.2f" % nv if nv == int(nv) else str(nv))
        else:
            i2 = m.end()
            m2 = _NUM.match(d, i2)
            if m2:
                val2 = float(m2.group(0))
                nx = val * s + (0 if rel else tx)
                ny = val2 * s + (0 if rel else ty)
                out.append(str(nx))
                out.append(",")
                out.append(str(ny))
                i = m2.end()
                continue
            else:
                out.append(str(val * s))
        i = m.end()
    return "".join(out)


def _parse_fill(a, rid, gid_counter, s=1.0, tx=0.0, ty=0.0):
    """解析 fillColor 引用 → (SVG fill 属性, 渐变 def 或 None)"""
    c = _resolve_color(a, rid)
    if c:
        return 'fill="%s"' % _argb_to_css(c), None
    f = _resolve_res_file(a, rid)
    if f and f.endswith(".xml"):
        try:
            from androguard.core.axml import AXMLPrinter
            root = ET.fromstring(AXMLPrinter(a.get_file(f)).get_xml().decode("utf-8", errors="replace"))
            tag = root.tag.split("}")[-1]
            if tag == "gradient":
                gid_counter[0] += 1
                gid = "g%d" % gid_counter[0]
                gtype = int(aget(root, "type") or "0")
                stops = []
                for item in root:
                    if item.tag.split("}")[-1] == "item":
                        col = aget(item, "color") or "#000000"
                        off = aget(item, "offset")
                        stops.append((float(off) if off else None, _argb_to_css(col)))
                n = len(stops)
                stop_xml = ""
                for idx, (off, col) in enumerate(stops):
                    o = off if off is not None else (idx / (n - 1) if n > 1 else 0)
                    stop_xml += '<stop offset="%.2f" stop-color="%s"/>' % (o, col)
                if gtype == 0:
                    x1 = float(aget(root, "startX") or "0"); y1 = float(aget(root, "startY") or "0")
                    x2 = float(aget(root, "endX") or "100"); y2 = float(aget(root, "endY") or "0")
                    grad = ('<linearGradient id="%s" gradientUnits="userSpaceOnUse" '
                            'x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f">%s</linearGradient>'
                            % (gid, x1 * s + tx, y1 * s + ty, x2 * s + tx, y2 * s + ty, stop_xml))
                else:
                    grad = '<radialGradient id="%s">%s</radialGradient>' % (gid, stop_xml)
                return 'fill="url(#%s)"' % gid, grad
        except Exception:
            pass
    return 'fill="#000000"', None


def _vector_to_svg(a, xml_text, s, tx, ty):
    """VectorDrawable XML → (渐变 defs, path 内容)；pathData 已数值变换"""
    root = ET.fromstring(xml_text)
    gid = [0]
    defs = []
    parts = []

    def walk_flat(elem, cs, ctx, cty):
        tag = elem.tag.split("}")[-1]
        if tag == "group":
            gs = float(aget(elem, "scaleX") or "1")
            gy = float(aget(elem, "scaleY") or "1")
            gtx = float(aget(elem, "translateX") or "0")
            gty = float(aget(elem, "translateY") or "0")
            rot = float(aget(elem, "rotate") or "0")
            if rot != 0 or gs != gy:
                return  # 复杂变换（旋转/非等比缩放）：跳过
            for ch in elem:
                walk_flat(ch, cs * gs, ctx + gtx * cs, cty + gty * cs)
        elif tag == "path":
            d = aget(elem, "pathData")
            if not d:
                return
            nd = _transform_path_data(d, cs, ctx, cty)
            attrs = ['d="%s"' % nd]
            fc = aget(elem, "fillColor")
            if fc and fc.startswith("@"):
                fill, grad = _parse_fill(a, int(fc[1:], 16), gid, cs, ctx, cty)
                attrs.append(fill)
                if grad:
                    defs.append(grad)
            elif fc:
                attrs.append('fill="%s"' % _argb_to_css(fc))
            else:
                attrs.append('fill="none"')
            sc = aget(elem, "strokeColor")
            if sc:
                attrs.append('stroke="%s"' % (_argb_to_css(sc) if sc.startswith("#") else sc))
                attrs.append('stroke-width="%s"' % (aget(elem, "strokeWidth") or "1"))
            fa = aget(elem, "fillAlpha")
            if fa:
                attrs.append('fill-opacity="%s"' % fa)
            ft = aget(elem, "fillType")
            if ft in ("evenOdd", "1"):
                attrs.append('fill-rule="evenodd"')
            parts.append("<path " + " ".join(attrs) + "/>")

    for ch in root:
        walk_flat(ch, s, tx, ty)
    return "".join(defs), "".join(parts)


def render_adaptive_icon(a, icon_path, dest, size=512):
    """渲染自适应图标：背景色/图 + 前景矢量/PNG 合成"""
    from androguard.core.axml import AXMLPrinter
    xml = AXMLPrinter(a.get_file(icon_path)).get_xml().decode("utf-8", errors="replace")
    m_bg = re.search(r'<background[^>]*android:drawable="(@[0-9A-Fa-f]+)"', xml)
    m_fg = re.search(r'<foreground[^>]*android:drawable="(@[0-9A-Fa-f]+)"', xml)
    if not m_fg:
        return None
    fg_rid = int(m_fg.group(1)[1:], 16)
    bg_color = _resolve_color(a, int(m_bg.group(1)[1:], 16)) if m_bg else None
    fg_file = _resolve_res_file(a, fg_rid)
    if not fg_file:
        return None

    scale = size / 108 * 0.72
    offset = (size - 108 * scale) / 2

    if fg_file.endswith((".png", ".webp")):
        import base64
        import io
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(a.get_file(fg_file)))
            # WebP 转 PNG（cairosvg 的 image 标签对 webp data URI 支持不稳）
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
            data = buf.getvalue()
        except Exception:
            data = a.get_file(fg_file)
        uri = "data:image/png;base64," + base64.b64encode(data).decode()
        fg_defs, fg_content = "", '<image href="%s" width="%d" height="%d"/>' % (uri, size, size)
    else:
        fg_xml = AXMLPrinter(a.get_file(fg_file)).get_xml().decode("utf-8", errors="replace")
        fg_defs, fg_content = _vector_to_svg(a, fg_xml, scale, offset, offset)

    bg_rect = '<rect width="%d" height="%d" fill="%s"/>' % (size, size, bg_color or "#FFFFFF")
    combined = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
        "%s<defs>%s</defs>%s</svg>"
    ) % (size, size, bg_rect, fg_defs, fg_content)

    import cairosvg
    cairosvg.svg2png(bytestring=combined.encode(), write_to=dest, output_width=size, output_height=size)
    return dest


def extract_icon(a, icon_path, dest):
    """提取应用图标。

    优先级：
    1. APK 内真实 PNG（get_app_icon / ic_launcher / composeResources），取最大
    2. 无 PNG 时：自适应图标（adaptive-icon XML）矢量渲染合成
    """
    candidates = []
    try:
        primary = a.get_app_icon()
        if primary:
            candidates.append(primary)
    except Exception:
        pass
    for path in a.get_files():
        if re.search(r"(ic_launcher|app_icon).*\.(png|webp)$", path, re.IGNORECASE):
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
        # 无可用 PNG/WebP：尝试自适应图标矢量渲染（资源混淆 APK 的兜底方案）
        log("未找到有效 PNG 图标，尝试自适应图标渲染")
        for path in dict.fromkeys(candidates):
            if path and path.endswith(".xml"):
                try:
                    from androguard.core.axml import AXMLPrinter
                    xml = AXMLPrinter(a.get_file(path)).get_xml().decode("utf-8", errors="replace")
                    if "<adaptive-icon" in xml:
                        log(f"检测到自适应图标: {path}，尝试矢量渲染")
                        try:
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            if os.path.exists(dest):
                                os.remove(dest)
                            render_adaptive_icon(a, path, dest)
                            if os.path.exists(dest):
                                log(f"自适应图标渲染成功: {dest}")
                                return dest
                        except ImportError:
                            log("cairosvg 未安装，跳过矢量渲染")
                        except Exception as e:
                            log(f"自适应图标渲染失败: {e}")
                except Exception:
                    pass
        return None

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = best[1]
    # WebP 需转换为 PNG（dest 固定为 .png）
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
            data = buf.getvalue()
            log(f"WebP 已转为 PNG: {best[0]}")
        except Exception as e:
            log(f"WebP 转换失败: {e}")
    with open(dest, "wb") as f:
        f.write(data)
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
        # 本地调试模式不删除用户提供的 APK；仅清理下载的临时 APK
        if apk_path and os.path.exists(apk_path) and not os.environ.get("GSTORE_LOCAL_APK"):
            os.remove(apk_path)


if __name__ == "__main__":
    sys.exit(main())
