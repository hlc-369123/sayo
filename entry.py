"""PyInstaller 入口：从项目根导入 app 包，保证相对导入可用。"""
from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
