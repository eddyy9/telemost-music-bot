# -*- coding: utf-8 -*-

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_FRAMES = 1024
BYTES_PER_BLOCK = BLOCK_FRAMES * CHANNELS * 4       # float32 = 4 байта
SECONDS_PER_BLOCK = BLOCK_FRAMES / SAMPLE_RATE      # ~21 мс

BUFFER_BLOCKS = 220        # ~4.7 с ёмкость
PREBUFFER_BLOCKS = 90      # ~1.9 с накапливаем, прежде чем начать играть

# Меньше секунды звука за трек — это не музыка, а обрыв. Разбираемся почему.
MIN_OK_BYTES = BLOCK_FRAMES * CHANNELS * 4 * 46

# Сбой скачивания часто разовый: сеть моргнула, YouTube притормозил.
MAX_ATTEMPTS = 3
RETRY_DELAY = 2.0

# В обычном запуске рабочие файлы лежат рядом с исходниками. В сборке
# PyInstaller __file__ указывает внутрь служебной папки, поэтому используем
# папку самого exe: portable-архив можно переносить вместе с его историей.
_HERE = (os.path.dirname(os.path.abspath(sys.executable))
         if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
JOURNAL_PATH = os.path.join(_HERE, "history.log")
MARKS_PATH = os.path.join(_HERE, "positions.json")

# Запоминаем позицию только в длинном — в трёхминутной песне это ни к чему.
MARK_MIN_DURATION = 900.0     # 15 минут
MARK_MIN_POSITION = 120.0     # и если отслушали хотя бы две


def _load_marks():
    try:
        import json
        with open(MARKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_marks(marks):
    try:
        import json
        with open(MARKS_PATH, "w", encoding="utf-8") as f:
            json.dump(marks, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def fmt_time(seconds):
    """Секунды -> 40:12 или 1:12:30."""
    s = int(max(0, seconds or 0))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


class Track:
    """Один трек в очереди вместе со всем, что мы о нём знаем."""

    __slots__ = ("title", "url", "start", "status", "note", "duration", "attempts",
                 "direct", "headers", "direct_at", "direct_failed")

    # new -> ready | bad  (предполётная проверка)
    # ready -> playing -> done | failed  (воспроизведение)
    def __init__(self, title, url, start=0.0):
        self.title = title
        self.url = url
        self.start = float(start)     # с какой секунды играть
        self.status = "new"
        self.note = None              # причина, если что-то не так
        self.duration = None          # секунд, если удалось узнать
        self.attempts = 0
        self.direct = None            # прямая ссылка на аудиопоток
        self.headers = None           # заголовки, без которых её не отдадут
        self.direct_at = 0.0          # когда получили: они живут часы
        self.direct_failed = False    # прямая ссылка не пошла — только труба

    @property
    def alive(self):
        return self.status not in ("bad", "failed")

    def __repr__(self):
        return f"<Track {self.status} {self.title!r}>"


def as_tracks(items):
    """Принимаем и объекты Track, и старые пары (название, ссылка)."""
    out = []
    for it in items:
        out.append(it if isinstance(it, Track) else Track(it[0], it[1]))
    return out


# Прямые ссылки YouTube живут несколько часов; перезапрашиваем с запасом.
DIRECT_TTL = 1800.0


def direct_stream(url):
    """Прямая ссылка на аудиопоток. -> (ссылка, заголовки, длительность) или None.

    По ней работает перемотка через HTTP-запрос диапазона: ffmpeg просит у
    сервера кусок с 40-й минуты и получает его сразу. Через трубу от yt-dlp
    так нельзя — там пришлось бы скачать и выбросить всё начало.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "format": "bestaudio/best"}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None
    if not info:
        return None

    link = info.get("url")
    headers = info.get("http_headers")
    if not link:
        # На всякий случай ищем аудиодорожку среди форматов сами.
        best = None
        for f in (info.get("requested_formats") or []) + (info.get("formats") or []):
            if f.get("acodec") in (None, "none") or not f.get("url"):
                continue
            if best is None or (f.get("abr") or 0) > (best.get("abr") or 0):
                best = f
        if best:
            link, headers = best["url"], best.get("http_headers")
    if not link:
        return None
    return link, headers or {}, info.get("duration")


def probe_track(url):
    """Проверить трек, ничего не скачивая. -> (годен, причина, секунд)."""
    if url.startswith("test://"):
        return True, None, 6.0
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return True, None, None       # проверить нечем — не мешаем играть
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "ignoreerrors": False}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        reason = reason.replace("ERROR: ", "")
        return False, reason[:150], None
    if not info:
        return False, "данные о ролике не получены", None
    return True, None, info.get("duration")

# Нормализация по EBU R128. -14 LUFS — уровень, к которому приводят треки
# стриминговые сервисы; True Peak -1.5 дБ оставляет запас кодеку.
LOUDNESS_LUFS = -14.0
TRUE_PEAK_DB = -1.5

# Лимитер ведёт усиление, а не меняет форму волны: изменение формы само по
# себе рождает гармоники, то есть тот самый хрип. Здесь же, пока сигнал в
# пределах потолка, он проходит вообще нетронутым, бит в бит.
LIMITER_CEILING = 0.97
RELEASE_BLOCKS = 12.0      # возврат усиления к единице примерно за 250 мс

# Имя устройства, куда играем. VB-Audio Virtual Cable создаёт пару:
#   "CABLE Input"  -> устройство ВЫВОДА (сюда пишем мы)
#   "CABLE Output" -> устройство ВВОДА  (отсюда читает браузер как "микрофон")
CABLE_OUT_HINT = "CABLE Input"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_ffmpeg():
    """ffmpeg из PATH, иначе из пакета imageio-ffmpeg (он тянет статический бинарь)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def list_output_devices():
    out = []
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            api = ""
            if hostapis and d["hostapi"] < len(hostapis):
                api = hostapis[d["hostapi"]]["name"]
            out.append((i, d["name"], api))
    return out


def find_cable_device():
    """Ищем виртуальный кабель среди устройств вывода. WASAPI в приоритете."""
    candidates = [
        (i, name, api)
        for i, name, api in list_output_devices()
        if CABLE_OUT_HINT.lower() in name.lower() or "vb-audio" in name.lower()
    ]
    if not candidates:
        return None
    for i, name, api in candidates:
        if "wasapi" in api.lower():
            return i
    return candidates[0][0]


def raise_process_priority():
    """Чуть выше обычного: звук не должен захлёбываться, пока идёт игра."""
    if os.name != "nt":
        return None
    try:
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        k = ctypes.windll.kernel32
        if k.SetPriorityClass(k.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS):
            return "выше обычного"
    except Exception:
        pass
    return None


def _boost_current_thread():
    """Поток, который кормит звуковую карту, не должен ждать своей очереди."""
    if os.name != "nt":
        return
    try:
        THREAD_PRIORITY_HIGHEST = 2
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
    except Exception:
        pass


def _filter_works(ffmpeg, af):
    """Не все сборки ffmpeg собраны с soxr, а кривой фильтр убивает поток целиком."""
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=d=0.05",
             "-af", af, "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=25, creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _drain(pipe, sink):
    """Складываем stderr процесса в список, чтобы было что показать при поломке."""
    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                sink.append(line)
                if len(sink) > 40:
                    del sink[:-40]
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


# Признаки того, что YouTube в очередной раз сломал скачивание.
YTDLP_ROT = ("nsig", "player", "Sign in", "bot", "HTTP Error 4", "HTTP Error 5",
             "Unable to extract", "unavailable", "Precondition", "format is not available")


class Player(threading.Thread):
    """Один фоновый поток: играет треки из списка по порядку."""

    def __init__(self, device_index, log=print):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.log = log
        self.ffmpeg = find_ffmpeg()

        self.volume = 1.0
        self.normalize = True
        self.tracks = []          # [(title, url), ...]
        self.index = 0
        self.current_title = None

        self._lock = threading.Lock()
        self._skip = threading.Event()      # оборвать текущий трек
        self._paused = threading.Event()
        self._quit = threading.Event()
        self._wake = threading.Event()
        self._procs = []
        self._gen = 0                       # номер «поколения» списка треков
        self._soxr = None                   # определяется при первом запуске
        self.underruns = 0                  # сколько раз буфер пустел
        self._err_ydl = []                  # последние строки stderr процессов
        self._err_ff = []
        self._checked = 0                   # счётчики предполётной проверки
        self._bad = 0
        self._checker = threading.Thread(target=self._preflight, daemon=True)
        self._played_bytes = 0              # сколько отдано в карту в этом заходе
        self._base_pos = 0.0                # с какой секунды начали
        self._seeking = threading.Event()
        self._seek_to = None
        self._used_direct = False           # каким путём играли последнюю попытку
        self._marks = _load_marks()         # где мы остановились в каждой ссылке

    def _kill_procs(self):
        for proc in self._procs:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        self._procs = []

    # ---------- управление ----------

    def set_tracks(self, tracks):
        tracks = as_tracks(tracks)
        with self._lock:
            self.tracks = tracks
            self.index = 0
            self._gen += 1          # старому треку теперь нельзя двигать индекс
            self._skip.set()        # ...и он должен оборваться
        for t in tracks:
            self._journal("добавлен", t)
        self._kill_procs()
        self._wake.set()

    def append_tracks(self, tracks):
        tracks = as_tracks(tracks)
        with self._lock:
            self.tracks.extend(tracks)
        for t in tracks:
            self._journal("добавлен", t)
        self._wake.set()

    def _journal(self, event, track=None, extra=""):
        """Пишем историю в файл: чтобы потом не гадать, что случилось."""
        try:
            with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
                f.write("\t".join([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    event,
                    (track.title if track else "")[:100],
                    str(extra),
                ]) + "\n")
        except Exception:
            pass

    def skip(self):
        self._skip.set()
        self._kill_procs()

    def stop(self):
        with self._lock:
            self.tracks = []
            self.index = 0
            self._gen += 1
            self._skip.set()
        self._kill_procs()
        self.log("[стоп]")

    @property
    def position(self):
        """Сколько секунд трека уже прозвучало."""
        return self._base_pos + self._played_bytes / (SAMPLE_RATE * CHANNELS * 4)

    def current_track(self):
        with self._lock:
            if self.index < len(self.tracks):
                return self.tracks[self.index]
        return None

    def seek(self, seconds, relative=False):
        """Перемотать текущий трек. -> новая позиция или None."""
        tr = self.current_track()
        if tr is None or tr.status != "playing":
            return None      # мотать ещё не начатое бессмысленно — есть play @время
        target = (self.position + seconds) if relative else seconds
        target = max(0.0, target)
        if tr.duration:
            target = min(target, max(0.0, tr.duration - 2.0))
        with self._lock:
            self._seek_to = target
            self._seeking.set()
            self._skip.set()
        self._kill_procs()
        return target

    def remembered(self, url):
        """Где мы остановились в этой ссылке в прошлый раз."""
        v = self._marks.get(url)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    def _remember(self, track):
        """Запоминаем позицию — но только там, где это осмысленно."""
        if not isinstance(track, Track) or track.url.startswith("test://"):
            return
        pos = self.position
        long_enough = (track.duration or 0) >= MARK_MIN_DURATION
        near_end = track.duration and pos > track.duration - 30
        if long_enough and pos >= MARK_MIN_POSITION and not near_end:
            self._marks[track.url] = round(pos, 1)
        elif near_end:
            self._marks.pop(track.url, None)     # доиграли до конца, метка не нужна
        else:
            return
        _save_marks(self._marks)

    def toggle_pause(self):
        if self._paused.is_set():
            self._paused.clear()
            return False
        self._paused.set()
        return True

    @property
    def paused(self):
        return self._paused.is_set()

    def set_volume(self, percent):
        self.volume = max(0.0, min(300.0, float(percent))) / 100.0
        return int(self.volume * 100)

    def set_normalize(self, on):
        self.normalize = bool(on)
        return self.normalize

    def shutdown(self):
        self._quit.set()
        self._skip.set()
        self._wake.set()
        self._kill_procs()

    def queue_view(self):
        with self._lock:
            return list(self.tracks), self.index

    # ---------- внутреннее ----------

    def _preflight(self):
        """Проверяем добавленные треки заранее, чтобы не узнавать о сбоях по факту."""
        while not self._quit.is_set():
            with self._lock:
                pending = [t for t in self.tracks if t.status == "new"]
                target = pending[0] if pending else None
                left = len(pending)

            if target is None:
                if self._checked:
                    if self._bad:
                        self.log(f"[проверка] готовы {self._checked - self._bad} из "
                                 f"{self._checked}, недоступны {self._bad}")
                    self._checked = self._bad = 0
                time.sleep(0.4)
                continue

            good, reason, dur = probe_track(target.url)
            with self._lock:
                if target.status == "new":       # мог смениться, пока проверяли
                    target.status = "ready" if good else "bad"
                    target.note = reason
                    target.duration = dur
            self._checked += 1
            if not good:
                self._bad += 1
                self.log(f"[!] недоступен: {target.title} — {reason}")
                self._journal("недоступен", target, reason)
            time.sleep(0.25)                     # не долбим YouTube очередью запросов

    def start(self):
        super().start()
        self._checker.start()

    def run(self):
        while not self._quit.is_set():
            # Выбор трека и сброс флага пропуска — под одним замком, иначе
            # play/stop, прилетевший в этот момент, съедал бы новый первый трек.
            with self._lock:
                gen = self._gen
                tr = self.tracks[self.index] if self.index < len(self.tracks) else None
                if tr is not None:
                    self._skip.clear()

            if tr is None:
                self.current_title = None
                self._wake.wait(0.3)
                self._wake.clear()
                continue

            if tr.status == "bad":
                self.log(f"[пропуск] {tr.title} — {tr.note}")
                self._advance(gen)
                continue

            self.current_title = tr.title
            tr.status = "playing"
            self.log(f"[играет] {tr.title}")
            self._journal("начал", tr)
            before = self.underruns

            got = 0
            for attempt in range(1, MAX_ATTEMPTS + 1):
                tr.attempts = attempt
                try:
                    got = self._play_one(tr, report=(attempt == MAX_ATTEMPTS))
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[ошибка] {tr.title}: {exc}")
                    got = 0
                if got >= MIN_OK_BYTES or self._skip.is_set() or self._quit.is_set():
                    break
                if self._seeking.is_set():
                    break
                if self._used_direct:
                    # Прямую ссылку отвергли (у нас это бывает при обрывах
                    # соединения с YouTube) — дальше только через yt-dlp.
                    tr.direct_failed = True
                    self.log("[перемотка] прямую ссылку отбили, "
                             "перехожу на yt-dlp")
                if attempt < MAX_ATTEMPTS:
                    self.log(f"[повтор] {attempt + 1}-я попытка из {MAX_ATTEMPTS}...")
                    time.sleep(RETRY_DELAY)

            # Перемотка: играем ТОТ ЖЕ трек с новой позиции, очередь не двигаем.
            if self._seeking.is_set():
                self._seeking.clear()
                with self._lock:
                    stale = gen != self._gen
                    target = self._seek_to
                    self._seek_to = None
                if not stale and target is not None:
                    tr.start = target
                    tr.status = "ready"
                    self.log(f"[перемотка] {fmt_time(target)}")
                    self._journal("перемотка", tr, fmt_time(target))
                    continue

            self._remember(tr)
            secs = got / (SAMPLE_RATE * CHANNELS * 4)
            if got >= MIN_OK_BYTES or self._skip.is_set():
                tr.status = "done"
                self._journal("сыграл", tr, f"{secs:.0f} с")
            else:
                tr.status = "failed"
                tr.note = tr.note or "не удалось скачать"
                self._journal("не сыграл", tr, tr.note)

            gaps = self.underruns - before
            if gaps > 5 and got >= MIN_OK_BYTES:      # у павшего трека и так всё ясно
                self.log(f"[звук] буфер голодал {gaps * SECONDS_PER_BLOCK:.1f} с — "
                         f"проверь сеть или подними BUFFER_BLOCKS в audio.py")

            self._advance(gen)

        self.current_title = None

    def _advance(self, gen):
        """Сдвинуть указатель, если список за это время не подменили."""
        with self._lock:
            if gen == self._gen:
                self.index += 1

    def _audio_filter(self):
        """Цепочка фильтров ffmpeg, собранная из того, что сборка реально умеет."""
        if self._soxr is None:
            # Сначала тривиальный фильтр: если не проходит даже он, дело не в
            # soxr, а в самом ffmpeg — и об этом надо сказать громко.
            if not _filter_works(self.ffmpeg, "anull"):
                self._soxr = False
                self.log("[!] ffmpeg не запускается — звука не будет вообще.")
                self.log("    Проверь: " + str(self.ffmpeg))
                self.log("    Обычно лечится повторным запуском setup.bat")
            else:
                self._soxr = _filter_works(self.ffmpeg, "aresample=48000:resampler=soxr")
                if not self._soxr:
                    self.log("[звук] soxr недоступен, беру штатный ресемплер "
                             "(на качество почти не влияет)")

        parts = []
        if self.normalize:
            # loudnorm внутри работает на 192 кГц и оставляет поток таким же,
            # поэтому следом обязателен возврат на 48 кГц.
            parts.append(f"loudnorm=I={LOUDNESS_LUFS}:TP={TRUE_PEAK_DB}:LRA=11")
        if self._soxr:
            parts.append(f"aresample={SAMPLE_RATE}:resampler=soxr:precision=28")
        else:
            parts.append(f"aresample={SAMPLE_RATE}")
        return ",".join(parts)

    def _resolve_direct(self, track):
        """Прямая ссылка на поток, с кэшем: перемотка не должна каждый раз ждать."""
        if track.direct and (time.time() - track.direct_at) < DIRECT_TTL:
            return True
        got = direct_stream(track.url)
        if not got:
            return False
        track.direct, track.headers, dur = got
        track.direct_at = time.time()
        if dur and not track.duration:
            track.duration = dur
        return True

    def _spawn(self, track):
        """Запускаем декодирование в float32 PCM. Три способа подачи.

        stderr процессов перехватываем: без этого поломка скачивания
        выглядит как обычная тишина, и понять причину невозможно.
        """
        url = track.url if isinstance(track, Track) else track
        start = track.start if isinstance(track, Track) else 0.0

        self._err_ydl = []
        self._err_ff = []
        drains = []

        out_args = [
            "-vn",
            "-af", self._audio_filter(),
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "pipe:1",
        ]

        if url.startswith("test://"):
            # Локальный сигнал мимо сети и yt-dlp — проверка тракта вывода.
            ydl = None
            ff = subprocess.Popen(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i",
                 "aevalsrc='0.35*sin(2*PI*440*t)|0.35*sin(2*PI*554.37*t)'"
                 ":s=48000:d=6"] + out_args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        elif (isinstance(track, Track) and start > 0.5 and not track.direct_failed
              and self._resolve_direct(track)):
            # Основной путь: ffmpeg сам тянет поток и умеет прыгать по времени.
            pre = ["-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_delay_max", "5"]
            if track.headers:
                pre += ["-headers", "".join(f"{k}: {v}\r\n"
                                            for k, v in track.headers.items())]
            pre += ["-ss", f"{start:.3f}"]          # ДО -i, то есть быстрый прыжок
            self._used_direct = True
            ydl = None
            ff = subprocess.Popen(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error"] + pre +
                ["-i", track.direct] + out_args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            # Основной и проверенный путь: yt-dlp в трубу. Он переживает всё,
            # что YouTube делает с прямыми ссылками — обрывы, троттлинг, подписи.
            # Перемотка тут дорогая: ffmpeg вынужден продекодировать и выбросить
            # начало, поэтому -ss идёт ПОСЛЕ -i.
            self._used_direct = False
            if start > 0.5:
                self.log("[перемотка] иду через yt-dlp — будет медленнее, "
                         "но надёжно")
            ydl = subprocess.Popen(
                [
                    sys.executable, "-m", "yt_dlp",
                    "-f", "bestaudio/best",
                    "--no-playlist",
                    "--no-warnings",
                    "-o", "-",
                    url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
            mid = ["-i", "pipe:0"]
            if start > 0.5:
                mid += ["-ss", f"{start:.3f}"]
            ff = subprocess.Popen(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error"] + mid + out_args,
                stdin=ydl.stdout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
            ydl.stdout.close()  # чтобы ffmpeg увидел EOF, когда yt-dlp закончит
            drains.append((ydl.stderr, self._err_ydl))

        drains.append((ff.stderr, self._err_ff))
        for pipe, sink in drains:
            threading.Thread(target=_drain, args=(pipe, sink), daemon=True).start()

        self._procs = [p for p in (ff, ydl) if p]
        return ydl, ff

    def _report_silence(self, url, got=0):
        """Трек оборвался почти сразу. Показываем, что сказали процессы."""
        if got:
            secs = got / (SAMPLE_RATE * CHANNELS * 4)
            self.log(f"[!] трек оборвался через {secs:.2f} с — "
                     f"вот что говорят программы:")
        else:
            self.log("[!] звука не получено ни байта — вот что говорят программы:")

        shown = False
        for name, sink in (("yt-dlp", self._err_ydl), ("ffmpeg", self._err_ff)):
            for line in sink[-4:]:
                self.log(f"    {name}: {line[:160]}")
                shown = True
        if not shown:
            self.log("    (обе промолчали)")

        blob = " ".join(self._err_ydl)
        low = blob.lower()

        if "no module named" in low:
            self.log("    -> yt-dlp вообще не установлен. Запусти setup.bat")
        elif self._err_ydl:
            # Жалобы ffmpeg на «Invalid data» — лишь следствие: ему нечего было
            # декодировать. Первопричина всегда выше по цепочке.
            if any(sign.lower() in low for sign in YTDLP_ROT):
                self.log("    -> YouTube опять сломал скачивание.")
            else:
                self.log("    -> сломалось на стороне скачивания.")
            self.log("       Закрой бота, запусти update.bat, попробуй снова.")
        elif self._err_ff:
            ff_low = " ".join(self._err_ff).lower()
            network = any(sign in ff_low for sign in
                          ("opening input", "io error", "10054", "connection",
                           "timed out", "403", "tls"))
            if network:
                self.log("    -> соединение оборвали на стороне YouTube.")
                self.log("       Следующая попытка пойдёт через yt-dlp — он это переживает.")
                self.log("       Если повторяется на всех треках, запусти update.bat")
            else:
                self.log("    -> ругается ffmpeg. Если ссылка рабочая, попробуй norm off:")
                self.log("       возможно, не собирается фильтр выравнивания громкости.")
        elif not url.startswith("test://"):
            self.log("    -> обе программы промолчали. Набери test — он играет сигнал")
            self.log("       мимо интернета и покажет, цел ли сам тракт вывода.")

    def _play_one(self, track, report=True):
        """Проиграть трек. Возвращает, сколько байт звука реально получилось."""
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg не найден — поставь его или пакет imageio-ffmpeg")

        url = track.url if isinstance(track, Track) else track
        base = track.start if isinstance(track, Track) else 0.0
        self._played_bytes = 0
        self._base_pos = base
        ydl, ff = self._spawn(track)
        buf = queue.Queue(maxsize=BUFFER_BLOCKS)
        eof = threading.Event()
        got = [0]

        def reader():
            """Тянет из ffmpeg с опережением, чтобы плеер не зависел от сети."""
            try:
                while not self._skip.is_set() and not self._quit.is_set():
                    try:
                        chunk = ff.stdout.read(BYTES_PER_BLOCK)
                    except (ValueError, OSError):
                        break               # процесс убит из другого потока
                    if not chunk:
                        break
                    got[0] += len(chunk)
                    while not self._skip.is_set() and not self._quit.is_set():
                        try:
                            buf.put(chunk, timeout=0.2)
                            break
                        except queue.Full:
                            continue
            finally:
                eof.set()

        pump = threading.Thread(target=reader, daemon=True)
        pump.start()

        # Преднакопление: стартуем, когда набралось про запас или трек короче буфера.
        deadline = time.time() + 20
        while (buf.qsize() < PREBUFFER_BLOCKS and not eof.is_set()
               and not self._skip.is_set() and not self._quit.is_set()
               and time.time() < deadline):
            time.sleep(0.05)

        stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            device=self.device_index,
            blocksize=BLOCK_FRAMES,
            latency="high",
        )
        silence = np.zeros(BLOCK_FRAMES * CHANNELS, dtype=np.float32).tobytes()
        self._limiter_reset()
        try:
            stream.start()
            _boost_current_thread()
            while not self._skip.is_set() and not self._quit.is_set():
                if self._paused.is_set():
                    stream.write(silence)
                    continue
                try:
                    chunk = buf.get(timeout=0.2)
                except queue.Empty:
                    if eof.is_set():
                        break
                    # Буфер опустел: тишина звучит куда лучше щелчка.
                    self.underruns += 1
                    stream.write(silence)
                    continue
                data = self._shape(chunk)
                if data:
                    stream.write(data)
                    self._played_bytes += len(data)
            tail = self._flush()
            if tail and not self._skip.is_set() and not self._quit.is_set():
                stream.write(tail)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            self._kill_procs()
            pump.join(timeout=1.0)
            time.sleep(0.15)          # даём сборщикам stderr договорить
            if (report and got[0] < MIN_OK_BYTES and not self._skip.is_set()
                    and not self._quit.is_set()):
                self._report_silence(url, got[0])
        return got[0]

    def _limiter_reset(self):
        self._pending = None
        self._gain = 1.0

    def _shape(self, chunk):
        """Громкость и лимитер с упреждением в один блок (~21 мс).

        Блок придерживается, а усиление для него рассчитывается по СЛЕДУЮЩЕМУ
        блоку. Поэтому к моменту, когда громкий кусок дойдёт до карты, усиление
        уже опущено — обрезать нечего.
        """
        n = len(chunk) - (len(chunk) % 4)
        if n <= 0:
            return b""
        x = np.frombuffer(chunk[:n], dtype="<f4").astype(np.float32)
        if abs(self.volume - 1.0) > 1e-3:
            x *= self.volume

        peak = float(np.abs(x).max()) if x.size else 0.0
        need = min(1.0, LIMITER_CEILING / peak) if peak > LIMITER_CEILING else 1.0

        out, self._pending = self._pending, x
        if out is None:
            return b""                      # первый блок придержали

        g0 = self._gain
        # Атака мгновенная — иначе пик успеет проскочить. Отпускание плавное,
        # иначе на каждом всплеске слышно «дыхание».
        g1 = need if need < g0 else g0 + (need - g0) / RELEASE_BLOCKS
        self._gain = g1

        if g0 < 1.0 or g1 < 1.0:
            out = out * np.linspace(g0, g1, out.size, dtype=np.float32)
        np.clip(out, -1.0, 1.0, out=out)     # страховка от неожиданностей
        return out.tobytes()

    def _flush(self):
        """Выпустить придержанный блок в конце трека."""
        if self._pending is None:
            return b""
        out, self._pending = self._pending, None
        if self._gain < 1.0:
            out = out * self._gain
        np.clip(out, -1.0, 1.0, out=out)
        return out.tobytes()
