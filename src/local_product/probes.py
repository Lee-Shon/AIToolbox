"""Own generated capability fixtures; no third-party recorded audio."""
import os
from pathlib import Path
import subprocess
import tempfile
from .service import ProductError

def speech_probe() -> bytes:
    """Generate our own short sentence locally; no recorded audio is shipped."""
    script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voice = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' } | Select-Object -First 1
  if ($null -eq $voice) { throw 'english_speech_voice_missing' }
  $speaker.SelectVoice($voice.VoiceInfo.Name)
  $speaker.SetOutputToWaveFile($env:AITOOLBOX_PROBE_AUDIO)
  $speaker.Speak('The blue sky has seven bright stars.')
} finally { $speaker.Dispose() }
"""
    with tempfile.TemporaryDirectory(prefix="aitoolbox-probe-") as folder:
        target = Path(folder) / "probe.wav"
        env = dict(os.environ, AITOOLBOX_PROBE_AUDIO=str(target))
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                env=env, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not target.is_file():
            raise ProductError("audio_validation_requires_windows_english_speech_voice", 422)
        return target.read_bytes()
