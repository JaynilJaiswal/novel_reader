import sys
import os
import asyncio
import queue
import sounddevice as sd
import soundfile as sf
import io
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QTextEdit, QPushButton, QComboBox, QHBoxLayout,
                             QFileDialog, QMessageBox, QLabel, QSlider, QDialog,
                             QFormLayout, QFontComboBox, QSpinBox, QDialogButtonBox,
                             QColorDialog)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat, QFont, QAction, QIcon
import edge_tts

# A selection of highly realistic English neural voices from Edge-TTS
EDGE_VOICES = [
    "en-US-AriaNeural",
    "en-US-AnaNeural",
    "en-US-BrianNeural",
    "en-US-EmmaNeural",
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-US-ChristopherNeural",
    "en-US-EricNeural",
    "en-US-MichelleNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural"
]

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
        self.completed_color_btn = self.create_color_button(self.settings["completed_color"])
        color_layout.addWidget(QLabel("BG:")); color_layout.addWidget(self.bg_color_btn)
        color_layout.addWidget(QLabel("Text:")); color_layout.addWidget(self.text_color_btn)
        color_layout.addWidget(QLabel("Highlight:")); color_layout.addWidget(self.highlight_color_btn)
        color_layout.addWidget(QLabel("Completed:")); color_layout.addWidget(self.completed_color_btn)
        layout.addRow(color_layout)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept); button_box.rejected.connect(self.reject)
        layout.addRow(button_box)
        self.bg_color_btn.clicked.connect(lambda: self.pick_color(self.bg_color_btn, "bg_color"))
        self.text_color_btn.clicked.connect(lambda: self.pick_color(self.text_color_btn, "text_color"))
        self.highlight_color_btn.clicked.connect(lambda: self.pick_color(self.highlight_color_btn, "highlight_color"))
        self.completed_color_btn.clicked.connect(lambda: self.pick_color(self.completed_color_btn, "completed_color"))

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


class EdgeSynthWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    
    def __init__(self, lines, voice, audio_queue, speed_rate):
        super().__init__()
        self.lines = lines
        self.voice = voice
        self.audio_queue = audio_queue
        self.speed_rate = speed_rate
        self._is_running = True
        
    def run(self):
        try:
            asyncio.run(self.async_run())
        except Exception as e:
            if self._is_running: self.error.emit(f"Edge-TTS worker error:\n\n{e}")
        finally:
            try: self.audio_queue.put_nowait(None)
            except queue.Full: pass
            self.finished.emit()

    async def async_run(self):
        for i, line in enumerate(self.lines):
            if not self._is_running: break
            if not line.strip(): continue 
            
            try:
                comm = edge_tts.Communicate(line, self.voice, rate=self.speed_rate)
                audio_bytes = b""
                
                async for chunk in comm.stream():
                    if not self._is_running: 
                        break 
                    if chunk["type"] == "audio":
                        audio_bytes += chunk["data"]
                
                if not self._is_running:
                    break
                
                if audio_bytes:
                    data, samplerate = sf.read(io.BytesIO(audio_bytes), dtype='float32')
                    self.audio_queue.put({'index': i, 'data': data, 'samplerate': samplerate})
                    
            except Exception as e:
                print(f"Edge-TTS synthesis error on line '{line}': {e}")
                continue
                
        await asyncio.sleep(0.1)
            
    def stop(self): 
        self._is_running = False

