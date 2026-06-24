"""
A3试卷转A4 — 核心算法
功能：PDF渲染、分割线检测、页面裁切、A4 PDF合并
"""

import io
import os
import numpy as np
from typing import List, Tuple, Optional
from PIL import Image
from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError
import fitz  # PyMuPDF


def load_pdf_pages(pdf_path: str, dpi: int = 300) -> List[Image.Image]:
    """将PDF每页渲染为高分辨率PIL Image。

    优先用 pdf2image(poppler)；如果系统未安装 poppler 或 pdf2image 拿不到
    页数 / 不到可执行文件，回退到 PyMuPDF。其他异常（PDF 损坏、权限不足等）
    向上抛出，避免被静默吞掉后用错误后端再失败一次。
    """
    try:
        return convert_from_path(pdf_path, dpi=dpi)
    except (PDFInfoNotInstalledError, PDFPageCountError, FileNotFoundError):
        # poppler 未安装或不可用 → 回退 PyMuPDF
        pass

    doc = fitz.open(pdf_path)
    try:
        pages: List[Image.Image] = []
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for page in doc:
            # 显式关闭 alpha，确保 samples 为纯 RGB；否则 RGBA 数据按 RGB 解码会色彩错位
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(img)
        return pages
    finally:
        doc.close()


