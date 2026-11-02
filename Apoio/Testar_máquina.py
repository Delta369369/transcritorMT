"""Prepara um ambiente local e valida os requisitos do transcritor.

Não requer privilégios de administrador. Instalações e modelo exigem internet.
"""
import argparse
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
from uuid import uuid4
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"


def requirements_for(group):
    groups = {"base": [], "media": [], "gpu-windows": []}
    active = "base"
    seen = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith('# [') and line.endswith(']'):
            active = line[3:-1]
            if active not in groups:
                raise ValueError(f"Grupo desconhecido em requirements.txt: {active}")
        elif line and not line.startswith('#'):
            match = re.match(r'[A-Za-z0-9][A-Za-z0-9_.-]*', line)
            if not match:
                raise ValueError(f"Requisito inválido: {line}")
            name = re.sub(r'[-_.]+', '-', match[0]).lower()
            if name in seen:
                raise ValueError(f"Dependência duplicada: {name}")
            seen.add(name)
            groups[active].append(line)
    if any(not items for items in groups.values()):
        raise ValueError("requirements.txt deve conter os grupos base, media e gpu-windows.")
    return groups[group]


def run(command, **kwargs):
    allowed_exit_codes = kwargs.pop("allowed_exit_codes", (0,))
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    quiet = kwargs.get("capture_output", False)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("env", {**os.environ, "PYTHONIOENCODING": "utf-8"})
    result = subprocess.run(command, check=False, **kwargs)
    if not quiet:
        if result.stdout:
            print(result.stdout.rstrip(), flush=True)
        if result.stderr:
            print(result.stderr.rstrip(), flush=True)
    if result.returncode not in allowed_exit_codes:
        result.check_returncode()
    return result


def probe_python312(command):
    """Return the base executable only for standard 64-bit Python 3.12."""
    code = ("import sys,struct; assert sys.version_info[:2]==(3,12); "
            "assert struct.calcsize('P')==8; print(sys._base_executable)")
    try:
        result = run([*map(str, command), '-c', code], capture_output=True, timeout=30,
                     env={**os.environ, 'PYTHONIOENCODING': 'utf-8',
                          'PYTHON_MANAGER_AUTOMATIC_INSTALL': 'false'})
        path = Path(result.stdout.strip())
        return path if path.is_file() else None
    except (OSError, subprocess.SubprocessError):
        return None


