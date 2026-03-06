pkgname=edge-reader-bin
pkgver=1.0.0
pkgrel=1
pkgdesc="A highly optimized Edge-TTS reading application built with PyQt6"
arch=('x86_64')
url="https://github.com/yourusername/edge-reader"
license=('MIT')
depends=('alsa-lib' 'portaudio') # System-level audio libraries it relies on
source=('edge-reader.desktop' 'edge-tts.png')
md5sums=('SKIP' 'SKIP')

package() {
    # 1. Create the system directories
    mkdir -p "${pkgdir}/usr/bin"
    mkdir -p "${pkgdir}/usr/share/applications"
    mkdir -p "${pkgdir}/usr/share/pixmaps"

    # 2. Install the PyInstaller binary
    install -Dm755 "${srcdir}/../dist/reader_qt_edge" "${pkgdir}/usr/bin/edge-reader"

    # 3. Install the Desktop entry and Icon
    install -Dm644 "${srcdir}/edge-reader.desktop" "${pkgdir}/usr/share/applications/edge-reader.desktop"
    install -Dm644 "${srcdir}/edge-tts.png" "${pkgdir}/usr/share/pixmaps/edge-tts.png"
}