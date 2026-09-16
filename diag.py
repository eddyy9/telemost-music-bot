# -*- coding: utf-8 -*-
import os
import sys

TELEMOST_ORIGIN = "https://telemost.yandex.ru"
PROBE_URL = TELEMOST_ORIGIN + "/__mic_diag"

PROBE_HTML = """<!doctype html><meta charset="utf-8"><title>mic diag</title>
<body><h1>проверка микрофона</h1><script>
window.__diag = null;
(async () => {
  const r = { steps: {} };
  const put = (k, v) => { r.steps[k] = v; };

  try {
    const d = await navigator.mediaDevices.enumerateDevices();
    put('enum_before', d.filter(x => x.kind === 'audioinput')
                        .map(x => x.label || '(метка скрыта)'));
  } catch (e) { put('enum_before_error', e.name + ': ' + e.message); }

  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    const t = s.getAudioTracks()[0];
    put('gum_plain', { ok: true, label: t.label, settings: t.getSettings() });
    s.getTracks().forEach(x => x.stop());
  } catch (e) {
    put('gum_plain', { ok: false, error: e.name, message: e.message });
  }

  let cableId = null;
  try {
    const d = await navigator.mediaDevices.enumerateDevices();
    const ins = d.filter(x => x.kind === 'audioinput');
    put('enum_after', ins.map(x => x.label || '(метка скрыта)'));
    const hit = ins.find(x => /CABLE Output|VB-Audio|VB-Cable/i.test(x.label));
    if (hit) { cableId = hit.deviceId; put('cable_label', hit.label); }
  } catch (e) { put('enum_after_error', e.name + ': ' + e.message); }
  put('cable_found', !!cableId);

  if (cableId) {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: {
        deviceId: { exact: cableId },
        echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
      const t = s.getAudioTracks()[0];
      put('gum_cable', { ok: true, label: t.label, settings: t.getSettings() });
      s.getTracks().forEach(x => x.stop());
    } catch (e) {
      put('gum_cable', { ok: false, error: e.name, message: e.message });
    }
  }
  window.__diag = r;
})();
</script></body>"""


def head(title):
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def ok(msg):
    print("  [ок]   " + msg)


def bad(msg):
    print("  [!!]   " + msg)


def info(msg):
    print("         " + msg)


problems = []


# ---------------------------------------------------------------- 1. Windows

def check_privacy():
    head("1. Разрешения микрофона в Windows")
    if os.name != "nt":
        info("не Windows — пропускаю")
        return
    import winreg

    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

    def read(root, path, name="Value"):
        try:
            with winreg.OpenKey(root, path) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    glob = read(winreg.HKEY_LOCAL_MACHINE, base)
    user = read(winreg.HKEY_CURRENT_USER, base)
    desk = read(winreg.HKEY_CURRENT_USER, base + r"\NonPackaged")

    for label, val in (("доступ к микрофону (система)", glob),
                       ("доступ к микрофону (профиль)", user),
                       ("доступ для обычных программ", desk)):
        if val is None:
            info(f"{label}: не задано (обычно значит «разрешено»)")
        elif str(val).lower() == "allow":
            ok(f"{label}: разрешено")
        else:
            bad(f"{label}: {val}")
            problems.append(
                "Windows запрещает доступ к микрофону.\n"
                "         Параметры -> Конфиденциальность -> Микрофон:\n"
                "         включи «Доступ к микрофону» И «Разрешить классическим\n"
                "         приложениям доступ к микрофону»."
            )

    pol = read(winreg.HKEY_LOCAL_MACHINE,
               r"SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
               "LetAppsAccessMicrophone")
    if pol == 2:
        bad("групповая политика запрещает микрофон всем приложениям")
        problems.append("Групповая политика LetAppsAccessMicrophone=2 блокирует микрофон.")
    elif pol is not None:
        info(f"групповая политика LetAppsAccessMicrophone = {pol}")


# ---------------------------------------------------------------- 2. Python

