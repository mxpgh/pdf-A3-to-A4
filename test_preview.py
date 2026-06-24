"""测试预览功能的完整流程"""
import sys
import os

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

app = QApplication(sys.argv)

# 导入主窗口
sys.path.insert(0, os.path.dirname(__file__))
from main import MainWindow

win = MainWindow()
win.show()

def run_tests():
    print("=== 窗口已创建 ===")
    print(f"窗口大小: {win.width()}x{win.height()}")
    print(f"预览窗口大小: {win.preview.width()}x{win.preview.height()}")
    print(f"预览窗口可见: {win.preview.isVisible()}")

    # Step 1: 设置PDF
    pdf_path = os.path.join(os.path.dirname(__file__), 'test_a3_exam.pdf')
    print(f"\n=== Step 1: 设置PDF ===")
    win._set_pdf(pdf_path)
    print(f"PDF已设置: {win.pdf_path}")
    print(f"预览按钮启用: {win.btn_preview.isEnabled()}")

    # Step 2: 触发预览
    print(f"\n=== Step 2: 触发预览 ===")
    try:
        win._show_preview()
        print("预览加载成功!")
        print(f"预览窗口可见: {win.preview.isVisible()}")
        print(f"预览窗口大小: {win.preview.width()}x{win.preview.height()}")
        print(f"_original_pages 数量: {len(win._original_pages)}")
        print(f"divider_x: {win.divider_x}")
        print(f"滑块启用: {win.slider.isEnabled()}")
        print(f"滑块值: {win.slider.value()}")
        print(f"滑块最大值: {win.slider.maximum()}")

        # 检查预览窗口内部状态
        print(f"\n预览窗口内部状态:")
        print(f"  _pixmap_item: {win.preview._pixmap_item is not None}")
        print(f"  _divider_line: {win.preview._divider_line is not None}")
        if win.preview._pixmap_item:
            pm = win.preview._pixmap_item.pixmap()
            print(f"  pixmap 尺寸: {pm.width()}x{pm.height()}")
            print(f"  pixmap 是否为空: {pm.isNull()}")
        if win.preview._divider_line:
            line = win.preview._divider_line.line()
            print(f"  分割线位置: x1={line.x1()}, x2={line.x2()}")

        # 检查 scene
        scene = win.preview._scene
        print(f"  scene items 数量: {len(scene.items())}")

    except Exception as e:
        import traceback
        print(f"预览加载失败: {e}")
        traceback.print_exc()

    # Step 3: 测试滑块更新
    print(f"\n=== Step 3: 测试滑块更新 ===")
    try:
        new_val = 300
        win.slider.setValue(new_val)
        print(f"滑块设为 {new_val}")
        print(f"divider_x: {win.divider_x}")
        if win.preview._divider_line:
            line = win.preview._divider_line.line()
            print(f"红线位置: x1={line.x1()}")
    except Exception as e:
        import traceback
        print(f"滑块更新失败: {e}")
        traceback.print_exc()

    # Step 4: 测试翻页
    print(f"\n=== Step 4: 测试翻页 ===")
    try:
        win._nav_preview(1)
        print(f"翻页后 current_preview_idx: {win.current_preview_idx}")
        print(f"翻页后 divider_x: {win.divider_x}")
    except Exception as e:
        import traceback
        print(f"翻页失败: {e}")
        traceback.print_exc()

    print("\n=== 测试完成 ===")
    app.quit()

# 延迟执行测试，让窗口完全初始化
QTimer.singleShot(500, run_tests)
sys.exit(app.exec_())
