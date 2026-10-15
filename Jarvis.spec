# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

a = Analysis(
    ['jarvis.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('jarvis_ui.py', '.'),
        ('assets', 'assets'),
        ('voice_profile.wav', '.'),
        ('.env.example', '.'),
        ('enroll_voice.py', '.'),
    ],
    hiddenimports=[
        'jarvis_ui',
        'speech_recognition',
        'edge_tts',
        'pycaw',
        'pycaw.pycaw',
        'comtypes',
        'comtypes.client',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'sounddevice',
        'numpy',
        'dotenv',
        'websockets',
        'pyautogui',
        'pyperclip',
    ] + collect_submodules('sounddevice')
      + collect_submodules('comtypes')
      + collect_submodules('speech_recognition'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Jarvis',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
