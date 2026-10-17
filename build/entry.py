"""PyInstaller / zipapp 的统一入口。"""
import sys

from ooxml_doctor.cli import main

if __name__ == "__main__":
    sys.exit(main())
