import sys
import os
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QTextEdit, QPushButton, QComboBox, QHBoxLayout,
                             QFileDialog, QMessageBox, QLabel, QSlider, QDialog,
                             QFormLayout, QFontComboBox, QSpinBox, QDialogButtonBox,
                             QColorDialog)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat, QFont, QAction, QIcon

# Import our new speech worker
from speech_worker import SpeechWorker

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Piper-Qt TTS (speechd)")
        self.setGeometry(100, 100, 800, 600)
        self.lines = []
        self.playback_state = "stopped" # Can be "stopped", "playing", "paused"
        self.current_line_index = 0
        
        self.config_path = os.path.expanduser("~/.config/piper-qt/settings.json")
        self.settings = {
            "font_family": "Noto Sans", "font_size": 14, "bg_color": "#ffffff", 
            "text_color": "#000000", "highlight_color": "#a8d8ff", "completed_color": "#808080",
            "voice": "", "speed": 10, "volume": 50, "session_text": "", "session_cursor_line": 0
        }
        self.load_settings()

        # --- NEW: Setup the single SpeechWorker thread ---
        self.speech_thread = QThread()
        self.speech_worker = SpeechWorker()
        self.speech_worker.moveToThread(self.speech_thread)

        # Connect signals from the worker to our main window's functions (slots)
        self.speech_worker.line_completed.connect(self.mark_line_as_completed)
        self.speech_worker.playback_finished.connect(self.on_playback_finished)
        self.speech_worker.error.connect(self.show_error)
        
        # Start the thread and the worker's setup
        self.speech_thread.started.connect(self.speech_worker.setup)
        self.speech_thread.start()
        
        self.setup_ui()
        self.apply_settings()
        self.restore_session()

    def setup_ui(self):
        """Creates all the UI elements."""
        self.setup_actions(); self.setup_menu(); self.setup_toolbar()
        central_widget = QWidget(); self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Voice:")); self.voice_combo = QComboBox(); self.populate_voices()
        controls_layout.addWidget(self.voice_combo)
        controls_layout.addWidget(QLabel("Speed:"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal); self.speed_slider.setRange(5, 20); self.speed_slider.setValue(10)
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
        self.prev_button = QPushButton("◀ Prev"); self.play_button = QPushButton("▶ Play"); self.next_button = QPushButton("Next ▶")
        self.stop_button = QPushButton("⏹ Stop"); self.save_button = QPushButton("Save to WAV")
        button_layout.addWidget(self.prev_button); button_layout.addWidget(self.play_button); 
        button_layout.addWidget(self.next_button); button_layout.addWidget(self.stop_button);
        button_layout.addWidget(self.save_button)
        layout.addLayout(button_layout)
        self.stop_button.setEnabled(False); self.prev_button.setEnabled(False); self.next_button.setEnabled(False)
        self.save_button.setEnabled(False) # Save is not yet re-implemented
        self.play_button.clicked.connect(self.toggle_playback)
        self.stop_button.clicked.connect(self.full_stop)
        self.speed_slider.valueChanged.connect(self.update_speed_label)
        self.volume_slider.valueChanged.connect(self.update_volume_label)
        self.prev_button.clicked.connect(lambda: self.skip_line(-1))
        self.next_button.clicked.connect(lambda: self.skip_line(1))

    def populate_voices(self):
        """Gets the list of voices from speech-dispatcher."""
        self.voice_combo.clear()
        try:
            # We must use a list() to consume the generator from the worker
            voices = list(self.speech_worker.list_voices())
            if voices:
                # Filter for Piper voices, as they are high quality
                piper_voices = [v[0] for v in voices if 'piper' in v[1].lower()]
                if piper_voices:
                    self.voice_combo.addItems(piper_voices)
                else: # If no piper, add all voices
                    self.voice_combo.addItems([v[0] for v in voices])
        except Exception as e:
            print(f"Could not load voices: {e}")

    def toggle_playback(self):
        if self.playback_state == "playing":
            self.pause_audio()
        elif self.playback_state == "paused":
            self.resume_audio()
        else: # "stopped"
            self.play_audio()
            
    def play_audio(self):
        if self.playback_state == "stopped":
            cursor = self.text_edit.textCursor(); self.current_line_index = cursor.blockNumber()
            full_text = self.text_edit.toPlainText()
            self.lines = [line for line in full_text.splitlines()]
        
        lines_to_play = self.lines[self.current_line_index:]
        if not lines_to_play: self.full_stop(); return

        # Set all properties on the worker
        self.speech_worker.set_voice(self.voice_combo.currentText())
        self.speech_worker.set_speed(self.speed_slider.value())
        self.speech_worker.set_volume(self.volume_slider.value())
        
        # Tell the worker to start speaking the list of lines
        self.speech_worker.play(lines_to_play, self.current_line_index)
        
        self.playback_state = "playing"; self.play_button.setText("⏸ Pause")
        self.stop_button.setEnabled(True); self.text_edit.setReadOnly(True)
        self.prev_button.setEnabled(True); self.next_button.setEnabled(True)

    def pause_audio(self):
        self.playback_state = "paused"; self.play_button.setText("▶ Resume")
        self.speech_worker.pause()

    def resume_audio(self):
        self.playback_state = "playing"; self.play_button.setText("⏸ Pause")
        self.speech_worker.resume()

    def full_stop(self):
        self.playback_state = "stopped"; self.play_button.setText("▶ Play")
        self.speech_worker.stop()
        self.clear_highlight(force_clear_all=True)
        self.current_line_index = 0; self.lines = []
        self.prev_button.setEnabled(False); self.next_button.setEnabled(False)
        self.stop_button.setEnabled(False); self.text_edit.setReadOnly(False)

    def skip_line(self, delta):
        if not self.lines:
            full_text = self.text_edit.toPlainText()
            if not full_text.strip(): return
            self.lines = [line for line in full_text.splitlines()]
            self.current_line_index = self.text_edit.textCursor().blockNumber()
        
        # Stop current speech
        self.speech_worker.stop()

        new_index = self.current_line_index + delta
        new_index = max(0, min(new_index, len(self.lines) - 1))
        self.current_line_index = new_index

        # Clear all highlights and apply the new "current" one
        self.clear_highlight(force_clear_all=True)
        self.update_highlight(self.current_line_index)
        
        # Move the visible cursor
        block = self.text_edit.document().findBlockByNumber(self.current_line_index)
        if block.isValid(): self.text_edit.setTextCursor(QTextCursor(block))
        self.text_edit.ensureCursorVisible()

        # Restart playback from the new line
        self.play_audio()
        
    def on_playback_finished(self):
        """Slot called by the worker when speech is all done."""
        self.full_stop()

    def mark_line_as_completed(self, line_index):
        """Slot to handle the line_completed signal."""
        doc = self.text_edit.document()
        block = doc.findBlockByNumber(line_index)
        if block.isValid():
            cursor = QTextCursor(block); fmt = QTextCharFormat()
            fmt.setForeground(QColor(self.settings["completed_color"]))
            fmt.setBackground(Qt.GlobalColor.transparent) # Clear background
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor); cursor.mergeCharFormat(fmt)
            self.last_highlighted_block = None

    def update_highlight(self, line_index):
        """Slot to highlight the currently speaking line."""
        self.clear_highlight()
        self.current_line_index = line_index
        doc = self.text_edit.document()
        block = doc.findBlockByNumber(line_index)
        if block.isValid():
            self.last_highlighted_block = block
            cursor = QTextCursor(block); fmt = QTextCharFormat()
            fmt.setBackground(QColor(self.settings["highlight_color"]))
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor); cursor.mergeCharFormat(fmt)
            cursor_rect = self.text_edit.cursorRect(cursor); viewport_height = self.text_edit.viewport().height()
            if cursor_rect.bottom() > (viewport_height * 0.8):
                scrollbar = self.text_edit.verticalScrollBar()
                scrollbar.setValue(scrollbar.value() + viewport_height // 2)

    def clear_highlight(self, force_clear_all=False):
        clear_format = QTextCharFormat()
        clear_format.setBackground(Qt.GlobalColor.transparent)
        if force_clear_all:
             # Reset both background and the default text color
             clear_format.setForeground(QColor(self.settings["text_color"]))
             temp_cursor = QTextCursor(self.text_edit.document()); temp_cursor.select(QTextCursor.SelectionType.Document)
             temp_cursor.mergeCharFormat(clear_format)
        elif hasattr(self, 'last_highlighted_block') and self.last_highlighted_block and self.last_highlighted_block.isValid():
            temp_cursor = QTextCursor(self.last_highlighted_block); temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            temp_cursor.mergeCharFormat(clear_format)
        self.last_highlighted_block = None
        
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
        self.text_edit.setFont(font)
        self.text_edit.setStyleSheet(f"background-color: {self.settings['bg_color']}; color: {self.settings['text_color']};")
        if self.settings["voice"]: self.voice_combo.setCurrentText(self.settings["voice"])
        self.speed_slider.setValue(self.settings["speed"]); self.volume_slider.setValue(self.settings["volume"])
    def update_speed_label(self, value): self.speed_label.setText(f"{value / 10.0:.1f}x")
    def update_volume_label(self, value): self.volume_label.setText(f"{value}%")
    def save_audio(self):
        self.show_error("Save to file is not supported in this version.")
    def get_selected_voice_path(self):
        return self.voice_combo.currentText()
    def show_error(self, message):
        QMessageBox.critical(self, "Error", message); self.full_stop()
    
    def closeEvent(self, event):
        self.save_settings()
        self.full_stop()
        # Cleanly shut down the speech worker and thread
        self.speech_worker.close()
        self.speech_thread.quit()
        self.speech_thread.wait()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())