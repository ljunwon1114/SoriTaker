# SoriTaker app icon

`SoriTaker.png` is the generated RGBA source used by the GUI. `SoriTaker.icns`
packages the same artwork at multiple resolutions for the macOS application
bundle. Both are shipped with the source package; installing or updating the
app does not require an image-generation service or an additional dependency.

Created with the built-in image-generation tool. Design prompt: a minimal teal
macOS rounded-square icon with a warm-white folded paper note, five rounded teal
waveform bars inside it, and one short writing line below. No text, microphone,
musical note, device mockup, or extra symbols; transparent canvas outside the tile.

The ICNS asset uses PyInstaller's documented
[macOS bundle icon setting](https://pyinstaller.org/en/stable/spec-files.html#spec-file-options-for-a-macos-bundle).

For development only, the ICNS file can be regenerated with Pillow:

```python
from PIL import Image
with Image.open('assets/SoriTaker.png') as icon:
    icon.save('assets/SoriTaker.icns', format='ICNS')
```
