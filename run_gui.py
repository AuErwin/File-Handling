"""双击或运行本文件即可启动图形界面（放在 file_organizer 文件夹的上一级）。

Windows 想要不弹黑色命令行窗口：把本文件重命名为 run_gui.pyw 再双击。
"""
import sys

from file_organizer.gui import main

if __name__ == "__main__":
    sys.exit(main())