def find_python312():
    candidates = [Path(getattr(sys, '_base_executable', sys.executable))]
    if os.name == 'nt':
        local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
        candidates.extend((local / 'Programs/Python/Python312/python.exe',
                           local / 'Programs/Python/Python312_Transcritor/python.exe',
                           Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Python312/python.exe'))
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for tag in ('3.12', '3.12-64'):
                try:
                    with winreg.OpenKey(hive, rf'Software\Python\PythonCore\{tag}\InstallPath') as key:
                        candidates.append(Path(winreg.QueryValue(key, None)) / 'python.exe')
                except OSError:
                    pass
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            found = probe_python312([candidate])
            if found:
                return found
    launcher = shutil.which('py.exe') if os.name == 'nt' else shutil.which('python3.12')
    if launcher:
        return probe_python312([launcher, '-3.12-64'] if os.name == 'nt' else [launcher])
    return None


def ensure_python312():
    found = find_python312()
    if found:
        print(f"Python 3.12 (64 bits) disponível: {found}", flush=True)
        return found
    if os.name != 'nt':
        raise RuntimeError('Instale Python 3.12 de 64 bits para criar o ambiente do transcritor.')
    # Prefer the official manager, which can provide current 3.12 patch releases.
    manager = shutil.which('pymanager.exe') or shutil.which('py.exe')
    if manager:
        try:
            run([manager, 'help', 'install'], capture_output=True, timeout=30)
            print('Instalando Python 3.12 de 64 bits pelo gerenciador oficial...', flush=True)
            run([manager, 'install', '3.12-64'], timeout=900,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHON_MANAGER_CONFIRM': 'false'})
            found = find_python312() or probe_python312([manager, 'exec', '-V:3.12-64'])
            if found:
                return found
        except (OSError, subprocess.SubprocessError):
            print('Gerenciador indisponível; tentando instalador oficial do Python.', flush=True)
    # Last official full Windows installer for the validated 3.12 series.
    url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
    target = local / 'Programs/Python/Python312_Transcritor'
    print('Baixando o instalador oficial do Python 3.12 (64 bits)...', flush=True)
    with tempfile.TemporaryDirectory(prefix='transcritor_python_') as temporary:
        installer = Path(temporary) / 'python-3.12.10-amd64.exe'
        with urllib.request.urlopen(url, timeout=60) as response, installer.open('wb') as output:
            shutil.copyfileobj(response, output)
        # Do not execute downloads unless Windows validates the PSF signature.
        verification = ("$s=Get-AuthenticodeSignature -LiteralPath $env:TRANSCRITOR_INSTALLER; "
                        "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Python Software Foundation*') "
                        "{ throw 'Assinatura do instalador Python não validada.' }")
        run(['powershell.exe', '-NoProfile', '-Command', verification], timeout=120,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'TRANSCRITOR_INSTALLER': str(installer)})
        print('Instalando Python 3.12 sem alterar o Python padrão do computador...', flush=True)
        run([str(installer), '/quiet', '/norestart', 'InstallAllUsers=0', 'PrependPath=0',
             'Include_launcher=0', 'Include_test=0', 'Include_pip=1', 'AssociateFiles=0',
             'Shortcuts=0', f'TargetDir={target}'], timeout=900, allowed_exit_codes=(0, 3010))
    found = probe_python312([target / 'python.exe'])
    if not found:
        raise RuntimeError('A instalação do Python 3.12 não pôde ser validada. Confira a saída acima.')
    return found


def python_in_environment():
    return ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_environment(base_python):
    executable = python_in_environment()
    healthy = False
    if executable.is_file():
        try:
            result = run([str(executable), "-c", "import sys,struct; assert sys.version_info[:2]==(3,12); assert struct.calcsize('P')==8; print(sys.prefix)"],
                         capture_output=True, text=True, timeout=30)
            healthy = Path(result.stdout.strip()).resolve() == ENV.resolve()
        except (OSError, subprocess.SubprocessError):
            pass
    if not healthy and ENV.exists():
        # A copied venv can point to a Python installation absent on this machine.
        if ENV.is_symlink() or getattr(ENV, "is_junction", lambda: False)() or ENV.resolve().parent != ROOT.resolve():
            raise RuntimeError("A .venv aponta para fora da pasta do programa; preparação cancelada.")
        backup = ROOT / ('.venv_anterior_' + uuid4().hex[:8])
        if backup.resolve().parent != ROOT.resolve():
            raise RuntimeError("Caminho de cópia de segurança inválido.")
        print(f"Ambiente incompatível. Preservando em {backup.name} e criando outro...", flush=True)
        ENV.rename(backup)
    if not healthy:
        print("Criando ambiente Python local...", flush=True)
        run([str(base_python), "-m", "venv", str(ENV)])
    try:
        run([str(executable), "-m", "pip", "--version"], capture_output=True, timeout=30)
    except subprocess.CalledProcessError:
        run([str(executable), "-m", "ensurepip", "--upgrade"])
    return executable


def ensure_package(executable, requirement):
    # Execute imports in a fresh process so repaired packages are never cached.
    distribution = re.match(r'[A-Za-z0-9][A-Za-z0-9_.-]*', requirement)[0]
    module = {
        'python-docx': 'docx',
        'nvidia-cublas-cu12': 'nvidia.cublas',
        'nvidia-cudnn-cu12': 'nvidia.cudnn',
    }.get(distribution, distribution.replace('-', '_'))
    code = (
        "import importlib, importlib.metadata; from pip._vendor.packaging.requirements import Requirement; "
        f"r=Requirement({requirement!r}); "
        "assert not r.marker or r.marker.evaluate(), 'Requisito incompatível com este sistema'; "
        "v=importlib.metadata.version(r.name); assert r.specifier.contains(v); "
        f"importlib.import_module({module!r})"
    )
    try:
        run([str(executable), "-c", code], capture_output=True, timeout=120)
        print(f"OK: {distribution}")
        return
    except (OSError, subprocess.SubprocessError):
        print(f"Instalando/reparando {distribution}...", flush=True)
    run([str(executable), "-m", "pip", "install", "-c", str(REQUIREMENTS), requirement])
    try:
        run([str(executable), "-c", code], capture_output=True, timeout=120)
    except subprocess.SubprocessError:
        # A matching version can still contain broken native dependencies.
        run([str(executable), "-m", "pip", "install", "--force-reinstall", "-c", str(REQUIREMENTS), requirement])
        run([str(executable), "-c", code], timeout=120)


def prepare_media_tools(executable):
    try:
        from Transcrição_Única import verify_media_tools
        verify_media_tools()
        print("OK: FFmpeg e FFprobe do sistema")
        return
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("Preparando FFmpeg e FFprobe portáteis...", flush=True)
    for requirement in requirements_for("media"):
        ensure_package(executable, requirement)
    run([str(executable), "-c",
         "from static_ffmpeg import run; from Transcrição_Única import verify_media_tools; "
         "a,b=run.get_or_fetch_platform_executables_else_raise(); "
         "verify_media_tools(a,b); print('OK: FFmpeg e FFprobe portáteis')"], cwd=str(ROOT))


def validate_runtime(executable, download_model=True):
    # Exercise decoding and the actual DOCX writer, rather than imports alone.
    code = """
import tempfile, wave
from pathlib import Path
import ctranslate2
from faster_whisper.audio import decode_audio
from faster_whisper.vad import get_speech_timestamps
from docx import Document
from Transcrição_Única import resolve_media_tools, normalize_media, probe_media
from Transcrição_Única import build_final_content, write_final_docx, Config, TranscriptionEngine
assert 'int8' in ctranslate2.get_supported_compute_types('cpu'), 'CPU sem suporte INT8'
tools=resolve_media_tools()
with tempfile.TemporaryDirectory() as tmp:
    tmp=Path(tmp)
    audio=tmp/'teste.wav'
    with wave.open(str(audio), 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(b'\0'*32000)
    assert probe_media(audio, tools['ffprobe'])['has_audio']
    normalized=tmp/'normalizado.wav'
    normalize_media(audio, normalized, tools['ffmpeg'])
    samples=decode_audio(str(normalized))
    assert len(samples) > 0
    assert get_speech_timestamps(samples) == []
    report=tmp/'teste.docx'
    content=build_final_content({'source':str(audio),'raw_segments':[{'start':0.0,'text':'Teste de transcrição.'}]})
    write_final_docx(report, content)
    assert any('Teste de transcrição.' in p.text for p in Document(report).paragraphs)
    if DOWNLOAD_MODEL:
        print('Verificando/baixando large-v3-turbo (vários GB na primeira execução)...', flush=True)
        engine=TranscriptionEngine(Config(device='cpu'), diagnostics={})
        model=engine._load_model('large-v3-turbo','cpu','int8')
        segments,info=model.transcribe(str(normalized), language='pt', vad_filter=False, beam_size=1)
        list(segments)
        print('OK: inferência CPU com large-v3-turbo')
print('OK: áudio, processamento CPU e relatório Word')
"""
    # Keep backslash-zero literal inside the child Python source.
    code = code.replace("\x00", "\\0")
    run([str(executable), "-c", "DOWNLOAD_MODEL=" + repr(download_model) + "\n" + code], cwd=str(ROOT))


def prepare_gpu(executable, test_model=True):
    """GPU is optional; validate in a child process to contain native failures."""
    from Transcrição_Única import query_nvidia_inventory
    inventory = query_nvidia_inventory()
    if not inventory:
        print("GPU NVIDIA não detectada; execução em CPU.", flush=True)
        return
    print(f"GPU detectada: {inventory[0]['name']}", flush=True)
    try:
        if os.name == 'nt':
            probe = "from Transcrição_Única import configure_cuda_libraries; configure_cuda_libraries(); import ctypes; " + "; ".join(
                f"ctypes.WinDLL({dll!r})" for dll in ('cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll'))
            try:
                run([str(executable), '-c', probe], capture_output=True, timeout=60, cwd=str(ROOT))
            except subprocess.SubprocessError:
                print("Preparando cuBLAS/cuDNN na .venv (download volumoso)...", flush=True)
                for requirement in requirements_for("gpu-windows"):
                    ensure_package(executable, requirement)
                run([str(executable), '-c', probe], timeout=60, cwd=str(ROOT))
        if not test_model:
            print("Bibliotecas GPU preparadas; inferência GPU ainda não validada.", flush=True)
            return
        print("Testando inferência real na GPU...", flush=True)
        code = """
from Transcrição_Única import configure_cuda_libraries
configure_cuda_libraries()
import ctranslate2, numpy as np
from faster_whisper import WhisperModel
supported=ctranslate2.get_supported_compute_types('cuda', 0)
compute=next((x for x in ('int8_float16','float16','int8') if x in supported), None)
assert compute, 'GPU sem tipo de cálculo compatível'
model=WhisperModel('large-v3-turbo', device='cuda', compute_type=compute)
segments,info=model.transcribe(np.zeros(16000, dtype=np.float32), language='pt', vad_filter=False, beam_size=1)
list(segments)
assert model.model.device == 'cuda'
print('OK: GPU validada por inferência (' + compute + ')')
"""
        run([str(executable), '-c', code], cwd=str(ROOT), timeout=600)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        detail = f"código {error.returncode}" if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"GPU pendente ({detail}); CPU permanece disponível. Drivers NVIDIA não são instalados automaticamente.", flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Verifica e instala os requisitos do transcritor nesta máquina.")
    parser.add_argument("--sem-modelo", action="store_true",
                        help="Verifica apenas requisitos; não baixa nem testa large-v3-turbo. Não declara máquina pronta.")
    parser.add_argument("--cpu", action="store_true", help="Prepara somente CPU; não instala nem testa bibliotecas NVIDIA.")
    args = parser.parse_args(argv)
    print(f"Máquina: {platform.system()} {platform.machine()} | Python inicial: {platform.python_version()}", flush=True)
    if not (ROOT / "Transcrição_Única.py").is_file():
        print("Coloque Testar_máquina.py e Transcrição_Única.py na mesma pasta.")
        return 1
    try:
        base_python = ensure_python312()
        free_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)
        print(f"Espaço livre na unidade do projeto: {free_gb:.1f} GB")
        executable = prepare_environment(base_python)
        for requirement in requirements_for("base"):
            ensure_package(executable, requirement)
        prepare_media_tools(executable)
        print("Testando áudio e Word" + (" e carregando large-v3-turbo (download de vários GB no primeiro uso)..." if not args.sem_modelo else "..."), flush=True)
        validate_runtime(executable, download_model=not args.sem_modelo)
        if args.cpu:
            print("CPU validada; GPU não solicitada.")
        else:
            prepare_gpu(executable, test_model=not args.sem_modelo)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(f"Máquina pendente: uma verificação/instalação terminou com código {error.returncode}.")
        else:
            print(f"Máquina pendente: {error}")
        print("Confira o erro acima, a conexão com a internet e a permissão de escrita na pasta.")
        return 1
    if args.sem_modelo:
        print("Requisitos OK. Modelo ainda não validado; execute sem --sem-modelo para concluir.")
    else:
        print("Maquina OK!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
