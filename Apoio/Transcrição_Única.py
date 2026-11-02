"""Transcrição local em precisão. Saídas TXT/Word, sem classificação de falas."""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


_CUDA_DLL_HANDLES = {}


def configure_cuda_libraries():
    """Expose NVIDIA wheels' DLLs to both Python and native LoadLibrary calls."""
    if os.name != 'nt':
        return
    directories = []
    for base in sys.path:
        for library in ('cublas', 'cudnn', 'cuda_runtime', 'cuda_nvrtc', 'nvrtc', 'nvjitlink'):
            directory = Path(base) / 'nvidia' / library / 'bin'
            if directory.is_dir():
                path = str(directory.resolve())
                if path not in _CUDA_DLL_HANDLES:
                    _CUDA_DLL_HANDLES[path] = os.add_dll_directory(path)
                directories.append(path)
    existing = os.environ.get('PATH', '').split(os.pathsep)
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(directories + existing))


@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float | None = None

@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
IGNORED_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.ico', '.svg', '.rar', '.zip', '.7z', '.tar', '.gz', '.bz2', '.xz', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.rtf', '.txt', '.csv', '.json', '.jsonl', '.xml', '.md', '.html', '.css', '.toml', '.yaml', '.yml', '.exe', '.dll', '.bat', '.cmd', '.ps1', '.py', '.pyc', '.pyd', '.js', '.tsv', '.bin', '.safetensors', '.onnx', '.lock', '.log', '.srt', '.vtt'})
SKIP_DIRECTORIES = frozenset({'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.test-artifacts', '.model-cache', 'transcricoes_v3_1'})
CUDA_COMPUTE_PRIORITY = ('float16', 'int8_float16', 'int8_float32', 'float32', 'int8')
CUDA_MEMORY_FIRST_PRIORITY = ('int8_float16', 'float16', 'int8', 'int8_float32', 'float32')
VRAM_ESTIMATES_MB = {'large-v3-turbo': {'float16': 3400, 'int8_float16': 2400, 'int8_float32': 3100, 'float32': 6400, 'int8': 2200}}

@dataclass(frozen=True)
class AttemptSpec:
    model: str
    device: str
    compute_type: str
    selection_reason: str
    estimated_vram_mb: int | None = None
    vram_assessment: str | None = None

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _run(command: list[str], timeout: int=30) -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'errors': 'replace', 'timeout': timeout, 'check': False}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(command, **kwargs)

def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None

def _parse_nvidia_inventory(output: str) -> list[dict[str, Any]]:
    inventory = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != 6:
            continue
        try:
            inventory.append({'index': int(parts[0]), 'name': parts[1], 'driver_version': parts[2], 'memory_total_mb': int(parts[3]), 'memory_free_mb': int(parts[4]), 'memory_used_mb': int(parts[5])})
        except ValueError:
            continue
    return inventory

def query_nvidia_inventory(executable: str | None=None) -> list[dict[str, Any]]:
    executable = executable or shutil.which('nvidia-smi')
    if not executable:
        return []
    process = _run([executable, '--query-gpu=index,name,driver_version,memory.total,memory.free,memory.used', '--format=csv,noheader,nounits'])
    return _parse_nvidia_inventory(process.stdout) if process.returncode == 0 else []

