#!/bin/bash
# A3试卷转A4 - PyInstaller 打包脚本（优化体积）
# 输出: dist/A3转A4试卷转换.app （双击即用）
#
# 打包配置全部在 A3转A4试卷转换.spec 中维护，
# 修改尺寸/优化/依赖时请改 spec 而非此脚本。

set -e
cd "$(dirname "$0")"

echo "=== 清理旧构建 ==="
rm -rf build dist

echo "=== PyInstaller 打包 ==="
pyinstaller --noconfirm "A3转A4试卷转换.spec"

echo ""
echo "=== 打包完成 ==="
APP_PATH="dist/A3转A4试卷转换.app"

if [ -d "$APP_PATH" ]; then
    SIZE=$(du -sh "$APP_PATH" | cut -f1)
    echo "输出: $APP_PATH ($SIZE)"
    echo ""
    echo "提示："
    echo "  1. 双击即可运行"
    echo "  2. 首次运行可能被macOS安全拦截，右键→打开即可"
    echo "  3. 如需要pdf2image支持，请确保系统安装了poppler（可选）"
else
    echo "打包失败，请检查错误信息"
    exit 1
fi