def check_devices():
    head("2. Аудиоустройства глазами Python")
    try:
        from audio import find_cable_device, find_ffmpeg, list_output_devices
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        bad(f"не могу импортировать модули: {exc}")
        problems.append("Похоже, зависимости не встали. Перезапусти setup.bat")
        return

    ins, outs = [], []
    try:
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            api = apis[d["hostapi"]]["name"] if d["hostapi"] < len(apis) else "?"
            if d["max_input_channels"] > 0:
                ins.append((i, d["name"], api))
            if d["max_output_channels"] > 0:
                outs.append((i, d["name"], api))
    except Exception as exc:  # noqa: BLE001
        bad(f"не могу прочитать список устройств: {exc}")
        return

    print("\n  Устройства ВЫВОДА (куда бот пишет музыку):")
    for i, n, a in outs:
        print(f"     {i:>3}  {n}  [{a}]")
    print("\n  Устройства ВВОДА (что браузер может взять как микрофон):")
    for i, n, a in ins:
        print(f"     {i:>3}  {n}  [{a}]")
    print()

    dev = find_cable_device()
    if dev is None:
        bad("«CABLE Input» среди устройств вывода не найден")
        problems.append(
            "VB-Audio Virtual Cable не установлен или не было перезагрузки.\n"
            "         Если он в списке, но выключен — включи его в\n"
            "         «Параметры звука -> Дополнительные параметры звука»."
        )
    else:
        ok(f"музыка пойдёт в устройство №{dev}")

    if not any("cable output" in n.lower() or "vb-audio" in n.lower() for _, n, _ in ins):
        bad("«CABLE Output» среди устройств ЗАПИСИ не найден")
        problems.append(
            "Windows не показывает «CABLE Output» как устройство записи.\n"
            "         Включи его: Параметры -> Система -> Звук ->\n"
            "         Дополнительные параметры звука -> вкладка «Запись»."
        )
    else:
        ok("«CABLE Output» доступен как микрофон")

    if find_ffmpeg():
        ok("ffmpeg на месте")
    else:
        bad("ffmpeg не найден")
        problems.append("Нет ffmpeg. Перезапусти setup.bat")


# ---------------------------------------------------------------- 3. браузер

def check_browser():
    head("3. Что видит браузер")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        bad("Playwright не установлен — перезапусти setup.bat")
        problems.append("Playwright не установлен.")
        return

    import browser as botbrowser
    import tempfile

    args = list(botbrowser.CHROMIUM_ARGS)
    # Профиль — во временную папку, а не в OneDrive: синхронизация умеет
    # ломать создание профиля Chromium (тысячи мелких файлов).
    profile = tempfile.mkdtemp(prefix="tmbot-diag-")
    info(f"временный профиль: {profile}")

    with sync_playwright() as p:
        try:
            exe = p.chromium.executable_path
            if os.path.exists(exe):
                ok("встроенный chromium на месте")
            else:
                bad("встроенный chromium не скачан")
                info(f"ожидался тут: {exe}")
        except Exception as exc:  # noqa: BLE001
            info(f"путь к встроенному chromium неизвестен: {exc}")

        ctx = None
        name = None
        for channel in ("chrome", "msedge", None):
            label = channel or "chromium (встроенный)"
            try:
                kw = dict(user_data_dir=profile,
                          headless=False, args=args,
                          permissions=["microphone"],
                          viewport={"width": 900, "height": 620},
                          timeout=90000)
                if channel:
                    kw["channel"] = channel
                ctx = p.chromium.launch_persistent_context(**kw)
                name = label
                ok(f"{label}: запустился")
                break
            except Exception as exc:  # noqa: BLE001
                bad(f"{label}: не запустился")
                info(f"причина: {type(exc).__name__}: {_short(exc)}")

        if ctx is None:
            _diagnose_launch_failure(p, args)
            return

        print(f"\n  --- {name} ---")
        try:
            try:
                ctx.grant_permissions(["microphone"], origin=TELEMOST_ORIGIN)
            except Exception as exc:  # noqa: BLE001
                info(f"grant_permissions не сработал: {exc}")

            ctx.route(PROBE_URL, lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=PROBE_HTML))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_function("window.__diag !== null", timeout=45000)
            report(page.evaluate("window.__diag"), name)
        except Exception as exc:  # noqa: BLE001
            bad(f"{name}: проверка сорвалась — {_short(exc)}")
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def _short(exc):
    """Первая содержательная строка исключения — Playwright пишет простыни."""
    text = str(exc).strip()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("="):
            return line[:220]
    return text[:220] or type(exc).__name__


def _try_launch(p, channel, args):
    """Поднимается ли браузер с таким набором флагов? Без своего профиля."""
    br = None
    try:
        kw = dict(headless=True, args=list(args), timeout=90000)
        if channel:
            kw["channel"] = channel
        br = p.chromium.launch(**kw)
        return True
    except Exception:
        return False
    finally:
        if br:
            try:
                br.close()
            except Exception:
                pass


