#!/usr/bin/env python3
"""Кладёт расшифровку видео с YouTube в raw/ как источник для вики.

    python3 tools/yt2raw.py <url> [--lang ru] [--bucket 45]

Забирает субтитры без скачивания видео, склеивает бегущую строку автосубтитров,
расставляет таймкоды и пишет файл с фронтматтером. Дальше — обычный /ingest.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

def find_ytdlp():
    """Сборка из pipx умеет подменять TLS-отпечаток, сборка из Homebrew — нет.

    YouTube без этого отвечает 429 на эндпоинте субтитров, поэтому сборка
    с impersonation предпочитается, если она установлена.
    """
    home = os.path.expanduser('~/.local/bin/yt-dlp')
    if os.path.exists(home):
        return home
    found = shutil.which('yt-dlp')
    if not found:
        sys.exit('yt-dlp не найден. Установить: pipx install "yt-dlp[default,curl-cffi]"')
    return found


YTDLP = find_ytdlp()

TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
    'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def slugify(text, limit=48):
    out = []
    for ch in text.lower():
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
        elif ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        else:
            out.append('-')
    slug = re.sub(r'-+', '-', ''.join(out)).strip('-')
    if len(slug) > limit:
        slug = slug[:limit].rsplit('-', 1)[0]
    return slug or 'video'


def run(cmd, cookies=None, quiet=False):
    """YouTube отдаёт 429 на субтитрах без авторизации — тогда повторяем с куками."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return proc.stdout
    if cookies:
        if not quiet:
            print('YouTube отказал, повтор с куками из %s' % cookies, file=sys.stderr)
        proc = subprocess.run(cmd + ['--cookies-from-browser', cookies],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
    sys.exit('yt-dlp упал:\n' + proc.stderr.strip())


def pick_track(meta, want):
    """Язык оригинала важнее удобного.

    YouTube отдаёт автосубтитры на десятках языков, но все кроме одного —
    машинный перевод машинного же распознавания. Второй лишний слой искажает
    формулировки незаметно, поэтому берётся язык, на котором говорят в кадре.
    Авторские субтитры предпочитаются автоматическим: в них есть пунктуация.
    """
    manual = meta.get('subtitles') or {}
    auto = meta.get('automatic_captions') or {}
    original = (meta.get('language') or '').split('-')[0]
    order = [x for x in (want, original, 'en', 'ru') if x]
    for source, label in ((manual, 'авторские'), (auto, 'автоматические')):
        codes = sorted(source)
        for pref in order:
            for match in (lambda c: c == pref,
                          lambda c: c.startswith(pref + '-'),
                          lambda c: c.split('-')[0] == pref):
                for code in codes:
                    if match(code):
                        return code, label
    return None, None


def parse_vtt(path):
    """Возвращает [(секунды, строка)] без дублей бегущей строки."""
    cues = []
    current = None
    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        stamp = re.match(r'^(\d\d):(\d\d):(\d\d)\.(\d\d\d)\s+-->', line)
        if stamp:
            h, m, s, _ = stamp.groups()
            current = int(h) * 3600 + int(m) * 60 + int(s)
            continue
        if current is None or not line.strip():
            continue
        if line.startswith(('WEBVTT', 'Kind:', 'Language:', 'NOTE')):
            continue
        text = re.sub(r'<[^>]+>', '', line)
        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            continue
        if cues and cues[-1][1] == text:
            continue
        cues.append((current, text))
    return cues


def to_paragraphs(cues, bucket, width=600):
    """Абзац на каждые ~width символов, с таймкодом в начале."""
    out = []
    chunk, start = [], None
    last_mark = -bucket
    for sec, text in cues:
        if start is None:
            start = sec
        chunk.append(text)
        if sum(len(x) + 1 for x in chunk) >= width:
            mark = ''
            if start - last_mark >= bucket:
                mark = '[%d:%02d] ' % (start // 60, start % 60)
                last_mark = start
            out.append(mark + ' '.join(chunk))
            chunk, start = [], None
    if chunk:
        out.append('[%d:%02d] ' % (start // 60, start % 60) + ' '.join(chunk))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('--lang', default=None,
                    help='язык субтитров; по умолчанию берётся язык оригинала')
    ap.add_argument('--bucket', type=int, default=45, help='секунд между таймкодами')
    ap.add_argument('--out', default='raw', help='каталог назначения')
    ap.add_argument('--cookies', default='chrome',
                    help='браузер, из которого брать куки при отказе YouTube; none чтобы не пробовать')
    args = ap.parse_args()

    cookies = None if args.cookies == 'none' else args.cookies
    meta = json.loads(run([YTDLP, '--dump-single-json', '--skip-download', args.url], cookies))
    code, kind = pick_track(meta, args.lang)
    if not code:
        sys.exit('Субтитров нет ни на одном языке. Нужен запасной путь через распознавание аудио.')

    with tempfile.TemporaryDirectory() as tmp:
        run([YTDLP, '--skip-download', '--write-subs', '--write-auto-subs',
             '--sub-langs', code, '--sub-format', 'vtt/best', '--convert-subs', 'vtt',
             '-o', os.path.join(tmp, 'sub'), args.url], cookies)
        files = [f for f in os.listdir(tmp) if f.endswith('.vtt')]
        if not files:
            sys.exit('yt-dlp не отдал файл субтитров для языка ' + code)
        cues = parse_vtt(os.path.join(tmp, files[0]))

    if not cues:
        sys.exit('Файл субтитров пуст — расшифровка не получена.')

    today = datetime.date.today().isoformat()
    upload = meta.get('upload_date') or ''
    source_date = '%s-%s-%s' % (upload[:4], upload[4:6], upload[6:]) if len(upload) == 8 else 'неизвестна'
    channel = meta.get('uploader') or meta.get('channel') or 'неизвестен'
    title = meta.get('title') or 'без названия'
    dur = int(meta.get('duration') or 0)

    name = '%s-%s-%s.md' % (today, slugify(channel, 20), slugify(title))
    path = os.path.join(args.out, name)

    body = '\n\n'.join(to_paragraphs(cues, args.bucket))
    words = len(body.split())
    header = (
        '---\n'
        'url: %s\n'
        'автор: %s\n'
        'дата-источника: %s\n'
        'тип: транскрипция\n'
        'добавлено: %s\n'
        'канал: %s\n'
        'длительность: %d:%02d:%02d\n'
        'субтитры: %s (%s)\n'
        'слов: %d\n'
        '---\n\n'
        '# %s\n\n'
        'Расшифровка видео. Таймкоды в квадратных скобках ведут к месту в записи —\n'
        'по ним проверяется цитата, не пересматривая всё целиком.\n\n'
    ) % (meta.get('webpage_url') or args.url, channel, source_date, today, channel,
         dur // 3600, dur % 3600 // 60, dur % 60, kind, code, words, title)

    os.makedirs(args.out, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(header + body + '\n')

    print(path)
    print('%s, %s, субтитры %s (%s), %d слов' % (title, channel, kind, code, words))


if __name__ == '__main__':
    main()
