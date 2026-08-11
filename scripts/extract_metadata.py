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
    """从 resources.arsc 解析颜色资源（ARGB8=0x1C / RGB8=0x1D）
    系统颜色（android 包 0x01）直接查映射表"""
    # Android 系统颜色（android.R.color，包 ID 0x01）
    _ANDROID_COLORS = {
        0x0106000B: "#FF000000",  # black
        0x0106000C: "#FFFFFFFF",  # white
        0x0106000D: "#00000000",  # transparent
        0x0106000E: "#FF444444",  # darker_gray
        0x01060010: "#FFAAAAAA",  # lighter_gray
        0x01060012: "#FF0000FF",  # holo_blue_bright
        0x01060013: "#FF0099CC",  # holo_blue_dark
        0x01060014: "#FF33B5E5",  # holo_blue_light
        0x01060015: "#FFFF8800",  # holo_orange_dark
        0x01060016: "#FFFFBB33",  # holo_orange_light
        0x01060017: "#FF669900",  # holo_green_dark
        0x01060018: "#FF99CC00",  # holo_green_light
        0x01060019: "#FFCC0000",  # holo_red_dark
        0x0106001A: "#FFFF4444",  # holo_red_light
        0x0106001B: "#FF9933CC",  # holo_purple
    }
    if rid in _ANDROID_COLORS:
        return _ANDROID_COLORS[rid]
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
                    # Android ARGB 格式 #AARRGGBB（与 path fillColor 一致，供 _argb_to_css 解析）
                    return "#%02X%02X%02X%02X" % (al, r, g, b)
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


def _resolve_res_files(a, rid):
    """返回资源引用的全部文件变体（同一资源 ID 的多个 density 条目）"""
    try:
        from androguard.core.axml import ARSCResTableEntry
        res = a.get_android_resources()
        files = []
        for e in res.packages[list(res.packages.keys())[0]]:
            if isinstance(e, ARSCResTableEntry) and e.mResId == rid:
                key = e.get_key_data()
                if key and key not in files:
                    files.append(key)
        return files
    except Exception:
        return []


def _argb_to_css(v):
    """Android #AARRGGBB → CSS rgba()（cairosvg 不支持 #RRGGBBAA）"""
    if re.match(r"^#[0-9A-Fa-f]{8}$", v):
        a, r, g, b = int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16), int(v[7:9], 16)
        return "rgba(%d,%d,%d,%.3f)" % (r, g, b, a / 255.0)
    return v


def _parse_fill(a, rid, gid_counter):
    """解析 fillColor 引用 → (SVG fill 属性, 渐变 def 或 None)
    渐变坐标不手动变换（userSpaceOnUse 会跟随嵌套 group transform，由渲染器处理）"""
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
                    x1 = aget(root, "startX") or "0"; y1 = aget(root, "startY") or "0"
                    x2 = aget(root, "endX") or "100"; y2 = aget(root, "endY") or "0"
                    grad = ('<linearGradient id="%s" gradientUnits="userSpaceOnUse" '
                            'x1="%s" y1="%s" x2="%s" y2="%s">%s</linearGradient>'
                            % (gid, x1, y1, x2, y2, stop_xml))
                else:
                    grad = '<radialGradient id="%s">%s</radialGradient>' % (gid, stop_xml)
                return 'fill="url(#%s)"' % gid, grad
        except Exception:
            pass
    return 'fill="#000000"', None


def _group_transform(elem):
    """Android VGroup 变换顺序：translate(-pivot) → scale → rotate → translate(tx+pivot, ty+pivot)
    输出 SVG transform 属性（从右到左应用，与 Android post 系列一致）"""
    tx = float(aget(elem, "translateX") or "0")
    ty = float(aget(elem, "translateY") or "0")
    sx = float(aget(elem, "scaleX") or "1")
    sy = float(aget(elem, "scaleY") or "1")
    rot = float(aget(elem, "rotate") or "0")
    px = float(aget(elem, "pivotX") or "0")
    py = float(aget(elem, "pivotY") or "0")
    if not (tx or ty or sx != 1 or sy != 1 or rot or px or py):
        return ""
    parts = []
    # 注意 SVG transform 列表从右到左应用：最后写最先生效的变换
    parts.append("translate(%s,%s)" % (tx + px, ty + py))  # 最后应用（Android 最后 post）
    if rot:
        parts.append("rotate(%s)" % rot)
    if sx != 1 or sy != 1:
        parts.append("scale(%s,%s)" % (sx, sy))
    parts.append("translate(%s,%s)" % (-px, -py))  # 最先应用
    return ' transform="' + " ".join(parts) + '"'