def _diagnose_launch_failure(p, args):
    """Ни один браузер не поднялся — ищем, что именно его валит."""
    bad("ни один браузер не запустился с рабочим набором флагов")
    print()
    info("проверяю голый запуск, вообще без флагов...")

    survivors = []
    for channel in ("chrome", "msedge", None):
        label = channel or "chromium (встроенный)"
        if _try_launch(p, channel, []):
            survivors.append((channel, label))
            ok(f"{label}: без флагов запускается")
        else:
            info(f"{label}: не запускается даже без флагов")

    if not survivors:
        problems.append(
            "Браузер не стартует даже без флагов — значит сломана сама установка.\n"
            "         Выполни в папке бота:\n"
            "         .venv\\Scripts\\python.exe -m playwright install chromium\n"
            "         Если не помогло, проверь антивирус: он может убивать\n"
            "         процесс браузера, запущенный из скрипта."
        )
        return

    channel, label = survivors[0]
    info(f"ищу виноватый флаг на {label}...")
    culprits = [flag for flag in args if not _try_launch(p, channel, [flag])]

    if culprits:
        for flag in culprits:
            bad(f"браузер падает от флага: {flag}")
        problems.append(
            "Браузер валят эти флаги:\n         " + "\n         ".join(culprits) +
            "\n         Открой browser.py и убери их из списка CHROMIUM_ARGS.\n"
            "         Бот и сам умеет откатываться на набор победнее,\n"
            "         так что просто попробуй запустить его снова."
        )
    else:
        problems.append(
            "По отдельности флаги безвредны, а вместе валят браузер.\n"
            "         Бот теперь умеет откатываться на урезанный набор —\n"
            "         просто запусти start.bat ещё раз."
        )


ERROR_HINTS = {
    "NotAllowedError": (
        "Доступ к микрофону запрещён.\n"
        "         Если в сообщении есть «system» — виноват Windows:\n"
        "         Параметры -> Конфиденциальность -> Микрофон -> включить\n"
        "         «Разрешить классическим приложениям доступ к микрофону».\n"
        "         Иначе разрешение заблокировано для сайта: удали папку\n"
        "         %LOCALAPPDATA%\\telemost-music-bot и попробуй снова."
    ),
    "NotFoundError": (
        "Браузер вообще не видит ни одного микрофона.\n"
        "         Проверь вкладку «Запись» в параметрах звука Windows."
    ),
    "NotReadableError": (
        "Устройство занято другой программой или заблокировано системой.\n"
        "         Закрой Zoom/Discord/Skype и повтори."
    ),
    "OverconstrainedError": (
        "Нужный микрофон исчез между проверками — просто повтори."
    ),
    "SecurityError": (
        "Браузер счёл страницу небезопасной. Такого быть не должно —\n"
        "         пришли вывод целиком."
    ),
}


def report(d, browser_name):
    steps = (d or {}).get("steps", {})

    before = steps.get("enum_before")
    if before is not None:
        info(f"микрофонов видно до разрешения: {len(before)}")

    plain = steps.get("gum_plain") or {}
    if plain.get("ok"):
        ok(f"микрофон выдаётся, по умолчанию: {plain.get('label')}")
    else:
        err = plain.get("error", "?")
        bad(f"микрофон НЕ выдаётся: {err} — {plain.get('message')}")
        problems.append(ERROR_HINTS.get(err, f"Ошибка браузера {err}: {plain.get('message')}"))
        return

    after = steps.get("enum_after") or []
    print("\n  Микрофоны глазами браузера:")
    for lbl in after:
        print(f"     - {lbl}")
    print()

    if steps.get("cable_found"):
        ok(f"кабель найден: {steps.get('cable_label')}")
    else:
        bad("среди микрофонов браузера нет «CABLE Output»")
        problems.append(
            "Браузер не видит виртуальный кабель, хотя микрофон ему выдали.\n"
            "         Включи «CABLE Output» во вкладке «Запись» параметров звука."
        )
        return

    cable = steps.get("gum_cable") or {}
    if cable.get("ok"):
        s = cable.get("settings") or {}
        ok(f"кабель успешно захвачен как микрофон ({cable.get('label')})")
        info(f"эхоподавление={s.get('echoCancellation')}, "
             f"шумодав={s.get('noiseSuppression')}, AGC={s.get('autoGainControl')}")
        ok(f"со стороны {browser_name} всё в порядке")
    else:
        err = cable.get("error", "?")
        bad(f"кабель захватить не вышло: {err} — {cable.get('message')}")
        problems.append(ERROR_HINTS.get(err, f"Ошибка при захвате кабеля: {err}"))


def main():
    print("\n  Диагностика Telemost Music Bot")
    check_privacy()
    check_devices()
    check_browser()

    head("ИТОГ")
    if not problems:
        print("\n  Явных проблем не нашёл: микрофон выдаётся, кабель виден и"
              "\n  захватывается. Если музыки всё равно не слышно — проверь, что"
              "\n  бот не замьючен в самом Телемосте и что громкость не в нуле.\n")
    else:
        seen = []
        for i, p in enumerate(problems, 1):
            if p in seen:
                continue
            seen.append(p)
            print(f"\n  {len(seen)}. {p}")
        print()

    input("  Нажми Enter, чтобы закрыть...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Диагностика упала: {type(exc).__name__}: {exc}")
        input("  Enter...")
