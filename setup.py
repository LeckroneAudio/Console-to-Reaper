"""
Setup script for Console to Reaper Converter
"""
from setuptools import setup

APP = ['console_to_reaper.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': False,
    'packages': ['rumps'],
    # Not used by the app; Tcl/Tk 9 frameworks also break py2app's ad-hoc signing
    'excludes': ['tkinter', 'Tkinter', '_tkinter'],
    'plist': {
        'CFBundleName': 'Console to Reaper',
        'CFBundleDisplayName': 'Console to Reaper',
        'CFBundleIdentifier': 'com.leckroneaudio.consoletoreaper',
        'CFBundleVersion': '4.2.0',
        'CFBundleShortVersionString': '4.2.0',
        'LSUIElement': False,  # Show in Dock
        'NSHighResolutionCapable': True,
        'NSAppSleepDisabled': True,  # no App Nap — server must stay responsive
    },
}

setup(
    name='Console to Reaper',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