def _vector_to_content(a, xml_text, gid_counter):
    """VectorDrawable XML → (渐变 defs, 嵌套 group 的 SVG 内容)
    变换保留为嵌套 group transform，由渲染器（cairosvg）处理，
    避免数值变换 pathData 的 arc 参数/rotate 出错导致的偏移"""
    root = ET.fromstring(xml_text)
    defs = []
    out = []

    def walk(elem):
        tag = elem.tag.split("}")[-1]
        if tag == "group":
            # 收集 clip-path 子元素 → defs 定义 + 本组 clip-path 引用
            clip_ref = ""
            children = []
            for ch in elem:
                if ch.tag.split("}")[-1] == "clip-path":
                    cp_id = "cp%d" % (len(defs) + 1)
                    for cp in ch:
                        cp_d = aget(cp, "pathData")
                        if cp_d:
                            defs.append('<clipPath id="%s"><path d="%s"/></clipPath>' % (cp_id, cp_d))
                            clip_ref = ' clip-path="url(#%s)"' % cp_id
                else:
                    children.append(ch)
            out.append("<g%s%s>" % (_group_transform(elem), clip_ref))
            for ch in children:
                walk(ch)
            out.append("</g>")
        elif tag == "path":
            d = aget(elem, "pathData")
            if not d:
                return
            attrs = ['d="%s"' % d]
            fc = aget(elem, "fillColor")
            if fc and fc.startswith("@"):
                fill, grad = _parse_fill(a, int(fc[1:], 16), gid_counter)
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
            out.append("<path " + " ".join(attrs) + "/>")

    # 遍历顶层 children 生成内容
    for ch in root:
        walk(ch)
    body = "".join(out)
    # viewport 归一化：非 108 viewport 的 vector（如 403x403）先缩放到 108 画布，
    # 再被外层 adaptive 缩放（否则内容超出画布导致空白）
    vw = float(aget(root, "viewportWidth") or "108")
    vh = float(aget(root, "viewportHeight") or "108")
    if vw != 108 or vh != 108:
        body = '<g transform="scale(%.6f,%.6f)">%s</g>' % (108.0 / vw, 108.0 / vh, body)
    # vector 根 alpha（透明度）
    alpha = aget(root, "alpha")
    if alpha is not None and float(alpha) < 1:
        body = '<g opacity="%s">%s</g>' % (alpha, body)
    return "".join(defs), body


def _parse_shape(a, xml_text, size, gid_counter):
    """<shape> drawable → SVG 元素（solid/渐变 rect/ellipse + 圆角）
    借鉴 APIE 的 gradient-shape 模型；渐变坐标按 shape 尺寸百分比（0-100）映射"""
    root = ET.fromstring(xml_text)
    shape = aget(root, "shape") or "rectangle"
    radius = 0.0
    stops: list = []  # (offset, css)
    grad_type = None
    gx1 = gy1 = gx2 = gy2 = None
    for ch in root:
        tag = ch.tag.split("}")[-1]
        if tag == "solid":
            col = aget(ch, "color")
            if col:
                stops = [(None, _argb_to_css(col))]
        elif tag == "gradient":
            grad_type = aget(ch, "type") or "0"
            gx1, gy1 = aget(ch, "startX"), aget(ch, "startY")
            gx2, gy2 = aget(ch, "endX"), aget(ch, "endY")
            for item in ch:
                if item.tag.split("}")[-1] == "item":
                    off = aget(item, "offset")
                    offset = float(off) if off else None
                    stops.append((offset, _argb_to_css(aget(item, "color") or "#000000")))
        elif tag == "corners":
            radius = float(aget(ch, "radius") or "0")
    defs = []
    fill_attr = 'fill="#FFFFFF"'
    if grad_type == "0" and len(stops) >= 2:
        gid_counter[0] += 1
        gid = "g%d" % gid_counter[0]
        n = len(stops)
        stop_xml = ""
        for idx, (off, col) in enumerate(stops):
            o = off if off is not None else (idx / (n - 1) if n > 1 else 0)
            stop_xml += '<stop offset="%.2f" stop-color="%s"/>' % (o, col)
        x1 = float(gx1 or 0) / 100 * size
        y1 = float(gy1 or 0) / 100 * size
        x2 = float(gx2 or 100) / 100 * size
        y2 = float(gy2 or 0) / 100 * size
        defs.append('<linearGradient id="%s" gradientUnits="userSpaceOnUse" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f">%s</linearGradient>'
                    % (gid, x1, y1, x2, y2, stop_xml))
        fill_attr = 'fill="url(#%s)"' % gid
    elif len(stops) >= 2:
        # radial（Android shape 用 centerX/centerY/gradientRadius，默认 50%）
        gid_counter[0] += 1
        gid = "g%d" % gid_counter[0]
        n = len(stops)
        stop_xml = ""
        for idx, (off, col) in enumerate(stops):
            o = off if off is not None else (idx / (n - 1) if n > 1 else 0)
            stop_xml += '<stop offset="%.2f" stop-color="%s"/>' % (o, col)
        defs.append('<radialGradient id="%s" gradientUnits="userSpaceOnUse" cx="%.2f" cy="%.2f" r="%.2f">%s</radialGradient>'
                    % (gid, size / 2, size / 2, size / 2, stop_xml))
        fill_attr = 'fill="url(#%s)"' % gid
    elif stops:
        fill_attr = 'fill="%s"' % stops[0][1]

    if shape == "oval":
        elem = '<ellipse cx="%d" cy="%d" rx="%d" ry="%d" %s/>' % (size / 2, size / 2, size / 2, size / 2, fill_attr)
    else:
        rx = ' rx="%.2f"' % radius if radius > 0 else ""
        elem = '<rect width="%d" height="%d"%s %s/>' % (size, size, rx, fill_attr)
    return "".join(defs), elem


