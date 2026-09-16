# -*- coding: utf-8 -*-

import os
import queue
import re
import sys
import threading
import time

if getattr(sys, "frozen", False):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

BROWSER = "auto"

TARGET_KBPS = 128

PTIME_MS = 20

_BASE = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
PROFILE_DIR = os.path.join(_BASE, "telemost-music-bot", "chrome-profile")


def _profile_dir(channel):
    if channel == "msedge":
        return PROFILE_DIR
    suffix = channel or "chromium"
    return f"{PROFILE_DIR}-{suffix}"

TELEMOST_ORIGIN = "https://telemost.yandex.ru"

TELEMOST_WEB_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

CHROMIUM_ARGS = [
    "--start-maximized",                       # использовать весь доступный экран
    "--use-fake-ui-for-media-stream",       # молча соглашаться на микрофон
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
]

ARG_SETS = [
    CHROMIUM_ARGS,
    ["--start-maximized", "--use-fake-ui-for-media-stream",
     "--autoplay-policy=no-user-gesture-required"],
    ["--start-maximized", "--use-fake-ui-for-media-stream"],
    ["--start-maximized"],
    [],
]

# --- патч, который выполняется в каждом документе ДО кода Телемоста ---
INIT_SCRIPT = r"""
(() => {
  const CABLE = /CABLE Output|VB-Audio|VB-Cable/i;

  const fixAudio = (a) => {
    const o = (a && typeof a === 'object') ? Object.assign({}, a) : {};
    o.echoCancellation = false;
    o.noiseSuppression = false;
    o.autoGainControl = false;
    o.googEchoCancellation = false;
    o.googAutoGainControl = false;
    o.googNoiseSuppression = false;
    o.googHighpassFilter = false;
    return o;
  };

  const md = navigator.mediaDevices;
  if (!md || !md.getUserMedia) return;
  const origGUM = md.getUserMedia.bind(md);
  const origEnum = md.enumerateDevices.bind(md);

  let cableId = null;
  const findCable = async () => {
    if (cableId) return cableId;
    try {
      let devs = await origEnum();
      let hit = devs.find(d => d.kind === 'audioinput' && CABLE.test(d.label));
      if (!hit) {
        // Метки пустые, пока не выдано разрешение — берём его и пробуем снова.
        try {
          const s = await origGUM({ audio: true });
          s.getTracks().forEach(t => t.stop());
        } catch (e) {}
        devs = await origEnum();
        hit = devs.find(d => d.kind === 'audioinput' && CABLE.test(d.label));
      }
      if (hit) cableId = hit.deviceId;
    } catch (e) {}
    return cableId;
  };

  md.getUserMedia = async function (constraints) {
    const c = Object.assign({}, constraints || {});
    if (!c.audio) return origGUM(c);

    const a = fixAudio(c.audio);
    const id = await findCable();

    const attempts = [];
    if (id) {
      attempts.push(Object.assign({}, c, { audio: Object.assign({}, a, { deviceId: { exact: id } }) }));
      attempts.push(Object.assign({}, c, { audio: Object.assign({}, a, { deviceId: { ideal: id } }) }));
    }
    attempts.push(Object.assign({}, c, { audio: a }));
    attempts.push(c);

    let lastErr = null;
    for (let i = 0; i < attempts.length; i++) {
      try {
        const s = await origGUM(attempts[i]);
        const t = s.getAudioTracks ? s.getAudioTracks()[0] : null;
        window.__botMic = t ? t.label : '(нет аудиодорожки)';
        window.__botMicIsCable = !!(t && CABLE.test(t.label));
        window.__botMicAttempt = i;
        console.log('[bot] микрофон:', window.__botMic, '| кабель:', window.__botMicIsCable);
        return s;
      } catch (e) {
        lastErr = e;
        console.warn('[bot] попытка', i, 'не прошла:', e.name, e.message);
      }
    }
    window.__botMicError = lastErr ? (lastErr.name + ': ' + lastErr.message) : 'unknown';
    console.error('[bot] микрофон получить не удалось:', window.__botMicError);
    throw lastErr;
  };

  if (window.MediaStreamTrack && MediaStreamTrack.prototype.applyConstraints) {
    const origApply = MediaStreamTrack.prototype.applyConstraints;
    MediaStreamTrack.prototype.applyConstraints = function (c) {
      if (this.kind === 'audio') c = fixAudio(c);
      return origApply.call(this, c);
    };
  }
})();

// --- перехват соединений, чтобы можно было снять статистику кодека ---
(() => {
  const Orig = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  if (!Orig) return;
  window.__botPCs = [];

  function Patched() {
    const pc = new Orig(...arguments);
    try { window.__botPCs.push(pc); } catch (e) {}
    return pc;
  }
  Patched.prototype = Orig.prototype;
  Object.setPrototypeOf(Patched, Orig);
  window.RTCPeerConnection = Patched;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Patched;

  const OPUS = {
    'stereo': '1',                 // разрешаем принимать стерео
    'sprop-stereo': '1',           // и сообщаем, что сами шлём стерео
    'maxaveragebitrate': String(__TARGET_BPS__),
    'maxplaybackrate': '48000',
    'sprop-maxcapturerate': '48000',
    'usedtx': '0',                 // в музыке тихое место — это музыка, не пауза
    'cbr': '0',
    'useinbandfec': '1',
    'minptime': String(__PTIME_MS__),
  };

  const mergeFmtp = (params, extra) => {
    const map = {};
    (params || '').split(';').forEach(p => {
      p = p.trim();
      if (!p) return;
      const i = p.indexOf('=');
      if (i > 0) map[p.slice(0, i).trim()] = p.slice(i + 1).trim();
      else map[p] = null;
    });
    Object.keys(extra).forEach(k => { map[k] = extra[k]; });
    return Object.keys(map).map(k => map[k] === null ? k : k + '=' + map[k]).join(';');
  };

  const tuneSdp = (sdp) => {
    if (!sdp || sdp.indexOf('opus') < 0) return sdp;
    const eol = sdp.indexOf('\r\n') >= 0 ? '\r\n' : '\n';
    const lines = sdp.split(/\r\n|\n/);

    let inAudio = false;
    const pts = [];
    for (const l of lines) {
      if (l.charAt(0) === 'm') inAudio = l.indexOf('m=audio') === 0;
      if (!inAudio) continue;
      const m = /^a=rtpmap:(\d+)\s+opus\/48000/i.exec(l);
      if (m) pts.push(m[1]);
    }
    if (!pts.length) return sdp;

    const out = [];
    const done = {};
    inAudio = false;
    for (const l of lines) {
      if (l.charAt(0) === 'm') inAudio = l.indexOf('m=audio') === 0;
      if (inAudio) {
        const f = /^a=fmtp:(\d+)\s+(.*)$/.exec(l);
        if (f && pts.indexOf(f[1]) >= 0) {
          done[f[1]] = true;
          out.push('a=fmtp:' + f[1] + ' ' + mergeFmtp(f[2], OPUS));
          // Размер пакета задаём сразу следом, чтобы он был ровно один.
          out.push('a=ptime:' + __PTIME_MS__);
          out.push('a=maxptime:' + Math.max(60, __PTIME_MS__ * 3));
          continue;
        }
        // Старые a=ptime/a=maxptime выбрасываем — свои уже поставили.
        if (l.indexOf('a=ptime:') === 0 || l.indexOf('a=maxptime:') === 0) continue;
        // b=AS в килобитах, b=TIAS в битах — оба режут поток сверху
        if (l.indexOf('b=AS:') === 0) {
          out.push('b=AS:' + Math.ceil(__TARGET_BPS__ / 1000)); continue;
        }
        if (l.indexOf('b=TIAS:') === 0) { out.push('b=TIAS:' + __TARGET_BPS__); continue; }
      }
      out.push(l);
    }

    const res = [];
    inAudio = false;
    for (const l of out) {
      if (l.charAt(0) === 'm') inAudio = l.indexOf('m=audio') === 0;
      res.push(l);
      if (!inAudio) continue;
      const m = /^a=rtpmap:(\d+)\s+opus\/48000/i.exec(l);
      if (m && !done[m[1]]) {
        done[m[1]] = true;
        res.push('a=fmtp:' + m[1] + ' ' + mergeFmtp('', OPUS));
        res.push('a=ptime:' + __PTIME_MS__);
        res.push('a=maxptime:' + Math.max(60, __PTIME_MS__ * 3));
      }
    }
    return res.join(eol);
  };
  window.__botTuneSdp = tuneSdp;

  const proto = Orig.prototype;
  ['setLocalDescription', 'setRemoteDescription'].forEach((name) => {
    const orig = proto[name];
    if (!orig) return;
    proto[name] = function (desc) {
      const rest = Array.prototype.slice.call(arguments, 1);
      if (desc && desc.sdp) {
        let tuned;
        try { tuned = tuneSdp(desc.sdp); } catch (e) { tuned = null; }
        if (tuned && tuned !== desc.sdp) {
          const self = this;
          // Если Телемост или Chrome не примут правленый SDP — тихо
          // откатываемся на оригинал, звонок важнее качества.
          return Promise.resolve(orig.apply(self, [{ type: desc.type, sdp: tuned }].concat(rest)))
            .then(() => { window.__botSdpTuned = true; })
            .catch((e) => {
              console.warn('[bot] правленый SDP отклонён (' + name + '):', e && e.message);
              window.__botSdpTuned = false;
              return orig.apply(self, [desc].concat(rest));
            });
        }
      }
      return orig.apply(this, arguments);
    };
  });

  const tuneSenders = () => {
    (window.__botPCs || []).forEach((pc) => {
      let senders;
      try { senders = pc.getSenders(); } catch (e) { return; }
      senders.forEach((s) => {
        if (!s.track || s.track.kind !== 'audio') return;
        try {
          const p = s.getParameters();
          if (!p.encodings || !p.encodings.length) p.encodings = [{}];
          if (p.encodings[0].maxBitrate === __TARGET_BPS__) return;
          p.encodings[0].maxBitrate = __TARGET_BPS__;
          s.setParameters(p).then(() => { window.__botSenderTuned = true; })
                            .catch(() => {});
        } catch (e) {}
      });
    });
  };
  setInterval(tuneSenders, 2000);

  window.__botCollect = async () => {
    const res = { ts: Date.now(), bytes: 0, packets: 0, pcs: 0,
                  codec: null, target: null, source: null, remote: null };
    for (const pc of (window.__botPCs || [])) {
      let stats;
      try { stats = await pc.getStats(); } catch (e) { continue; }
      const byId = {};
      stats.forEach(r => { byId[r.id] = r; });
      stats.forEach(r => {
        const audio = r.kind === 'audio' || r.mediaType === 'audio';
        if (r.type === 'outbound-rtp' && audio) {
          res.pcs++;
          res.bytes += r.bytesSent || 0;
          res.packets += r.packetsSent || 0;
          if (r.targetBitrate) res.target = r.targetBitrate;
          const c = r.codecId && byId[r.codecId];
          if (c) res.codec = { mime: c.mimeType, rate: c.clockRate,
                               channels: c.channels || null, fmtp: c.sdpFmtpLine || '' };
        }
        if (r.type === 'media-source' && audio) {
          res.source = { level: r.audioLevel, channels: r.channelCount || null };
        }
        if (r.type === 'remote-inbound-rtp' && audio) {
          res.remote = { lost: r.packetsLost, jitter: r.jitter, rtt: r.roundTripTime };
        }
      });
    }
    return res;
  };
})();
"""

