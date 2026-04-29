from __future__ import annotations

import argparse
import asyncio
import audioop
import collections
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import sounddevice as sd
from curl_cffi import requests as cf_requests


TOKEN_FILE = "token.txt"
TRANSCRIBE_URL = "https://chatgpt.com/backend-api/transcribe"
ANON_TRANSCRIBE_URL = "https://chatgpt.com/backend-anon/transcribe"

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 30
SAMPLE_WIDTH = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


@dataclass
class VoiceSettings:
    threshold: int = 260
    continue_threshold: int = 220
    silence_ms: int = 800
    pre_roll_ms: int = 600
    start_required_ms: int = 80
    continue_required_ms: int = 90
    min_ms: int = 700
    min_speech_ms: int = 250
    discard_cooldown_ms: int = 250
    audio_queue_size: int = 5000


@dataclass
class Turn:
    idx: int
    frames: list[bytes]
    duration_ms: int
    speech_ms: int
    avg: int
    peak: int
    created_at: float


def rms(frame: bytes) -> int:
    return audioop.rms(frame, SAMPLE_WIDTH)


def stats(frames: list[bytes]) -> tuple[int, int]:
    if not frames:
        return 0, 0
    values = [rms(frame) for frame in frames]
    return int(sum(values) / len(values)), max(values)


def carregar_token_txt(token_file: str | Path = TOKEN_FILE) -> dict[str, str]:
    path = Path(token_file)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items() if v is not None}


def parse_devtools_block(text: str) -> dict[str, str]:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    headers: dict[str, str] = {}
    known_keys = {"authorization", "cookie", "chatgpt-account-id", "oai-device-id", "oai-language"}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if i == 0 and "." in line and " " not in line:
            i += 1
            continue

        lower_line = line.lower()
        is_key = bool(re.match(r"^:?[a-z0-9][a-z0-9\-]*$", lower_line))
        if is_key and i + 1 < len(lines):
            value = lines[i + 1].strip()
            next_is_key = bool(re.match(r"^:?[a-z0-9][a-z0-9\-]*$", value.lower()))
            if lower_line in known_keys or not next_is_key:
                headers[lower_line] = value
                i += 2
                continue

        if ": " in line:
            key, _, value = line.partition(": ")
            headers[key.strip().lower()] = value.strip()
        elif ":" in line and not line.startswith("http"):
            key, _, value = line.partition(":")
            if key.strip():
                headers[key.strip().lower()] = value.strip()
        i += 1
    return headers


def extrair_credenciais(text: str) -> tuple[dict[str, str], list[str]]:
    headers = parse_devtools_block(text)
    data = {
        "authorization": headers.get("authorization", ""),
        "cookie": headers.get("cookie", ""),
        "chatgpt-account-id": headers.get("chatgpt-account-id", ""),
        "oai-device-id": headers.get("oai-device-id", ""),
        "oai-language": headers.get("oai-language", "pt-BR"),
    }
    errors: list[str] = []
    if not data["authorization"]:
        errors.append("Nao encontrei authorization.")
    if not data["cookie"]:
        errors.append("Nao encontrei cookie.")
    return data, errors


def salvar_token_txt(data: dict[str, str], token_file: str | Path = TOKEN_FILE) -> None:
    Path(token_file).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def salvar_wav(frames: list[bytes]) -> tuple[str, int]:
    audio_bytes = b"".join(frames)
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_path = temp.name
    temp.close()
    with wave.open(temp_path, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio_bytes)
    duration_ms = int((len(audio_bytes) / SAMPLE_WIDTH / SAMPLE_RATE) * 1000)
    return temp_path, duration_ms


def converter_wav_para_webm(wav_path: str) -> str:
    webm_path = tempfile.NamedTemporaryFile(delete=False, suffix=".webm").name
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        wav_path,
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        webm_path,
    ]
    subprocess.run(cmd, check=True)
    return webm_path


def montar_body(audio_path: str, duration_ms: int, fmt: Literal["wav", "webm"] = "wav") -> tuple[bytes, str]:
    audio_bytes = Path(audio_path).read_bytes()
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    if fmt == "webm":
        filename = "whisper.webm"
        content_type = "audio/webm;codecs=opus"
    else:
        filename = "audio.wav"
        content_type = "audio/wav"
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += audio_bytes
    body += f"\r\n--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="duration_ms"\r\n\r\n'
    body += f"{int(duration_ms)}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()
    return body, boundary


