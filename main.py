"""
A3试卷转A4 — 图形界面
双击即用，支持拖拽文件、预览分割线、手动微调
"""

import sys
import os
import faulthandler
import traceback
from typing import Optional, List, Tuple

# 设置 Qt 插件路径（PyInstaller 打包后需要）
if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
    # macOS 打包后：exe 在 Contents/MacOS/，plugins 在 Contents/Frameworks/PyQt5/Qt5/plugins/
    base_path = os.path.dirname(os.path.dirname(sys.executable))
    qt_plugins = os.path.join(base_path, 'Frameworks', 'PyQt5', 'Qt5', 'plugins')
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.join(qt_plugins, 'platforms')

# 崩溃/异常日志：所有未捕获异常和 C 级 fault 都会被记录到这里，
# 方便 .app 打包后没有 stderr 时排查问题。
_LOG_DIR = os.path.expanduser('~/Library/Logs/A3转A4试卷转换')
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _CRASH_LOG = os.path.join(_LOG_DIR, 'crash.log')
    _fault_fp = open(_CRASH_LOG, 'a', buffering=1)
    faulthandler.enable(file=_fault_fp)
except Exception:
    _CRASH_LOG = None


def _log_exception(exc_type, exc_value, exc_tb):
    """全局未捕获异常 → 日志文件 + 控制台"""
    tb = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    sys.__stderr__.write(tb)
    if _CRASH_LOG:
        try:
            with open(_CRASH_LOG, 'a') as f:
                f.write('\n=== uncaught exception ===\n')
                f.write(tb)
        except Exception:
            pass


sys.excepthook = _log_exception

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QFileDialog, QMessageBox,
    QSlider, QSpinBox, QGroupBox, QGraphicsScene, QGraphicsView,
    QGraphicsLineItem, QGraphicsPixmapItem,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import (
    QPixmap, QPen, QColor, QDragEnterEvent, QDropEvent, QFont, QPainter, QImage,
)

from splitter_core import process_pdf, detect_divider, load_pdf_pages, split_page
from PIL import Image


# ─── 后台转换线程 ───────────────────────────────────────────────

class ConvertWorker(QThread):
    """在后台线程执行PDF转换，避免阻塞GUI"""
    progress = pyqtSignal(str, int)   # (消息, 进度百分比)
    done = pyqtSignal(str)            # (输出文件路径) — 不能叫 finished，会遮蔽 QThread 内置信号
    error = pyqtSignal(str)           # (错误信息，含 traceback)

    def __init__(
        self,
        pdf_path: str,
        out_dir: str,
        divider_override: Optional[List[Optional[int]]] = None,
    ):
        super().__init__()
        self.pdf_path = pdf_path
        self.out_dir = out_dir
        self.divider_override = divider_override

    def run(self):
        try:
            out_path, _ = process_pdf(
                self.pdf_path, self.out_dir,
                divider_override=self.divider_override,
                progress_cb=lambda msg, pct: self.progress.emit(msg, pct),
            )
            self.done.emit(out_path)
        except Exception:
            # 完整 traceback 透传到 UI，便于排查（仅 str(e) 时用户无法定位）
            self.error.emit(traceback.format_exc())


class PreviewWorker(QThread):
    """在后台线程渲染PDF并检测分割线，避免主线程阻塞导致窗口卡死。

    注意：PIL Image 跨线程使用在某些环境下会触发底层 abort（PIL 内部
    thread-local 状态 + libjpeg/libpng 不可重入）。所以这里 worker 完成后
    把图片**序列化成纯字节**传给主线程，主线程再重建 PIL Image。
    """
    loaded = pyqtSignal(list, list)   # (page_data: List[(bytes, w, h)], dividers: List[int])
    error = pyqtSignal(str)

    def __init__(self, pdf_path: str, dpi: int):
        super().__init__()
        self.pdf_path = pdf_path
        self.dpi = dpi

    def run(self):
        try:
            pages = load_pdf_pages(self.pdf_path, dpi=self.dpi)
            dividers = [detect_divider(p) for p in pages]
            # 在 worker 线程里 detect 完成后，把 PIL Image 转成 (bytes, w, h)
            # 这样跨线程传输的只是 bytes 和 int，主线程拿到后再 Image.frombytes 重建，
            # 避开 PIL Image 对象跨线程访问带来的 C 库不可重入问题。
            page_data: List[Tuple[bytes, int, int]] = []
            for p in pages:
                if p.mode != 'RGB':
                    p = p.convert('RGB')
                page_data.append((p.tobytes('raw', 'RGB'), p.width, p.height))
            self.loaded.emit(page_data, dividers)
        except Exception:
            self.error.emit(traceback.format_exc())