INIT_SCRIPT = (INIT_SCRIPT
               .replace("__TARGET_BPS__", str(int(TARGET_KBPS * 1000)))
               .replace("__PTIME_MS__", str(int(PTIME_MS))))

SINK_SCRIPT = r"""
(() => {
  const CABLE = /CABLE Input|VB-Audio|VB-Cable/i;
  const S = window.__ymSink = { state: 'ищу', device: null, applied: 0, error: null };

  let sinkId = null;
  const waiting = [];          // элементы и контексты, созданные до готовности

  const findSink = async () => {
    const md = navigator.mediaDevices;
    if (!md || !md.enumerateDevices) return null;
    const look = async () => (await md.enumerateDevices())
      .find(d => d.kind === 'audiooutput' && CABLE.test(d.label));
    try {
      let hit = await look();
      if (!hit) {
        // Пока не выдано разрешение, у устройств пустые метки. Запрос
        // микрофона открывает список целиком.
        try {
          const s = await md.getUserMedia({ audio: true });
          s.getTracks().forEach(t => t.stop());
        } catch (e) {}
        hit = await look();
      }
      return hit || null;
    } catch (e) { return null; }
  };

  const route = async (obj) => {
    if (!sinkId || !obj || obj.__ymRouted) return;
    if (typeof obj.setSinkId !== 'function') return;
    try {
      await obj.setSinkId(sinkId);
      obj.__ymRouted = true;
      S.applied++;
    } catch (e) {
      S.error = e.name + ': ' + e.message;
    }
  };

  const scan = () => {
    document.querySelectorAll('audio,video').forEach(route);
  };

  // Плеер может создать элемент в любой момент — ловим и его.
  if (window.HTMLMediaElement) {
    const origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      const self = this;
      const args = arguments;
      if (sinkId && !self.__ymRouted) {
        return route(self).then(() => origPlay.apply(self, args));
      }
      if (!sinkId) waiting.push(self);
      return origPlay.apply(self, args);
    };
  }

  // Часть плееров работает через Web Audio, а не через <audio>.
  const OrigAC = window.AudioContext || window.webkitAudioContext;
  if (OrigAC) {
    function PatchedAC() {
      const ctx = new OrigAC(...arguments);
      if (sinkId) route(ctx); else waiting.push(ctx);
      return ctx;
    }
    PatchedAC.prototype = OrigAC.prototype;
    Object.setPrototypeOf(PatchedAC, OrigAC);
    window.AudioContext = PatchedAC;
    if (window.webkitAudioContext) window.webkitAudioContext = PatchedAC;
  }

  (async () => {
    const dev = await findSink();
    if (!dev) {
      S.state = 'нет-устройства';
      return;
    }
    sinkId = dev.deviceId;
    S.device = dev.label;

    // Проверяем разрешение сразу на пустышке, не дожидаясь плеера:
    // так сбой виден мгновенно, а не через полчаса тишины.
    try {
      const probe = new Audio();
      await probe.setSinkId(sinkId);
      S.state = 'готово';
    } catch (e) {
      S.state = 'отказано';
      S.error = e.name + ': ' + e.message;
      return;
    }

    while (waiting.length) route(waiting.pop());
    scan();
    try {
      new MutationObserver(scan).observe(document.documentElement,
                                         { childList: true, subtree: true });
    } catch (e) {}
    document.addEventListener('play', (e) => route(e.target), true);
  })();
})();
"""