def limpar_texto_resposta(data: Any) -> str:
    if isinstance(data, dict):
        text = data.get("text") or data.get("transcript") or ""
    else:
        text = str(data)
    return " ".join(text.strip().split())


def transcrever_arquivo(audio_path: str, duration_ms: int, token_data: dict[str, str]) -> tuple[str, int, int, str]:
    body, boundary = montar_body(audio_path, duration_ms)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Authorization": token_data["authorization"],
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-language": token_data.get("oai-language", "pt-BR"),
    }
    if token_data.get("cookie"):
        headers["Cookie"] = token_data["cookie"]
    if token_data.get("chatgpt-account-id"):
        headers["chatgpt-account-id"] = token_data["chatgpt-account-id"]
    if token_data.get("oai-device-id"):
        headers["oai-device-id"] = token_data["oai-device-id"]

    start = time.perf_counter()
    response = cf_requests.post(TRANSCRIBE_URL, headers=headers, data=body, impersonate="chrome", timeout=120)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    raw = response.text[:600]

    if response.status_code == 200:
        try:
            return limpar_texto_resposta(response.json()), response.status_code, elapsed_ms, raw
        except Exception:
            return response.text.strip(), response.status_code, elapsed_ms, raw
    return f"[Erro {response.status_code}] {response.text[:400]}", response.status_code, elapsed_ms, raw


def transcrever_arquivo_anon(
    audio_path: str,
    duration_ms: int,
    *,
    fmt: Literal["wav", "webm"] = "wav",
    language: str = "pt-BR",
    device_id: str | None = None,
    session_id: str | None = None,
) -> tuple[str, int, int, str]:
    send_path = audio_path
    cleanup_extra: str | None = None
    if fmt == "webm":
        send_path = converter_wav_para_webm(audio_path)
        cleanup_extra = send_path

    try:
        body, boundary = montar_body(send_path, duration_ms, fmt)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id or str(uuid.uuid4()),
            "oai-session-id": session_id or str(uuid.uuid4()),
            "oai-language": language,
            "oai-client-build-number": "6232230",
            "oai-client-version": "prod-5d86787f9f8d1f6b6e7e021b6aa4d6b14a14445c",
        }

        start = time.perf_counter()
        response = cf_requests.post(ANON_TRANSCRIBE_URL, headers=headers, data=body, impersonate="chrome", timeout=120)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        raw = response.text[:1200]

        if response.status_code == 200:
            try:
                return limpar_texto_resposta(response.json()), response.status_code, elapsed_ms, raw
            except Exception:
                return response.text.strip(), response.status_code, elapsed_ms, raw
        return f"[Erro {response.status_code}] {response.text[:600]}", response.status_code, elapsed_ms, raw
    finally:
        if cleanup_extra and os.path.exists(cleanup_extra):
            try:
                os.remove(cleanup_extra)
            except Exception:
                pass


