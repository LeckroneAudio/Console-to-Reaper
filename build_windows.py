"""
Build script for Console to Reaper Converter — Windows

Produces a single-file "Console to Reaper.exe" via PyInstaller. Run on
Windows (not macOS — PyInstaller builds for the OS it runs on):

    python build_windows.py

Requires Python 3 from python.org (check "Add python.exe to PATH" during
setup). Installs PyInstaller, pystray, and Pillow automatically if missing.
"""
import subprocess
import sys

REQUIRED = {
    'PyInstaller': 'pyinstaller',
    'pystray': 'pystray',
    'PIL': 'Pillow',
}


def ensure_dependencies():
    for import_name, pip_name in REQUIRED.items():
        try:
            __import__(import_name)
        except ImportError:
            print(f"Installing {pip_name}...")
            subprocess.run([sys.executable, '-m', 'pip', 'install', pip_name], check=True)


def main():
    ensure_dependencies()
    import PyInstaller.__main__

    PyInstaller.__main__.run([
        'console_to_reaper.py',
        '--name=Console to Reaper',
        '--onefile',
        '--windowed',
        '--noconfirm',
        # pystray/Pillow pick their backend at runtime (platform checks,
        # dynamic imports) which PyInstaller's static analysis can miss —
        # collect everything so the tray icon doesn't silently fail to load.
        '--collect-all=pystray',
        '--collect-all=PIL',
    ])

    print('\nBuilt: dist/Console to Reaper.exe')


if __name__ == '__main__':
    main()
