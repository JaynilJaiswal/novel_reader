import sys
import os
import subprocess
import queue
import sounddevice as sd
import soundfile as sf
import io
import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QTextEdit, QPushButton, QComboBox, QHBoxLayout,
                             QFileDialog, QMessageBox, QLabel, QSlider)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat

# Define the path where Piper voices are stored
VOICE_DIR = os.path.expanduser("~/.local/share/piper-voices")

class PiperSynthWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    # New signal for saving that returns the full audio data
    save_data_ready = pyqtSignal(object, int)
    
    def __init__(self, lines, voice_path, audio_queue, speed, for_saving=False):
        super().__init__()
        self.lines = lines
        self.voice_path = voice_path
        self.audio_queue = audio_queue
        self.speed = speed
        self.for_saving = for_saving
        self._is_running = True

    def run(self):
        try:
            # If saving, we need to collect all chunks
            all_audio_chunks = []
            final_samplerate = None

            for i, line in enumerate(self.lines):
                if not self._is_running: break
                
                length_scale = 1.0 / self.speed
                command = ['piper-tts', '--model', self.voice_path, '--length_scale', str(length_scale), '--output_file', '-']
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout_data, stderr_data = process.communicate(input=line.encode('utf-8'))

                if process.returncode != 0:
                    self.error.emit(f"Piper error on line '{line}':\n\n{stderr_data.decode('utf-8')}")
                    continue

                if stdout_data:
                    data, samplerate = sf.read(io.BytesIO(stdout_data), dtype='float32')
                    if self.for_saving:
                        all_audio_chunks.append(data)
                        if final_samplerate is None:
                            final_samplerate = samplerate
                    else:
                        self.audio_queue.put({'index': i, 'data': data, 'samplerate': samplerate})
            
            if self.for_saving and all_audio_chunks:
                combined_audio = np.concatenate(all_audio_chunks)
                self.save_data_ready.emit(combined_audio, final_samplerate)

        except Exception as e:
            self.error.emit(f"Synthesis worker error:\n\n{e}")
        finally:
            if not self.for_saving:
                self.audio_queue.put(None)
            self.finished.emit()
            
    def stop(self):
        self._is_running = False