def _resolve_background(a, rid, size, gid_counter):
    """解析 adaptive icon 背景引用 → (渐变 defs, SVG 背景元素)
    支持：color / <shape>（solid/渐变/圆角/oval）/ vector / PNG / WebP"""
    from androguard.core.axml import AXMLPrinter
    c = _resolve_color(a, rid)
    if c:
        return "", '<rect width="%d" height="%d" fill="%s"/>' % (size, size, _argb_to_css(c))
    f = _resolve_res_file(a, rid)
    if not f:
        return "", '<rect width="%d" height="%d" fill="#FFFFFF"/>' % (size, size)
    if f.endswith((".png", ".webp")):
        import base64
        import io
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(a.get_file(f)))
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
            data = buf.getvalue()
        except Exception:
            data = a.get_file(f)
        uri = "data:image/png;base64," + base64.b64encode(data).decode()
        return "", '<image href="%s" width="%d" height="%d" preserveAspectRatio="xMidYMid slice"/>' % (uri, size, size)
    try:
        xml = AXMLPrinter(a.get_file(f)).get_xml().decode("utf-8", errors="replace")
        m_tag = re.search(r"<(\w+)[\s>]", xml)
        tag = m_tag.group(1) if m_tag else ""
        if tag == "shape":
            return _parse_shape(a, xml, size, gid_counter)
        if tag == "vector":
            defs, content = _vector_to_content(a, xml, gid_counter)
            return defs, content
    except Exception:
        pass
    return "", '<rect width="%d" height="%d" fill="#FFFFFF"/>' % (size, size)


def render_adaptive_icon(a, icon_path, dest, size=512):
    """渲染自适应图标：背景（色/shape/图/矢量）+ 前景（矢量/PNG）合成"""
    from androguard.core.axml import AXMLPrinter
    xml = AXMLPrinter(a.get_file(icon_path)).get_xml().decode("utf-8", errors="replace")
    m_bg = re.search(r'<background[^>]*android:drawable="(@[0-9A-Fa-f]+)"', xml)
    m_fg = re.search(r'<foreground[^>]*android:drawable="(@[0-9A-Fa-f]+)"', xml)
    if not m_fg:
        return None
    fg_rid = int(m_fg.group(1)[1:], 16)
    fg_file = _resolve_res_file(a, fg_rid)
    if not fg_file:
        return None

    gid = [0]
    if m_bg:
        bg_defs, bg_content = _resolve_background(a, int(m_bg.group(1)[1:], 16), size, gid)
    else:
        bg_defs, bg_content = "", '<rect width="%d" height="%d" fill="#FFFFFF"/>' % (size, size)

    # 前景缩放（借鉴 ApkInfoTool 规范）：画布放大居中、超出部分被裁剪
    # - vector 前景 1.28x（108 viewport 内容按官方模板占中央 ~66dp 安全区）
    # - bitmap 前景 1.60x（PNG/WebP 资源通常内容占画布比例更小）
    fg_scale = 1.60 if fg_file.endswith((".png", ".webp")) else 1.28
    scale = size / 108 * fg_scale
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
        fg_defs, fg_content = "", '<image href="%s" width="%d" height="%d"/>' % (uri, 108, 108)
    else:
        fg_xml = AXMLPrinter(a.get_file(fg_file)).get_xml().decode("utf-8", errors="replace")
        fg_defs, fg_content = _vector_to_content(a, fg_xml, gid)

    combined = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
        "<defs>%s%s</defs>%s"
        '<g transform="translate(%s,%s) scale(%s)">%s</g>'
        "</svg>"
    ) % (size, size, bg_defs, fg_defs, bg_content, offset, offset, scale, fg_content)

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
            # BitmapDrawable 图标（<bitmap android:src="@...">，如 Shizuku）：
            # 解析引用指向的实际图像文件（含全部 density 变体）
            if primary.endswith(".xml"):
                try:
                    from androguard.core.axml import AXMLPrinter
                    xml = AXMLPrinter(a.get_file(primary)).get_xml().decode("utf-8", errors="replace")
                    m = re.search(r'android:src="(@[0-9A-Fa-f]+)"', xml)
                    if m and "<adaptive-icon" not in xml:
                        rid = int(m.group(1)[1:], 16)
                        variants = _resolve_res_files(a, rid)
                        candidates.extend(variants)
                        log(f"BitmapDrawable 图标引用: {primary} -> {variants}")
                except Exception:
                    pass
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
