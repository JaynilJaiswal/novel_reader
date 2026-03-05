
# Edge-Qt TTS Reader

A highly optimized, zero-latency Text-to-Speech (TTS) reading application built with Python and PyQt6. This application leverages Microsoft's Edge-TTS neural voices to provide incredibly lifelike, natural-sounding audio for long-form reading, completely offline and free of API keys.

## ✨ Features

* **Studio-Quality Neural Voices:** Uses `edge-tts` to access high-quality, natural-sounding voices (e.g., Aria, Guy, Christopher) with perfect punctuation and prosody.
* **Zero-Latency Prefetching:** Utilizes a highly efficient asynchronous Producer-Consumer queue. The app silently downloads and decodes upcoming sentences in the background while you listen, guaranteeing gapless playback.
* **O(1) Playback Tracking:** Optimized memory and UI tracking. Replaces heavy string manipulation and document-tree traversing with O(1) cursor synchronization, resulting in near-zero CPU usage during playback.
* **Balabolka-Style Navigation:** * Strict UI caret tracking prevents you from losing your place.
  * Centered auto-scrolling keeps the active text in the middle of your screen.
  * Instant-interrupt "Next" and "Previous" sentence skipping.
* **Smart ETA Calculation:** Real-time remaining reading time calculation based on pre-calculated word counts and your exact speed multiplier.
* **Customizable UI:** Full control over fonts, sizes, and highlight/completed text colors. Settings are persistently saved to `~/.config/edge-qt/settings.json`.

## ⌨️ Keyboard Shortcuts

| Shortcut | Action | Condition |
| :--- | :--- | :--- |
| `Ctrl + Enter` | Play / Pause | Anytime |
| `Enter` | Play / Pause | While Playing/Paused |
| `Left Arrow` | Skip to Previous Sentence | While Playing/Paused |
| `Right Arrow` | Skip to Next Sentence | While Playing/Paused |

*(Note: When the player is completely stopped, the arrow keys and Enter key return to their normal text-editing behavior).*

## 🛠️ Requirements

* Python 3.8+
* System-level audio libraries (Linux): `alsa-lib`, `portaudio`

**Python Dependencies:**
```bash
pip install PyQt6 edge-tts sounddevice soundfile

```

## 🚀 Running Directly

1. Clone or download this repository.
2. Install the required Python packages.
3. Run the script:

```bash
python reader_qt.py

```

## 🐧 Arch Linux Native Installation

You can package and install this application natively on Arch Linux using the provided `PKGBUILD`. This creates a standalone application with a desktop launcher and icon.

1. Ensure you have PyInstaller installed (`pip install pyinstaller`).
2. Build the standalone binary:

```bash
pyinstaller --onefile --windowed reader_qt.py

```

3. Place your application icon (named `edge-reader.png`), the `edge-reader.desktop` file, and the `PKGBUILD` in the project root.
4. Build and install the package via pacman:

```bash
makepkg -si

```

## ⚙️ Configuration

Application settings are automatically saved upon exiting and restored on the next launch.

* **Config path:** `~/.config/edge-qt/settings.json`
* The app automatically remembers your last used voice, volume, speed multiplier, text payload, and cursor position.

## 📄 License

MIT License
