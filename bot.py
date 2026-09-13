# -*- coding: utf-8 -*-
"""
Музыкальный бот для Яндекс Телемоста. Терминальный интерфейс.

    join <ссылка>   подключить бота к встрече
    play <ссылка>   играть ролик/плейлист с YouTube (или просто текст — найдёт поиском)
    vol 70          громкость в процентах
    +  /  -         громкость ±10

Полный список — команда help.
"""

import time

from audio import (JOURNAL_PATH, Player, Track, find_cable_device, find_ffmpeg,
                   fmt_time, list_output_devices, raise_process_priority)
from browser import TelemostBrowser

BOT_NAME = "Music Bot"

BANNER = r"""
  ┌──────────────────────────────────────────────┐
  │   Телемост Music Bot                         │
  │   help — список команд, quit — выход         │
  └──────────────────────────────────────────────┘
"""

HELP = """
  join <ссылка>       подключить бота к встрече Телемоста
  play <ссылка|текст> играть ролик или плейлист с YouTube
  play <ссылка> @40:00  начать сразу с сороковой минуты
  add  <ссылка|текст> дописать в конец списка
  seek 40:00          перемотать (можно +60 и -30)
  pos                 сколько уже отыграло
  resume              продолжить с места, где остановились в прошлый раз
  pause               пауза / снять с паузы
  next                следующий трек
  stop                остановить и очистить список
  vol <0-300>         громкость в процентах (100 = как есть)
  +  /  -             громкость ±10
  norm [on|off]       выравнивание громкости треков (по умолчанию включено)
  test                проиграть свой сигнал мимо интернета — проверка тракта
  ym [ссылка]         открыть Яндекс.Музыку и увести её звук в звонок
  ym off              закрыть это окно, звук вернётся на место
  list                что сейчас играет и что дальше
  stats               что бот реально отдаёт в звонок: битрейт, кодек, потери
  dev                 список устройств вывода
  dev <номер>         вручную выбрать устройство вывода
  quit                выход
"""


def log(msg):
    print(f"  {msg}")


# Как выглядит состояние трека в списке
MARK = {"new": ".", "ready": "+", "playing": ">", "done": " ",
        "bad": "x", "failed": "x"}


def plural(n):
    if n % 10 == 1 and n % 100 != 11:
        return "трек"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "трека"
    return "треков"


