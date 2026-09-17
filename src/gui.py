from __future__ import annotations
import queue
import sys
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QLockFile
from PySide6.QtGui import QDesktopServices, QFontDatabase, QIcon
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QCheckBox, QSpinBox, QFileDialog, QMessageBox,
    QDialog, QProgressBar, QFrame, QTreeWidget, QTreeWidgetItem, QHeaderView, QScrollArea)
from core import ensure_dirs, atomic_json, read_json, new_note, save_note, timestamp, discard_pending
from models import ready, MODELS
from job_queue import JobQueue, ACTIVE, RETRYABLE, source_of

STYLE = '''
QWidget { background: #f8fafb; color: #233441; font-family: ".AppleSystemUIFont", "Helvetica Neue", sans-serif; font-size: 14px; }
QLabel#brand { color: #177d70; font-size: 21px; font-weight: 650; }
QLabel#muted { color: #7a8993; font-size: 12px; }
QLabel#timer { font-size: 54px; font-weight: 300; color: #233d43; }
QLabel#state { color: #598279; font-size: 13px; }
QFrame#card { background: white; border: 1px solid #e0e7eb; border-radius: 12px; }
QFrame#card QLabel { background: white; }
QPushButton { background: white; border: 1px solid #d6e0e4; border-radius: 9px; padding: 10px 18px; }
QPushButton:hover { background: #eff7f4; border-color: #75aca2; }
QPushButton:disabled { color: #a4adb3; border-color: #e5e9ec; background: #f4f6f7; }
QPushButton#record { background: #fff5f3; border-color: #f0d7d1; color: #b55748; }
QPushButton#record:disabled { background: #f4f6f7; border-color: #e5e9ec; color: #a4adb3; }
QPushButton#save { background: #197e6e; border-color: #197e6e; color: white; font-weight: 600; }
QPushButton#save:disabled { background: #b7d1ca; border-color: #b7d1ca; }
QPushButton#link { background: transparent; border: none; color: #69808a; padding: 4px 8px; font-size: 12px; }
QPushButton#link:disabled { color: #b0bbc0; }
QTreeWidget { background: white; border: 1px solid #d8e2e7; border-radius: 8px; padding: 4px; outline: 0; }
QHeaderView::section { background: #f0f5f3; color: #69808a; border: none; padding: 6px 8px; font-size: 12px; }
QTreeWidget::item { padding: 5px 8px; }
QTreeWidget::item:selected { background: #dff1eb; color: #1b5f53; }
QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #d8e2e7; border-radius: 7px; padding: 8px; }
QProgressBar { background: #e3ece9; border: none; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #32917d; border-radius: 3px; }
QCheckBox { spacing: 8px; }
'''


def text(value, role='muted'):
    label = QLabel(value)
    label.setObjectName(role)
    label.setWordWrap(True)
    return label


def button(value, callback, role=''):
    result = QPushButton(value)
    result.setObjectName(role)
    result.clicked.connect(callback)
    return result


