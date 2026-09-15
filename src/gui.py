from __future__ import annotations
import os
import queue
import signal
import subprocess
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
    QDialog, QProgressBar, QFrame)
from core import ensure_dirs, atomic_json, read_json, new_note, save_note, timestamp, discard_pending
from models import ready, MODELS, clear_legacy_offline_flags

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
QPushButton#save { background: #197e6e; border-color: #197e6e; color: white; font-weight: 600; }
QPushButton#save:disabled { background: #b7d1ca; border-color: #b7d1ca; }
QPushButton#link { background: transparent; border: none; color: #69808a; padding: 4px 8px; font-size: 12px; }
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
        super().__init__(parent)
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
        parent=self.parent()
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
        if self.parent().recording:
            return
        self.commit()
        self.parent().start_job(dict(kind='download',model=key))


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
        self.proc=None
        self.job_log=None
        self.job_spec=None
        self.job_folder=None
        self.source=''
        self.saved=False
        self.saved_paths=[]
        self.demo=demo
        self.setWindowTitle('SoriTaker')
        self.setMinimumSize(590,690)
        self.resize(630,720)
        self.setAcceptDrops(True)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(30,25,30,23)
        layout.setSpacing(18)
        header=QHBoxLayout()
        header.addWidget(text('SoriTaker','brand'),1)
        self.options_button=button('Options',self.open_options,'link')
        header.addWidget(self.options_button)
        layout.addLayout(header)
        layout.addWidget(text('Offline recording. Transcribe only when you choose.'))
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
        layout.addWidget(self.save_panel)
        self.save_panel.hide()
        layout.addStretch(1)
        self.status=text('Press Record, or import an existing audio file.')
        layout.addWidget(self.status)
        self.progress=QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        footer=QHBoxLayout()
        self.import_button=button('Import audio',self.import_audio,'link')
        footer.addWidget(self.import_button)
        self.cancel_button=button('Cancel',self.cancel_job,'link')
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        self.discard_button=button('Discard',self.discard,'link')
        self.discard_button.hide()
        footer.addWidget(self.discard_button)
        footer.addStretch()
        self.save_button=button('Save',self.save,'save')
        self.save_button.setMinimumWidth(125)
        self.save_button.setEnabled(False)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self.timer=QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(250)
        self.refresh_summary()
        if not demo:
            pending = sorted((self.root/'recordings').glob('*.wav'), key=lambda p:p.stat().st_mtime, reverse=True)
            if pending and pending[0].stat().st_size > 44:
                self.set_source(str(pending[0]))
                self.status.setText('Recovered an unsaved recording. Save audio, add a transcript, or Discard.')
        if demo:
            self.state.setText('RECORDING COMPLETE')
            self.clock.setText('00:42:18')
            self.filename.setText('Seminar_2026-09-14')
            self.folder.setText('~/Documents/SoriTaker')
            self.save_panel.show()
            self.save_button.setEnabled(True)
            self.discard_button.show()
            self.status.setText('Choose what to save, or Discard this recording.')

    def refresh_summary(self):
        language={'ko':'Korean','en':'English','auto':'Auto-detect'}.get(self.language,'Korean')
        self.summary.setText(f"{language}  ·  {'Fast' if self.model=='turbo' else 'Accurate'}  ·  Speaker labels {'on' if self.diar else 'off'}")

    def save_settings(self):
        atomic_json(self.root/'settings.json',dict(language=self.language,model=self.model,diar=self.diar,
            speakers=self.speakers,prompt=self.prompt,folder=self.folder.text(),recording_mode=self.recording_mode))

    def open_options(self):
        if not self.proc:
            Options(self).exec()

    def choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,'Save to folder',self.folder.text())
        if folder:
            self.folder.setText(folder)
            self.save_settings()

    def keep_pending(self):
        # Unsaved recordings are kept on disk even when starting another recording.
        if self.source and not self.saved and Path(self.source).parent==self.root/'recordings':
            QMessageBox.information(self,'Recording preserved',
                'The previous unsaved recording is still available at:\n'+self.source+'\n\nYou can import it later.')

    def record(self):
        if self.proc or self.recording:
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
        self.discard_button.hide()
        self.save_button.setText('Save')
        self.save_button.setEnabled(False)
        self.state.setText('RECORDING')
        self.status.setText('Recording is stored temporarily. You can change transcription settings in Options.')
        self.progress.setValue(0)

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
        self.recording=None
        self.record_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText('Pause')
        self.stop_button.setEnabled(False)
        self.options_button.setEnabled(True)
        self.import_button.setEnabled(True)
        self.meter.setValue(0)
        if rec.path.exists() and rec.path.stat().st_size>44:
            self.set_source(str(rec.path),rec.duration)
        else:
            self.state.setText('READY TO RECORD')
        if rec.error:
            QMessageBox.warning(self,'Recording could not finish',rec.error+
                '\n\nSystem Settings → Privacy & Security → Microphone → allow SoriTaker.')

    def set_source(self,path,duration=None):
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
        self.refresh_save_mode()

    def refresh_save_mode(self):
        if not hasattr(self,'output_hint'):
            return
        self.output_hint.setText('Audio + transcript (.txt) will be saved together.' if self.save_mode.currentData()=='transcribe'
            else 'Only the original file will be saved. No transcription or model loading.')

    def discard(self):
        if not self.source or self.saved or self.proc or self.recording:
            return
        owned=Path(self.source).resolve().parent==(self.root/'recordings').resolve()
        message=('Delete this unsaved recording and any temporary transcript? This cannot be undone.' if owned else
            'Discard this task and any temporary transcript? The imported original file will stay where it is.')
        answer=QMessageBox.question(self,'Discard current recording?',message,
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:
            return
        try:
            discard_pending(self.source)
        except OSError as exc:
            QMessageBox.warning(self,'Could not discard',str(exc))
            return
        self.source=''
        self.saved_paths=[]
        self.job_spec=None
        self.job_folder=None
        self.save_panel.hide()
        self.discard_button.hide()
        self.save_button.setEnabled(False)
        self.state.setText('READY TO RECORD')
        self.clock.setText('00:00:00')
        self.progress.setRange(0,100)
        self.progress.setValue(0)
        self.status.setText('Recording discarded.' if owned else 'Task discarded. Your imported original is preserved.')

    def import_audio(self):
        if self.proc or self.recording:
            return
        path,_=QFileDialog.getOpenFileName(self,'Import audio',str(Path.home()),
            'Audio / video (*.m4a *.mp3 *.wav *.mp4 *.mov *.flac *.aac *.ogg *.webm);;All files (*)')
        if path:
            self.keep_pending()
            self.set_source(path)

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and not self.proc and not self.recording:
            event.acceptProposedAction()

    def dropEvent(self,event):
        if not self.proc and not self.recording:
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
        if not self.source or self.proc or self.recording:
            return
        kind=self.save_mode.currentData()
        if kind=='transcribe' and (not ready(self.model) or (self.diar and self.speakers!=1 and not ready('diarization'))):
            self.status.setText('Download the selected models once in Options, then press Save again.')
            self.open_options()
            return
        title=self.filename.text().strip()
        if not title or title in ('.','..') or any(c in title for c in '/\\\x00'):
            QMessageBox.warning(self,'File name','Enter a file name without / or \\.')
            return
        folder=Path(self.folder.text()).expanduser()
        try:
            folder.mkdir(parents=True,exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self,'Save folder',str(exc))
            return
        self.save_settings()
        spec=dict(kind=kind,export_folder=str(folder.resolve()),export_name=title)
        if kind=='transcribe':
            note=new_note(self.source,self.model,self.language,self.diar,self.speakers,self.prompt)
            note['title']=title
            save_note(note)
            spec['note']=note
        else:
            spec['source']=self.source
        self.start_job(spec)

    def start_job(self,spec):
        if self.proc or self.recording:
            return
        self.job_folder=self.root/'jobs'/uuid.uuid4().hex
        self.job_folder.mkdir()
        self.job_spec=spec
        path=self.job_folder/'job.json'
        atomic_json(path,spec)
        entry=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).with_name('main.py'))]
        env=clear_legacy_offline_flags(os.environ.copy())
        env['PYINSTALLER_RESET_ENVIRONMENT']='1'
        self.job_log=(self.job_folder/'worker.log').open('w',encoding='utf-8')
        try:
            self.proc=subprocess.Popen(entry+['--worker',str(path)],stdout=self.job_log,stderr=self.job_log,
                start_new_session=True,env=env)
        except Exception as exc:
            self.job_log.close()
            self.job_log=None
            QMessageBox.warning(self,'Could not start',str(exc))
            return
        if sys.platform=='darwin':
            subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(self.proc.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.busy(True)
        self.state.setText({'transcribe':'PREPARING TRANSCRIPT','save_audio':'SAVING AUDIO','download':'DOWNLOADING MODEL'}[spec['kind']])
        self.status.setText('Preparing…')
        self.progress.setRange(0,0)

    def busy(self,value):
        for widget in (self.record_button,self.options_button,self.import_button,self.browse,self.filename,self.discard_button):
            widget.setEnabled(not value)
        self.save_mode.setEnabled(not value and not self.saved)
        self.save_button.setEnabled(not value and bool(self.source))
        self.cancel_button.setVisible(value)

    def poll(self):
        if self.recording:
            self.clock.setText(timestamp(self.recording.duration))
            self.meter.setValue(self.recording.level)
            if not self.recording.is_alive():
                self.finished_recording()
        if not self.proc:
            return
        status=read_json(self.job_folder/'status.json',{})
        if status:
            self.status.setText(status.get('message','Processing…'))
            value=status.get('progress')
            if value is None:
                self.progress.setRange(0,0)
            else:
                self.progress.setRange(0,100)
                self.progress.setValue(int(value))
        code=self.proc.poll()
        if code is None:
            return
        status=read_json(self.job_folder/'status.json',status)
        self.proc=None
        self.job_log.close()
        self.job_log=None
        self.busy(False)
        self.progress.setRange(0,100)
        if code==0 and status.get('state')=='complete':
            self.progress.setValue(100)
            if self.job_spec['kind'] in ('transcribe','save_audio'):
                self.saved=True
                self.saved_paths=status.get('saved_paths',[])
                self.save_button.setText('Open folder')
                self.state.setText('SAVED')
                self.output_hint.setText('\n'.join(Path(p).name for p in self.saved_paths))
                self.save_mode.setEnabled(False)
                self.discard_button.hide()
                self.status.setText('Audio saved. No transcript was created.' if self.job_spec['kind']=='save_audio' else
                    'Audio and transcript saved. You can start a new recording.')
                if status.get('warnings'):
                    self.status.setText('Saved with a note: '+' / '.join(status['warnings']))
            else:
                self.state.setText('MODEL READY')
                self.status.setText('Model installed. You can now save a recording or download another model in Options.')
        else:
            self.progress.setValue(0)
            self.state.setText('PLEASE CHECK')
            message=status.get('message') or 'Could not finish. Your original audio has been preserved.'
            self.status.setText(message)
            QMessageBox.warning(self,'Could not finish',message+'\n\nLog folder:\n'+str(self.job_folder))

    def cancel_job(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.poll()
            return
        try:
            os.killpg(self.proc.pid,signal.SIGTERM)
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid,signal.SIGKILL)
            self.proc.wait(timeout=2)
        except ProcessLookupError:
            pass
        # The worker may have finished saving just before the cancel request.
        if read_json(self.job_folder/'status.json',{}).get('state')=='complete':
            self.poll()
            return
        self.proc=None
        self.job_log.close()
        self.job_log=None
        self.busy(False)
        self.progress.setRange(0,100)
        self.progress.setValue(0)
        self.state.setText('CANCELLED')
        self.status.setText('Cancelled. Your audio is preserved. Save audio only, retry transcription, or Discard.')

    def closeEvent(self,event):
        if self.recording or self.proc:
            answer=QMessageBox.question(self,'Close SoriTaker?','Stop the current task and close? Recorded audio is preserved.',
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
            if self.proc:
                self.cancel_job()
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