# ─── 预览窗口（显示分割线位置） ──────────────────────────────────

class PreviewWindow(QGraphicsView):
    """可缩放预览A3页面，红色竖线标记分割位置"""

    # 缩放上下限，相对 fit_view 后的初始缩放
    MIN_ZOOM = 0.2
    MAX_ZOOM = 20.0
    # 初始缩放：fit-to-window 之后再放大此倍数，让用户一打开就能看清字。
    # 1.0 = 整张 A3 刚好填满窗口（字偏小），1.6 是一个折中。
    DEFAULT_ZOOM = 1.6

    def __init__(self):
        super().__init__()
        self._scene = QGraphicsScene()
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._divider_line: Optional[QGraphicsLineItem] = None
        self._divider_x: int = 0
        self._img_w: int = 0
        self._base_scale: float = 1.0   # fit_view 后的缩放，作为 zoom=1.0 基准
        self._zoom: float = 1.0
        # 缩放以鼠标位置为锚点
        self.setTransformationAnchor(self.AnchorUnderMouse)
        self.setResizeAnchor(self.AnchorUnderMouse)
        self.setDragMode(self.ScrollHandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def load_image(self, img: Image.Image, divider_x: int):
        """加载图片并绘制分割线"""
        self._scene.clear()

        # 转换为RGB模式（确保兼容）
        if img.mode != 'RGB':
            img = img.convert('RGB')

        # 将PIL Image转换为QImage；必须 .copy() 强制深拷贝，否则 QImage 内部
        # 引用的是局部 img_bytes，函数返回后该 bytes 被 GC → 野指针 → 偶发
        # 显示乱码 / 撕裂 / crash（PyQt 经典坑）
        img_bytes = img.tobytes('raw', 'RGB')
        qimg = QImage(img_bytes, img.width, img.height, img.width * 3, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg.copy())

        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._img_w = pixmap.width()
        self._divider_x = divider_x

        # 红色分割线
        pen = QPen(QColor(220, 40, 40), 3)
        self._divider_line = self._scene.addLine(
            divider_x, 0, divider_x, pixmap.height(), pen
        )
        self.fit_view()

    def update_divider(self, x: int):
        """更新分割线位置"""
        self._divider_x = x
        if self._divider_line:
            self._divider_line.setLine(x, 0, x, self._pixmap_item.pixmap().height())

    def fit_view(self):
        """初始视图 = fit-to-window 之后再放大 DEFAULT_ZOOM 倍，字更清楚。

        想看完整页面，反向滚动一下即可；MIN_ZOOM=0.2 留足余地。
        """
        if not self._pixmap_item:
            return
        self.resetTransform()
        super().fitInView(self._pixmap_item.boundingRect(), Qt.KeepAspectRatio)
        # 记录 fit 时的缩放因子作为 base，后续 zoom 全部相对它
        self._base_scale = self.transform().m11()
        # 应用初始放大
        self.scale(self.DEFAULT_ZOOM, self.DEFAULT_ZOOM)
        self._zoom = self.DEFAULT_ZOOM

    def wheelEvent(self, event):
        """平滑缩放：鼠标滚轮 / 触控板都按 angleDelta 等比例缩放。

        - 鼠标滚轮一格 angleDelta ≈ 120 → 一次 1.15×（明显缩放）
        - 触控板两指滑动 angleDelta 通常 1–10 → 一次 ≈ 1.001–1.013×（细滑）
        - 上下双向都钳到 [MIN_ZOOM, MAX_ZOOM]，到底之后反向滚动可立即恢复，
          不会"卡死"在某个缩放档位。
        """
        angle = event.angleDelta().y()
        if angle == 0:
            event.ignore()
            return
        # 每 120 个单位缩放 1.15 倍；小增量按指数平滑插值
        factor = 1.15 ** (angle / 120.0)
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom * factor))
        if new_zoom == self._zoom:
            event.accept()
            return
        applied = new_zoom / self._zoom
        self.scale(applied, applied)
        self._zoom = new_zoom
        event.accept()

    def mouseDoubleClickEvent(self, event):
        """双击恢复初始 fit_view，作为缩到看不见时的保险出口"""
        self.fit_view()
        event.accept()