class Recorder(threading.Thread):
    """Stream to disk; paused audio is excluded from the saved recording."""
    def __init__(self, path, device=None):
        super().__init__(daemon=True)
        self.path, self.device = path, device
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.frames = queue.Queue(maxsize=1024)
        self.error = ''
        self.started = False
        self.level = 0
        self.samples_written = 0
        self.rate = 48000

    @property
    def duration(self):
        return self.samples_written / self.rate

    def accept_audio(self, data, status=None):
        if self.paused.is_set() or self.stop_event.is_set():
            self.level = 0
            return
        if status:
            self.error = 'Audio input was interrupted. The recording so far has been preserved.'
            self.stop_event.set()
            return
        try:
            import numpy as np
            block = bytes(data)
            self.frames.put_nowait(block)
            samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
            self.level = min(100, int(float(np.max(np.abs(samples))) / 32768 * 180)) if len(samples) else 0
        except queue.Full:
            self.error = 'Recording stopped because the disk could not keep up. Recorded audio is preserved.'
            self.stop_event.set()

    def run(self):
        try:
            import sounddevice as sd
            self.rate = int(sd.query_devices(self.device, 'input')['default_samplerate'])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(self.path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(self.rate)
                with sd.RawInputStream(device=self.device, samplerate=self.rate, channels=1,
                    dtype='int16', blocksize=2048,
                    callback=lambda data, count, timing, status: self.accept_audio(data, status)):
                    self.started = True
                    while not self.stop_event.is_set():
                        try:
                            block = self.frames.get(timeout=0.1)
                            output.writeframesraw(block)
                            self.samples_written += len(block) // 2
                        except queue.Empty:
                            pass
                while not self.frames.empty():
                    block = self.frames.get_nowait()
                    output.writeframesraw(block)
                    self.samples_written += len(block) // 2
        except Exception as exc:
            self.error = f'Could not record audio. Check your microphone permission and input device.\n{exc}'


class Options(QDialog):
    def __init__(self, parent):
        super().__init__(parent.save_dialog if parent.save_dialog.isVisible() else parent)
        self.owner = parent
        self.setWindowTitle('Options')
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24,24,24,24)
        layout.setSpacing(10)
        layout.addWidget(text('Recording & transcription', 'brand'))
        layout.addWidget(text(('Recording continues. ' if parent.recording else '')+
            'Transcription settings apply to the whole recording when you press Save.'))
        layout.addWidget(text('Microphone'))
        self.device = QComboBox()
        self.device.addItem('System default', None)
        if parent.recording:
            if parent.device is not None:
                self.device.addItem('Current recording input', parent.device)
        else:
            try:
                import sounddevice as sd
                for i,d in enumerate(sd.query_devices()):
                    if d['max_input_channels'] > 0:
                        self.device.addItem(d['name'],i)
            except Exception:
                pass
        self.device.setCurrentIndex(max(0,self.device.findData(parent.device)))
        self.device.setEnabled(not bool(parent.recording))
        if parent.recording:
            self.device.setToolTip('The microphone can be changed after Stop.')
        layout.addWidget(self.device)
        layout.addWidget(text('Language'))
        self.language = QComboBox()
        for name,code in [('한국어','ko'),('English','en'),('Auto-detect','auto')]:
            self.language.addItem(name,code)
        self.language.setCurrentIndex(max(0,self.language.findData(parent.language)))
        layout.addWidget(self.language)
        layout.addWidget(text('Transcription model'))
        self.model = QComboBox()
        self.model.addItem('Fast · Large v3 Turbo','turbo')
        self.model.addItem('Accurate · Large v3','large')
        self.model.setCurrentIndex(max(0,self.model.findData(parent.model)))
        layout.addWidget(self.model)
        row=QHBoxLayout()
        self.diar=QCheckBox('Speaker labels')
        self.diar.setChecked(parent.diar)
        row.addWidget(self.diar)
        self.speakers=QSpinBox()
        self.speakers.setRange(0,30)
        self.speakers.setSpecialValueText('Auto')
        self.speakers.setSuffix(' speakers')
        self.speakers.setValue(parent.speakers)
        row.addWidget(self.speakers)
        layout.addLayout(row)
        self.prompt=QLineEdit(parent.prompt)
        layout.addWidget(text('For a lecture, turn Speaker labels off. TXT is grouped by sentence.'))
        self.prompt.setMaxLength(800)
        self.prompt.setPlaceholderText('Vocabulary hints (optional)')
        layout.addWidget(self.prompt)
        layout.addWidget(text('Stop recording to download a model.' if parent.recording else
            'Download models once. Recordings stay on this Mac.'))
        for key, name in [('turbo','Fast model · 1.6 GB'),('large','Accurate model · 3.1 GB'),('diarization','Speaker labels · 47 MB')]:
            row=QHBoxLayout()
            row.addWidget(QLabel(name),1)
            action=button('Ready' if ready(key) else 'Download', lambda checked=False,k=key:self.download(k))
            action.setEnabled(not ready(key) and not parent.recording)
            row.addWidget(action)
            layout.addLayout(row)
        layout.addWidget(button('Done',self.commit,'save'))

    def commit(self):
        parent=self.owner
        if not parent.recording:
            parent.device=self.device.currentData()
        parent.language=self.language.currentData()
        parent.model=self.model.currentData()
        parent.diar=self.diar.isChecked()
        parent.speakers=self.speakers.value()
        parent.prompt=self.prompt.text()
        parent.recording_mode='lecture' if not parent.diar or parent.speakers==1 else 'meeting'
        parent.save_settings()
        parent.refresh_summary()
        self.accept()

    def download(self,key):
        if self.owner.recording:
            return
        self.commit()
        self.owner.start_job(dict(kind='download',model=key))


