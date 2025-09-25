import sys
import os
import subprocess
import queue
import sounddevice as sd
import soundfile as sf
import io
import numpy as np
import json # Import the json library
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QTextEdit, QPushButton, QComboBox, QHBoxLayout,
                             QFileDialog, QMessageBox, QLabel, QSlider, QDialog,
                             QFormLayout, QFontComboBox, QSpinBox, QDialogButtonBox,
                             QColorDialog)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat, QFont, QAction, QIcon

# Define the path where Piper voices are stored
VOICE_DIR = os.path.expanduser("~/.local/share/piper-voices")

class SettingsDialog(QDialog):
    def __init__(self, current_settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Appearance Settings")
        self.settings = current_settings.copy()
        layout = QFormLayout(self)
        self.font_combo = QFontComboBox(); self.font_combo.setCurrentFont(QFont(self.settings["font_family"]))
        layout.addRow("Font Family:", self.font_combo)
        self.font_size_spinbox = QSpinBox(); self.font_size_spinbox.setRange(8, 72); self.font_size_spinbox.setValue(self.settings["font_size"])
        layout.addRow("Font Size:", self.font_size_spinbox)
        color_layout = QHBoxLayout()
        self.bg_color_btn = self.create_color_button(self.settings["bg_color"])
        self.text_color_btn = self.create_color_button(self.settings["text_color"])
        self.highlight_color_btn = self.create_color_button(self.settings["highlight_color"])
        color_layout.addWidget(QLabel("Background:")); color_layout.addWidget(self.bg_color_btn)
        color_layout.addWidget(QLabel("Text:")); color_layout.addWidget(self.text_color_btn)
        color_layout.addWidget(QLabel("Highlight:")); color_layout.addWidget(self.highlight_color_btn)
        layout.addRow(color_layout)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept); button_box.rejected.connect(self.reject)
        layout.addRow(button_box)
        self.bg_color_btn.clicked.connect(lambda: self.pick_color(self.bg_color_btn, "bg_color"))
        self.text_color_btn.clicked.connect(lambda: self.pick_color(self.text_color_btn, "text_color"))
        self.highlight_color_btn.clicked.connect(lambda: self.pick_color(self.highlight_color_btn, "highlight_color"))

    def create_color_button(self, color):
        btn = QPushButton(); btn.setFixedSize(24, 24); btn.setStyleSheet(f"background-color: {color}; border: 1px solid grey;")
        return btn

    def pick_color(self, btn, key):
        color = QColorDialog.getColor(QColor(self.settings[key]), self)
        if color.isValid(): self.settings[key] = color.name(); btn.setStyleSheet(f"background-color: {color.name()}; border: 1px solid grey;")

    def accept(self):
        self.settings["font_family"] = self.font_combo.currentFont().family()
        self.settings["font_size"] = self.font_size_spinbox.value()
        super().accept()

    def get_settings(self): return self.settings

# (Worker classes remain unchanged)
class PiperSynthWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    save_data_ready = pyqtSignal(object, int)
    
    def __init__(self, lines, voice_path, audio_queue, speed, for_saving=False):
        super().__init__(); self.lines = lines; self.voice_path = voice_path; self.audio_queue = audio_queue
        self.speed = speed; self.for_saving = for_saving; self._is_running = True
    def run(self):
        try:
            all_audio_chunks = []; final_samplerate = None
            for i, line in enumerate(self.lines):
                if not self._is_running: break
                length_scale = 1.0 / self.speed
                command = ['piper-tts', '--model', self.voice_path, '--length_scale', str(length_scale), '--output_file', '-']
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout_data, stderr_data = process.communicate(input=line.encode('utf-8'))
                if process.returncode != 0: self.error.emit(f"Piper error on line '{line}':\n\n{stderr_data.decode('utf-8')}"); continue
                if stdout_data:
                    data, samplerate = sf.read(io.BytesIO(stdout_data), dtype='float32')
                    if self.for_saving:
                        all_audio_chunks.append(data)
                        if final_samplerate is None: final_samplerate = samplerate
                    else: self.audio_queue.put({'index': i, 'data': data, 'samplerate': samplerate})
            if self.for_saving and all_audio_chunks: self.save_data_ready.emit(np.concatenate(all_audio_chunks), final_samplerate)
        except Exception as e: self.error.emit(f"Synthesis worker error:\n\n{e}")
        finally:
            if not self.for_saving: self.audio_queue.put(None)
            self.finished.emit()
    def stop(self): self._is_running = False