class AudioPlaybackWorker(QObject):
    playback_finished = pyqtSignal()
    highlight_line = pyqtSignal(int)
    line_completed = pyqtSignal(int)
    
    def __init__(self, audio_queue, volume, line_index_offset):
        super().__init__(); self.audio_queue = audio_queue; self.volume = volume
        self.line_index_offset = line_index_offset; self._is_running = True
        self.stream = None
        
    def run(self):
        try:
            while self._is_running:
                item = self.audio_queue.get()
                if item is None: break
                local_line_index = item['index']; audio_data = item['data']; samplerate = item['samplerate']
                if not self._is_running: break
                
                original_line_index = self.line_index_offset + local_line_index
                self.highlight_line.emit(original_line_index)
                
                if self.stream is None or self.stream.samplerate != samplerate:
                    if self.stream: 
                        try: self.stream.close()
                        except Exception: pass
                    self.stream = sd.OutputStream(samplerate=samplerate, channels=1, dtype='float32')
                    self.stream.start()
                
                chunk_size = int(samplerate * 0.05) 
                for i in range(0, len(audio_data), chunk_size):
                    if not self._is_running: break
                    chunk = audio_data[i:i + chunk_size]
                    try: self.stream.write(chunk * self.volume) 
                    except Exception as e: print(f"Stream write error: {e}"); break
                
                if self._is_running:
                    self.line_completed.emit(original_line_index)

        except Exception as e: print(f"Playback error: {e}")
        finally:
            if self.stream: 
                try: self.stream.close() 
                except Exception: pass
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
        self.setWindowTitle("Edge-Qt TTS Reader")
        self.setGeometry(100, 100, 800, 600)
        self.lines = []; self.line_word_counts = []; self.words_remaining = 0
        self.last_highlighted_block = None; self.playback_state = "stopped"; self.current_line_index = 0
        
        self.config_path = os.path.expanduser("~/.config/edge-qt/settings.json")
        self.settings = {
            "font_family": "Noto Sans", "font_size": 14,
            "bg_color": "#ffffff", "text_color": "#000000",
            "highlight_color": "#a8d8ff", "completed_color": "#808080",
            "voice": "en-US-AriaNeural", "speed": 10, "volume": 100,
            "session_text": "", "session_cursor_line": 0
        }
        self.load_settings()

        self.setup_actions(); self.setup_menu(); self.setup_toolbar()
        central_widget = QWidget(); self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        controls_layout = QHBoxLayout()
        
        controls_layout.addWidget(QLabel("Voice:")); self.voice_combo = QComboBox()
        self.voice_combo.addItems(EDGE_VOICES)
        controls_layout.addWidget(self.voice_combo)
        
        controls_layout.addWidget(QLabel("Speed:")); self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        # --- FIX: Max slider range capped at 30 to reflect Azure's hard +200% speed limit ---
        self.speed_slider.setRange(5, 30); self.speed_slider.setValue(10)
        controls_layout.addWidget(self.speed_slider)
        self.speed_label = QLabel("1.0x"); controls_layout.addWidget(self.speed_label)
        
        controls_layout.addWidget(QLabel("Volume:")); self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100); self.volume_slider.setValue(100)
        controls_layout.addWidget(self.volume_slider)
        self.volume_label = QLabel("100%"); controls_layout.addWidget(self.volume_label)
        
        self.eta_label = QLabel("Total ETA: 00:00:00")
        controls_layout.addWidget(self.eta_label)
        layout.addLayout(controls_layout)
        
        self.text_edit = QTextEdit(); self.text_edit.setPlaceholderText("Enter text to read.")
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.text_edit)
        
        button_layout = QHBoxLayout()
        self.prev_button = QPushButton("⏮ Prev"); self.play_button = QPushButton("▶ Play")
        self.stop_button = QPushButton("⏹ Stop"); self.next_button = QPushButton("⏭ Next")
        
        button_layout.addWidget(self.prev_button); button_layout.addWidget(self.play_button)
        button_layout.addWidget(self.stop_button); button_layout.addWidget(self.next_button)
        layout.addLayout(button_layout)
        
        self.stop_button.setEnabled(False)
        
        self.prev_button.clicked.connect(self.play_prev); self.play_button.clicked.connect(self.toggle_playback)
        self.stop_button.clicked.connect(self.full_stop); self.next_button.clicked.connect(self.play_next)
        
        self.speed_slider.valueChanged.connect(self.update_speed_label)
        self.volume_slider.valueChanged.connect(self.update_volume_label)
        self.text_edit.textChanged.connect(self.update_eta)
        self.speed_slider.valueChanged.connect(self.update_eta)
        
        self.apply_settings()
        self.restore_session()
        self.update_eta()
        self.text_edit.installEventFilter(self)

    def eventFilter(self, source, event):
        if source == self.text_edit and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                self.toggle_playback(); return True 
            if self.playback_state in ["playing", "paused"]:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self.toggle_playback(); return True
                elif event.key() == Qt.Key.Key_Left:
                    self.play_prev(); return True
                elif event.key() == Qt.Key.Key_Right:
                    self.play_next(); return True
        return super().eventFilter(source, event)

    def play_prev(self): self.navigate_playback(-1)
    def play_next(self): self.navigate_playback(1)

    def navigate_playback(self, offset):
        full_text = self.text_edit.toPlainText()
        current_lines = [line for line in full_text.splitlines()]
        if not current_lines: return

        if self.playback_state == "stopped" or not self.lines:
            self.lines = current_lines
            self.line_word_counts = [len(line.split()) for line in self.lines]
            self.current_line_index = self.text_edit.textCursor().blockNumber()

        new_index = max(0, min(self.current_line_index + offset, len(self.lines) - 1))
        if new_index == self.current_line_index and self.playback_state != "stopped": return 
        
        was_playing = (self.playback_state == "playing")

        if self.playback_state in ["playing", "paused"]:
            self.stop_threads(reset_highlight=False)
            
        cursor = QTextCursor(self.text_edit.document().findBlockByNumber(new_index))
        self.text_edit.setTextCursor(cursor)
        self.current_line_index = new_index
            
        self.playback_state = "stopped" 
        if was_playing: self.play_audio()
        else: self.playback_state = "paused"; self.update_highlight(new_index)

    def play_audio(self):
        if self.playback_state == "stopped":
            cursor = self.text_edit.textCursor(); self.current_line_index = cursor.blockNumber()
            full_text = self.text_edit.toPlainText()
            self.lines = [line for line in full_text.splitlines()]
            self.line_word_counts = [len(line.split()) for line in self.lines]
            
        lines_to_play = self.lines[self.current_line_index:]
        if not lines_to_play: self.full_stop(); return
        
        self.words_remaining = sum(self.line_word_counts[self.current_line_index:])
        self.playback_cursor = QTextCursor(self.text_edit.document().findBlockByNumber(self.current_line_index))
        
        speed_percentage = int(((self.speed_slider.value() / 10.0) - 1.0) * 100)
        rate_str = f"{speed_percentage:+d}%"
        volume = self.volume_slider.value() / 100.0
        
        self.audio_queue = queue.Queue(maxsize=5)
        self.playback_thread = QThread()
        self.audio_player = AudioPlaybackWorker(self.audio_queue, volume, self.current_line_index)
        self.audio_player.moveToThread(self.playback_thread)
        
        self.synth_thread = QThread()
        self.synth_worker = EdgeSynthWorker(lines_to_play, self.voice_combo.currentText(), self.audio_queue, rate_str)
        self.synth_worker.moveToThread(self.synth_thread)
        
        self.audio_player.highlight_line.connect(self.update_highlight)
        self.audio_player.line_completed.connect(self.mark_line_as_completed)
        self.synth_worker.error.connect(self.show_error)
        self.audio_player.playback_finished.connect(self.on_playback_finished)
        
        self.playback_thread.started.connect(self.audio_player.run)
        self.synth_thread.started.connect(self.synth_worker.run)
        self.playback_thread.start(); self.synth_thread.start()
        
        self.playback_state = "playing"; self.play_button.setText("⏸ Pause")
        self.stop_button.setEnabled(True); self.text_edit.setReadOnly(True)

    def mark_line_as_completed(self, line_index):
        if self.last_highlighted_block and self.last_highlighted_block.blockNumber() == line_index:
            temp_cursor = QTextCursor(self.last_highlighted_block)
            fmt = QTextCharFormat(); fmt.setForeground(QColor(self.settings["completed_color"]))
            fmt.setBackground(Qt.GlobalColor.transparent)
            temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor); temp_cursor.mergeCharFormat(fmt)
            self.last_highlighted_block = None 
            
        self.playback_cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
        if line_index < len(self.line_word_counts): self.words_remaining -= self.line_word_counts[line_index]
        self.update_eta()

    def clear_highlight(self, force_clear_all=False):
        clear_format = QTextCharFormat(); clear_format.setBackground(Qt.GlobalColor.transparent)
        current_visible_cursor = self.text_edit.textCursor()
        if force_clear_all:
             temp_cursor = QTextCursor(self.text_edit.document()); temp_cursor.select(QTextCursor.SelectionType.Document)
             temp_cursor.mergeCharFormat(clear_format); temp_cursor.mergeCharFormat(self.text_edit.currentCharFormat()) 
        elif hasattr(self, 'last_highlighted_block') and self.last_highlighted_block and self.last_highlighted_block.isValid():
            temp_cursor = QTextCursor(self.last_highlighted_block); temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            temp_cursor.mergeCharFormat(clear_format)
        self.last_highlighted_block = None; self.text_edit.setTextCursor(current_visible_cursor)

    def update_eta(self, *args):
        if self.playback_state in ["playing", "paused"] and hasattr(self, 'words_remaining'):
            word_count = max(0, self.words_remaining); prefix = "Time Left: "
        else:
            text = self.text_edit.toPlainText(); word_count = len(text.split()); prefix = "Total ETA: "
            
        # --- FIX: Adjusted to 165 WPM for accurate Edge neural reading speed ---
        base_wpm = 165.0
        speed_multiplier = self.speed_slider.value() / 10.0; adjusted_wpm = base_wpm * speed_multiplier
        if adjusted_wpm > 0 and word_count > 0:
            total_seconds = (word_count / adjusted_wpm) * 60
            hours = int(total_seconds // 3600); minutes = int((total_seconds % 3600) // 60); seconds = int(total_seconds % 60)
            self.eta_label.setText(f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}")
        else: self.eta_label.setText(f"{prefix}00:00:00")

    def load_settings(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f: self.settings.update(json.load(f))
        except Exception as e: print(f"Could not load settings: {e}")
    def save_settings(self):
        try:
            self.settings["voice"] = self.voice_combo.currentText(); self.settings["speed"] = self.speed_slider.value(); self.settings["volume"] = self.volume_slider.value()
            self.settings["session_text"] = self.text_edit.toPlainText(); self.settings["session_cursor_line"] = self.text_edit.textCursor().blockNumber()
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f: json.dump(self.settings, f, indent=4)
        except Exception as e: print(f"Could not save settings: {e}")
    def restore_session(self):
        if self.settings.get("session_text"):
            self.text_edit.setText(self.settings["session_text"])
            cursor_line = self.settings.get("session_cursor_line", 0)
            block = self.text_edit.document().findBlockByNumber(cursor_line)
            if block.isValid(): self.text_edit.setTextCursor(QTextCursor(block))
            
    def setup_actions(self):
        self.open_action = QAction(QIcon.fromTheme("document-open"), "&Open Text File...", self); self.open_action.triggered.connect(self.open_text_file)
        self.settings_action = QAction(QIcon.fromTheme("preferences-system"), "&Settings...", self); self.settings_action.triggered.connect(self.open_settings_dialog)
    def setup_menu(self):
        menu = self.menuBar(); file_menu = menu.addMenu("&File"); file_menu.addAction(self.open_action); edit_menu = menu.addMenu("&Edit"); edit_menu.addAction(self.settings_action)
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
        if dialog.exec(): self.settings = dialog.get_settings(); self.apply_settings(); self.save_settings()
    def apply_settings(self):
        font = QFont(self.settings["font_family"], self.settings["font_size"])
        self.text_edit.setFont(font); self.text_edit.setStyleSheet(f"background-color: {self.settings['bg_color']}; color: {self.settings['text_color']};")
        if self.settings["voice"]: self.voice_combo.setCurrentText(self.settings["voice"])
        self.speed_slider.setValue(self.settings["speed"]); self.volume_slider.setValue(self.settings["volume"])
        
    def update_highlight(self, line_index):
        self.clear_highlight(); self.current_line_index = line_index
        if self.playback_cursor.blockNumber() != line_index:
            self.playback_cursor = QTextCursor(self.text_edit.document().findBlockByNumber(line_index))
        block = self.playback_cursor.block()
        if block.isValid():
            self.last_highlighted_block = block
            fmt = QTextCharFormat(); fmt.setBackground(QColor(self.settings["highlight_color"]))
            self.playback_cursor.select(QTextCursor.SelectionType.BlockUnderCursor); self.playback_cursor.mergeCharFormat(fmt)
            self.playback_cursor.clearSelection(); self.text_edit.setTextCursor(self.playback_cursor)
            cursor_rect = self.text_edit.cursorRect(self.playback_cursor); viewport_height = self.text_edit.viewport().height()
            if cursor_rect.bottom() > (viewport_height * 0.7) or cursor_rect.top() < (viewport_height * 0.3):
                scrollbar = self.text_edit.verticalScrollBar(); center_offset = cursor_rect.top() - (viewport_height // 2)
                scrollbar.setValue(scrollbar.value() + center_offset)
        self.update_eta()
        
    def update_speed_label(self, value): self.speed_label.setText(f"{value / 10.0:.1f}x")
    def update_volume_label(self, value): self.volume_label.setText(f"{value}%")
    def toggle_playback(self):
        if self.playback_state == "playing": self.pause_audio()
        else: self.play_audio()
    def pause_audio(self):
        self.playback_state = "paused"; self.play_button.setText("▶ Resume")
        self.stop_threads(reset_highlight=False)
    def full_stop(self):
        self.playback_state = "stopped"; self.play_button.setText("▶ Play")
        self.stop_threads(reset_highlight=True); self.lines = []; self.update_eta()
    def on_playback_finished(self): 
        if self.playback_state == "playing": self.full_stop()
    def stop_threads(self, reset_highlight=False):
        if hasattr(self, 'audio_player'):
            try: self.audio_player.playback_finished.disconnect(self.on_playback_finished)
            except Exception: pass
            self.audio_player.stop(); self.playback_thread.quit(); self.playback_thread.wait()
            self.audio_player.deleteLater(); self.playback_thread.deleteLater()
            del self.audio_player; del self.playback_thread
        if hasattr(self, 'synth_worker'): 
            self.synth_worker.stop(); self.synth_thread.quit(); self.synth_thread.wait()
            self.synth_worker.deleteLater(); self.synth_thread.deleteLater()
            del self.synth_worker; del self.synth_thread
        if reset_highlight: self.clear_highlight(force_clear_all=True)
        self.text_edit.setReadOnly(False); self.stop_button.setEnabled(False)
        
    def show_error(self, message): QMessageBox.critical(self, "Error", message); self.full_stop()
    def closeEvent(self, event): self.save_settings(); self.full_stop(); event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())