class RecordSetup(QDialog):
    """Choosing a model never downloads it or starts transcription."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('New recording')
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24,24,24,24)
        layout.setSpacing(14)
        layout.addWidget(text('New recording', 'brand'))
        layout.addWidget(text('Choose initial settings. You can change them in Options while recording or after Stop.'))
        layout.addWidget(text('Language'))
        self.language = QComboBox()
        for name, code in [('한국어','ko'), ('English','en'), ('Auto-detect','auto')]:
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(parent.language)))
        layout.addWidget(self.language)
        layout.addWidget(text('Transcription model'))
        self.model = QComboBox()
        for key, name in [('turbo','Fast · Large v3 Turbo'), ('large','Accurate · Large v3')]:
            self.model.addItem(name, key)
        self.model.setCurrentIndex(max(0, self.model.findData(parent.model)))
        layout.addWidget(self.model)
        self.hint = text('')
        layout.addWidget(self.hint)
        self.model.currentIndexChanged.connect(self.refresh_hint)
        self.refresh_hint()
        layout.addWidget(text('Speakers'))
        self.speaker_mode = QComboBox()
        self.speaker_mode.addItem('Single speaker / lecture', 'lecture')
        self.speaker_mode.addItem('Multiple speakers / meeting', 'meeting')
        self.speaker_mode.setCurrentIndex(max(0, self.speaker_mode.findData(parent.recording_mode)))
        layout.addWidget(self.speaker_mode)
        layout.addWidget(text('Lecture: no speaker splitting. TXT is grouped by sentence.'))
        layout.addWidget(text('After Stop, choose audio only, audio + transcript, or Discard.'))
        row = QHBoxLayout()
        row.addWidget(button('Cancel', self.reject))
        self.start_button = button('Start recording', self.accept, 'record')
        self.start_button.setDefault(True)
        row.addWidget(self.start_button)
        layout.addLayout(row)

    def refresh_hint(self):
        self.hint.setText('Model ready for offline transcription.' if ready(self.model.currentData()) else
            'Model not downloaded. You can record and save audio now. Download it in Options to transcribe later.')


class Window(QWidget):
    def __init__(self,demo=False):
        super().__init__()
        self.root=ensure_dirs()
        settings=read_json(self.root/'settings.json',{})
        self.device=None  # Device indices may change after reconnecting a microphone.
        self.language=settings.get('language','ko')
        self.model=settings.get('model','turbo')
        self.diar=settings.get('diar',True)
        self.speakers=settings.get('speakers',0)
        self.prompt=settings.get('prompt','')
        self.recording_mode=settings.get('recording_mode','lecture')
        self.recording=None
        self.queue=JobQueue(self.root, recover=not demo)
        self.queue_rows={}
        self.completed_ids=set()
        self.last_output_folder=''
        self.source=''
        self.saved=False
        self.saved_paths=[]
        self.demo=demo
        self.setWindowTitle('SoriTaker')
        self.setMinimumSize(600,660)
        self.resize(660,820)
        self.setAcceptDrops(True)
        outer=QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body=QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        layout=QVBoxLayout(body)
        layout.setContentsMargins(26,22,26,20)
        layout.setSpacing(18)
        header=QHBoxLayout()
        header.addWidget(text('SoriTaker','brand'),1)
        self.options_button=button('Options',self.open_options,'link')
        header.addWidget(self.options_button)
        layout.addLayout(header)
        layout.addWidget(text('Record the next conversation while earlier audio is transcribed.'))
        card=QFrame()
        card.setObjectName('card')
        inner=QVBoxLayout(card)
        inner.setContentsMargins(24,27,24,24)
        inner.setSpacing(15)
        self.state=text('READY TO RECORD','state')
        self.state.setAlignment(Qt.AlignCenter)
        inner.addWidget(self.state)
        self.clock=text('00:00:00','timer')
        self.clock.setAlignment(Qt.AlignCenter)
        inner.addWidget(self.clock)
        self.meter=QProgressBar()
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(5)
        self.meter.setValue(0)
        inner.addWidget(self.meter)
        controls=QHBoxLayout()
        self.record_button=button('●  Record',self.record,'record')
        self.pause_button=button('Pause',self.pause)
        self.stop_button=button('■  Stop',self.stop)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.record_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.stop_button)
        inner.addLayout(controls)
        layout.addWidget(card)
        self.summary=text('')
        layout.addWidget(self.summary)
        self.save_dialog=QDialog(self)
        self.save_dialog.setWindowTitle('Save recording')
        self.save_dialog.setMinimumWidth(500)
        save_layout=QVBoxLayout(self.save_dialog)
        save_layout.setContentsMargins(24,24,24,24)
        save_layout.setSpacing(14)
        save_header=QHBoxLayout()
        save_header.addWidget(text('Save recording','brand'),1)
        save_header.addWidget(button('Options',self.open_options,'link'))
        save_layout.addLayout(save_header)
        self.save_summary=text('')
        save_layout.addWidget(self.save_summary)
        self.save_panel=QFrame()
        dest=QVBoxLayout(self.save_panel)
        dest.setContentsMargins(0,0,0,0)
        dest.setSpacing(8)
        dest.addWidget(text('SAVE TO'))
        row=QHBoxLayout()
        self.folder=QLineEdit(settings.get('folder',str(Path.home()/'Documents'/'SoriTaker')))
        self.folder.setReadOnly(True)
        row.addWidget(self.folder,1)
        self.browse=button('Choose…',self.choose_folder)
        row.addWidget(self.browse)
        dest.addLayout(row)
        self.filename=QLineEdit()
        self.filename.setPlaceholderText('File name')
        dest.addWidget(self.filename)
        row=QHBoxLayout()
        row.addWidget(text('Save as'))
        self.save_mode=QComboBox()
        self.save_mode.addItem('Audio + transcript (.txt)', 'transcribe')
        self.save_mode.addItem('Audio only (no transcription)', 'save_audio')
        self.save_mode.currentIndexChanged.connect(self.refresh_save_mode)
        row.addWidget(self.save_mode,1)
        dest.addLayout(row)
        self.output_hint=text('Audio + transcript (.txt) will be saved together.')
        dest.addWidget(self.output_hint)
        save_layout.addWidget(self.save_panel)
        self.save_panel.hide()
        save_layout.addWidget(text('Cancel keeps the recording on this Mac so you can save it later.'))
        save_actions=QHBoxLayout()
        self.discard_button=button('Discard',self.discard,'link')
        self.discard_button.hide()
        save_actions.addWidget(self.discard_button)
        save_actions.addStretch()
        self.save_cancel=button('Cancel',self.save_dialog.reject)
        self.save_cancel.setAutoDefault(False)
        self.discard_button.setAutoDefault(False)
        save_actions.addWidget(self.save_cancel)
        self.save_button=button('Save',self.save,'save')
        self.save_button.setDefault(True)
        self.save_button.setMinimumWidth(125)
        self.save_button.setEnabled(False)
        save_actions.addWidget(self.save_button)
        save_layout.addLayout(save_actions)
        self.save_dialog.rejected.connect(self.save_dialog_closed)
        self.queue_panel=QFrame()
        queue_layout=QVBoxLayout(self.queue_panel)
        queue_layout.setContentsMargins(0,8,0,0)
        queue_layout.setSpacing(8)
        self.queue_summary=text('QUEUE','state')
        queue_layout.addWidget(self.queue_summary)
        self.queue_view=QTreeWidget()
        self.queue_view.setColumnCount(3)
        self.queue_view.setHeaderLabels(['File / task','Status','Progress'])
        self.queue_view.setRootIsDecorated(False)
        self.queue_view.setMinimumHeight(105)
        self.queue_view.setMaximumHeight(145)
        self.queue_view.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.queue_view.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.queue_view.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.queue_view.itemSelectionChanged.connect(self.refresh_queue_actions)
        queue_layout.addWidget(self.queue_view)
        self.queue_detail=text('')
        self.queue_detail.setMinimumHeight(30)
        self.queue_detail.setMaximumHeight(44)
        queue_layout.addWidget(self.queue_detail)
        self.progress=QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.setValue(0)
        queue_layout.addWidget(self.progress)
        actions=QHBoxLayout()
        self.queue_open=button('Open folder',self.open_job_folder,'link')
        self.queue_audio=button('Use audio',self.use_job_audio,'link')
        self.queue_retry=button('Retry',self.retry_job,'link')
        self.cancel_button=button('Cancel task',self.cancel_job,'link')
        for widget in (self.queue_open,self.queue_audio,self.queue_retry,self.cancel_button):
            actions.addWidget(widget)
        actions.addStretch()
        queue_layout.addLayout(actions)
        layout.addWidget(self.queue_panel)
        self.queue_panel.hide()
        layout.addStretch(1)
        self.status=text('Press Record, or import an existing audio file.')
        layout.addWidget(self.status)
        self.completion_notice=text('')
        self.completion_notice.hide()
        layout.addWidget(self.completion_notice)
        footer=QHBoxLayout()
        self.import_button=button('Import audio',self.import_audio,'link')
        footer.addWidget(self.import_button)
        self.review_button=button('Review recording',self.open_save_dialog,'link')
        self.review_button.hide()
        footer.addWidget(self.review_button)
        footer.addStretch()
        footer.addWidget(button('Open output folder',self.open_output_folder,'link'))
        layout.addLayout(footer)
        self.timer=QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(250)
        self.refresh_summary()
        if not demo:
            pending = sorted((p for p in (self.root/'recordings').glob('*.wav') if not self.queue.owns_source(p)),
                             key=lambda p:p.stat().st_mtime, reverse=True)
            if pending and pending[0].stat().st_size > 44:
                self.set_source(str(pending[0]),show_dialog=False)
                self.status.setText('Recovered an unsaved recording. Save audio, add a transcript, or Discard.')
                QTimer.singleShot(0,self.open_save_dialog)
        self.refresh_queue()

    def refresh_summary(self):
        language={'ko':'Korean','en':'English','auto':'Auto-detect'}.get(self.language,'Korean')
        self.summary.setText(f"{language}  ·  {'Fast' if self.model=='turbo' else 'Accurate'}  ·  Speaker labels {'on' if self.diar else 'off'}")
        self.save_summary.setText(self.summary.text())

    def save_settings(self):
        atomic_json(self.root/'settings.json',dict(language=self.language,model=self.model,diar=self.diar,
            speakers=self.speakers,prompt=self.prompt,folder=self.folder.text(),recording_mode=self.recording_mode))

    def open_options(self):
        Options(self).exec()

    def choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self.save_dialog,'Save to folder',self.folder.text())
        if folder:
            self.folder.setText(folder)
            self.save_settings()

    def open_save_dialog(self):
        if not self.source or self.recording:
            return
        self.save_dialog.open()
        self.filename.setFocus()
        self.filename.selectAll()

    def save_dialog_closed(self):
        if self.source and not self.recording:
            self.status.setText('Unsaved audio is preserved. Click Review recording to save or discard it.')

    def open_output_folder(self):
        folder=Path(self.last_output_folder or self.folder.text()).expanduser()
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            QMessageBox.information(self,'Output folder','This folder will be created when you save:\n'+str(folder))

    def keep_pending(self):
        # Unsaved recordings are kept on disk even when starting another recording.
        if self.source and not self.saved and Path(self.source).parent==self.root/'recordings':
            QMessageBox.information(self,'Recording preserved',
                'The previous unsaved recording is still available at:\n'+self.source+'\n\nYou can import it later.')

    def record(self):
        if self.recording:
            return
        setup=RecordSetup(self)
        if setup.exec()!=QDialog.Accepted:
            return
        self.language=setup.language.currentData()
        self.model=setup.model.currentData()
        self.recording_mode=setup.speaker_mode.currentData()
        if self.recording_mode=='lecture':
            self.diar=False
            self.speakers=1
        else:
            self.diar=True
            if self.speakers==1:
                self.speakers=0
        self.save_settings()
        self.refresh_summary()
        self.keep_pending()
        self.source=''
        self.saved=False
        self.saved_paths=[]
        path=self.root/'recordings'/(datetime.now().strftime('Recording_%Y-%m-%d_%H-%M-%S_')+uuid.uuid4().hex[:4]+'.wav')
        self.recording=Recorder(path,self.device)
        self.recording.start()
        self.record_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.options_button.setEnabled(True)
        self.import_button.setEnabled(False)
        self.save_panel.hide()
        self.save_dialog.hide()
        self.review_button.hide()
        self.discard_button.hide()
        self.save_button.setText('Save')
        self.save_button.setEnabled(False)
        self.state.setText('RECORDING')
        self.status.setText('Recording is stored temporarily. You can change transcription settings in Options.')
        self.refresh_queue_actions()

    def pause(self):
        if not self.recording:
            return
        if self.recording.paused.is_set():
            self.recording.paused.clear()
            self.pause_button.setText('Pause')
            self.state.setText('RECORDING')
        else:
            self.recording.paused.set()
            self.pause_button.setText('Resume')
            self.state.setText('PAUSED')
            self.meter.setValue(0)

    def stop(self):
        if self.recording:
            self.recording.stop_event.set()
            self.pause_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.state.setText('FINISHING RECORDING')

    def finished_recording(self):
        rec=self.recording
        if rec is None:
            return
        self.recording=None
        self.record_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText('Pause')
        self.stop_button.setEnabled(False)
        self.options_button.setEnabled(True)
        self.import_button.setEnabled(True)
        self.meter.setValue(0)
        if rec.error:
            QMessageBox.warning(self,'Recording could not finish',rec.error+
                '\n\nSystem Settings → Privacy & Security → Microphone → allow SoriTaker.')
        if rec.path.exists() and rec.path.stat().st_size>44:
            self.set_source(str(rec.path),rec.duration)
        else:
            self.state.setText('READY TO RECORD')

    def set_source(self,path,duration=None,show_dialog=True):
        if self.queue.owns_source(path):
            self.status.setText('This audio is already in the queue. Cancel its task before using it again.')
            return
        self.source=str(Path(path).resolve())
        self.saved=False
        self.saved_paths=[]
        self.filename.setText(Path(path).stem)
        if duration is not None:
            self.clock.setText(timestamp(duration))
        else:
            self.clock.setText('Audio file')
        self.state.setText('RECORDING COMPLETE' if duration is not None else 'AUDIO READY')
        self.status.setText('Choose what to save, or Discard this recording.')
        self.save_panel.show()
        self.save_button.setText('Save')
        self.save_button.setEnabled(True)
        self.save_mode.setEnabled(True)
        self.save_mode.setCurrentIndex(0)
        self.discard_button.show()
        self.review_button.show()
        self.refresh_save_mode()
        if show_dialog:
            self.open_save_dialog()

    def refresh_save_mode(self):
        if not hasattr(self,'output_hint'):
            return
        self.output_hint.setText('Audio + transcript (.txt) will be saved together.' if self.save_mode.currentData()=='transcribe'
            else 'Only the original file will be saved. No transcription or model loading.')

    def discard(self):
        if not self.source or self.saved or self.recording:
            return
        if self.queue.owns_source(self.source):
            self.status.setText('This audio belongs to a queued task. Cancel it in the queue first.')
            return
        owned=Path(self.source).resolve().parent==(self.root/'recordings').resolve()
        message=('Delete this unsaved recording and any temporary transcript? This cannot be undone.' if owned else
            'Discard this task and any temporary transcript? The imported original file will stay where it is.')
        answer=QMessageBox.question(self.save_dialog,'Discard current recording?',message,
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:
            return
        try:
            discard_pending(self.source)
        except OSError as exc:
            QMessageBox.warning(self.save_dialog,'Could not discard',str(exc))
            return
        self.source=''
        self.saved_paths=[]
        self.save_panel.hide()
        self.discard_button.hide()
        self.save_button.setEnabled(False)
        self.review_button.hide()
        self.save_dialog.accept()
        self.state.setText('READY TO RECORD')
        self.clock.setText('00:00:00')
        self.status.setText('Recording discarded.' if owned else 'Task discarded. Your imported original is preserved.')

    def import_audio(self):
        if self.recording:
            return
        path,_=QFileDialog.getOpenFileName(self,'Import audio',str(Path.home()),
            'Audio / video (*.m4a *.mp3 *.wav *.mp4 *.mov *.flac *.aac *.ogg *.webm);;All files (*)')
        if path:
            self.keep_pending()
            self.set_source(path)

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and not self.recording:
            event.acceptProposedAction()

    def dropEvent(self,event):
        if not self.recording:
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                    self.keep_pending()
                    self.set_source(url.toLocalFile())
                    event.acceptProposedAction()
                    break

    def save(self):
        if self.saved:
            folder=str(Path(self.saved_paths[0]).parent) if self.saved_paths else self.folder.text()
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            return
        if not self.source or self.recording:
            return
        kind=self.save_mode.currentData()
        if kind=='transcribe' and (not ready(self.model) or (self.diar and self.speakers!=1 and not ready('diarization'))):
            self.status.setText('Download the selected models once in Options, then press Save again.')
            self.open_options()
            return
        title=self.filename.text().strip()
        if not title or title in ('.','..') or any(c in title for c in '/\\\x00'):
            QMessageBox.warning(self.save_dialog,'File name','Enter a file name without / or \\.')
            return
        folder=Path(self.folder.text()).expanduser()
        try:
            folder.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self.save_dialog,'Save folder',str(exc))
            return
        self.save_settings()
        spec=dict(kind=kind,export_folder=str(folder.resolve()),export_name=title)
        if kind=='transcribe':
            note=new_note(self.source,self.model,self.language,self.diar,self.speakers,self.prompt)
            note['title']=title
            spec['note']=note
        else:
            spec['source']=self.source
        self.start_job(spec)

    def release_foreground(self, message):
        self.source=''
        self.saved=False
        self.saved_paths=[]
        self.filename.clear()
        self.save_panel.hide()
        self.discard_button.hide()
        self.save_button.setEnabled(False)
        self.review_button.hide()
        self.save_dialog.accept()
        self.state.setText('READY TO RECORD')
        self.clock.setText('00:00:00')
        self.status.setText(message)

    def start_job(self,spec):
        if self.recording:
            return
        try:
            item=self.queue.enqueue(spec)
        except Exception as exc:
            QMessageBox.warning(self.save_dialog if self.save_dialog.isVisible() else self,'Could not queue task',str(exc))
            return
        if spec['kind'] in ('transcribe','save_audio'):
            self.release_foreground('Added to the queue. You can start the next recording now.')
        self.queue.start_next()
        self.refresh_queue()
        self.queue_view.setCurrentItem(self.queue_rows[item['id']])
        return item

    def selected_job(self):
        row=self.queue_view.currentItem()
        key=row.data(0,Qt.UserRole) if row else None
        return next((item for item in self.queue.items if item['id']==key),None)

    def refresh_queue(self):
        # A reported 100% can precede file export. Hide only confirmed successes.
        completed=[item for item in self.queue.items if item['state']=='complete' and item['id'] not in self.completed_ids]
        if completed:
            self.completed_ids.update(item['id'] for item in completed)
            item=completed[-1]
            spec=item['spec']
            result=item.get('result',{})
            paths=result.get('saved_paths',[])
            if paths:
                self.last_output_folder=str(Path(paths[0]).parent)
            message=('Model ready: '+MODELS[spec['model']]['name'] if spec['kind']=='download' else
                     'Saved: '+spec['export_name'])
            if result.get('warnings'):
                message+=' — '+' / '.join(result['warnings'])
            self.completion_notice.setText(message)
            self.completion_notice.setToolTip(message)
            self.completion_notice.show()
        visible=[item for item in self.queue.items if item['state']!='complete']
        visible_ids={item['id'] for item in visible}
        self.queue_view.blockSignals(True)
        for key in list(self.queue_rows):
            if key not in visible_ids:
                row=self.queue_rows.pop(key)
                self.queue_view.takeTopLevelItem(self.queue_view.indexOfTopLevelItem(row))
        running=sum(item['state'] in ('running','cancelling') for item in self.queue.items)
        waiting=sum(item['state']=='queued' for item in self.queue.items)
        self.queue_summary.setText(f'QUEUE  ·  {running} running  ·  {waiting} waiting')
        positions={item['id']:i+1 for i,item in enumerate(item for item in self.queue.items if item['state']=='queued')}
        for item in visible:
            row=self.queue_rows.get(item['id'])
            if row is None:
                row=QTreeWidgetItem(self.queue_view)
                row.setData(0,Qt.UserRole,item['id'])
                self.queue_rows[item['id']]=row
            spec=item['spec']
            title=spec.get('export_name') or ('Download '+MODELS[spec['model']]['name'])
            row.setText(0,title)
            row.setToolTip(0,title)
            state=item['state']
            label={'queued':'Waiting','running':'Processing','cancelling':'Cancelling',
                   'failed':'Failed','cancelled':'Cancelled','interrupted':'Interrupted'}[state]
            row.setText(1,f'Waiting #{positions[item["id"]]}' if state=='queued' else label)
            percent=item.get('progress')
            row.setText(2,f'{int(percent)}%' if percent is not None and state=='running' else '')
            row.setToolTip(1,item.get('message',''))
        self.queue_panel.setVisible(bool(visible))
        if visible and not self.queue_view.currentItem():
            current=self.queue.active if self.queue.active in visible else visible[0]
            self.queue_view.setCurrentItem(self.queue_rows[current['id']])
        self.queue_view.blockSignals(False)
        self.refresh_queue_actions()

    def refresh_queue_actions(self):
        item=self.selected_job()
        state=item['state'] if item else ''
        self.queue_open.setEnabled(bool(item))
        self.queue_retry.setEnabled(state in RETRYABLE)
        self.cancel_button.setEnabled(state in ('queued','running'))
        source=source_of(item['spec']) if item else ''
        self.queue_audio.setEnabled(state in RETRYABLE and bool(source) and Path(source).is_file() and
                                   not self.recording and not self.queue.owns_source(source))
        message=item.get('message','') if item else ''
        if item and item.get('result',{}).get('warnings'):
            message+=' '+ ' / '.join(item['result']['warnings'])
        self.queue_detail.setText(message)
        self.queue_detail.setToolTip(message)
        percent=item.get('progress') if item else None
        self.progress.setRange(0,0 if state in ('running','cancelling') and percent is None else 100)
        self.progress.setValue(100 if state=='complete' else int(percent or 0))

    def open_job_folder(self):
        item=self.selected_job()
        if item:
            paths=item.get('result',{}).get('saved_paths',[])
            folder=Path(paths[0]).parent if paths else self.queue.folder(item)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def use_job_audio(self):
        item=self.selected_job()
        if not item or item['state'] not in RETRYABLE or self.recording:
            return
        source=source_of(item['spec'])
        if source and Path(source).is_file() and not self.queue.owns_source(source):
            self.keep_pending()
            self.set_source(source,show_dialog=False)
            self.filename.setText(item['spec'].get('export_name',Path(source).stem))
            if item['spec'].get('export_folder'):
                self.folder.setText(item['spec']['export_folder'])
            self.open_save_dialog()

    def retry_job(self):
        item=self.selected_job()
        if not item:
            return
        try:
            self.queue.retry(item)
        except Exception as exc:
            QMessageBox.warning(self,'Could not retry',str(exc))
            return
        if self.source and self.source==source_of(item['spec']) and not self.recording:
            self.release_foreground('Task returned to the queue. You can start another recording.')
        self.queue.start_next()
        self.refresh_queue()

    def poll(self):
        if self.recording:
            self.clock.setText(timestamp(self.recording.duration))
            self.meter.setValue(self.recording.level)
            if not self.recording.is_alive():
                self.finished_recording()
        self.queue.tick()
        self.refresh_queue()

    def cancel_job(self):
        item=self.selected_job()
        if item:
            self.queue.cancel(item)
            self.refresh_queue()

    def closeEvent(self,event):
        if self.recording or any(item['state'] in ACTIVE for item in self.queue.items):
            answer=QMessageBox.question(self,'Close SoriTaker?',
                'Stop recording and pause unfinished work? Audio is preserved. Waiting tasks resume next time; interrupted tasks can be retried.',
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
            if answer!=QMessageBox.Yes:
                event.ignore()
                return
            if self.recording:
                self.recording.stop_event.set()
                self.recording.join(timeout=5)
                if self.recording.is_alive():
                    event.ignore()
                    return
            try:
                self.queue.shutdown()
            except Exception as exc:
                QMessageBox.warning(self,'Could not close',str(exc))
                event.ignore()
                return
        self.timer.stop()
        event.accept()


def run_gui(demo=False,screenshot=None):
    app=QApplication(sys.argv[:1])
    app.setApplicationName('SoriTaker')
    app.setOrganizationName('SoriTaker')
    resource_root=Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    app.setWindowIcon(QIcon(str(resource_root/'assets'/'SoriTaker.png')))
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    lock=QLockFile(str(ensure_dirs()/'application.lock'))
    lock.setStaleLockTime(0)
    if not demo and not lock.tryLock(100):
        QMessageBox.information(None,'SoriTaker','SoriTaker is already running. Open its window from the Dock.')
        return 0
    window=Window(demo)
    window.show()
    if screenshot:
        def capture():
            window.grab().save(screenshot)
            app.quit()
        QTimer.singleShot(500,capture)
    return app.exec()