JOIN_BUTTONS = [
    r"Продолжить в браузере",
    r"Открыть в браузере",
    r"Продолжить как гость",
    r"Присоединиться",
    r"Подключиться",
    r"Войти",
    r"Продолжить",
    r"Join",
    r"Continue",
]

CAMERA_OFF = [r"Выключить камеру", r"Turn off camera"]
MIC_ON = [r"Включить микрофон", r"Включить микро", r"Unmute", r"Turn on microphone"]


def _channels():
    if BROWSER == "auto":
        if getattr(sys, "frozen", False):
            return (None, "chrome", "msedge")
        return ("chrome", "msedge", None)
    if BROWSER == "chromium":
        return (None,)
    return (BROWSER,)


class TelemostBrowser(threading.Thread):
    """Держит окно браузера живым в своём потоке (sync-API Playwright этого требует)."""

    def __init__(self, meet_url, bot_name="Music Bot", log=print):
        super().__init__(daemon=True)
        self.meet_url = meet_url
        self.bot_name = bot_name
        self.log = log
        self._stop_evt = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self._jobs = queue.Queue()
        self._ctx = None
        self._music_page = None

    def close(self):
        self._stop_evt.set()

    def call(self, fn, timeout=30.0):
        """Выполнить fn(page) внутри потока браузера и вернуть результат."""
        if not self.is_alive():
            raise RuntimeError("браузер не запущен")
        box = []
        self._jobs.put((fn, box))
        deadline = time.time() + timeout
        while not box and time.time() < deadline:
            time.sleep(0.03)
        if not box:
            raise TimeoutError("браузер не ответил")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def open_music(self, url):
        """Открыть страницу с музыкой и увести её звук в кабель."""
        def job(_page):
            p = self._music_page
            if p is None or p.is_closed():
                p = self._ctx.new_page()
                p.add_init_script(SINK_SCRIPT)
                self._music_page = p
            p.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                p.bring_to_front()
            except Exception:
                pass
            try:
                p.wait_for_function(
                    "window.__ymSink && window.__ymSink.state !== 'ищу'",
                    timeout=20000)
            except Exception:
                pass
            try:
                return p.evaluate("window.__ymSink || null")
            except Exception:
                return None
        return self.call(job, timeout=120)

    def music_status(self):
        def job(_page):
            p = self._music_page
            if p is None or p.is_closed():
                return None
            return p.evaluate("window.__ymSink || null")
        return self.call(job, timeout=20)

    def close_music(self):
        def job(_page):
            p = self._music_page
            self._music_page = None
            if p is None or p.is_closed():
                return False
            p.close()
            return True
        return self.call(job, timeout=30)

    def audio_stats(self, window_s=4.0):
        """Два среза статистики с паузой — из них считается реальный битрейт."""
        js = "() => window.__botCollect ? window.__botCollect() : null"

        def grab(page):
            best = None
            # Звонок может жить в iframe, поэтому обходим все фреймы.
            for frame in page.frames:
                try:
                    r = frame.evaluate(js)
                except Exception:
                    continue
                if r and r.get("pcs"):
                    if best is None or r["bytes"] > best["bytes"]:
                        best = r
            return best

        first = self.call(grab)
        if not first:
            return None
        time.sleep(window_s)
        second = self.call(grab)
        if not second:
            return None

        dt = max((second["ts"] - first["ts"]) / 1000.0, 0.001)
        second["kbps"] = (second["bytes"] - first["bytes"]) * 8 / dt / 1000.0
        second["dpackets"] = second["packets"] - first["packets"]
        second["seconds"] = dt
        return second

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.error = "Playwright не установлен. Запусти setup.bat"
            self.log(f"[браузер] {self.error}")
            self.ready.set()
            return

        try:
            os.makedirs(PROFILE_DIR, exist_ok=True)
        except Exception:
            pass

        with sync_playwright() as p:
            ctx = None
            page = None
            last_error = None
            for channel in _channels():
                label = channel or "chromium (встроенный)"
                for args in ARG_SETS:
                    candidate = None
                    try:
                        kwargs = dict(
                            user_data_dir=_profile_dir(channel),
                            headless=False,
                            args=args,
                            permissions=["microphone"],
                            no_viewport=True,
                            timeout=90000,
                        )
                        if channel:
                            kwargs["channel"] = channel
                        candidate = p.chromium.launch_persistent_context(**kwargs)

                        candidate_page = (
                            candidate.pages[0] if candidate.pages
                            else candidate.new_page()
                        )
                        candidate_page.wait_for_timeout(350)
                        candidate_page.evaluate("1")

                        ctx = candidate
                        page = candidate_page
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        if candidate is not None:
                            try:
                                candidate.close()
                            except Exception:
                                pass

                if ctx is not None:
                    if args is CHROMIUM_ARGS:
                        self.log(f"[браузер] запущен: {label}")
                    else:
                        self.log(f"[браузер] запущен: {label} (урезанный набор флагов: "
                                 f"{' '.join(args) or 'без флагов'})")
                        if "--use-fake-ui-for-media-stream" not in args:
                            self.log("[браузер] разрешение на микрофон может спросить окном — разреши")
                    break

                self.log(f"[браузер] {label} не поднялся ни с какими флагами: "
                         f"{type(last_error).__name__}: "
                         f"{str(last_error).strip().splitlines()[0][:150]}")

            if ctx is None:
                self.error = f"браузер не запустился ({last_error}). Запусти diag.bat"
                self.log("[браузер] ни один браузер не запустился — запусти diag.bat")
                self.ready.set()
                return

            try:
                try:
                    ctx.grant_permissions(["microphone"], origin=TELEMOST_ORIGIN)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[браузер] не смог выдать разрешение заранее: {exc}")

                ctx.add_init_script(INIT_SCRIPT)
                self._ctx = ctx
                page.route(f"{TELEMOST_ORIGIN}/**", self._route_telemost_web)
                self._open_telemost(page)
                self._try_join(page)
                self._report_mic(page)
                self.ready.set()
                while not self._stop_evt.is_set():
                    try:
                        fn, box = self._jobs.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    try:
                        box.append(("ok", fn(page)))
                    except Exception as exc:  # noqa: BLE001
                        box.append(("err", exc))
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                self.log(f"[браузер] {exc}")
                self.ready.set()
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
                self.log("[браузер] закрыт")

    # ---------- best-effort автоклик ----------

    def _route_telemost_web(self, route, request):
        """Не даём Windows-странице уводить вход в desktop-приложение."""
        headers = dict(request.headers)
        if request.is_navigation_request() and request.resource_type == "document":
            headers["user-agent"] = TELEMOST_WEB_UA
        route.continue_(headers=headers)

    def _open_telemost(self, page):
        """Открыть Телемост и восстановиться после тайм-аута его CDN."""
        last_error = None
        for attempt in range(1, 4):
            if attempt > 1:
                self.log(f"[браузер] Телемост не загрузился, повтор {attempt}/3...")
                try:
                    page.goto("about:blank", wait_until="commit", timeout=5000)
                except Exception:
                    pass

            try:
                page.goto(
                    self.meet_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                last_error = None
            except Exception as exc:  # noqa: BLE001
                last_error = exc

            try:
                page.wait_for_function(
                    """() => {
                        const text = document.body ? document.body.innerText : '';
                        return /Продолжить в браузере|Присоединиться|Подключиться|Выйти|Покинуть|Join|Continue|Leave/i.test(text);
                    }""",
                    timeout=12000,
                )
                return
            except Exception:
                continue

        detail = ""
        if last_error:
            detail = ": " + str(last_error).strip().splitlines()[0][:180]
        raise RuntimeError(
            "Телемост не загрузил интерфейс после трёх попыток"
            f"{detail}. Проверь интернет и повтори join."
        )

    def _try_join(self, page):
        page.wait_for_timeout(3500)

        if self._fill_name(page):
            self.log(f"[браузер] имя подставлено: {self.bot_name}")

        clicked = []
        deadline = time.time() + 25
        while time.time() < deadline:
            hit = self._click_any(page, JOIN_BUTTONS)
            if hit:
                clicked.append(hit)
                page.wait_for_timeout(2500)
                self._fill_name(page)
            else:
                page.wait_for_timeout(1200)
            if self._looks_joined(page):
                break

        self._click_any(page, CAMERA_OFF)
        self._click_any(page, MIC_ON)

        if self._looks_joined(page):
            self.log("[браузер] похоже, бот в звонке. Проверь список участников.")
        else:
            self.log(
                "[браузер] автозаход не добил"
                + (f" (нажал: {', '.join(clicked)})" if clicked else "")
                + ". Дожми кнопку в окне браузера руками — звук всё равно пойдёт из кабеля."
            )

    def _report_mic(self, page):
        """Главный признак того, что бота будет слышно."""
        try:
            mic = page.evaluate("window.__botMic || null")
            is_cable = page.evaluate("window.__botMicIsCable || false")
            err = page.evaluate("window.__botMicError || null")
        except Exception:
            return

        if err:
            self.log(f"[браузер] МИКРОФОН НЕ ВЫДАН: {err}")
            self.log("[браузер] запусти diag.bat — он скажет, что именно блокирует")
        elif mic and is_cable:
            self.log(f"[браузер] микрофон бота: {mic} — то что надо")
        elif mic:
            self.log(f"[браузер] микрофон бота: {mic}")
            self.log("[браузер] ВНИМАНИЕ: это не виртуальный кабель, музыки слышно не будет.")
            self.log("[браузер] выбери \"CABLE Output\" микрофоном в настройках Телемоста")
        else:
            self.log("[браузер] Телемост ещё не запрашивал микрофон — проверь после входа")

    def _fill_name(self, page):
        selectors = [
            'input[placeholder*="мя" i]',
            'input[placeholder*="name" i]',
            'input[name*="name" i]',
            'input[type="text"]:visible',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1200):
                    if (loc.input_value() or "").strip() != self.bot_name:
                        loc.fill(self.bot_name, timeout=2000)
                    return True
            except Exception:
                continue
        return False

    def _click_any(self, page, patterns):
        for pat in patterns:
            rx = re.compile(pat, re.I)
            for getter in (
                lambda: page.get_by_role("button", name=rx),
                lambda: page.get_by_text(rx),
            ):
                try:
                    loc = getter().first
                    if loc.is_visible(timeout=700):
                        loc.click(timeout=2500)
                        return pat
                except Exception:
                    continue
        return None

    def _looks_joined(self, page):
        """Грубая эвристика: на экране звонка есть кнопка выхода/завершения."""
        for pat in (r"Выйти", r"Завершить", r"Покинуть", r"Leave"):
            try:
                if page.get_by_role("button", name=re.compile(pat, re.I)).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False
