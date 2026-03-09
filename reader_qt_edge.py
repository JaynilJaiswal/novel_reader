import sys
import os
import asyncio
import queue
import sounddevice as sd
import soundfile as sf
import io
import hashlib
import glob
import logging

# Configure the global logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s'
)

import json
import re  # NEW: Required for precise word highlighting
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QTextEdit, QPushButton, QComboBox, QHBoxLayout,
                             QFileDialog, QMessageBox, QLabel, QSlider, QDialog,
                             QFormLayout, QFontComboBox, QSpinBox, QDialogButtonBox,
                             QColorDialog, QLineEdit)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat, QFont, QAction, QIcon, QTextDocument, QKeySequence
import edge_tts

EDGE_VOICES = [
    "en-US-AriaNeural", "en-US-AnaNeural", "en-US-BrianNeural",
    "en-US-EmmaNeural", "en-US-JennyNeural", "en-US-GuyNeural",
    "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-MichelleNeural",
    "en-GB-SoniaNeural", "en-GB-RyanNeural"
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
        self.lines = lines; self.voice = voice; self.audio_queue = audio_queue
        self.speed_rate = speed_rate; self._is_running = True
        
        # Initialize a specific logger for this class
        self.logger = logging.getLogger("EdgeSynthCache")
        
        # Ensure cache directory exists
        self.cache_dir = os.path.expanduser("~/.cache/edge-qt/audio")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.clean_lru_cache() # Run cleanup on startup
        
    def clean_lru_cache(self, max_files=200):
        """Keep cache from growing infinitely. Deletes oldest files if over limit."""
        try:
            files = glob.glob(os.path.join(self.cache_dir, "*.wav"))
            if len(files) > max_files:
                self.logger.info(f"LRU Cache limit exceeded ({len(files)} files). Cleaning up...")
                # Sort by last accessed time (oldest first)
                files.sort(key=os.path.getatime)
                for old_wav in files[:-max_files]:
                    os.remove(old_wav)
                    old_json = old_wav.replace('.wav', '.json')
                    if os.path.exists(old_json): os.remove(old_json)
                self.logger.info("Cache cleanup complete.")
        except Exception as e: 
            self.logger.error(f"Cache cleanup failed: {e}")

    def run(self):
        try: asyncio.run(self.async_run())
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
            
            # 1. Generate unique hash
            hash_string = f"{line.strip()}_{self.voice}_{self.speed_rate}".encode('utf-8')
            hash_id = hashlib.md5(hash_string).hexdigest()
            
            wav_path = os.path.join(self.cache_dir, f"{hash_id}.wav")
            json_path = os.path.join(self.cache_dir, f"{hash_id}.json")
            
            # 2. CACHE HIT
            if os.path.exists(wav_path) and os.path.exists(json_path):
                try:
                    data, samplerate = sf.read(wav_path, dtype='float32')
                    with open(json_path, 'r') as f: boundaries = json.load(f)
                    os.utime(wav_path, None) 
                    
                    self.logger.info(f"Cache HIT  | Line {i}")
                    
                    # --- FIX: Safe queue insertion with timeout ---
                    item = {'index': i, 'data': data, 'samplerate': samplerate, 'boundaries': boundaries}
                    while self._is_running:
                        try:
                            self.audio_queue.put(item, timeout=0.1)
                            break
                        except queue.Full: pass
                    continue 
                except Exception as e:
                    self.logger.warning(f"Cache read error: {e}. Falling back to network.")

            # 3. CACHE MISS
            try:
                self.logger.info(f"Cache MISS | Line {i} | Downloading...")
                comm = edge_tts.Communicate(line, self.voice, rate=self.speed_rate)
                audio_bytes = b""
                boundaries = [] 
                
                async for chunk in comm.stream():
                    if not self._is_running: break 
                    if chunk["type"] == "audio":
                        audio_bytes += chunk["data"]
                    elif chunk["type"] == "WordBoundary":
                        time_sec = chunk["offset"] / 10_000_000.0
                        boundaries.append({'time': time_sec, 'text': chunk['text']})
                
                if not self._is_running: break
                
                if audio_bytes:
                    data, samplerate = sf.read(io.BytesIO(audio_bytes), dtype='float32')
                    
                    if len(boundaries) == 0:
                        import re
                        total_duration = len(data) / samplerate
                        total_chars = max(1, len(line))
                        for match in re.finditer(r"\b[\w']+\b", line):
                            time_sec = (match.start() / total_chars) * total_duration
                            boundaries.append({'time': time_sec, 'text': match.group()})
                    
                    sf.write(wav_path, data, samplerate)
                    with open(json_path, 'w') as f: json.dump(boundaries, f)
                            
                    # --- FIX: Safe queue insertion with timeout ---
                    item = {'index': i, 'data': data, 'samplerate': samplerate, 'boundaries': boundaries}
                    while self._is_running:
                        try:
                            self.audio_queue.put(item, timeout=0.1)
                            break
                        except queue.Full: pass
                    
            except Exception as e:
                self.logger.error(f"Edge-TTS synthesis error on line '{line}': {e}")
                continue
                
        await asyncio.sleep(0.1)
        
    def stop(self): 
        self._is_running = False

class AudioPlaybackWorker(QObject):
    playback_finished = pyqtSignal()
    highlight_line = pyqtSignal(int)
    highlight_word = pyqtSignal(int, int, str) # NEW: line_index, word_index, word_text
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
                boundaries = item.get('boundaries', []) # Get the word boundaries
                
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
                frames_played = 0
                current_word_idx = 0
                
                for i in range(0, len(audio_data), chunk_size):
                    if not self._is_running: break
                    chunk = audio_data[i:i + chunk_size]
                    
                    # --- NEW: Word boundary synchronization ---
                    current_time = frames_played / samplerate
                    # Peek ahead: if the current 50ms block hits a boundary, emit the signal
                    while current_word_idx < len(boundaries) and current_time >= boundaries[current_word_idx]['time']:
                        self.highlight_word.emit(original_line_index, current_word_idx, boundaries[current_word_idx]['text'])
                        current_word_idx += 1
                        
                    try: self.stream.write(chunk * self.volume) 
                    except Exception as e: print(f"Stream write error: {e}"); break
                    
                    frames_played += len(chunk)
                
                if self._is_running: self.line_completed.emit(original_line_index)

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
        
        self.last_word_cursor = None # Track the currently highlighted word
        self.current_line_search_pos = 0 # Track position to handle duplicate words natively
        
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

        # --- NEW: Inline Search Bar ---
        self.search_widget = QWidget()
        search_layout = QHBoxLayout(self.search_widget)
        search_layout.setContentsMargins(0, 0, 0, 0)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find...")
        self.search_next_btn = QPushButton("↓ Next")
        self.search_prev_btn = QPushButton("↑ Prev")
        self.search_close_btn = QPushButton("✖")
        self.search_close_btn.setFixedSize(24, 24)
        
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_prev_btn)
        search_layout.addWidget(self.search_next_btn)
        search_layout.addWidget(self.search_close_btn)
        
        layout.addWidget(self.search_widget)
        self.search_widget.setVisible(False) # Hidden by default
        
        # Connect search signals
        self.search_input.returnPressed.connect(self.find_next)
        self.search_next_btn.clicked.connect(self.find_next)
        self.search_prev_btn.clicked.connect(self.find_prev)
        self.search_close_btn.clicked.connect(self.hide_search_bar)
        self.search_input.textChanged.connect(self.find_next) # Search as you type
        
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

        self.find_action = QAction(self)
        self.find_action.setShortcut(QKeySequence("Ctrl+F"))
        self.find_action.triggered.connect(self.show_search_bar)
        self.addAction(self.find_action)

    def eventFilter(self, source, event):
        if event.type() == event.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            if self.search_widget.isVisible():
                self.hide_search_bar()
                return True

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
            cursor = self.text_edit.textCursor()
            self.current_line_index = cursor.blockNumber()
            full_text = self.text_edit.toPlainText()
            self.lines = [line for line in full_text.splitlines()]
            self.line_word_counts = [len(line.split()) for line in self.lines]
            
            # --- NEW: Look-Ahead Eraser ---
            # If we start playing, ensure all upcoming text is reset to white (unread).
            # This prevents old yellow text from remaining if we click backward in the document.
            reset_cursor = QTextCursor(self.text_edit.document().findBlockByNumber(self.current_line_index))
            reset_cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self.settings["text_color"]))
            fmt.setBackground(Qt.GlobalColor.transparent)
            reset_cursor.mergeCharFormat(fmt)
            
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
        self.audio_player.highlight_word.connect(self.update_word_highlight)
        self.audio_player.line_completed.connect(self.mark_line_as_completed)
        self.synth_worker.error.connect(self.show_error)
        self.audio_player.playback_finished.connect(self.on_playback_finished)
        
        self.playback_thread.started.connect(self.audio_player.run)
        self.synth_thread.started.connect(self.synth_worker.run)
        self.playback_thread.start()
        self.synth_thread.start()
        
        self.playback_state = "playing"
        self.play_button.setText("⏸ Pause")
        self.stop_button.setEnabled(True)
        self.text_edit.setReadOnly(True)

    # --- NEW: Core UI Engine for precise word targeting ---
    def update_word_highlight(self, line_index, word_index, word_text):
        if self.current_line_index != line_index: return

        block = self.text_edit.document().findBlockByNumber(line_index)
        if not block.isValid(): return
        
        # Reset search tracker if it's the start of a new line
        if word_index == 0:
            self.current_line_search_pos = 0

        # 1. Transform the previous word into the 'completed' state
        if self.last_word_cursor:
            fmt = QTextCharFormat()
            # Remove the blue background
            fmt.setBackground(Qt.GlobalColor.transparent) 
            # --- FIX: Set the text to your completed color (Yellow) ---
            fmt.setForeground(QColor(self.settings["completed_color"])) 
            self.last_word_cursor.mergeCharFormat(fmt)
            self.last_word_cursor = None

        text = block.text()
        clean_word = word_text.strip(".,!?\"';:()[]{} ")
        if not clean_word: return
        
        # 2. Progressive Search
        escaped_word = re.escape(clean_word)
        match = re.search(escaped_word, text[self.current_line_search_pos:], re.IGNORECASE)

        if match:
            start = self.current_line_search_pos + match.start()
            end = self.current_line_search_pos + match.end()
            self.current_line_search_pos = end # Save position for next word

            cursor = QTextCursor(block)
            cursor.setPosition(block.position() + start)
            cursor.setPosition(block.position() + end, QTextCursor.MoveMode.KeepAnchor)

            # 3. Paint the active word (Blue BG, White Text)
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(self.settings["highlight_color"]))
            fmt.setForeground(QColor(self.settings["text_color"]))
            cursor.mergeCharFormat(fmt)
            
            self.last_word_cursor = cursor

    def mark_line_as_completed(self, line_index):
        if self.last_highlighted_block and self.last_highlighted_block.blockNumber() == line_index:
            temp_cursor = QTextCursor(self.last_highlighted_block)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self.settings["completed_color"]))
            fmt.setBackground(Qt.GlobalColor.transparent)
            temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            temp_cursor.mergeCharFormat(fmt)
            self.last_highlighted_block = None 
            
            # Ensure the last word highlight dies with the line
            if hasattr(self, 'last_word_cursor') and self.last_word_cursor:
                self.last_word_cursor.setCharFormat(fmt)
                self.last_word_cursor = None
            
        self.playback_cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
        if line_index < len(self.line_word_counts): 
            self.words_remaining -= self.line_word_counts[line_index]
        self.update_eta()

    def update_highlight(self, line_index):
        self.clear_highlight()
        self.current_line_index = line_index
        
        if self.playback_cursor.blockNumber() != line_index:
            self.playback_cursor = QTextCursor(self.text_edit.document().findBlockByNumber(line_index))
            
        block = self.playback_cursor.block()
        if block.isValid():
            self.last_highlighted_block = block # Save for completion coloring later
            
            # --- Auto-Scroll Logic (No background coloring here anymore) ---
            cursor_rect = self.text_edit.cursorRect(self.playback_cursor)
            viewport_height = self.text_edit.viewport().height()
            
            if cursor_rect.bottom() > (viewport_height * 0.7) or cursor_rect.top() < (viewport_height * 0.3):
                scrollbar = self.text_edit.verticalScrollBar()
                center_offset = cursor_rect.top() - (viewport_height // 2)
                scrollbar.setValue(scrollbar.value() + center_offset)
                
        self.update_eta()
        
    def clear_highlight(self, force_clear_all=False):
        clear_format = QTextCharFormat()
        clear_format.setBackground(QColor(self.settings["bg_color"]))
        clear_format.setForeground(QColor(self.settings["text_color"]))
        
        current_visible_cursor = self.text_edit.textCursor()
        
        # Wipe out any orphaned word highlights before clearing the line
        if hasattr(self, 'last_word_cursor') and self.last_word_cursor:
            self.last_word_cursor.mergeCharFormat(clear_format)
            self.last_word_cursor = None
            
        if force_clear_all:
             temp_cursor = QTextCursor(self.text_edit.document())
             temp_cursor.select(QTextCursor.SelectionType.Document)
             temp_cursor.mergeCharFormat(clear_format)
             
        self.last_highlighted_block = None
        self.text_edit.setTextCursor(current_visible_cursor)

    def update_eta(self, *args):
        if self.playback_state in ["playing", "paused"] and hasattr(self, 'words_remaining'):
            word_count = max(0, self.words_remaining); prefix = "Time Left: "
        else:
            text = self.text_edit.toPlainText(); word_count = len(text.split()); prefix = "Total ETA: "
            
        base_wpm = 165.0; speed_multiplier = self.speed_slider.value() / 10.0; adjusted_wpm = base_wpm * speed_multiplier
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
        
    def update_speed_label(self, value): self.speed_label.setText(f"{value / 10.0:.1f}x")
    def update_volume_label(self, value): self.volume_label.setText(f"{value}%")
    def toggle_playback(self):
        if self.playback_state == "playing": self.pause_audio()
        else: self.play_audio()
    def pause_audio(self):
        self.playback_state = "paused"; self.play_button.setText("▶ Resume")
        self.stop_threads(reset_highlight=False)
    def full_stop(self):
        self.playback_state = "stopped"
        self.play_button.setText("▶ Play")
        
        # 1. Stop threads safely WITHOUT wiping the entire document's formatting
        self.stop_threads(reset_highlight=False)
        
        # 2. Lock in the 'completed' yellow color for the exact word we stopped on
        fmt = QTextCharFormat()
        fmt.setBackground(Qt.GlobalColor.transparent)
        fmt.setForeground(QColor(self.settings["completed_color"]))
        
        if hasattr(self, 'last_word_cursor') and self.last_word_cursor:
            self.last_word_cursor.mergeCharFormat(fmt)
            self.last_word_cursor = None
            
        # Clear any leftover background highlight from the active block, keeping text yellow
        if hasattr(self, 'last_highlighted_block') and self.last_highlighted_block:
            temp_cursor = QTextCursor(self.last_highlighted_block)
            temp_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            clear_bg = QTextCharFormat()
            clear_bg.setBackground(Qt.GlobalColor.transparent)
            temp_cursor.mergeCharFormat(clear_bg)
            self.last_highlighted_block = None

        # 3. Move the physical UI cursor exactly to the line we stopped at
        cursor = QTextCursor(self.text_edit.document().findBlockByNumber(self.current_line_index))
        self.text_edit.setTextCursor(cursor)
        
        self.lines = []
        self.update_eta()
    def on_playback_finished(self): 
        if self.playback_state == "playing": self.full_stop()
    def stop_threads(self, reset_highlight=False):
        # 1. Safely signal audio player to stop
        if hasattr(self, 'audio_player') and getattr(self, 'audio_player', None) is not None:
            try: self.audio_player.playback_finished.disconnect(self.on_playback_finished)
            except Exception: pass
            self.audio_player.stop()
            
        # 2. Safely signal synth worker to stop
        if hasattr(self, 'synth_worker') and getattr(self, 'synth_worker', None) is not None:
            self.synth_worker.stop()
            
        # 3. Wait for audio thread to close
        if hasattr(self, 'playback_thread') and getattr(self, 'playback_thread', None) is not None:
            self.playback_thread.quit()
            self.playback_thread.wait()
            if hasattr(self, 'audio_player'):
                self.audio_player.deleteLater()
                del self.audio_player
            self.playback_thread.deleteLater()
            del self.playback_thread
            
        # 4. Wait for synth thread to close
        if hasattr(self, 'synth_thread') and getattr(self, 'synth_thread', None) is not None:
            self.synth_thread.quit()
            self.synth_thread.wait()
            if hasattr(self, 'synth_worker'):
                self.synth_worker.deleteLater()
                del self.synth_worker
            self.synth_thread.deleteLater()
            del self.synth_thread
            
        if reset_highlight: self.clear_highlight(force_clear_all=True)
        self.text_edit.setReadOnly(False)
        self.stop_button.setEnabled(False)
        
    def show_error(self, message): QMessageBox.critical(self, "Error", message); self.full_stop()
    def closeEvent(self, event): self.save_settings(); self.full_stop(); event.accept()

    # --- NEW: Find Feature Methods ---
    def show_search_bar(self):
        self.search_widget.setVisible(True)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def hide_search_bar(self):
        self.search_widget.setVisible(False)
        self.text_edit.setFocus()

    def find_next(self):
        text = self.search_input.text()
        if not text: return
        
        # Standard forward search
        found = self.text_edit.find(text)
        
        # If it reaches the bottom, wrap around to the top
        if not found:
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.find(text)

    def find_prev(self):
        text = self.search_input.text()
        if not text: return
        
        # Search backwards
        options = QTextDocument.FindFlag.FindBackward
        found = self.text_edit.find(text, options)
        
        # If it reaches the top, wrap around to the bottom
        if not found:
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.find(text, options)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())