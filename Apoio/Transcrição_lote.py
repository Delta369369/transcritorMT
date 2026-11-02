"""Transcrição genérica em lote, com TXT e DOCX consolidados por execução."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# Reuse the validated engine without modifying the single-transcription script.
core = importlib.import_module('Transcrição_Única')
STATUS_MESSAGES = {
    'failed': 'Não foi possível transcrever este arquivo.',
    'no_audio': 'Nenhum fluxo de áudio encontrado neste arquivo.',
    'no_speech': 'Nenhuma fala reconhecida; confira a mídia original.',
}


def use_local_environment():
    python = Path(__file__).resolve().parent / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if python.is_file() and Path(sys.prefix).resolve() != python.parent.parent.resolve():
        return subprocess.call([str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    return None


def batch_item(result, created_at, index, total, source):
    """Prepare a consolidated section; never infer speakers or alter wording."""
    content = core.build_final_content(result, created_at)
    text = ' '.join(turn['text'] for turn in content.turns)
    messages = []
    if result['status'] != 'completed':
        messages.append(result.get('message') or STATUS_MESSAGES.get(result['status'], 'Arquivo não concluído.'))
        messages.extend(error.get('error', '') for error in result.get('errors', []) if error.get('error'))
    return {
        'heading': f'ARQUIVO_{index:02d}/{total:02d}',
        # Relative paths distinguish files with the same name in subfolders.
        'filename': str(Path(result['source']).relative_to(source)),
        'media_type': content.media_type,
        'duration': content.duration,
        'report_date': content.report_date,
        'text': text,
        'messages': messages,
        'status': result['status'],
        'total': total,
    }


def render_batch_text(items):
    rule = '=' * core.REPORT_WIDTH
    separator = '-' * core.REPORT_WIDTH
    fine_separator = '- ' * (core.REPORT_WIDTH // 2) + '-'
    lines = [rule, 'TRANSCRIÇÃO DE ÁUDIO/VÍDEO'.center(core.SECTION_WIDTH).rstrip(), rule, '']
    for item in items:
        lines.extend([
            item['heading'].center(77).rstrip(),
            *core._wrap(f"Nome/Formato: {item['filename']};"),
            f"Tipo de mídia: {item['media_type']};",
            f"Data da transcrição: {item['report_date']}",
            f"Duração: {item['duration']};",
            fine_separator,
        ])
        if item['messages']:
            for message in item['messages']:
                lines.extend(core._wrap('AVISO: ' + message))
        lines.extend(core._justify_text('TRANSCRIÇÃO:' + item['text']))
        lines.extend([rule, ''])
    if items and len(items) < items[-1]['total']:
        lines.extend(core._wrap(f"LOTE PARCIAL: {len(items)} de {items[-1]['total']} arquivos processados."))
        lines.append('')
    lines.extend([
        'FIM DA TRANSCRIÇÃO'.center(core.SECTION_WIDTH).rstrip(),
        '', '', '', separator, 'OBSERVAÇÃO'.center(core.SECTION_WIDTH).rstrip(),
        *core._justify_text(core.FINAL_OBSERVATION.removeprefix('Observação: ')), rule,
    ])
    return '\n'.join(lines) + '\n'


def write_batch_docx(path, items):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    document = Document()
    core._configure_word_format(document)
    document.core_properties.title = 'TRANSCRIÇÃO DE ÁUDIO/VÍDEO EM LOTE'

    def label(text, centered=False):
        paragraph = document.add_paragraph(style='Normal')
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if centered else WD_ALIGN_PARAGRAPH.LEFT
        paragraph.paragraph_format.keep_with_next = True
        core._font(paragraph.add_run(text), size=14, bold=True).underline = True

    def metadata(fields):
        paragraph = document.add_paragraph(style='Normal')
        for position, (name, value) in enumerate(fields):
            if position:
                core._font(paragraph.add_run('; '))
            core._font(paragraph.add_run(name + ': '), bold=True)
            core._font(paragraph.add_run(value))
        paragraph.paragraph_format.keep_with_next = True

    label('TRANSCRIÇÃO DE ÁUDIO/VÍDEO', centered=True)
    document.add_paragraph()
    for item in items:
        label(item['heading'], centered=True)
        metadata([('Nome/Formato', item['filename'] + ';')])
        metadata([('Tipo de mídia', item['media_type'] + ';')])
        metadata([('Data da transcrição', item['report_date'])])
        metadata([('Duração', item['duration'] + ';')])
        for message in item['messages']:
            paragraph = document.add_paragraph('AVISO: ' + message, style='Normal')
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph = document.add_paragraph(style='Normal')
        core._font(paragraph.add_run('TRANSCRIÇÃO:'), size=14, bold=True).underline = True
        core._font(paragraph.add_run(item['text']))
        # A long recording must flow naturally over multiple pages.
        paragraph.paragraph_format.keep_together = False
        document.add_paragraph()
    label('FIM DA TRANSCRIÇÃO', centered=True)
    for _ in range(3):
        document.add_paragraph()
    label('OBSERVAÇÃO', centered=True)
    document.add_paragraph(core.FINAL_OBSERVATION.removeprefix('Observação: '), style='Normal')
    path = Path(path)
    temporary = path.parent / f'.{path.name}.{core.uuid4().hex}.tmp.docx'
    try:
        document.save(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def output_paths(directory):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / ('Transcrição_Lote_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    counter = 1
    while any(Path(str(base) + extension).exists() for extension in ('.txt', '.docx')):
        base = directory / (f'Transcrição_Lote_{datetime.now():%Y%m%d_%H%M%S}_{counter}')
        counter += 1
    return {'txt': Path(str(base) + '.txt'), 'docx': Path(str(base) + '.docx')}


def main(argv=None):
    parser = core.build_parser()
    parser.description = 'Transcrição genérica em lote: uma pasta, um TXT e um Word consolidados.'
    parser.add_argument('--saida', type=Path, help='Pasta dos relatórios; por padrão, a própria pasta de origem.')
    args = parser.parse_args(argv)
    try:
        print('TRANSCRIÇÃO EM LOTE — large-v3-turbo', flush=True)
        source = core.choose_source(args.origem)
        if not source.is_dir():
            raise ValueError('Para transcrever em lote, informe uma pasta de áudios/vídeos.')
        files = core.discover_media(source)
        if not files:
            print('Nenhuma mídia candidata encontrada.')
            return 0
        tools = core.resolve_media_tools(args.ffmpeg, args.ffprobe)
        config = core.Config(device=args.dispositivo, model=args.modelo, beam_size=args.beam_size,
                             cpu_threads=args.cpu_threads, hash_media=args.hash)
        # No institutional prompt/hotwords for general-purpose recordings.
        engine = core.TranscriptionEngine(config, core.collect_hardware_diagnostics(tools['ffmpeg'], tools['ffprobe']),
                                          runtime_logger=lambda line: print(line, flush=True))
        paths = output_paths(args.saida or source)
        created_at = core.utc_now()
        items = []
        failed = 0
        for index, path in enumerate(files, 1):
            print(f'[{index}/{len(files)}] {path.relative_to(source)}', flush=True)
            result = core.process_media(path, engine, tools)
            result['report_media_type'] = core.detect_media_type(path, tools['ffprobe'])
            items.append(batch_item(result, created_at, index, len(files), source))
            if result['status'] == 'failed':
                failed += 1
            if result['status'] != 'completed':
                print('  ' + '; '.join(items[-1]['messages']), flush=True)
            # Persist each finished file so an interrupted batch retains its work.
            core._atomic_text(paths['txt'], render_batch_text(items), 'utf-8')
        write_batch_docx(paths['docx'], items)
        print(f"TXT: {paths['txt']}\nDOCX: {paths['docx']}")
        completed = sum(item['status'] == 'completed' for item in items)
        no_audio = sum(item['status'] == 'no_audio' for item in items)
        no_speech = sum(item['status'] == 'no_speech' for item in items)
        print(f'Lote concluído: {completed} transcrito(s), {no_audio} sem áudio, {no_speech} sem fala, {failed} falha(s).')
        return 1 if failed else 0
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f'ERRO: {error}', file=sys.stderr)
        print('Execute a opção 1 para verificar as dependências.')
        return 2


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    code = use_local_environment()
    raise SystemExit(main() if code is None else code)
