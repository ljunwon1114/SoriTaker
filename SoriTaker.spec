# Builds locally on the user's Mac; this is not a cross-compiled application.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

base = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ['mlx', 'mlx_whisper', 'sherpa_onnx', 'sherpa_onnx_core', 'imageio_ffmpeg', '_sounddevice_data']:
    try:
        data, binary, hidden = collect_all(package)
        datas += data
        binaries += binary
        hiddenimports += hidden
    except ModuleNotFoundError:
        pass
hiddenimports += collect_submodules('tiktoken_ext')
hiddenimports += ['sounddevice', 'soundfile', 'PySide6.QtMultimedia', 'numpy', 'huggingface_hub']
datas += [(str(base / 'THIRD_PARTY_NOTICES.md'), '.')]
datas += [(str(base / 'assets' / 'SoriTaker.png'), 'assets')]

a = Analysis(
    [str(base / 'src' / 'main.py')], pathex=[str(base / 'src')],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=['torch', 'tensorflow', 'matplotlib', 'IPython', 'notebook'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SoriTaker',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, target_arch='arm64', codesign_identity=None,
          entitlements_file=str(base / 'entitlements.plist'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SoriTaker')
# Preserve the bundle identity across the rename for macOS app continuity.
app = BUNDLE(coll, name='SoriTaker.app', bundle_identifier='local.sorinote.desktop',
             icon=str(base / 'assets' / 'SoriTaker.icns'),
             info_plist={
                 'CFBundleDisplayName': 'SoriTaker', 'CFBundleShortVersionString': '0.4.2',
                 'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '14.0',
                 'NSMicrophoneUsageDescription': 'SoriTaker uses your microphone to record audio on this Mac and transcribe it offline.',
                 'NSHumanReadableCopyright': 'SoriTaker. Open-source components retain their respective licenses.',
             })
