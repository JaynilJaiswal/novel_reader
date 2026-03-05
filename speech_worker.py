import speechd
from PyQt6.QtCore import QObject, pyqtSignal

class SpeechWorker(QObject):
    """
    This worker runs in a separate thread and handles all communication
    with the speech-dispatcher daemon using its callback-based API.
    """
    # Signal: (line_index) - Emitted AFTER a line is finished speaking
    line_completed = pyqtSignal(int)
    
    # Signal: () - Emitted when the entire queue is finished
    playback_finished = pyqtSignal()
    
    # Signal: (str) - For sending errors to the GUI
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.client = None
        self.lines_to_speak = []
        self.current_line_index = 0
        self.line_index_offset = 0
        self._is_running = False

    def setup(self):
        """Initializes the connection to speech-dispatcher."""
        try:
            self.client = speechd.client.SSIPClient('Piper-Qt')
            self.client.set_punctuation(speechd.PunctuationMode.NONE)
            # We need these notifications for our per-message callbacks
            # self.client.set_notification_on(speechd.Notification.END, True)
            # self.client.set_notification_on(speechd.Notification.CANCEL, True)
        except Exception as e:
            self.error.emit(f"Failed to connect to speech-dispatcher: {e}")
            self.client = None

    def list_voices(self):
        """Returns a list of available voices from speech-dispatcher."""
        if self.client:
            return self.client.list_synthesis_voices()
        return []

    def set_voice(self, voice_name):
        if self.client: self.client.set_synthesis_voice(voice_name)

    def set_speed(self, speed_value):
        """Sets the speech rate. speechd uses a range of -100 to 100."""
        # Maps our slider 5-20 (0.5x to 2.0x) to speechd's -100 to 100
        # 10 (1.0x) maps to 0. 20 (2.0x) maps to 100. 5 (0.5x) maps to -50.
        rate = int((speed_value - 10) * (100 / 10)) 
        if self.client: self.client.set_rate(rate)

    def set_volume(self, volume_value):
        """Sets the speech volume. speechd uses a range of -100 to 100."""
        # Maps 0-100 to -100 to 0 (since 100 is often too loud)
        # Let's map 0-100 to -100 to 100. 50 is the new 'normal' (0).
        volume = int((volume_value - 50) * 2)
        if self.client: self.client.set_volume(volume)

    def play(self, lines, start_index):
        """Starts speaking a list of lines from a specific index."""
        if not self.client: return
        
        self.lines_to_speak = lines
        self.current_line_index = 0 # This is the index *of the lines_to_play list*
        self.line_index_offset = start_index # This is the original index in the main document
        self._is_running = True
        
        self.speak_next_line()

    def speak_next_line(self):
        """Internal method to speak the next line in the queue."""
        if not self._is_running:
            self.playback_finished.emit()
            return
            
        if self.current_line_index < len(self.lines_to_speak):
            line = self.lines_to_speak[self.current_line_index]
            
            if line.strip():
                # self.on_line_finished will be called when this line ends
                self.client.speak(line, callback=self.on_line_finished)
            else:
                # If the line is blank, mark it as "completed" immediately
                self.line_completed.emit(self.line_index_offset + self.current_line_index)
                self.current_line_index += 1
                self.speak_next_line() # Immediately speak the next line
        else:
            # We've finished all lines
            self.playback_finished.emit()
            
    def on_line_finished(self, type, **kwargs):
        """Callback function passed to self.client.speak()"""
        if not self._is_running: return

        if type == speechd.CallbackType.END:
            # Report the original line index as completed
            self.line_completed.emit(self.line_index_offset + self.current_line_index)
            # Move to the next line
            self.current_line_index += 1
            self.speak_next_line()
        elif type == speechd.CallbackType.CANCEL:
            # This happens if stop() is called
            self.playback_finished.emit()

    def pause(self):
        """Pauses the current speech."""
        if self.client: self.client.pause()

    def resume(self):
        """Resumes the current speech."""
        if self.client: self.client.resume()

    def stop(self):
        """Stops speech and clears the internal queue."""
        self._is_running = False
        if self.client:
            self.client.cancel()
        self.lines_to_speak = []
        self.current_line_index = 0
            
    def close(self):
        """Closes the connection to speech-dispatcher."""
        if self.client:
            self.client.close()