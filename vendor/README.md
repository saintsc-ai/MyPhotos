# vendor/

OS-specific external binaries used by the worker:

- `exiftool` — fallback EXIF extractor (handles Pentax MakerNote, HEIC, etc.)
- `ffmpeg`  — video thumbnail extraction

Layout:

```
vendor/
├── linux-x64/
│   ├── exiftool
│   └── ffmpeg
├── linux-arm64/
├── windows-x64/
│   ├── exiftool.exe
│   └── ffmpeg.exe
└── macos-arm64/
```

`app/external.py` auto-selects the directory based on `platform.system()` /
`platform.machine()` and falls back to `$PATH`.

## Download sources

- **ffmpeg static builds**:
  - Linux x64 / arm64: https://johnvansickle.com/ffmpeg/ or https://github.com/BtbN/FFmpeg-Builds/releases
  - Windows x64:       https://www.gyan.dev/ffmpeg/builds/
- **ExifTool**:
  - All platforms:     https://exiftool.org/

Binaries are NOT committed to git. Bootstrap downloads or you place them manually.
