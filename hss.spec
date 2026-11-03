# PyInstaller spec: one-folder, windowed (no console), nothing secret or user-specific bundled.
# Build:  .venv\Scripts\pyinstaller hss.spec --noconfirm --distpath dist --workpath build
# Bundled data = config/*.example.json only (templates with no real data). NOT bundled: .env, config.json,
# test_identities.json, firmware, tools, docs, tests, evidence, logs, face templates.
block_cipher = None

EXCLUDES = [
    "tkinter", "unittest", "pydoc", "test", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtQuick",
    "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtSql", "PySide6.QtNetwork", "PySide6.QtBluetooth", "PySide6.QtSensors",
    "PySide6.QtPositioning", "PySide6.QtOpenGL", "PySide6.QtSvg", "PySide6.QtTest", "PySide6.QtDesigner",
]

a = Analysis(
    ["run_hss.py"],
    pathex=["."],
    binaries=[],
    datas=[("config/config.example.json", "config"), ("config/test_identities.example.json", "config"),
           ("models/face_detection_yunet_2023mar.onnx", "models"), ("models/face_recognition_sface_2021dec.onnx", "models")],
    hiddenimports=["PySide6.QtPdf", "PySide6.QtPdfWidgets", "cv2", "numpy"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HSS",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # windowed: no console window
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="HSS")