class TTSManager:
    def __init__(
        self,
        *,
        enabled: bool = False,
        provider: str = "edge",
        voice: str = "pt-BR-FranciscaNeural",
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.provider = provider
        self.voice = voice
        self.on_status = on_status
        self.speaking_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def configure(self, *, enabled: bool | None = None, provider: str | None = None, voice: str | None = None) -> None:
        if enabled is not None:
            self.enabled = enabled
        if provider:
            self.provider = provider
        if voice:
            self.voice = voice

    def speak_async(self, text: str) -> None:
        text = " ".join((text or "").split())
        if not text or not self.enabled or self.provider in {"off", "none"}:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._status("TTS ocupado; resposta anterior ainda tocando.")
                return
            self._thread = threading.Thread(target=self._speak_worker, args=(text,), daemon=True)
            self._thread.start()

    def _speak_worker(self, text: str) -> None:
        self.speaking_event.set()
        try:
            if self.provider != "edge":
                self._status(f"TTS provider ainda nao implementado: {self.provider}")
                return
            self._speak_edge(text)
        except Exception as exc:
            self._status(f"Erro no TTS: {exc}")
        finally:
            self.speaking_event.clear()

    def _speak_edge(self, text: str) -> None:
        try:
            import edge_tts
            import pygame
        except Exception as exc:
            raise RuntimeError("edge_tts/pygame nao esta disponivel") from exc

        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        temp_path = temp.name
        temp.close()
        try:
            async def synthesize() -> None:
                communicate = edge_tts.Communicate(text, self.voice)
                await communicate.save(temp_path)

            self._status("TTS sintetizando...")
            asyncio.run(synthesize())

            self._status("TTS falando...")
            pygame.mixer.init()
            pygame.mixer.music.load(temp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.05)
            pygame.mixer.music.stop()
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass
            self._status("TTS pronto.")

    def _status(self, message: str) -> None:
        if self.on_status:
            self.on_status(message)


class VoiceService:
    def __init__(
        self,
        *,
        token_file: str | Path = TOKEN_FILE,
        mode: Literal["anon", "token"] = "anon",
        audio_format: Literal["wav", "webm"] = "wav",
        language: str = "pt-BR",
        settings: VoiceSettings | None = None,
        pause_event: threading.Event | None = None,
        on_transcript: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_level: Callable[[int], None] | None = None,
    ) -> None:
        self.token_file = Path(token_file)
        self.mode = mode
        self.audio_format = audio_format
        self.language = language
        self.anon_device_id = str(uuid.uuid4())
        self.anon_session_id = str(uuid.uuid4())
        self.settings = settings or VoiceSettings()
        self.pause_event = pause_event or threading.Event()
        self.on_transcript = on_transcript
        self.on_status = on_status
        self.on_level = on_level
        self.stop_event = threading.Event()
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=self.settings.audio_queue_size)
        self.turn_queue: queue.Queue[Turn | None] = queue.Queue()
        self.capture_thread: threading.Thread | None = None
        self.worker_thread: threading.Thread | None = None
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        token_data: dict[str, str] | None = None
        if self.mode == "token":
            token_data = carregar_token_txt(self.token_file)
            if not token_data.get("authorization") or not token_data.get("cookie"):
                raise RuntimeError(f"Credenciais STT ausentes em {self.token_file}.")

        self.stop_event.clear()
        self.audio_queue = queue.Queue(maxsize=self.settings.audio_queue_size)
        self.turn_queue = queue.Queue()
        self.running = True

        self.worker_thread = threading.Thread(target=self._transcription_worker, args=(token_data,), daemon=True)
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.worker_thread.start()
        self.capture_thread.start()
        self._status(f"STT iniciado ({self.mode}).")

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self.turn_queue.put(None)
        self.running = False
        self._status("STT parando...")

    def _transcription_worker(self, token_data: dict[str, str] | None) -> None:
        while not self.stop_event.is_set() or not self.turn_queue.empty():
            try:
                turn = self.turn_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if turn is None:
                self.turn_queue.task_done()
                break

            wav_path = None
            try:
                wav_path, duration_ms = salvar_wav(turn.frames)
                self._status(f"Transcrevendo turno {turn.idx} ({duration_ms}ms)...")
                if self.mode == "token":
                    if token_data is None:
                        raise RuntimeError("Token STT ausente.")
                    text, status, elapsed_ms, _raw = transcrever_arquivo(wav_path, duration_ms, token_data)
                else:
                    text, status, elapsed_ms, _raw = transcrever_arquivo_anon(
                        wav_path,
                        duration_ms,
                        fmt=self.audio_format,
                        language=self.language,
                        device_id=self.anon_device_id,
                        session_id=self.anon_session_id,
                    )
                if status == 200 and text:
                    self._status(f"STT turno {turn.idx}: {elapsed_ms}ms")
                    if self.on_transcript:
                        self.on_transcript(text)
                elif status == 200:
                    self._status(f"STT turno {turn.idx}: sem texto.")
                else:
                    self._status(f"STT erro turno {turn.idx}: {text}")
            except Exception as exc:
                self._status(f"Erro STT turno {turn.idx}: {exc}")
            finally:
                if wav_path:
                    try:
                        os.remove(wav_path)
                    except Exception:
                        pass
                self.turn_queue.task_done()

    def _capture_loop(self) -> None:
        settings = self.settings
        frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
        pre_roll_count = max(1, int(settings.pre_roll_ms / FRAME_MS))
        silence_frames_limit = max(1, int(settings.silence_ms / FRAME_MS))
        min_frames = max(1, int(settings.min_ms / FRAME_MS))
        start_required_frames = max(1, int(settings.start_required_ms / FRAME_MS))
        continue_required_frames = max(1, int(settings.continue_required_ms / FRAME_MS))
        cooldown_frames = max(0, int(settings.discard_cooldown_ms / FRAME_MS))

        pre_roll: collections.deque[bytes] = collections.deque(maxlen=pre_roll_count)
        recording = False
        frames: list[bytes] = []
        turn_idx = 0
        start_count = 0
        silence_count = 0
        speech_frames = 0
        voice_run = 0
        discard_cooldown = 0

        def callback(indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
            if status:
                self._status(f"Audio status: {status}")
            try:
                self.audio_queue.put_nowait(bytes(indata))
            except queue.Full:
                pass

        try:
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=frame_samples,
                dtype="int16",
                channels=CHANNELS,
                callback=callback,
            ):
                while not self.stop_event.is_set():
                    try:
                        frame = self.audio_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    volume = rms(frame)
                    if self.on_level:
                        self.on_level(volume)

                    if self.pause_event.is_set():
                        recording = False
                        frames = []
                        pre_roll.clear()
                        start_count = 0
                        silence_count = 0
                        speech_frames = 0
                        voice_run = 0
                        continue

                    if not recording:
                        pre_roll.append(frame)
                        if discard_cooldown > 0:
                            discard_cooldown -= 1
                            start_count = 0
                        elif volume >= settings.threshold:
                            start_count += 1
                        else:
                            start_count = 0

                        if start_count >= start_required_frames:
                            recording = True
                            turn_idx += 1
                            frames = list(pre_roll)
                            silence_count = 0
                            speech_frames = start_count
                            voice_run = start_count
                            self._status(f"Fala detectada #{turn_idx}.")
                        continue

                    frames.append(frame)
                    is_speech_raw = volume >= settings.continue_threshold
                    if is_speech_raw:
                        voice_run += 1
                    else:
                        voice_run = 0

                    if voice_run >= continue_required_frames:
                        silence_count = 0
                        speech_frames += 1
                    else:
                        silence_count += 1

                    duration_ms = len(frames) * FRAME_MS
                    speech_ms = speech_frames * FRAME_MS
                    should_close = silence_count >= silence_frames_limit and len(frames) >= min_frames
                    if not should_close:
                        continue

                    avg, peak = stats(frames)
                    if duration_ms < settings.min_ms or speech_ms < settings.min_speech_ms:
                        discard_cooldown = cooldown_frames
                        self._status(f"Turno ignorado: dur={duration_ms}ms fala={speech_ms}ms.")
                    else:
                        self.turn_queue.put(
                            Turn(
                                idx=turn_idx,
                                frames=list(frames),
                                duration_ms=duration_ms,
                                speech_ms=speech_ms,
                                avg=avg,
                                peak=peak,
                                created_at=time.time(),
                            )
                        )
                        self._status(f"Turno {turn_idx} fechado: dur={duration_ms}ms peak={peak}.")

                    recording = False
                    frames = []
                    pre_roll.clear()
                    start_count = 0
                    silence_count = 0
                    speech_frames = 0
                    voice_run = 0
        except Exception as exc:
            self._status(f"Erro na captura de audio: {exc}")
        finally:
            self.running = False
            self._status("STT parado.")

    def _status(self, message: str) -> None:
        if self.on_status:
            self.on_status(message)


def settings_from_namespace(args: argparse.Namespace) -> VoiceSettings:
    return VoiceSettings(
        threshold=args.threshold,
        continue_threshold=args.continue_threshold,
        silence_ms=args.silence_ms,
        pre_roll_ms=args.pre_roll_ms,
        start_required_ms=args.start_required_ms,
        continue_required_ms=args.continue_required_ms,
        min_ms=args.min_ms,
        min_speech_ms=args.min_speech_ms,
        discard_cooldown_ms=args.discard_cooldown_ms,
    )