def parse_time(text):
    """40:00, 1:12:30, 90, +30, -10 -> (секунды, относительное ли)."""
    t = text.strip().replace(",", ".")
    relative = t[:1] in "+-"
    sign = -1.0 if t.startswith("-") else 1.0
    if relative:
        t = t[1:]
    if not t:
        return None
    try:
        parts = [float(p) for p in t.split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    seconds = 0.0
    for p in parts:                       # 90 -> 90 c; 1:30 -> 90 c; 1:00:00 -> час
        seconds = seconds * 60 + p
    return sign * seconds, relative


def split_timecode(arg):
    """'<ссылка> @40:00' -> ('<ссылка>', 2400.0).

    Пробел перед @ обязателен: в самих ссылках собака встречается
    (youtube.com/@канал), и трогать её нельзя.
    """
    idx = arg.rfind(" @")
    if idx < 0:
        return arg, 0.0
    head, tail = arg[:idx].strip(), arg[idx + 2:].strip()
    parsed = parse_time(tail)
    if head and parsed and not parsed[1]:
        return head, max(0.0, parsed[0])
    return arg, 0.0


def dur_str(seconds):
    if not seconds:
        return ""
    s = int(seconds)
    if s >= 3600:
        return f"  [{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}]"
    return f"  [{s // 60}:{s % 60:02d}]"


def wants_whole_playlist(url):
    """Ссылка на ролик — это ролик, даже если у неё хвост &list=...

    Скопированная с YouTube ссылка почти всегда выглядит как
    watch?v=XXX&list=PLYYY&index=7. Раньше бот раскрывал такую в ВЕСЬ
    плейлист: очередь распухала чужими треками, а при добавлении второй
    ссылки из того же плейлиста он приезжал повторно. Плейлист целиком
    берём только когда его попросили явно.
    """
    from urllib.parse import parse_qs, urlparse

    u = urlparse(url)
    qs = parse_qs(u.query)
    has_list = "list" in qs
    is_single = "v" in qs or u.netloc.endswith("youtu.be")
    explicit = has_list and not is_single      # /playlist?list=... или /list=...
    return explicit, (has_list and is_single)


def resolve(query):
    """Ссылка или поисковый запрос -> (список Track, пояснение для человека)."""
    from yt_dlp import YoutubeDL

    is_url = query.startswith(("http://", "https://"))
    target = query if is_url else f"ytsearch1:{query}"

    whole_playlist, trimmed = (False, False)
    if is_url:
        whole_playlist, trimmed = wants_whole_playlist(query)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "noplaylist": not whole_playlist,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

    if not info:
        return [], None

    note = None
    entries = info.get("entries")
    if entries is None:
        out = [Track(info.get("title") or query, info.get("webpage_url") or query)]
        if trimmed:
            note = ("в ссылке был ещё и плейлист — добавил только сам ролик. "
                    "Нужен весь список? Дай ссылку вида youtube.com/playlist?list=...")
        return out, note

    out = []
    for e in entries:
        if not e:
            continue
        url = e.get("url") or e.get("webpage_url")
        vid = e.get("id")
        if not url and vid:
            url = f"https://www.youtube.com/watch?v={vid}"
        if url:
            out.append(Track(e.get("title") or url, url))
    return out, note


def report_music(st):
    """Что получилось с перенаправлением звука страницы в кабель."""
    if not st:
        log("окно открылось, но страница не отозвалась — попробуй ym ещё раз")
        return

    state = st.get("state")
    if state == "готово":
        log(f"звук страницы уходит в {st.get('device')}")
        log("жми play в открывшемся окне — музыку услышат все в звонке")
        log("закончите — набери ym off, и всё вернётся само")
    elif state == "нет-устройства":
        log("[!] среди устройств вывода нет CABLE Input.")
        log("    Проверь, что VB-Audio Virtual Cable установлен (diag.bat покажет).")
    elif state == "отказано":
        log("[!] браузер не дал переключить вывод звука.")
        log(f"    {st.get('error')}")
        log("    Значит этот путь не сработал. Остаётся ручной способ из README:")
        log("    Параметры -> Звук -> Микшер громкости -> вывод в CABLE Input.")
    else:
        log(f"страница ответила непонятно: {st}")


def print_stats(s):
    codec = s.get("codec") or {}
    mime = (codec.get("mime") or "?").split("/")[-1]
    kbps = s.get("kbps", 0.0)
    fmtp = codec.get("fmtp") or ""
    # У Opus в rtpmap всегда стоит /2, поэтому поле channels ни о чём не
    # говорит. Реальное стерео включает только stereo=1 в fmtp.
    stereo = "stereo=1" in fmtp.replace(" ", "")

    print()
    print(f"   кодек        {mime}, {codec.get('rate', '?')} Гц")
    print(f"   стерео       {'да' if stereo else 'нет (моно)'}")
    if fmtp:
        print(f"   параметры    {fmtp}")
    print(f"   битрейт      {kbps:.1f} кбит/с   (замер за {s['seconds']:.1f} с, "
          f"{s['dpackets']} пакетов)")
    if s.get("target"):
        print(f"   цель кодека  {s['target'] / 1000:.0f} кбит/с")

    lvl = (s.get("source") or {}).get("level")
    if isinstance(lvl, (int, float)):
        note = "  <- тишина, музыка до кодека не доходит" if lvl < 0.001 else ""
        print(f"   на входе     уровень сигнала {lvl:.3f}{note}")
    rem = s.get("remote") or {}
    if rem and rem.get("lost") is not None:
        rtt = rem.get("rtt")
        print(f"   у слушателей потеряно пакетов: {rem['lost']}"
              + (f", задержка {rtt * 1000:.0f} мс" if rtt else ""))

    print()
    if kbps < 45:
        print("   ВЕРДИКТ: это речевой режим Opus — та самая «музыка как с радио».")
        print("            Для музыки нужно 64-128 кбит/с и стерео.")
        if not stereo:
            print("            Разгон не применился: в параметрах нет stereo=1.")
    elif not stereo:
        print("   ВЕРДИКТ: битрейт музыкальный, но поток моно. Уже сильно лучше.")
    elif kbps < 64:
        print("   ВЕРДИКТ: стерео есть, битрейта маловато. Возможно, режет Телемост.")
    else:
        print("   ВЕРДИКТ: стерео и музыкальный битрейт. Кодек больше не узкое место.")

    lost = (s.get("remote") or {}).get("lost")
    if isinstance(lost, int) and lost > 20:
        print("            Потерь много — опусти TARGET_KBPS в browser.py до 96 или 64.")
    print()


def main():
    print(BANNER)

    if find_ffmpeg() is None:
        log("[!] ffmpeg не найден. Запусти setup.bat или поставь ffmpeg в PATH.")

    device = find_cable_device()
    if device is None:
        log("[!] Виртуальный кабель не найден.")
        log("    Поставь VB-Audio Virtual Cable (vb-audio.com/Cable) и перезагрузись.")
        log("    Или выбери устройство вручную командой: dev <номер>")
    else:
        name = list_output_devices()
        label = next((n for i, n, _ in name if i == device), str(device))
        log(f"Звук пойдёт в: {label}")
        log(f"В Chrome-окне бота микрофоном должен быть \"CABLE Output\".")

    prio = raise_process_priority()
    if prio:
        log(f"Приоритет процесса: {prio} — меньше щелчков во время игры")

    player = Player(device, log=log)
    player.start()
    browser = None

    print()
    while True:
        try:
            raw = input("bot> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue

        cmd, _, arg = raw.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()

        if cmd in ("quit", "exit", "q"):
            break

        elif cmd in ("help", "h", "?"):
            print(HELP)

        elif cmd == "join":
            if not arg:
                log("нужна ссылка: join https://telemost.yandex.ru/j/...")
                continue
            if browser and browser.is_alive():
                log("закрываю прошлое окно...")
                browser.close()
                browser.join(timeout=8)
            browser = TelemostBrowser(arg, bot_name=BOT_NAME, log=log)
            browser.start()
            browser.ready.wait(timeout=90)

        elif cmd in ("play", "add"):
            if not arg:
                log("нужна ссылка или название трека")
                continue
            target, at = split_timecode(arg)
            log("ищу...")
            try:
                tracks, note = resolve(target)
            except Exception as exc:  # noqa: BLE001
                log(f"[ошибка] {exc}")
                continue
            if not tracks:
                log("ничего не нашлось")
                continue
            if at:
                tracks[0].start = at
            if cmd == "play":
                player.set_tracks(tracks)
            else:
                player.append_tracks(tracks)
            log(f"добавлено: {len(tracks)} {plural(len(tracks))}"
                + (f", старт с {fmt_time(at)}" if at else ""))
            if note:
                log(note)

            mark = player.remembered(tracks[0].url) if not at else None
            if mark:
                log(f"в прошлый раз остановились на {fmt_time(mark)} — "
                    f"набери resume, чтобы продолжить оттуда")

        elif cmd in ("pause", "p"):
            log("пауза" if player.toggle_pause() else "поехали")

        elif cmd in ("next", "n", "skip"):
            player.skip()

        elif cmd in ("stop", "s"):
            player.stop()

        elif cmd in ("vol", "v"):
            if not arg:
                log(f"громкость: {int(player.volume * 100)}%")
                continue
            try:
                log(f"громкость: {player.set_volume(float(arg))}%")
            except ValueError:
                log("укажи число, например: vol 60")

        elif cmd in ("+", "++"):
            log(f"громкость: {player.set_volume(player.volume * 100 + 10)}%")

        elif cmd in ("-", "--"):
            log(f"громкость: {player.set_volume(player.volume * 100 - 10)}%")

        elif cmd == "list":
            tracks, idx = player.queue_view()
            if not tracks:
                log("список пуст")
            for i, t in enumerate(tracks):
                marker = ">" if i == idx else " "
                print(f"   {marker} {i + 1:>3}. {MARK.get(t.status, ' ')} {t.title}"
                      f"{dur_str(t.duration)}")
                if t.note and t.status in ("bad", "failed"):
                    print(f"           {t.note}")
            counts = {}
            for t in tracks:
                counts[t.status] = counts.get(t.status, 0) + 1
            broken = counts.get("bad", 0) + counts.get("failed", 0)
            if broken:
                log(f"проблемных треков: {broken} (помечены знаком x)")

        elif cmd == "ym":
            if not (browser and browser.is_alive()):
                log("бот не в звонке — сначала join")
                continue

            if arg.lower() in ("off", "close", "стоп", "выкл", "-"):
                try:
                    closed = browser.close_music()
                except Exception as exc:  # noqa: BLE001
                    log(f"[ошибка] {exc}")
                    continue
                log("окно закрыто, звук вернулся на место" if closed
                    else "окно и не было открыто")
                continue

            player.stop()
            log("открываю... первый раз надо будет войти в Яндекс, дальше запомнит")
            try:
                st = browser.open_music(arg or "https://music.yandex.ru")
            except Exception as exc:  # noqa: BLE001
                log(f"[ошибка] {exc}")
                continue
            report_music(st)

        elif cmd == "seek":
            parsed = parse_time(arg) if arg else None
            if not parsed:
                log("например: seek 40:00, seek 1:12:30, seek +60, seek -30")
                continue
            new = player.seek(parsed[0], relative=parsed[1])
            if new is None:
                log("сейчас нечего мотать — сначала включи трек")
            else:
                log(f"мотаю на {fmt_time(new)}...")

        elif cmd == "pos":
            tr = player.current_track()
            if tr is None or not player.current_title:
                log("ничего не играет")
            else:
                total = f" из {fmt_time(tr.duration)}" if tr.duration else ""
                log(f"{tr.title} — {fmt_time(player.position)}{total}")

        elif cmd == "resume":
            tr = player.current_track()
            if tr is None:
                log("сначала включи ролик через play, потом resume")
                continue
            mark = player.remembered(tr.url)
            if not mark:
                log("для этого ролика сохранённой позиции нет")
                continue
            new = player.seek(mark)
            log(f"продолжаю с {fmt_time(new)}..." if new is not None
                else "не получилось перемотать")

        elif cmd == "test":
            log("6 секунд ровного тона мимо интернета и yt-dlp.")
            log("Слышно в звонке — значит тракт цел, дело в скачивании.")
            player.set_tracks([Track("тестовый сигнал", "test://tone")])

        elif cmd == "norm":
            if arg.lower() in ("on", "вкл", "1", "да"):
                player.set_normalize(True)
            elif arg.lower() in ("off", "выкл", "0", "нет"):
                player.set_normalize(False)
            elif arg:
                log("norm on  или  norm off")
                continue
            else:
                player.set_normalize(not player.normalize)
            log(f"выравнивание громкости: {'включено' if player.normalize else 'выключено'}"
                f" (применится со следующего трека)")

        elif cmd == "stats":
            if not (browser and browser.is_alive()):
                log("бот не в звонке — сначала join")
                continue
            log("считаю 4 секунды, музыка должна играть...")
            try:
                s = browser.audio_stats()
            except Exception as exc:  # noqa: BLE001
                log(f"[ошибка] {exc}")
                continue
            if not s:
                log("исходящей аудиодорожки не видно — бот точно в звонке и не замьючен?")
                continue
            print_stats(s)

        elif cmd == "dev":
            if arg:
                try:
                    player.device_index = int(arg)
                    log(f"устройство вывода: {arg} (применится со следующего трека)")
                except ValueError:
                    log("укажи номер из списка dev")
            else:
                for i, name, api in list_output_devices():
                    mark = "*" if i == player.device_index else " "
                    print(f"   {mark} {i:>3}  {name}  [{api}]")

        else:
            log("не понял. help — список команд")

    log("выключаюсь...")
    player.shutdown()
    if browser and browser.is_alive():
        browser.close()
        browser.join(timeout=8)
    time.sleep(0.3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