def detect_divider(img: Image.Image, search_width: float = 0.15) -> int:
    """
    检测A3图片中间两页之间的分割位置，返回间隙/折痕中心的X坐标。

    自动识别两种场景：
    - 暗折痕（扫描的折叠装订页）→ 找最暗的竖线
    - 空白间隙（印刷A3试卷中间的留白）→ 找最亮的空白带的**中点**

    无论哪种场景都会优先选择靠近页面中心的位置。

    Args:
        search_width: 搜索半宽占页面宽度的比例（默认0.15 → 中心30%区域）
    """
    gray = img.convert('L')
    arr = np.array(gray, dtype=np.float32)
    h, w = arr.shape

    # 每列内容密度（较暗像素占比）
    threshold = np.percentile(arr, 25)
    dark = (arr < threshold).sum(axis=0).astype(np.float32) / h

    # 平滑
    window = max(3, w // 400)
    kernel = np.ones(window) / window
    dark_s = np.convolve(dark, kernel, mode='same')

    # 在中心区域分析
    margin = int(w * search_width)
    x_start = w // 2 - margin
    x_end = w // 2 + margin
    center_dark = dark_s[x_start:x_end]

    n = len(center_dark)
    mid = n // 2

    # 判断类型：中心是否明显比两侧更暗
    left_avg = center_dark[:mid].mean() if mid > 0 else 0.0
    right_avg = center_dark[mid + 1:].mean() if mid + 1 < n else 0.0
    side_avg = (left_avg + right_avg) / 2
    center_avg = center_dark[max(mid - n // 6, 0): min(mid + n // 6, n)].mean()
    is_dark_fold = center_avg > side_avg * 1.3 and side_avg > 0.01

    if is_dark_fold:
        # 暗折痕 → 找最暗列（dark_ratio 峰值），加权偏向中心
        x = np.arange(n)
        weight = np.exp(-((x - mid) ** 2) / (2 * (n * 0.30) ** 2))
        scored = center_dark * (1.0 + weight * 0.5)
        best = int(np.argmax(scored))
        return x_start + best

    # 空白间隙 → 找最大连续"近全白"白带，取**中点**，而不是用加权挑单列。
    # 原因：印刷 A3 试卷的中缝往往是 20–60 像素宽的纯白带，区段内每列
    # dark_ratio 都是 0，加权偏向中心会把切点拉到离白带中心几十像素的位置，
    # 导致切偏（实测某试卷 page2 偏 -34 像素）。
    # 阈值取整个搜索区段的低分位，保证"足够白"且对扫描噪点不敏感。
    blank_threshold = max(np.percentile(center_dark, 10), 0.002)
    is_blank = center_dark <= blank_threshold

    # 找最长的连续 is_blank True 段
    best_start, best_len = -1, 0
    cur_start, cur_len = -1, 0
    for i, b in enumerate(is_blank):
        if b:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
        else:
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
            cur_len = 0
    if cur_len > best_len:
        best_len, best_start = cur_len, cur_start

    if best_len <= 0:
        # 没找到任何近白列（不太可能），退回页面正中心
        return w // 2

    band_center = best_start + best_len // 2

    # 安全网：白带跨度超过搜索区段的 80%，说明该页几乎是空白（如末页只有
    # 页脚），白带横跨大半个候选区 → 任何切点都没有"内容意义"，退回页面
    # 正中心，保证至少左右对称。
    if best_len > n * 0.8:
        return w // 2

    return x_start + band_center


def auto_crop(
    img: Image.Image,
    padding: int = 15,
    threshold: int = 240,
    min_dark_ratio: float = 0.005,
) -> Image.Image:
    """
    自动检测并裁剪图片四周的空白区域。
    只保留有实际内容的区域，外加指定像素的padding。
    这样无论原文档边距大小，输出都能保持一致的视觉效果。

    Args:
        padding:        内容外保留的像素数（默认15px ≈ 1.3mm @ 300 DPI）
        threshold:      灰度阈值，低于此值视为"有内容"
        min_dark_ratio: 暗像素占整页面积的最低比例。低于此比例视为"近空白页"
                        （如只有页脚/页码 + 扫描杂点），直接返回原图，避免被
                        build_a4_pdf 等比放大后把页脚撑成整页中间巨字。
                        默认 0.005 = 0.5%；正常 A4 试卷一般 10–30%。
                        注意不能用 bbox 面积阈值——一行页脚 + 边缘一列杂点
                        的 bbox 可以占 1/4 页面，但暗像素总数还是很少。
    """
    gray = np.array(img.convert('L'))

    content_mask = gray < threshold
    rows = np.any(content_mask, axis=1)
    cols = np.any(content_mask, axis=0)

    if not rows.any() or not cols.any():
        return img  # 纯空白页不做裁剪

    # 近空白页保护：暗像素总量太少 → 不裁，原样保留视觉比例
    page_area = float(img.width) * float(img.height)
    if page_area > 0 and content_mask.sum() < min_dark_ratio * page_area:
        return img

    y_min, y_max = np.where(rows)[0][[0, -1]]
    x_min, x_max = np.where(cols)[0][[0, -1]]

    # 加padding，不越界
    x_min = max(0, x_min - padding)
    y_min = max(0, y_min - padding)
    x_max = min(img.width, x_max + padding)
    y_max = min(img.height, y_max + padding)

    return img.crop((x_min, y_min, x_max, y_max))


def split_page(img: Image.Image, divider_x: int, gap: int = 5) -> Tuple[Image.Image, Image.Image]:
    """沿分割线将A3页面裁切为左右两张A4子图。

    对 divider_x 做边界 clamp，避免上游传入异常值（如 detect_divider 在
    极窄页或全空白页上回退到 0）导致 PIL crop 收到负宽 / 零宽矩形。
    """
    w, h = img.size
    left_right = max(1, min(w - 1, divider_x - gap))
    right_left = max(1, min(w - 1, divider_x + gap))
    # 退化场景：clamp 后左右边界粘连或反转 → 按中点切，保证两张子图都非空
    if left_right >= right_left:
        mid = w // 2
        left_right = max(1, mid - gap)
        right_left = min(w - 1, mid + gap)
        if left_right >= right_left:
            # 极端情况（w 极小）：退化为对半切，不留 gap
            left_right = right_left = mid
    left = img.crop((0, 0, left_right, h))
    right = img.crop((right_left, 0, w, h))
    return left, right


def build_a4_pdf(
    pages: List[Image.Image],
    out_path: str,
    dpi: int = 300,
    margin: float = 30,
    target_print_dpi: int = 200,
    jpeg_quality: int = 80,
) -> str:
    """
    将裁切后的图片序列写入A4尺寸PDF（保留均匀边距，等比缩放）

    嵌入策略：
    - 图片用 JPEG 编码 —— 扫描件是连续色调，JPEG 比 PNG 小 5–10 倍。
    - 嵌入前按 `target_print_dpi` downsample。算法阶段我们以 300 DPI 渲染
      是为了 detect_divider/auto_crop 的精度，但实际打印到 A4 上 200 DPI
      足够清晰（家用激光打印机一般 300–600 DPI、喷墨同等）；不 downsample
      会把源 PDF 自带的低分辨率扫描图无谓"放大"导致输出体积膨胀几倍。

    Args:
        margin:           A4 页边距（pt，默认30 ≈ 10mm）
        target_print_dpi: 嵌入图的实际打印分辨率上限（默认 200，可调高保留更多细节）
        jpeg_quality:     JPEG 编码质量（0–95，默认 80）
    """
    doc = fitz.open()
    try:
        a4_w, a4_h = fitz.paper_size("a4")  # (595, 842) pt

        # 内容区域 = A4 减去边距
        content_w = a4_w - 2 * margin
        content_h = a4_h - 2 * margin

        # 内容区按 target_print_dpi 折算到像素：1 pt = 1/72 inch
        max_px_w = content_w / 72 * target_print_dpi
        max_px_h = content_h / 72 * target_print_dpi

        for img in pages:
            iw, ih = img.size
            # 等比缩放，确保图片完全在内容区域内
            scale = min(content_w / iw, content_h / ih)
            scaled_w = iw * scale
            scaled_h = ih * scale

            # 按 target_print_dpi downsample（如果原图分辨率比目标高）
            ds_scale = min(1.0, max_px_w / iw, max_px_h / ih)
            if ds_scale < 1.0:
                new_size = (max(1, int(iw * ds_scale)), max(1, int(ih * ds_scale)))
                img_to_embed = img.resize(new_size, Image.LANCZOS)
            else:
                img_to_embed = img

            page = doc.new_page(width=a4_w, height=a4_h)
            x0 = (a4_w - scaled_w) / 2
            y0 = (a4_h - scaled_h) / 2

            rect = fitz.Rect(x0, y0, x0 + scaled_w, y0 + scaled_h)
            img_bytes = _pil_to_jpeg_bytes(img_to_embed, quality=jpeg_quality)
            page.insert_image(rect, stream=img_bytes)

        doc.save(out_path, deflate=True, garbage=4)
        return out_path
    finally:
        doc.close()


def process_pdf(
    pdf_path: str,
    out_dir: str,
    dpi: int = 300,
    divider_override: Optional[List[Optional[int]]] = None,
    progress_cb=None,
) -> Tuple[str, List[Tuple[Image.Image, Image.Image]]]:
    """端到端处理：加载PDF → 检测分割线 → 裁切 → 输出A4 PDF。

    Args:
        divider_override: 按页提供的分割线 X 坐标列表（与 pages 对齐）。
            列表中某项为 None → 对该页走自动检测；
            传整个参数为 None → 全部自动检测；
            列表长度短于页数 → 缺失页自动检测。
            （历史 API 接受单个 int，仍兼容：自动包装成长度 1 的列表。）
    """
    if progress_cb:
        progress_cb("正在加载PDF...", 10)

    pages = load_pdf_pages(pdf_path, dpi=dpi)
    total = len(pages)

    # 兼容旧调用：单值 → 包成单元素列表（仅作用于第 1 页，其余自动检测）
    if isinstance(divider_override, int):
        divider_override = [divider_override]

    split_pages: List[Image.Image] = []
    preview_pairs: List[Tuple[Image.Image, Image.Image]] = []

    for i, page_img in enumerate(pages):
        if progress_cb:
            pct = 10 + int(60 * (i + 0.5) / total)
            progress_cb(f"正在处理第 {i+1}/{total} 页...", pct)

        override = (
            divider_override[i]
            if divider_override is not None and i < len(divider_override)
            else None
        )
        div_x = override if override is not None else detect_divider(page_img)

        left, right = split_page(page_img, div_x)
        # 自动裁掉左右两侧的空白区域
        left = auto_crop(left)
        right = auto_crop(right)
        preview_pairs.append((left, right))
        split_pages.append(left)
        split_pages.append(right)

    if progress_cb:
        progress_cb("正在生成A4 PDF...", 80)

    basename = os.path.splitext(os.path.basename(pdf_path))[0]
    out_path = os.path.join(out_dir, f"{basename}_A4.pdf")
    build_a4_pdf(split_pages, out_path, dpi=dpi)

    if progress_cb:
        progress_cb("转换完成！", 100)

    return out_path, preview_pairs


def _pil_to_png_bytes(img: Image.Image) -> bytes:
    """将PIL Image转为PNG内存字节（无损；体积大，仅在需要精确还原时使用）"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pil_to_jpeg_bytes(img: Image.Image, quality: int = 85) -> bytes:
    """将PIL Image转为JPEG内存字节。

    扫描件首选：体积比PNG小5–10倍，肉眼无差。JPEG 不支持透明，
    遇到带 alpha 的图先按白底合成。
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        # 透明背景合成白底，避免 JPEG 抛 "cannot write mode RGBA" 错误
        bg = Image.new("RGB", img.size, "white")
        img_rgba = img.convert("RGBA")
        bg.paste(img_rgba, mask=img_rgba.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