def collect_hardware_diagnostics(ffmpeg: str | Path | None=None, ffprobe: str | Path | None=None) -> dict[str, Any]:
    """Read capabilities only. Importing CTranslate2 is not a CUDA inference test."""
    result: dict[str, Any] = {'timestamp': utc_now(), 'platform': platform.platform(), 'python': platform.python_version(), 'cpu': platform.processor() or platform.machine(), 'cpu_logical_count': os.cpu_count(), 'faster_whisper_version': _package_version('faster-whisper'), 'ctranslate2_version': _package_version('ctranslate2'), 'ffmpeg': _resolve_executable('ffmpeg', ffmpeg), 'ffprobe': _resolve_executable('ffprobe', ffprobe), 'nvidia_smi': shutil.which('nvidia-smi'), 'nvidia_gpus': [], 'gpu': None, 'cuda_device_count': 0, 'cuda_compute_types': [], 'cpu_compute_types': [], 'cuda_candidate': False, 'cuda_verified': False, 'errors': [], 'cuda_status': 'Não verificada por inferência; diagnóstico de capacidade somente.'}
    result['media_tools'] = {}
    for tool in ('ffmpeg', 'ffprobe'):
        check: dict[str, Any] = {'path': result[tool], 'functional': False}
        try:
            if not result[tool]:
                raise FileNotFoundError(f'{tool} não encontrado no PATH/caminho configurado.')
            process = _run([result[tool], '-version'])
            if process.returncode:
                raise RuntimeError(process.stderr.strip() or f'Código de saída {process.returncode}')
            check.update(functional=True, version=process.stdout.splitlines()[0] if process.stdout else '')
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            check['error'] = str(exc)
            result['errors'].append({'stage': tool + '_verification', 'error': str(exc)})
        result['media_tools'][tool] = check
    if result['nvidia_smi']:
        try:
            result['nvidia_gpus'] = query_nvidia_inventory(result['nvidia_smi'])
            result['gpu'] = result['nvidia_gpus'][0] if result['nvidia_gpus'] else None
            if not result['nvidia_gpus']:
                result['errors'].append({'stage': 'nvidia_detection', 'error': 'nvidia-smi não retornou inventário utilizável.'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            result['errors'].append({'stage': 'nvidia_detection', 'error': str(exc)})
    try:
        import ctranslate2
        result['cpu_compute_types'] = sorted(ctranslate2.get_supported_compute_types('cpu'))
        result['cuda_device_count'] = ctranslate2.get_cuda_device_count()
        if result['cuda_device_count']:
            result['cuda_compute_types'] = sorted(ctranslate2.get_supported_compute_types('cuda', 0))
            result['cuda_candidate'] = bool(set(result['cuda_compute_types']) & {'float16', 'int8_float16', 'int8_float32', 'float32', 'int8'})
    except Exception as exc:
        result['errors'].append({'stage': 'ctranslate2_detection', 'error': str(exc)})
    return result

@dataclass
class Config:
    device: str = 'auto'
    model: str | None = None
    beam_size: int = 5
    cpu_threads: int = 0
    hash_media: bool = False

    def __post_init__(self) -> None:
        if self.device not in {'auto', 'cuda', 'cpu'}:
            raise ValueError('Dispositivo deve ser auto, cuda ou cpu.')
        if self.beam_size < 1 or self.cpu_threads < 0:
            raise ValueError('beam_size deve ser positivo e cpu_threads não negativo.')

    def model_for(self, device: str) -> str:
        return self.model or 'large-v3-turbo'

    def gpu_models(self) -> list[str]:
        return [self.model_for('cuda')]

class TranscriptionFailure(RuntimeError):

    def __init__(self, message: str, audit: dict[str, Any]):
        super().__init__(message)
        self.audit = audit

def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None

def _copy_segment(segment: Any) -> Segment:
    """Copy every original Whisper segment, including empty text and exact word spacing."""
    return Segment(id=segment.id, start=segment.start, end=segment.end, text=segment.text, words=[Word(start=w.start, end=w.end, word=w.word, probability=_float_or_none(getattr(w, 'probability', None))) for w in getattr(segment, 'words', None) or []], avg_logprob=_float_or_none(getattr(segment, 'avg_logprob', None)), no_speech_prob=_float_or_none(getattr(segment, 'no_speech_prob', None)))

class TranscriptionEngine:
    """Reuse loaded models; discard incomplete CUDA output and retry from the audio start."""

    def __init__(self, config: Config, diagnostics: dict[str, Any] | None=None, model_factory: Callable | None=None, memory_probe: Callable | None=None, runtime_logger: Callable[[str], None] | None=None):
        self.config = config
        self.diagnostics = diagnostics if diagnostics is not None else collect_hardware_diagnostics()
        self.model_factory = model_factory
        self._model: Any = None
        self._model_key: tuple | None = None
        self.memory_probe = memory_probe
        self.runtime_logger = runtime_logger
        self._cuda_exhausted = False
        self._runtime_log: list[str] = []

    def _load_model(self, model_name: str, device: str, compute_type: str) -> Any:
        if device == 'cuda':
            configure_cuda_libraries()
        key = (model_name, device, compute_type)
        if self._model_key == key and self._model is not None:
            return self._model
        self._release_model()
        factory = self.model_factory
        if factory is None:
            from faster_whisper import WhisperModel
            factory = WhisperModel
        self._model = factory(model_name, device=device, device_index=0, compute_type=compute_type, cpu_threads=self.config.cpu_threads)
        self._model_key = key
        return self._model

    def _release_model(self) -> None:
        self._model = None
        self._model_key = None
        gc.collect()

    def _emit(self, message: str) -> None:
        self._runtime_log.append(message)
        if self.runtime_logger:
            self.runtime_logger(message)

    def _memory_snapshot(self) -> dict[str, Any] | None:
        if self.memory_probe:
            return self.memory_probe()
        executable = self.diagnostics.get('nvidia_smi')
        inventory = query_nvidia_inventory(executable) if executable else []
        return inventory[0] if inventory else self.diagnostics.get('gpu')

    @staticmethod
    def _vram_assessment(model: str, compute_type: str, snapshot: dict[str, Any] | None) -> tuple[str, int | None, str]:
        estimate = VRAM_ESTIMATES_MB.get(model, {}).get(compute_type)
        if estimate is None or not snapshot:
            return ('unknown', estimate, 'VRAM desconhecida; tentativa controlada.')
        total = snapshot.get('memory_total_mb')
        free = snapshot.get('memory_free_mb')
        if not isinstance(total, (int, float)) or not isinstance(free, (int, float)):
            return ('unknown', estimate, 'VRAM incompleta; tentativa controlada.')
        headroom = max(384, int(total * 0.08))
        required = estimate + headroom
        if total < estimate or free < estimate * 0.7:
            return ('insufficient', estimate, f'VRAM claramente insuficiente: livre={free} MiB, estimada={estimate} MiB.')
        if free < required:
            return ('constrained', estimate, f'VRAM apertada: livre={free} MiB, estimada+reserva={required} MiB.')
        return ('adequate', estimate, f'VRAM adequada: livre={free} MiB, estimada+reserva={required} MiB.')

    def _attempt_plan(self) -> tuple[list[AttemptSpec], list[dict[str, Any]]]:
        if self.config.device == 'cpu':
            return ([AttemptSpec(self.config.model_for('cpu'), 'cpu', 'int8', 'CPU solicitada explicitamente.')], [])
        plan: list[AttemptSpec] = []
        skipped: list[dict[str, Any]] = []
        if not self._cuda_exhausted and self.diagnostics.get('cuda_candidate'):
            supported = set(self.diagnostics.get('cuda_compute_types', []))
            snapshot = self._memory_snapshot()
            total_vram = snapshot.get('memory_total_mb') if snapshot else None
            compute_priority = CUDA_MEMORY_FIRST_PRIORITY if isinstance(total_vram, (int, float)) and total_vram <= 6144 else CUDA_COMPUTE_PRIORITY
            preferred: list[AttemptSpec] = []
            constrained: list[AttemptSpec] = []
            for model in self.config.gpu_models():
                for compute_type in compute_priority:
                    if compute_type not in supported:
                        skipped.append({'model': model, 'device': 'cuda', 'compute_type': compute_type, 'reason': 'compute_type não suportado pelo backend.'})
                        continue
                    assessment, estimate, reason = self._vram_assessment(model, compute_type, snapshot)
                    item = AttemptSpec(model, 'cuda', compute_type, reason, estimate, assessment)
                    if assessment == 'insufficient':
                        skipped.append({'model': model, 'device': 'cuda', 'compute_type': compute_type, 'estimated_vram_mb': estimate, 'reason': reason})
                    elif assessment == 'constrained':
                        constrained.append(item)
                    else:
                        preferred.append(item)
            plan.extend(preferred)
            plan.extend(constrained)
        reason = 'Modelo explícito preservado no fallback CPU.' if self.config.model else 'Fallback final após esgotar alternativas CUDA viáveis.'
        plan.append(AttemptSpec(self.config.model_for('cpu'), 'cpu', 'int8', reason))
        return (plan, skipped)

    @staticmethod
    def _failure_category(error: Exception, device: str) -> str:
        message = str(error).casefold()
        if device == 'cuda' and any((token in message for token in ('out of memory', 'cuda_error_out_of_memory', 'cublas_status_alloc_failed'))):
            return 'cuda_oom_recoverable'
        if device == 'cuda' and any(token in message for token in ('cublas', 'cudnn', '.dll', 'libcuda')) and any(token in message for token in ('not found', 'cannot be loaded', 'could not load', 'cannot open', 'failed to load')):
            return 'cuda_library_missing'
        if device == 'cuda' and any((token in message for token in ('cuda', 'cudnn', 'cublas', 'driver'))):
            return 'cuda_backend_recoverable'
        return 'backend_error_recoverable' if device == 'cuda' else 'cpu_backend_error'

    def transcribe(self, audio: Path) -> tuple[list[Segment], dict[str, Any]]:
        # Avoid propagating a recognition error into later decoding windows.
        parameters = {'language': 'pt', 'beam_size': self.config.beam_size, 'vad_filter': True, 'word_timestamps': True, 'condition_on_previous_text': False}
        self._runtime_log = []
        gpu = self._memory_snapshot()
        audit: dict[str, Any] = {'started_at': utc_now(), 'parameters': parameters, 'attempts': [], 'requested_device': self.config.device, 'cuda_verified': False, 'faster_whisper_version': self.diagnostics.get('faster_whisper_version'), 'selected_mode': 'precisao', 'explicit_model': self.config.model, 'gpu': gpu, 'native_abort_catchable': False, 'native_abort_limitation': 'Abortos nativos que encerram o processo não permitem executar nem registrar fallback posterior.'}
        self._emit(f"[RUNTIME] modo=precisao modelo_explicito={self.config.model or 'não'}")
        if gpu:
            self._emit(f"[RUNTIME] GPU: {gpu.get('name', 'desconhecida')}")
            self._emit(f"[RUNTIME] VRAM total: {gpu.get('memory_total_mb', 'desconhecida')} MiB")
            self._emit(f"[RUNTIME] VRAM livre: {gpu.get('memory_free_mb', 'desconhecida')} MiB")
        else:
            self._emit('[RUNTIME] GPU/VRAM não disponível para planejamento.')
        plan, skipped = self._attempt_plan()
        audit['attempt_plan'] = [asdict(item) for item in plan]
        audit['skipped_attempts'] = skipped
        if self.config.device != 'cpu' and plan[0].device == 'cpu':
            audit['device_selection_reason'] = 'CUDA esgotada após cascata anterior.' if self._cuda_exhausted else 'CUDA funcional ainda não demonstrada; capacidade compatível não detectada.'
        previous_failure = None
        attempted_cuda = False
        for number, spec in enumerate(plan, 1):
            device, compute_type, model_name = (spec.device, spec.compute_type, spec.model)
            if device == 'cuda' and self._cuda_exhausted:
                continue
            if device == 'cpu' and attempted_cuda:
                self._cuda_exhausted = True
            attempted_cuda = attempted_cuda or device == 'cuda'
            memory_before = self._memory_snapshot() if device == 'cuda' else None
            attempt: dict[str, Any] = {'attempt_number': number, 'device': device, 'compute_type': compute_type, 'model': model_name, 'started_at': utc_now(), 'selection_reason': spec.selection_reason, 'estimated_vram_mb': spec.estimated_vram_mb, 'vram_assessment': spec.vram_assessment, 'vram_before': memory_before, 'fallback_reason': previous_failure, 'status': 'running', 'stage': 'model_initialization'}
            audit['attempts'].append(attempt)
            self._emit(f'[TENTATIVA {number}] modelo={model_name} device={device} compute_type={compute_type}')
            try:
                model = self._load_model(model_name, device, compute_type)
                attempt['stage'] = 'transcription'
                iterator, info = model.transcribe(str(audio), **parameters)
                segments = [_copy_segment(segment) for segment in iterator]
                backend = getattr(model, 'model', None)
                actual_device = getattr(backend, 'device', None)
                actual_compute_type = getattr(backend, 'compute_type', None)
                attempt.update(status='completed', completed_at=utc_now(), segment_count=len(segments))
                attempt.update(actual_device=actual_device, actual_compute_type=actual_compute_type)
                verified = actual_device == 'cuda' and any((s.text.strip() for s in segments))
                audit.update(model=model_name, device=actual_device or device, compute_type=actual_compute_type or compute_type, actual_device=actual_device, actual_compute_type=actual_compute_type, requested_compute_type=compute_type, cuda_verified=verified, completed_at=utc_now(), fallback_used=len(audit['attempts']) > 1, language=getattr(info, 'language', 'pt'), language_probability=_float_or_none(getattr(info, 'language_probability', None)), duration=_float_or_none(getattr(info, 'duration', None)), duration_after_vad=_float_or_none(getattr(info, 'duration_after_vad', None)))
                audit['cuda_status'] = 'Inferência CUDA concluída e segmentos obtidos.' if verified else 'Inferência CUDA não comprovada nesta transcrição.'
                audit['runtime_log'] = list(self._runtime_log)
                self._emit(f'[TENTATIVA {number}] resultado=SUCESSO')
                self._emit(f'[RUNTIME FINAL] modelo={model_name} device={actual_device or device} compute_type={actual_compute_type or compute_type}')
                audit['runtime_log'] = list(self._runtime_log)
                return (segments, audit)
            except Exception as exc:
                category = self._failure_category(exc, device)
                attempt.update(status='failed', completed_at=utc_now(), error=str(exc), error_type=type(exc).__name__, failure_category=category)
                previous_failure = f'{model_name}/{device}/{compute_type}: {type(exc).__name__}: {exc}'
                self._emit(f'[TENTATIVA {number}] resultado=FALHA categoria={category} motivo={exc}')
                model = None
                iterator = None
                backend = None
                self._release_model()
                if device == 'cuda':
                    if category == 'cuda_library_missing':
                        self._cuda_exhausted = True
                        self._emit('[RUNTIME] Biblioteca CUDA ausente/inacessível. Continuando na CPU; execute Testar_máquina.py para preparar a GPU.')
                    continue
                audit['runtime_log'] = list(self._runtime_log)
                audit['completed_at'] = utc_now()
                raise TranscriptionFailure(f'Falha de transcrição em CPU/INT8: {exc}', audit) from exc
        if attempted_cuda:
            self._cuda_exhausted = True
        raise TranscriptionFailure('Nenhuma configuração de transcrição disponível.', audit)

def _resolve_executable(tool: str, override: str | Path | None=None) -> str | None:
    candidate = str(override) if override else shutil.which(tool)
    if candidate is None and os.name == 'nt':
        for directory in os.get_exec_path():
            link = Path(directory) / (tool if tool.endswith('.exe') else tool + '.exe')
            if link.is_symlink():
                candidate = str(link)
                break
    if not candidate:
        return None
    path = Path(candidate)
    if path.exists() or path.is_symlink():
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())
    return shutil.which(candidate)

def verify_media_tools(ffmpeg: str | Path | None=None, ffprobe: str | Path | None=None) -> dict[str, str]:
    paths = {}
    for tool in ('ffmpeg', 'ffprobe'):
        executable = _resolve_executable(tool, ffmpeg if tool == 'ffmpeg' else ffprobe)
        if not executable:
            raise RuntimeError(f'{tool} não encontrado no PATH. Instale FFmpeg com FFprobe e reabra o PowerShell.')
        result = _run([executable, '-version'])
        if result.returncode:
            raise RuntimeError(f'{tool} não funcional: {result.stderr.strip()}')
        paths[tool] = executable
    return paths

def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False

def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())