class AudioPlaybackWorker(QObject):
    playback_finished = pyqtSignal()
    highlight_line = pyqtSignal(int)
    
    def __init__(self, audio_queue, volume, line_index_offset):
        super().__init__()
        self.audio_queue = audio_queue
        self.volume = volume
        self.line_index_offset = line_index_offset
        self._is_running = True

    def run(self):
        stream = None
        try:
            while self._is_running:
                item = self.audio_queue.get()
                if item is None: break

                local_line_index = item['index']
                audio_data = item['data']
                samplerate = item['samplerate']

                if not self._is_running: break

                original_line_index = self.line_index_offset + local_line_index
                self.highlight_line.emit(original_line_index)
                
                if stream is None or stream.samplerate != samplerate:
                    if stream: stream.close()
                    stream = sd.OutputStream(samplerate=samplerate, channels=1, dtype='float32')
                    stream.start()

                stream.write(audio_data * self.volume)
                
        except Exception as e:
            print(f"Playback error: {e}")
        finally:
            if stream:
                stream.stop()
                stream.close()
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
        self.setGeometry(100, 100, 700, 500)
        self.lines = []
        self.last_highlighted_block = None
        self.playback_state = "stopped"
        self.current_line_index = 0

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Voice:"))
        self.voice_combo = QComboBox(); self.populate_voices()
        controls_layout.addWidget(self.voice_combo)
        controls_layout.addWidget(QLabel("Speed:"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal); self.speed_slider.setRange(5, 20); self.speed_slider.setValue(10)
        controls_layout.addWidget(self.speed_slider)
        self.speed_label = QLabel("1.0x"); controls_layout.addWidget(self.speed_label)
        controls_layout.addWidget(QLabel("Volume:"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal); self.volume_slider.setRange(0, 100); self.volume_slider.setValue(100)
        controls_layout.addWidget(self.volume_slider)
        self.volume_label = QLabel("100%"); controls_layout.addWidget(self.volume_label)
        layout.addLayout(controls_layout)
        self.text_edit = QTextEdit() 
        self.text_edit.setPlaceholderText("Enter or paste text to be read aloud...")
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.text_edit)
        button_layout = QHBoxLayout()
        self.play_button = QPushButton("▶ Play")
        self.save_button = QPushButton("Save to WAV") # Added save button back
        self.stop_button = QPushButton("⏹ Stop")
        button_layout.addWidget(self.play_button); button_layout.addWidget(self.save_button); button_layout.addWidget(self.stop_button)
        layout.addLayout(button_layout)
        self.stop_button.setEnabled(False)

        self.play_button.clicked.connect(self.toggle_playback)
        self.stop_button.clicked.connect(self.full_stop)
        self.save_button.clicked.connect(self.save_audio)
        self.speed_slider.valueChanged.connect(self.update_speed_label)
        self.volume_slider.valueChanged.connect(self.update_volume_label)

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
            # --- NEW: Get text and starting line from cursor position ---
            cursor = self.text_edit.textCursor()
            self.current_line_index = cursor.blockNumber()
            full_text = self.text_edit.toPlainText()
            self.lines = [line for line in full_text.splitlines() if line.strip()]

        lines_to_play = self.lines[self.current_line_index:]
        if not lines_to_play: self.full_stop(); return

        speed = self.speed_slider.value() / 10.0
        volume = self.volume_slider.value() / 100.0
        self.audio_queue = queue.Queue()
        
        self.playback_thread = QThread(); self.audio_player = AudioPlaybackWorker(self.audio_queue, volume, self.current_line_index)
        self.audio_player.moveToThread(self.playback_thread)
        self.synth_thread = QThread(); self.synth_worker = PiperSynthWorker(lines_to_play, self.get_selected_voice_path(), self.audio_queue, speed)
        self.synth_worker.moveToThread(self.synth_thread)

        self.audio_player.highlight_line.connect(self.update_highlight)
        self.synth_worker.error.connect(self.show_error)
        self.audio_player.playback_finished.connect(self.on_playback_finished)
        
        self.playback_thread.started.connect(self.audio_player.run)
        self.synth_thread.started.connect(self.synth_worker.run)
        self.playback_thread.start(); self.synth_thread.start()
        
        self.playback_state = "playing"; self.play_button.setText("⏸ Pause")
        self.stop_button.setEnabled(True); self.text_edit.setReadOnly(True)

    def pause_audio(self):
        self.playback_state = "paused"
        self.play_button.setText("▶ Resume")
        if hasattr(self, 'audio_player'):
            self.audio_player.playback_finished.disconnect(self.on_playback_finished)
        self.stop_threads()

    def full_stop(self):
        self.playback_state = "stopped"
        self.play_button.setText("▶ Play")
        self.stop_threads(reset_highlight=True)
        self.current_line_index = 0
        self.lines = []

    def on_playback_finished(self):
        self.full_stop()

    def stop_threads(self, reset_highlight=False):
        if hasattr(self, 'synth_worker'):
            self.synth_worker.stop(); self.synth_thread.quit(); self.synth_thread.wait()
        if hasattr(self, 'audio_player'):
            self.audio_player.stop(); self.playback_thread.quit(); self.playback_thread.wait()
        
        if reset_highlight:
            self.clear_highlight(force_clear_all=True)
        
        self.text_edit.setReadOnly(False); self.stop_button.setEnabled(False)

    def update_highlight(self, line_index):
        self.clear_highlight()
        self.current_line_index = line_index
        doc = self.text_edit.document()
        block = doc.findBlockByNumber(line_index)
        
        if block.isValid():
            self.last_highlighted_block = block
            cursor = QTextCursor(block)
            fmt = QTextCharFormat(); fmt.setBackground(QColor("#a8d8ff"))
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.mergeCharFormat(fmt)

    def clear_highlight(self, force_clear_all=False):
        # Create a new format that ONLY defines a transparent background.
        clear_format = QTextCharFormat()
        clear_format.setBackground(Qt.GlobalColor.transparent)

        if force_clear_all:
            # Use a temporary cursor to perform the operation.
            # This leaves the user's main cursor untouched.
            temp_cursor = QTextCursor(self.text_edit.document())
            temp_cursor.select(QTextCursor.SelectionType.Document)
            
            # mergeCharFormat only changes the properties defined in clear_format (the background)
            # and leaves the font, size, boldness, etc. completely alone.
            temp_cursor.mergeCharFormat(clear_format)

        elif hasattr(self, 'last_highlighted_block') and self.last_highlighted_block and self.last_highlighted_block.isValid():
            # Use a temporary cursor for the single block as well.
            temp_cursor = QTextCursor(self.last_highlighted_block)
            temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            temp_cursor.mergeCharFormat(clear_format)
        
        # We no longer need to manage the main cursor, so it stays where it was.
        self.last_highlighted_block = None
            
    def save_audio(self):
        if self.playback_state == "playing":
            self.show_error("Please stop playback before saving.")
            return

        full_text = self.text_edit.toPlainText()
        if not full_text.strip():
            self.show_error("Text box is empty."); return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save Audio", "", "WAV Files (*.wav)")
        if not save_path: return

        lines_to_save = [line for line in full_text.splitlines() if line.strip()]
        speed = self.speed_slider.value() / 10.0
        
        self.save_thread = QThread()
        self.save_worker = PiperSynthWorker(lines_to_save, self.get_selected_voice_path(), None, speed, for_saving=True)
        self.save_worker.moveToThread(self.save_thread)
        
        self.save_worker.save_data_ready.connect(lambda data, sr: sf.write(save_path, data, sr))
        self.save_worker.finished.connect(lambda: (self.save_thread.quit(), self.save_thread.wait(), self.play_button.setEnabled(True)))
        self.save_worker.error.connect(self.show_error)
        
        self.save_thread.started.connect(self.save_worker.run)
        self.save_thread.start()
        self.play_button.setEnabled(False)

    def get_selected_voice_path(self):
        voice_file = self.voice_combo.currentText()
        if not voice_file: return None
        return os.path.join(VOICE_DIR, voice_file)

    def show_error(self, message):
        QMessageBox.critical(self, "Error", message); self.full_stop()
        
    def closeEvent(self, event):
        self.full_stop(); event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())