# ─── 主窗口 ──────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("A3试卷转A4转换工具")
        self.setMinimumSize(900, 700)
        self.resize(1100, 900)  # 默认开大一点，让预览区有足够高度显示清晰
        self.setStyleSheet(STYLESHEET)

        self.pdf_path: Optional[str] = None
        self.out_dir: Optional[str] = None
        self.divider_x: Optional[int] = None
        self._original_pages: List[Image.Image] = []
        self._auto_dividers: List[int] = []
        self._preview_dpi = 150
        self.current_preview_idx = 0
        self.worker: Optional[ConvertWorker] = None
        self.preview_worker: Optional[PreviewWorker] = None
        # 所有活着的 worker 强引用，由 QThread.finished 触发清理。
        # 防止 Python GC 在 worker run() 真正返回前提前回收 → SIGABRT。
        self._live_workers: List[QThread] = []

        self._build_ui()

    # ─── UI 构建 ────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 12, 16, 12)

        # ── 标题 ──
        title = QLabel("A3 试卷 → A4 格式")
        title.setFont(QFont("PingFang SC", 20, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("将左右排版的A3扫描试卷，自动拆分为单页A4，方便家用打印机打印")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #888; font-size: 13px;")
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        # ── 文件选择区 ──
        file_group = QGroupBox("  文件选择")
        file_layout = QVBoxLayout(file_group)

        self.file_label = QLabel("拖拽PDF文件到这里，或点击下方按钮选择")
        self.file_label.setAlignment(Qt.AlignCenter)
        self.file_label.setMinimumHeight(44)
        self.file_label.setStyleSheet(
            "background:#f5f7fa; border:2px dashed #ccc; border-radius:8px;"
            "font-size:14px; color:#666;"
        )
        self.file_label.setAcceptDrops(True)
        # 拖拽事件绑定
        self.file_label.dragEnterEvent = self._on_drag_enter
        self.file_label.dropEvent = self._on_drop
        file_layout.addWidget(self.file_label)

        btn_row = QHBoxLayout()
        self.btn_select = QPushButton("选择PDF文件")
        self.btn_select.clicked.connect(self._select_pdf)
        btn_row.addWidget(self.btn_select)

        self.btn_outdir = QPushButton("选择输出目录")
        self.btn_outdir.clicked.connect(self._select_outdir)
        btn_row.addWidget(self.btn_outdir)
        file_layout.addLayout(btn_row)

        self.outdir_label = QLabel("输出目录：未选择")
        self.outdir_label.setStyleSheet("color:#888; font-size:12px;")
        file_layout.addWidget(self.outdir_label)

        layout.addWidget(file_group)

        # ── 分割线调整区 ──
        adj_group = QGroupBox("  分割线调整")
        adj_layout = QVBoxLayout(adj_group)

        preview_btn_row = QHBoxLayout()
        self.btn_preview = QPushButton("预览分割线")
        self.btn_preview.clicked.connect(self._show_preview)
        self.btn_preview.setEnabled(False)
        preview_btn_row.addWidget(self.btn_preview)

        self.btn_prev_page = QPushButton("◀ 上一页")
        self.btn_prev_page.clicked.connect(lambda: self._nav_preview(-1))
        self.btn_prev_page.setEnabled(False)
        preview_btn_row.addWidget(self.btn_prev_page)

        self.btn_next_page = QPushButton("下一页 ▶")
        self.btn_next_page.clicked.connect(lambda: self._nav_preview(1))
        self.btn_next_page.setEnabled(False)
        preview_btn_row.addWidget(self.btn_next_page)

        self.btn_reset_view = QPushButton("适应窗口")
        self.btn_reset_view.setToolTip("缩放迷路时点这里，恢复初始视图（也可双击预览区）")
        self.btn_reset_view.clicked.connect(lambda: self.preview.fit_view())
        self.btn_reset_view.setEnabled(False)
        preview_btn_row.addWidget(self.btn_reset_view)

        self.page_info = QLabel("")
        self.page_info.setAlignment(Qt.AlignCenter)
        self.page_info.setStyleSheet("color:#888;")
        preview_btn_row.addWidget(self.page_info)
        adj_layout.addLayout(preview_btn_row)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("分割线位置："))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(1000)
        self.slider.setValue(500)
        self.slider.valueChanged.connect(self._on_slider_change)
        self.slider.setEnabled(False)
        slider_row.addWidget(self.slider)

        self.spinbox = QSpinBox()
        self.spinbox.setRange(0, 10000)
        self.spinbox.setValue(500)
        self.spinbox.setSingleStep(5)
        self.spinbox.valueChanged.connect(self._on_spinbox_change)
        self.spinbox.setEnabled(False)
        self.spinbox.setMaximumWidth(80)
        slider_row.addWidget(QLabel("像素:"))
        slider_row.addWidget(self.spinbox)
        adj_layout.addLayout(slider_row)

        self.divider_info = QLabel("提示：自动检测后将在下方显示分割线位置，可拖动滑块微调")
        self.divider_info.setStyleSheet("color:#888; font-size:12px;")
        adj_layout.addWidget(self.divider_info)

        layout.addWidget(adj_group)

        # ── 预览窗口（占据所有剩余空间） ──
        self.preview = PreviewWindow()
        self.preview.setMinimumHeight(480)
        self.preview.setVisible(False)
        layout.addWidget(self.preview, 1)

        # ── 转换按钮 + 进度条 ──
        bottom_layout = QHBoxLayout()
        self.btn_convert = QPushButton("开始转换")
        self.btn_convert.setFont(QFont("PingFang SC", 16, QFont.Bold))
        self.btn_convert.setMinimumHeight(50)
        self.btn_convert.clicked.connect(self._start_convert)
        self.btn_convert.setEnabled(False)
        bottom_layout.addWidget(self.btn_convert)
        layout.addLayout(bottom_layout)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color:#555; font-size:13px;")
        layout.addWidget(self.status_label)

    # ─── 事件处理 ────────────────────────────────────────────────

    def _on_drag_enter(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _on_drop(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path.lower().endswith(".pdf"):
                self._set_pdf(path)

    def _select_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择A3 PDF文件", "", "PDF Files (*.pdf)"
        )
        if path:
            self._set_pdf(path)

    def _select_outdir(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.out_dir = path
            self.outdir_label.setText(f"输出目录：{path}")
            self._update_convert_btn()

    def _set_pdf(self, path: str):
        self.pdf_path = path
        fname = os.path.basename(path)
        self.file_label.setText(f"已选择：{fname}")
        self.file_label.setStyleSheet(
            "background:#e8f5e9; border:2px solid #4caf50; border-radius:8px;"
            "font-size:14px; color:#2e7d32;"
        )
        # 默认输出到同目录
        if not self.out_dir:
            self.out_dir = os.path.dirname(path)
            self.outdir_label.setText(f"输出目录：{self.out_dir}")
        self.btn_preview.setEnabled(True)
        self._update_convert_btn()

    def _update_convert_btn(self):
        self.btn_convert.setEnabled(bool(self.pdf_path and self.out_dir))

    # ─── 预览 ────────────────────────────────────────────────────

    def _show_preview(self):
        """启动后台 worker 加载预览，主线程立即返回，避免窗口卡死。"""
        if not self.pdf_path:
            return
        # 已有 worker 在跑就忽略本次点击（避免重复启动）
        if self.preview_worker is not None and self.preview_worker.isRunning():
            return

        self.status_label.setText("正在加载预览...")
        self.btn_preview.setEnabled(False)
        self.btn_convert.setEnabled(False)

        # 旧引用置空即可，让 GC 回收。不主动 close PIL Image：pdf2image
        # 返回的 Image 内部还指着临时文件，激进 close 可能在再次点预览时
        # 触发底层 C 库崩溃。
        self._original_pages = []
        self._auto_dividers = []

        worker = PreviewWorker(self.pdf_path, self._preview_dpi)
        # 关键：把 worker 同时存进 _live_workers，避免 Python 引用提前消失。
        # QThread 必须等 C++ run() 完全返回（finished 信号触发）后才能销毁，
        # 否则会触发 "QThread: Destroyed while thread is still running" → SIGABRT。
        # 第二次点预览时如果只覆盖 self.preview_worker，旧的可能正处在
        # emit 完自定义信号、还没等到内置 finished 的间隙，Python GC 会
        # 立刻析构它 → crash。
        self._live_workers.append(worker)
        worker.loaded.connect(self._on_preview_loaded)
        worker.error.connect(self._on_preview_error)
        # 真 finished（QThread 内置无参信号，run() 完整返回后才触发）→
        # 安全 deleteLater + 从 _live_workers 移除
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda w=worker: self._live_workers.remove(w) if w in self._live_workers else None)
        self.preview_worker = worker
        worker.start()

    def _on_preview_loaded(self, page_data: List[Tuple[bytes, int, int]], auto_dividers: List[int]):
        """预览 worker 完成回调（已在主线程）。

        page_data: List[(rgb_bytes, width, height)]，在主线程里重建 PIL Image。
        """
        self.preview_worker = None
        if not page_data:
            self.status_label.setText("预览失败：PDF 中没有页面")
            self.btn_preview.setEnabled(True)
            self._update_convert_btn()
            return

        # 在主线程里重建 PIL Image，避免子线程对象被主线程 Qt 流程访问
        self._original_pages = [
            Image.frombytes('RGB', (w, h), b) for (b, w, h) in page_data
        ]
        self._auto_dividers = list(auto_dividers)
        self.current_preview_idx = 0
        self.divider_x = self._auto_dividers[0]

        # 设置滑块范围（基于第一页图片宽度）
        first_w = self._original_pages[0].size[0]
        self.slider.setMaximum(first_w)
        self.slider.blockSignals(True)
        self.slider.setValue(self.divider_x)
        self.slider.blockSignals(False)
        self.spinbox.setMaximum(first_w)
        self.spinbox.blockSignals(True)
        self.spinbox.setValue(self.divider_x)
        self.spinbox.blockSignals(False)
        self.slider.setEnabled(True)
        self.spinbox.setEnabled(True)

        self._update_preview_display()
        self.preview.setVisible(True)
        self.btn_prev_page.setEnabled(len(self._original_pages) > 1)
        self.btn_next_page.setEnabled(len(self._original_pages) > 1)
        self.btn_reset_view.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self._update_convert_btn()

        self.status_label.setText("预览已加载，可拖动滑块调整分割线位置")
        self.divider_info.setText(
            f"自动检测分割线位置：{self.divider_x}px（共{len(self._original_pages)}页）"
        )

    def _on_preview_error(self, err: str):
        """预览 worker 错误回调"""
        self.preview_worker = None
        self.status_label.setText("")
        self.btn_preview.setEnabled(True)
        self._update_convert_btn()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("错误")
        box.setText("加载预览失败")
        box.setDetailedText(err)
        box.exec_()

    def _update_preview_display(self):
        """更新当前预览页：显示原始A3页面 + 红色分割线"""
        if not self._original_pages:
            return
        page_img = self._original_pages[self.current_preview_idx]
        self.preview.load_image(page_img, self.divider_x or 0)
        self.page_info.setText(
            f"第 {self.current_preview_idx + 1}/{len(self._original_pages)} 页"
        )

    def _nav_preview(self, delta: int):
        self.current_preview_idx = max(
            0, min(len(self._original_pages) - 1, self.current_preview_idx + delta)
        )
        # 翻页时切换到该页的（自动检测 / 已手动微调后的）分割线位置
        if self._auto_dividers and self.current_preview_idx < len(self._auto_dividers):
            self.divider_x = self._auto_dividers[self.current_preview_idx]
            self.slider.blockSignals(True)
            self.slider.setValue(self.divider_x)
            self.slider.blockSignals(False)
            self.spinbox.blockSignals(True)
            self.spinbox.setValue(self.divider_x)
            self.spinbox.blockSignals(False)
        self._update_preview_display()

    def _on_slider_change(self, val: int):
        self.divider_x = val
        # 把当前页的分割线落到 _auto_dividers，保证转换时逐页生效（Fix #1）
        if self._auto_dividers and self.current_preview_idx < len(self._auto_dividers):
            self._auto_dividers[self.current_preview_idx] = val
        self.spinbox.blockSignals(True)
        self.spinbox.setValue(val)
        self.spinbox.blockSignals(False)
        self.divider_info.setText(f"分割线位置：{val}px（手动调整）")
        # 实时更新预览中的分割线位置
        if self._original_pages:
            self.preview.update_divider(val)

    def _on_spinbox_change(self, val: int):
        self.divider_x = val
        if self._auto_dividers and self.current_preview_idx < len(self._auto_dividers):
            self._auto_dividers[self.current_preview_idx] = val
        self.slider.blockSignals(True)
        self.slider.setValue(val)
        self.slider.blockSignals(False)
        self.divider_info.setText(f"分割线位置：{val}px（手动调整）")
        # 实时更新预览中的分割线位置
        if self._original_pages:
            self.preview.update_divider(val)

    # ─── 转换 ────────────────────────────────────────────────────

    def _start_convert(self):
        if not self.pdf_path or not self.out_dir:
            return
        if self.worker is not None and self.worker.isRunning():
            return  # 防止重复点击

        self.btn_convert.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)

        # 将每页 divider 从预览 DPI 换算到转换 DPI（300），逐页传递（Fix #1）
        overrides: Optional[List[Optional[int]]] = None
        if self._auto_dividers and self._preview_dpi:
            scale = 300 / self._preview_dpi
            overrides = [int(d * scale) for d in self._auto_dividers]

        self.worker = ConvertWorker(
            self.pdf_path, self.out_dir,
            divider_override=overrides,
        )
        self._live_workers.append(self.worker)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_convert_done)
        self.worker.error.connect(self._on_convert_error)
        # 真 finished → 安全 deleteLater + 从 _live_workers 移除
        worker = self.worker
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda w=worker: self._live_workers.remove(w) if w in self._live_workers else None)
        self.worker.start()

    def _on_progress(self, msg: str, pct: int):
        self.status_label.setText(msg)
        self.progress.setValue(pct)

    def _on_convert_done(self, out_path: str):
        self.status_label.setText("转换完成！")
        self.progress.setValue(100)
        self.btn_convert.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self._cleanup_worker()

        QMessageBox.information(
            self, "转换完成",
            f"已生成 A4 PDF 文件：\n{out_path}\n\n可以用打印机直接打印了！"
        )

    def _on_convert_error(self, err: str):
        self.status_label.setText("转换失败")
        self.progress.setVisible(False)
        self.btn_convert.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self._cleanup_worker()

        # traceback 放进 detailedText，主消息保持简洁
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("转换失败")
        box.setText("处理过程中出错，请查看详细信息排查。")
        box.setDetailedText(err)
        box.exec_()

    def _cleanup_worker(self):
        """转换 worker 完成后让出引用：deleteLater 已通过 finished 信号挂上"""
        self.worker = None

    def closeEvent(self, event):
        """关闭主窗口时，若 worker 还在跑：提示并等待，避免 QThread 被销毁警告/崩溃"""
        running = (
            (self.worker is not None and self.worker.isRunning())
            or (self.preview_worker is not None and self.preview_worker.isRunning())
        )
        if running:
            reply = QMessageBox.question(
                self, "任务进行中",
                "转换/预览仍在进行中，确定退出吗？\n（点'是'会等待最多 5 秒后强制退出）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            if self.worker is not None:
                self.worker.wait(5000)
            if self.preview_worker is not None:
                self.preview_worker.wait(5000)
        event.accept()


# ─── 样式表 ──────────────────────────────────────────────────────

STYLESHEET = """
MainWindow {
    background-color: #ffffff;
}
QGroupBox {
    font-size: 14px;
    font-weight: bold;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    margin-top: 8px;
    padding-top: 16px;
    background: #fafafa;
}
QGroupBox::title {
    color: #333;
}
QPushButton {
    background: #1976d2;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 13px;
}
QPushButton:hover {
    background: #1565c0;
}
QPushButton:disabled {
    background: #bdbdbd;
}
QProgressBar {
    border: 1px solid #ddd;
    border-radius: 6px;
    text-align: center;
    height: 20px;
}
QProgressBar::chunk {
    background: #4caf50;
    border-radius: 5px;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #e0e0e0;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #1976d2;
    width: 18px;
    margin: -6px 0;
    border-radius: 9px;
}
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