class AudioPlaybackWorker(QObject):
    playback_finished = pyqtSignal()
    highlight_line = pyqtSignal(int)
    
    def __init__(self, audio_queue, volume, line_index_offset):
        super().__init__(); self.audio_queue = audio_queue; self.volume = volume
        self.line_index_offset = line_index_offset; self._is_running = True
    def run(self):
        stream = None
        try:
            while self._is_running:
                item = self.audio_queue.get()
                if item is None: break
                local_line_index = item['index']; audio_data = item['data']; samplerate = item['samplerate']
                if not self._is_running: break
                original_line_index = self.line_index_offset + local_line_index
                self.highlight_line.emit(original_line_index)
                if stream is None or stream.samplerate != samplerate:
                    if stream: stream.close()
                    stream = sd.OutputStream(samplerate=samplerate, channels=1, dtype='float32'); stream.start()
                stream.write(audio_data * self.volume)
        except Exception as e: print(f"Playback error: {e}")
        finally:
            if stream: stream.stop(); stream.close()
            self.playback_finished.emit()
    def stop(self):
        self._is_running = False
        while not self.audio_queue.empty():
            try: self.audio_queue.get_nowait()
            except queue.Empty: continue
        self.audio_queue.put(None)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Piper-Qt TTS")
        self.setGeometry(100, 100, 800, 600)
        self.lines = []
        self.last_highlighted_block = None
        self.playback_state = "stopped"
        self.current_line_index = 0

        self.config_path = os.path.expanduser("~/.config/piper-qt/settings.json")
        # --- NEW: Add voice, speed, and volume to default settings ---
        self.settings = {
            "font_family": "Noto Sans", "font_size": 14,
            "bg_color": "#ffffff", "text_color": "#000000",
            "highlight_color": "#a8d8ff",
            "voice": "",
            "speed": 10,
            "volume": 100
        }
        self.load_settings() # Load saved settings over defaults

        self.setup_actions()
        self.setup_menu()
        self.setup_toolbar()
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Voice:")); self.voice_combo = QComboBox(); self.populate_voices()
        controls_layout.addWidget(self.voice_combo)
        controls_layout.addWidget(QLabel("Speed:")); self.speed_slider = QSlider(Qt.Orientation.Horizontal); self.speed_slider.setRange(5, 40); self.speed_slider.setValue(10)
        controls_layout.addWidget(self.speed_slider)
        self.speed_label = QLabel("1.0x"); controls_layout.addWidget(self.speed_label)
        controls_layout.addWidget(QLabel("Volume:")); self.volume_slider = QSlider(Qt.Orientation.Horizontal); self.volume_slider.setRange(0, 100); self.volume_slider.setValue(100)
        controls_layout.addWidget(self.volume_slider)
        self.volume_label = QLabel("100%"); controls_layout.addWidget(self.volume_label)
        layout.addLayout(controls_layout)
        self.text_edit = QTextEdit(); self.text_edit.setPlaceholderText("Enter text, or open a file from the File menu.")
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.text_edit)
        button_layout = QHBoxLayout()
        self.play_button = QPushButton("▶ Play"); self.save_button = QPushButton("Save to WAV"); self.stop_button = QPushButton("⏹ Stop")
        button_layout.addWidget(self.play_button); button_layout.addWidget(self.save_button); button_layout.addWidget(self.stop_button)
        layout.addLayout(button_layout)
        self.stop_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle_playback)
        self.stop_button.clicked.connect(self.full_stop)
        self.save_button.clicked.connect(self.save_audio)
        self.speed_slider.valueChanged.connect(self.update_speed_label)
        self.volume_slider.valueChanged.connect(self.update_volume_label)
        self.apply_settings()

    # --- NEW: Methods for saving and loading settings ---
    def load_settings(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    loaded_settings = json.load(f)
                    # Update defaults with loaded settings to ensure no missing keys
                    self.settings.update(loaded_settings)
                    print("Settings loaded successfully.")
        except (json.JSONDecodeError, IOError) as e:
            print(f"Could not load settings file: {e}. Using defaults.")

    def save_settings(self):
        try:
            # --- NEW: Update settings from UI before saving ---
            self.settings["voice"] = self.voice_combo.currentText()
            self.settings["speed"] = self.speed_slider.value()
            self.settings["volume"] = self.volume_slider.value()

            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump(self.settings, f, indent=4)
                print("Settings saved.")
        except IOError as e:
            print(f"Could not save settings file: {e}")

    def setup_actions(self):
        self.open_action = QAction(QIcon.fromTheme("document-open"), "&Open Text File...", self)
        self.open_action.triggered.connect(self.open_text_file)
        self.settings_action = QAction(QIcon.fromTheme("preferences-system"), "&Settings...", self)
        self.settings_action.triggered.connect(self.open_settings_dialog)
    def setup_menu(self):
        menu = self.menuBar(); file_menu = menu.addMenu("&File"); file_menu.addAction(self.open_action)
        edit_menu = menu.addMenu("&Edit"); edit_menu.addAction(self.settings_action)
    def setup_toolbar(self):
        toolbar = self.addToolBar("Main Toolbar"); toolbar.addAction(self.open_action); toolbar.addAction(self.settings_action)

    def open_text_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Text File", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f: self.text_edit.setText(f.read())
            except Exception as e: self.show_error(f"Failed to open file:\n\n{e}")

    def open_settings_dialog(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.get_settings()
            self.apply_settings()
            self.save_settings() # Save settings when they are changed

    def apply_settings(self):
        font = QFont(self.settings["font_family"], self.settings["font_size"])
        self.text_edit.setFont(font)
        self.text_edit.setStyleSheet(f"background-color: {self.settings['bg_color']}; color: {self.settings['text_color']};")
        # --- NEW: Apply loaded/default voice, speed, volume to UI ---
        if self.settings["voice"]:
            self.voice_combo.setCurrentText(self.settings["voice"])
        self.speed_slider.setValue(self.settings["speed"])
        self.volume_slider.setValue(self.settings["volume"])
 
    
    def update_highlight(self, line_index):
        self.clear_highlight()
        self.current_line_index = line_index
        doc = self.text_edit.document()
        block = doc.findBlockByNumber(line_index)
        if block.isValid():
            self.last_highlighted_block = block
            cursor = QTextCursor(block)
            fmt = QTextCharFormat(); fmt.setBackground(QColor(self.settings["highlight_color"]))
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor); cursor.mergeCharFormat(fmt)
            # Get the rectangle of the current cursor (the highlighted block)
            cursor_rect = self.text_edit.cursorRect(cursor)
            viewport_height = self.text_edit.viewport().height()

            # If the bottom of the highlight is in the lower 10% of the screen, scroll
            if cursor_rect.bottom() > (viewport_height * 0.9):
                scrollbar = self.text_edit.verticalScrollBar()
                # Scroll down by half the height of the viewport for a smooth jump
                scrollbar.setValue(scrollbar.value() + int(viewport_height * 0.8))



    # (Other methods are unchanged...)
    def update_speed_label(self, value): self.speed_label.setText(f"{value / 10.0:.1f}x")
    def update_volume_label(self, value): self.volume_label.setText(f"{value}%")
    def populate_voices(self):
        if not os.path.exists(VOICE_DIR): return
        for file in sorted(os.listdir(VOICE_DIR)):
            if file.endswith(".onnx"): self.voice_combo.addItem(file)
    def toggle_playback(self):
        if self.playback_state == "playing": self.pause_audio()
        else: self.play_audio()
    def play_audio(self):
        if self.playback_state == "stopped":
            cursor = self.text_edit.textCursor(); self.current_line_index = cursor.blockNumber()
            full_text = self.text_edit.toPlainText()
            self.lines = [line for line in full_text.splitlines()]
        lines_to_play = self.lines[self.current_line_index:]
        if not lines_to_play: self.full_stop(); return
        speed = self.speed_slider.value() / 10.0; volume = self.volume_slider.value() / 100.0
        self.audio_queue = queue.Queue()
        self.playback_thread = QThread(); self.audio_player = AudioPlaybackWorker(self.audio_queue, volume, self.current_line_index)
        self.audio_player.moveToThread(self.playback_thread)
        self.synth_thread = QThread(); self.synth_worker = PiperSynthWorker(lines_to_play, self.get_selected_voice_path(), self.audio_queue, speed)
        self.synth_worker.moveToThread(self.synth_thread)
        self.audio_player.highlight_line.connect(self.update_highlight)
        self.synth_worker.error.connect(self.show_error)
        self.audio_player.playback_finished.connect(self.on_playback_finished)
        self.playback_thread.started.connect(self.audio_player.run); self.synth_thread.started.connect(self.synth_worker.run)
        self.playback_thread.start(); self.synth_thread.start()
        self.playback_state = "playing"; self.play_button.setText("⏸ Pause")
        self.stop_button.setEnabled(True); self.text_edit.setReadOnly(True)
    def pause_audio(self):
        self.playback_state = "paused"; self.play_button.setText("▶ Resume")
        if hasattr(self, 'audio_player'): self.audio_player.playback_finished.disconnect(self.on_playback_finished)
        self.stop_threads()
    def full_stop(self):
        self.playback_state = "stopped"; self.play_button.setText("▶ Play")
        self.stop_threads(reset_highlight=True); self.current_line_index = 0; self.lines = []
    def on_playback_finished(self): self.full_stop()
    def stop_threads(self, reset_highlight=False):
        if hasattr(self, 'synth_worker'): self.synth_worker.stop(); self.synth_thread.quit(); self.synth_thread.wait()
        if hasattr(self, 'audio_player'): self.audio_player.stop(); self.playback_thread.quit(); self.playback_thread.wait()
        if reset_highlight: self.clear_highlight(force_clear_all=True)
        self.text_edit.setReadOnly(False); self.stop_button.setEnabled(False)
    def clear_highlight(self, force_clear_all=False):
        clear_format = QTextCharFormat(); clear_format.setBackground(Qt.GlobalColor.transparent)
        if force_clear_all:
             temp_cursor = QTextCursor(self.text_edit.document()); temp_cursor.select(QTextCursor.SelectionType.Document)
             temp_cursor.mergeCharFormat(clear_format)
        elif hasattr(self, 'last_highlighted_block') and self.last_highlighted_block and self.last_highlighted_block.isValid():
            temp_cursor = QTextCursor(self.last_highlighted_block); temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            temp_cursor.mergeCharFormat(clear_format)
        self.last_highlighted_block = None
    def save_audio(self):
        if self.playback_state == "playing": self.show_error("Please stop playback before saving."); return
        full_text = self.text_edit.toPlainText()
        if not full_text.strip(): self.show_error("Text box is empty."); return
        save_path, _ = QFileDialog.getSaveFileName(self, "Save Audio", "", "WAV Files (*.wav)")
        if not save_path: return
        lines_to_save = [line for line in full_text.splitlines() if line.strip()]
        speed = self.speed_slider.value() / 10.0
        self.save_thread = QThread()
        self.save_worker = PiperSynthWorker(lines_to_save, self.get_selected_voice_path(), None, speed, for_saving=True)
        self.save_worker.moveToThread(self.save_thread)
        self.save_worker.save_data_ready.connect(lambda data, sr: sf.write(save_path, data, sr))
        self.save_worker.finished.connect(lambda: (self.save_thread.quit(), self.save_thread.wait(), self.play_button.setEnabled(True), self.save_button.setEnabled(True)))
        self.save_worker.error.connect(self.show_error)
        self.save_thread.started.connect(self.save_worker.run)
        self.play_button.setEnabled(False); self.save_button.setEnabled(False)
    def get_selected_voice_path(self):
        voice_file = self.voice_combo.currentText()
        if not voice_file: return None
        return os.path.join(VOICE_DIR, voice_file)
    def show_error(self, message):
        QMessageBox.critical(self, "Error", message); self.full_stop()
    def closeEvent(self, event):
        self.save_settings() # Save settings on exit
        self.full_stop(); event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())