def discover_media(source: Path, excluded_roots: tuple[Path, ...]=()) -> list[Path]:
    """Walk recursively without following links/junctions or re-reading generated media."""
    source = Path(source).resolve()
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise ValueError(f'Origem inexistente: {source}')
    candidates = []
    for directory, dirs, files in os.walk(source, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted((d for d in dirs if d not in SKIP_DIRECTORIES and (not _is_link(base / d)) and (not any((_within(base / d, root) for root in excluded_roots)))))
        for name in sorted(files):
            path = base / name
            if path.suffix.casefold() in IGNORED_EXTENSIONS:
                continue
            if _is_link(path) or not _within(path, source):
                continue
            if any((_within(path, root) for root in excluded_roots)):
                continue
            candidates.append(path)
    return candidates

def probe_media(path: Path, ffprobe: str='ffprobe') -> dict[str, Any]:
    process = _run([ffprobe, '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index,codec_name,codec_type,duration:format=duration,format_name', '-of', 'json', str(path)], timeout=120)
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or 'FFprobe não conseguiu ler o arquivo.')
    payload = json.loads(process.stdout)
    streams = payload.get('streams', [])
    duration = payload.get('format', {}).get('duration')
    if duration is None and streams:
        duration = streams[0].get('duration')
    try:
        duration = _float_or_none(duration)
    except (TypeError, ValueError):
        duration = None
    return {'has_audio': any((s.get('codec_type') == 'audio' for s in streams)), 'duration': duration, 'audio_streams': streams, 'format': payload.get('format', {}).get('format_name')}

def normalize_media(source: Path, output: Path, ffmpeg: str='ffmpeg') -> None:
    """Only write a new temporary WAV; never use the source as an output target."""
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError('Conversão exige WAV temporário novo e distinto da mídia original.')
    process = _run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-i', str(source), '-map', '0:a:0', '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', str(output)], timeout=24 * 3600)
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or 'Falha ao normalizar áudio com FFmpeg.')
    if not output.exists() or output.stat().st_size <= 44:
        raise RuntimeError('FFmpeg não produziu amostras de áudio utilizáveis.')

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def process_media(path: Path, engine: TranscriptionEngine, media_tools: dict[str, str]) -> dict[str, Any]:
    """Transcribe media without inferring speakers or changing the source."""
    result: dict[str, Any] = {'source': str(path.resolve()), 'filename': path.name, 'started_at': utc_now(), 'status': 'running', 'errors': [], 'raw_segments': []}
    stage = 'source_read'
    original_stat = None
    try:
        original_stat = path.stat()
        result['source_size'] = original_stat.st_size
        result['source_mtime_ns'] = original_stat.st_mtime_ns
        if engine.config.hash_media:
            result['sha256'] = sha256_file(path)
        stage = 'probe'
        result['media'] = probe_media(path, media_tools['ffprobe'])
        if not result['media']['has_audio']:
            result.update(status='no_audio', message='Nenhum fluxo de áudio encontrado.')
            return result
        with tempfile.TemporaryDirectory(prefix='transcribe_v3_1_') as temporary:
            normalized = Path(temporary) / 'audio_16khz_mono.wav'
            stage = 'normalization'
            normalize_media(path, normalized, media_tools['ffmpeg'])
            stage = 'transcription'
            segments, result['transcription'] = engine.transcribe(normalized)
        result['raw_segments'] = [asdict(segment) for segment in segments]
        if not any((segment.text.strip() for segment in segments)):
            result.update(status='no_speech', message='Nenhuma fala reconhecida após VAD; confira a mídia original.')
            return result
        result['status'] = 'completed'
    except Exception as exc:
        result['status'] = 'failed'
        result['errors'].append({'stage': stage, 'error_type': type(exc).__name__, 'error': str(exc)})
        if isinstance(exc, TranscriptionFailure):
            result['transcription'] = exc.audit
    finally:
        result['completed_at'] = utc_now()
        if original_stat is not None:
            try:
                after = path.stat()
                unchanged = (original_stat.st_size, original_stat.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
                if engine.config.hash_media:
                    result['sha256_after'] = sha256_file(path)
                    unchanged = unchanged and result.get('sha256') == result['sha256_after']
                result['source_unchanged'] = unchanged
                if not unchanged:
                    result['errors'].append({'stage': 'source_integrity', 'error': 'A mídia mudou durante a execução; resultado requer nova conferência.'})
                    result['status'] = 'failed'
            except OSError as exc:
                result['errors'].append({'stage': 'source_integrity', 'error': str(exc)})
                result['status'] = 'failed'
    return result
REPORT_WIDTH = 85
SECTION_WIDTH = 80
REPORT_TITLE = 'TRANSCRIÇÃO DE ÁUDIO/VÍDEO'
FINAL_OBSERVATION = 'Observação: Esta transcrição foi gerada automaticamente e pode apresentar erros de reconhecimento de palavras, pontuação, nomes, números e segmentação. O conteúdo deverá ser conferido diretamente na mídia original anexada aos autos.'
VIDEO_EXTENSIONS = {'.3gp', '.avi', '.flv', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.mts', '.m2ts', '.ogv', '.ts', '.vob', '.webm', '.wmv'}
AUDIO_EXTENSIONS = {'.aac', '.ac3', '.aiff', '.alac', '.amr', '.ape', '.flac', '.m4a', '.mp3', '.oga', '.ogg', '.opus', '.wav', '.wma'}

@dataclass
class FinalReportContent:
    filename: str
    media_type: str
    duration: str
    report_date: str
    turns: list[dict[str, str]]

def _duration_hms(seconds: float | int | None) -> str:
    if seconds is None:
        return 'Não informada'
    total = max(0, int(round(float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, second = divmod(rest, 60)
    return f'{hours:02}:{minutes:02}:{second:02}'


def _segment_time(seconds) -> str:
    """Show the original segment start, truncated to whole seconds."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 'tempo não disponível'
    if not math.isfinite(value) or value < 0:
        return 'tempo não disponível'
    hours, rest = divmod(int(value), 3600)
    minutes, second = divmod(rest, 60)
    return f'{hours:02}:{minutes:02}:{second:02}'

def _date_br(created_at: str | None) -> str:
    if created_at:
        try:
            value = str(created_at).replace('Z', '+00:00')
            return datetime.fromisoformat(value).astimezone().strftime('%d/%m/%Y')
        except ValueError:
            pass
    return datetime.now().astimezone().strftime('%d/%m/%Y')

def _media_type(source: Path, media: dict[str, Any]) -> str:
    suffix = source.suffix.casefold()
    if suffix in VIDEO_EXTENSIONS:
        return 'Vídeo'
    if suffix in AUDIO_EXTENSIONS:
        return 'Áudio'
    format_name = str(media.get('format') or '').casefold()
    if any((name in format_name for name in ('matroska', 'webm', 'mp4', 'mov', 'avi', 'mpeg'))):
        return 'Vídeo'
    return 'Áudio'

def detect_media_type(source: Path, ffprobe: str, runner=None) -> str:
    """Inspect all streams for the report; fall back to extension/known format."""
    runner = runner or subprocess.run
    command = [str(ffprobe), '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(source)]
    kwargs = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'errors': 'replace', 'timeout': 120, 'check': False}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        process = runner(command, **kwargs)
        if process.returncode == 0:
            streams = json.loads(process.stdout or '{}').get('streams', [])
            types = {stream.get('codec_type') for stream in streams}
            if 'video' in types:
                return 'Vídeo'
            if 'audio' in types:
                return 'Áudio'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return _media_type(Path(source), {})

def build_final_content(file_result: dict[str, Any], created_at: str | None=None) -> FinalReportContent:
    source = Path(file_result['source'])
    media = file_result.get('media') or {}
    duration = media.get('duration')
    if duration is None:
        duration = (file_result.get('transcription') or {}).get('duration')
    turns = []
    for segment in file_result.get('raw_segments') or []:
        text = re.sub('\\s+', ' ', segment.get('text', '')).strip()
        if text:
            turns.append({'text': text, 'timestamp': _segment_time(segment.get('start'))})
    return FinalReportContent(filename=source.name, media_type=file_result.get('report_media_type') or _media_type(source, media), duration=_duration_hms(duration), report_date=_date_br(created_at), turns=turns)

def _wrap(text: str, width: int=REPORT_WIDTH, initial_indent: str='', subsequent_indent: str='') -> list[str]:
    clean = re.sub('\\s+', ' ', text).strip()
    wrapper = textwrap.TextWrapper(width=width, break_long_words=False, break_on_hyphens=False, replace_whitespace=True, drop_whitespace=True, initial_indent=initial_indent, subsequent_indent=subsequent_indent)
    return wrapper.wrap(clean) or [initial_indent.rstrip()]

def _field_lines(label: str, value: str) -> list[str]:
    prefix = f'{label}: '
    return _wrap(value, initial_indent=prefix, subsequent_indent=' ' * len(prefix))


def _justify_text(text: str, width: int=REPORT_WIDTH) -> list[str]:
    """Fill internal spaces; preserve words and leave the last line left aligned."""
    lines = _wrap(text, width=width)
    for index, line in enumerate(lines[:-1]):
        words = line.split()
        gaps = len(words) - 1
        if gaps < 1:
            continue
        spaces, remainder = divmod(width - sum(map(len, words)), gaps)
        lines[index] = ''.join(word + ' ' * (spaces + (gap < remainder))
                               for gap, word in enumerate(words[:-1])) + words[-1]
    return lines


def render_final_text(content: FinalReportContent) -> str:
    lines = ['=' * REPORT_WIDTH, REPORT_TITLE.center(SECTION_WIDTH).rstrip(), '=' * REPORT_WIDTH, '', *_field_lines('Arquivo', content.filename), *_field_lines('Tipo de mídia', content.media_type), *_field_lines('Duração', content.duration), *_field_lines('Data da transcrição', content.report_date), '', '-' * REPORT_WIDTH, 'TRANSCRIÇÃO'.center(SECTION_WIDTH).rstrip(), '-' * REPORT_WIDTH, '']
    for turn in content.turns:
        lines.extend(_justify_text(f"[{turn['timestamp']}] {turn['text']}"))
        lines.append('')
    lines.extend(['=' * REPORT_WIDTH, 'OBSERVAÇÃO'.center(SECTION_WIDTH).rstrip(), '-' * REPORT_WIDTH, *_justify_text(FINAL_OBSERVATION.removeprefix('Observação: ')), '', 'FIM DA TRANSCRIÇÃO'.center(SECTION_WIDTH).rstrip(), '=' * REPORT_WIDTH])
    return '\n'.join(lines) + '\n'

def final_output_paths(media_path):
    media = Path(media_path).resolve()
    base = media.parent / f'Transcrição_{media.name}'
    return {'txt': Path(str(base) + '.txt'), 'docx': Path(str(base) + '.docx')}

def _atomic_text(path: Path, content: str, encoding: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'.{path.name}.{uuid4().hex}.tmp'
    try:
        temporary.write_text(content, encoding=encoding, newline='\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path

def write_final_text(path: Path, content: FinalReportContent) -> Path:
    return _atomic_text(path, render_final_text(content), 'utf-8')

def _font(run, size=12, bold=None):
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    run.font.name = 'Palatino Linotype'
    run._element.get_or_add_rPr().rFonts.set(qn('w:ascii'), 'Palatino Linotype')
    run._element.get_or_add_rPr().rFonts.set(qn('w:hAnsi'), 'Palatino Linotype')
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    run.font.color.rgb = RGBColor(0, 0, 0)
    return run


def _configure_word_format(document):
    """Formatting extracted from Normal.dotm; no external template or macros."""
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Twips

    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Twips(11906)
    section.page_height = Twips(16838)
    for side in ('top_margin', 'bottom_margin', 'left_margin', 'right_margin'):
        setattr(section, side, Twips(567))  # Approximately 1 cm, not 0.5 cm.
    section.gutter = Twips(0)
    section.header_distance = Twips(709)
    section.footer_distance = Twips(709)
    normal = document.styles['Normal']
    normal.font.name = 'Palatino Linotype'
    normal.font.size = Pt(12)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    rpr = normal.element.get_or_add_rPr()
    fonts = rpr.get_or_add_rFonts()
    for attr in ('asciiTheme', 'hAnsiTheme'):
        fonts.attrib.pop(qn('w:' + attr), None)
    fonts.set(qn('w:ascii'), 'Palatino Linotype')
    fonts.set(qn('w:hAnsi'), 'Palatino Linotype')
    lang = rpr.find(qn('w:lang'))
    if lang is None:
        lang = OxmlElement('w:lang')
        rpr.append(lang)
    lang.set(qn('w:val'), 'pt-BR')
    # Word also reads the document defaults for spelling and new text.
    defaults = document.styles.element.find(qn('w:docDefaults'))
    if defaults is None:
        defaults = OxmlElement('w:docDefaults')
        document.styles.element.insert(0, defaults)
    run_defaults = defaults.find(qn('w:rPrDefault'))
    if run_defaults is None:
        run_defaults = OxmlElement('w:rPrDefault')
        defaults.append(run_defaults)
    default_properties = run_defaults.find(qn('w:rPr'))
    if default_properties is None:
        default_properties = OxmlElement('w:rPr')
        run_defaults.append(default_properties)
    default_lang = default_properties.find(qn('w:lang'))
    if default_lang is None:
        default_lang = OxmlElement('w:lang')
        default_properties.append(default_lang)
    default_lang.set(qn('w:val'), 'pt-BR')
    paragraph = normal.paragraph_format
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    paragraph.line_spacing = 1.0
    paragraph.space_before = Pt(0)
    paragraph.space_after = Pt(0)
    paragraph.left_indent = Pt(0)
    paragraph.right_indent = Pt(0)
    paragraph.first_line_indent = Pt(0)
    paragraph.keep_together = True
    tabstop = document.settings.element.find(qn('w:defaultTabStop'))
    if tabstop is None:
        tabstop = OxmlElement('w:defaultTabStop')
        document.settings.element.append(tabstop)
    tabstop.set(qn('w:val'), '708')
    theme_lang = document.settings.element.find(qn('w:themeFontLang'))
    if theme_lang is not None:
        theme_lang.set(qn('w:val'), 'pt-BR')

def write_final_docx(path: Path, content: FinalReportContent) -> Path:
    from docx import Document
    document = Document()
    _configure_word_format(document)
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    docx_title = 'TRANSCRIÇÃO DE ÁUDIO/VÍDEO'
    document.core_properties.title = docx_title

    def label(text, centered=False):
        paragraph = document.add_paragraph(style='Normal')
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if centered else WD_ALIGN_PARAGRAPH.LEFT
        paragraph.paragraph_format.keep_with_next = True
        run = _font(paragraph.add_run(text), size=14, bold=True)
        run.underline = True
        return paragraph

    label(docx_title, centered=True)
    document.add_paragraph()
    for name, value in (
        ('Arquivo', content.filename), ('Tipo de mídia', content.media_type),
        ('Duração', content.duration), ('Data da transcrição', content.report_date),
    ):
        paragraph = document.add_paragraph(style='Normal')
        _font(paragraph.add_run(name + ': '), bold=True)
        _font(paragraph.add_run(value))
    document.add_paragraph()
    label('TRANSCRIÇÃO:')
    for turn in content.turns:
        document.add_paragraph(f"[{turn['timestamp']}] {turn['text']}", style='Normal')
        document.add_paragraph()
    label('OBSERVAÇÃO:')
    document.add_paragraph(FINAL_OBSERVATION.removeprefix('Observação: '), style='Normal')
    document.add_paragraph()
    label('FIM DA TRANSCRIÇÃO', centered=True)
    # The reference template has no header/footer content or page numbers.
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f'.{output.name}.{uuid4().hex}.tmp.docx'
    try:
        document.save(temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


ASR_PROFILE = {'version': '1.0', 'description': 'Vocabulário contextual policial para reconhecimento de fala.', 'initial_prompt': 'Transcrição de oitiva da Polícia Civil. Termos institucionais: escrivã, escrivão, delegado, delegada, autoridade policial, declarante, interrogado, interrogada, investigado, investigada, depoente, testemunha, vítima, boletim de ocorrência, inquérito policial, termo de declarações e termo de interrogatório. Termos jurídicos: apropriação indébita, fórum e pessoal do fórum.', 'hotwords': ['escrivã', 'escrivão', 'escrivã de polícia', 'escrivão de polícia', 'delegado', 'delegada', 'delegado de polícia', 'delegada de polícia', 'Polícia Civil', 'autoridade policial', 'declarante', 'interrogado', 'interrogada', 'investigado', 'investigada', 'depoente', 'testemunha', 'vítima', 'boletim de ocorrência', 'inquérito policial', 'termo de declarações', 'termo de interrogatório', 'oitiva', 'apropriação indébita', 'fórum', 'pessoal do fórum']}

class _ContextualWhisperModel:
    """Transparent proxy that adds decoder context without changing engine policy."""

    def __init__(self, model: Any, profile: dict[str, Any]):
        self._wrapped_model = model
        self._initial_prompt = profile['initial_prompt']
        self._hotwords = ', '.join(profile['hotwords'])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped_model, name)

    def transcribe(self, audio, **parameters):
        parameters.setdefault('initial_prompt', self._initial_prompt)
        parameters.setdefault('hotwords', self._hotwords)
        return self._wrapped_model.transcribe(audio, **parameters)

def build_contextual_model_factory(profile_path: str | Path | None=None, base_factory: Callable | None=None) -> Callable:
    """Return a model factory compatible with TranscriptionEngine.model_factory."""
    profile = ASR_PROFILE

    def factory(model_name, **parameters):
        constructor = base_factory
        if constructor is None:
            from faster_whisper import WhisperModel
            constructor = WhisperModel
        return _ContextualWhisperModel(constructor(model_name, **parameters), profile)
    return factory

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Transcrição em precisão, com relatórios TXT e Word.')
    parser.add_argument('origem', nargs='?', help='Arquivo ou pasta de mídias.')
    parser.add_argument('--modelo', help='Modelo faster-whisper ou diretório CTranslate2.')
    parser.add_argument('--dispositivo', choices=('auto', 'cuda', 'cpu'), default='auto')
    parser.add_argument('--beam-size', type=int, default=5)
    parser.add_argument('--cpu-threads', type=int, default=0)
    parser.add_argument('--ffmpeg', type=Path)
    parser.add_argument('--ffprobe', type=Path)
    parser.add_argument('--hash', action='store_true')
    return parser

def clean_user_path(value: str | Path) -> Path:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and (text[0] in ('"', "'")):
        text = text[1:-1].strip()
    return Path(text).expanduser()

def choose_source(argument: str | Path | None, *, input_fn=None, output_fn=None) -> Path:
    input_fn = input if input_fn is None else input_fn
    output_fn = print if output_fn is None else output_fn
    if argument is not None:
        try:
            return clean_user_path(argument).resolve(strict=True)
        except OSError as error:
            raise ValueError('A pasta ou arquivo informado não existe ou não é válido.') from error
    while True:
        raw = input_fn('Informe o caminho da pasta onde estão os arquivos de áudio/vídeo: ')
        if not str(raw).strip():
            output_fn('ERRO: A pasta informada não existe ou não é válida.')
            continue
        try:
            source = clean_user_path(raw).resolve(strict=True)
        except OSError:
            output_fn('ERRO: A pasta informada não existe ou não é válida.')
            continue
        if not source.is_dir():
            output_fn('ERRO: A pasta informada não existe ou não é válida.')
            continue
        return source

def use_local_environment():
    """Use this machine's prepared environment even when launched with system Python."""
    local_python = Path(__file__).resolve().parent / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if local_python.is_file() and Path(sys.prefix).resolve() != local_python.parent.parent.resolve():
        return subprocess.call([str(local_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    return None

def resolve_media_tools(ffmpeg=None, ffprobe=None):
    try:
        return verify_media_tools(ffmpeg, ffprobe)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if ffmpeg or ffprobe:
            raise
        try:
            from static_ffmpeg import run
            portable_ffmpeg, portable_ffprobe = run.get_or_fetch_platform_executables_else_raise()
            return verify_media_tools(portable_ffmpeg, portable_ffprobe)
        except ImportError as error:
            raise RuntimeError('Execute primeiro Testar_máquina.py para preparar FFmpeg/FFprobe.') from error

def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print('TRANSCRITOR — PRECISÃO (large-v3-turbo)')
        source = choose_source(args.origem)
        config = Config(device=args.dispositivo, model=args.modelo, beam_size=args.beam_size, cpu_threads=args.cpu_threads, hash_media=args.hash)
        tools = resolve_media_tools(args.ffmpeg, args.ffprobe)
        files = discover_media(source)
        if not files:
            print('Nenhuma mídia candidata encontrada.')
            return 0
        engine = TranscriptionEngine(config, collect_hardware_diagnostics(tools['ffmpeg'], tools['ffprobe']), model_factory=build_contextual_model_factory(), runtime_logger=print)
        failed = 0
        for index, path in enumerate(files, 1):
            print(f'[{index}/{len(files)}] {path.name}', flush=True)
            result = process_media(path, engine, tools)
            if result['status'] != 'completed':
                print('  ' + result.get('message', 'Falha na transcrição.'))
                for error in result.get('errors', []):
                    print(f"  ERRO: {error['error']}")
                failed += result['status'] == 'failed'
                continue
            result['report_media_type'] = detect_media_type(path, tools['ffprobe'])
            content = build_final_content(result, utc_now())
            outputs = final_output_paths(path)
            try:
                write_final_text(outputs['txt'], content)
                print(f"  TXT: {outputs['txt']}")
                write_final_docx(outputs['docx'], content)
                print(f"  DOCX: {outputs['docx']}")
            except Exception as error:
                print(f'  ERRO ao salvar relatório: {error}')
                failed += 1
        print('Concluído.' if not failed else f'Concluído com {failed} falha(s).')
        return 1 if failed else 0
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f'ERRO: {error}', file=sys.stderr)
        print('Execute Testar_máquina.py para verificar os requisitos.')
        return 2
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    code = use_local_environment()
    raise SystemExit(main() if code is None else code)
