// Jarvis sur la tablette : WebRTC direct vers OpenAI Realtime, outils exécutés par le Pi.
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const orb = $("#orb"), statusEl = $("#status"), logEl = $("#log"), banner = $("#banner"), audioEl = $("#remote");
  const IDLE_MS = 15000;      // silence avant retour en veille
  const MAX_MS = 300000;      // durée maximale d'une conversation
  const CONNECT_MS = 15000;   // connexion trop longue
  const LABELS = {
    idle: "Appuyez pour parler", connecting: "Connexion…", listening: "Je vous écoute",
    thinking: "Un instant…", speaking: "Jarvis parle",
  };

  let S = null;               // conversation en cours
  let idleTimer = 0;

  const now = () => performance.now();

  function setState(state, label) {
    document.body.dataset.state = state;
    statusEl.textContent = label || LABELS[state];
  }

  function addLine(kind, text) {
    if (!text || !text.trim()) return;
    const d = document.createElement("div");
    d.className = "msg " + kind;
    d.textContent = text.trim();
    logEl.appendChild(d);
    while (logEl.children.length > 8) logEl.firstChild.remove();
    logEl.scrollTop = logEl.scrollHeight;
  }

  function showError(msg) {
    banner.textContent = msg;
    banner.hidden = false;
    clearTimeout(showError.t);
    showError.t = setTimeout(() => { banner.hidden = true; }, 20000);
  }

  async function api(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json", "x-jarvis": "1" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
    return data;
  }

  function toolLabel(name, args) {
    const ent = [].concat(args.entity_id || []).map((e) => String(e).split(".").pop()).join(", ");
    switch (name) {
      case "commander": return "Commande : " + (args.service || "") + " " + ent;
      case "lister_appareils": return "Recherche d'appareils";
      case "etat": return "Lecture : " + String(args.entity_id || "").split(".").pop();
      case "meteo": return "Météo";
      case "agenda": return "Agenda";
      case "ajouter_evenement": return "Ajout à l'agenda : " + (args.titre || "");
      case "dernier_mail": return "Dernier e-mail";
      case "annoncer": return "Annonce";
      case "minuteur": return "Minuteur";
      case "annuler_minuteur": return "Minuteur annulé";
      case "demander_a_claude": return "Question à Claude";
      default: return name;
    }
  }

  function makeMeter(ctx, stream) {
    const src = ctx.createMediaStreamSource(stream);
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    src.connect(an);
    const buf = new Uint8Array(an.fftSize);
    return () => {
      an.getByteTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) { const d = (v - 128) / 128; sum += d * d; }
      return Math.sqrt(sum / buf.length);
    };
  }

  function beep(ctx) {
    try {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.18);
      o.connect(g).connect(ctx.destination);
      o.start();
      o.stop(ctx.currentTime + 0.2);
    } catch (_) { /* pas de bip, pas grave */ }
  }

  function send(s, obj) {
    if (s.dc && s.dc.readyState === "open") s.dc.send(JSON.stringify(obj));
  }

  function markReady(s) {
    if (s.ready || s.closed) return;
    s.ready = true;
    clearTimeout(s.readyTimer);
    s.stream.getAudioTracks().forEach((t) => { t.enabled = true; });
    s.last = now();
    beep(s.ctx);
  }

  async function handleResponse(s, resp) {
    const calls = (resp.output || []).filter((o) => o.type === "function_call");
    if (!calls.length) return;
    let followup = false;
    for (const c of calls) {
      let args = {};
      try { args = JSON.parse(c.arguments || "{}"); } catch (_) { /* arguments vides */ }
      let result;
      if (c.name === "fin_conversation") {
        s.endRequested = true;
        s.endAt = now();
        result = { ok: true };
      } else {
        s.busy++;
        addLine("tool", toolLabel(c.name, args));
        try { result = await api("/api/tool", { name: c.name, args }); }
        catch (e) { result = { erreur: String(e.message || e) }; }
        s.busy--;
        followup = true;
      }
      send(s, { type: "conversation.item.create", item: {
        type: "function_call_output", call_id: c.call_id, output: JSON.stringify(result) } });
    }
    if (followup && !s.endRequested && !s.closed) send(s, { type: "response.create" });
  }

  function onEvent(s, ev) {
    switch (ev.type) {
      case "session.updated": markReady(s); break;
      case "input_audio_buffer.speech_started":
      case "input_audio_buffer.speech_stopped": s.last = now(); break;
      case "conversation.item.input_audio_transcription.completed": addLine("user", ev.transcript); break;
      case "response.output_audio_transcript.done":
      case "response.audio_transcript.done": addLine("bot", ev.transcript); break;
      case "response.created": s.responding = true; s.last = now(); break;
      case "response.done":
        s.responding = false;
        s.last = now();
        handleResponse(s, ev.response || {});
        break;
      case "error":
        console.error("Erreur OpenAI", ev);
        showError((ev.error && ev.error.message) || "Erreur OpenAI");
        break;
      default: break;
    }
  }

  async function exchangeSdp(cfg, sdp) {
    const base = "https://api.openai.com/v1/realtime/calls";
    let last = "";
    for (const url of [base, base + "?model=" + encodeURIComponent(cfg.model)]) {
      const r = await fetch(url, {
        method: "POST", body: sdp,
        headers: { Authorization: "Bearer " + cfg.key, "Content-Type": "application/sdp" },
      });
      if (r.ok) return r.text();
      last = "OpenAI " + r.status + " : " + (await r.text()).slice(0, 200);
    }
    throw new Error(last);
  }

  async function start() {
    if (S) return;
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError("Le micro n'est autorisé que sur une adresse HTTPS (https://…ts.net). Ouvrez la page avec l'adresse sécurisée Tailscale.");
      return;
    }
    banner.hidden = true;
    setState("connecting");

    const s = S = {
      pc: null, dc: null, stream: null, ctx: null, ready: false, closed: false,
      endRequested: false, endAt: 0, responding: false, busy: 0,
      begin: now(), last: now(), lastOut: 0, outMeter: null, micMeter: null, lvl: 0, raf: 0, iv: 0,
      readyTimer: 0, wake: null,
    };
    try {
      s.ctx = new (window.AudioContext || window.webkitAudioContext)();
      s.ctx.resume();
      const sessionReq = api("/api/session");
      sessionReq.catch(() => {});
      try {
        s.stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      } catch (e) {
        throw new Error("Micro refusé : autorisez le micro pour ce site (cadenas dans la barre d'adresse).");
      }
      s.stream.getAudioTracks().forEach((t) => { t.enabled = false; });   // muet jusqu'à la configuration
      s.micMeter = makeMeter(s.ctx, s.stream);
      const cfg = await sessionReq;

      const pc = s.pc = new RTCPeerConnection();
      pc.ontrack = (e) => {
        audioEl.srcObject = e.streams[0];
        s.outMeter = makeMeter(s.ctx, e.streams[0]);
      };
      s.stream.getTracks().forEach((t) => pc.addTrack(t, s.stream));
      const dc = s.dc = pc.createDataChannel("oai-events");
      dc.onopen = () => {
        dc.send(JSON.stringify(cfg.update));
        s.readyTimer = setTimeout(() => markReady(s), 4000);
      };
      dc.onmessage = (e) => { try { onEvent(s, JSON.parse(e.data)); } catch (err) { console.error(err); } };
      dc.onclose = () => stop("Connexion perdue");
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "failed" || pc.connectionState === "closed") stop("Connexion perdue");
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const answer = await exchangeSdp(cfg, offer.sdp);
      await pc.setRemoteDescription({ type: "answer", sdp: answer });

      if (navigator.wakeLock) navigator.wakeLock.request("screen").then((w) => { s.wake = w; }).catch(() => {});
      s.iv = setInterval(() => watch(s), 250);
      const paint = () => {
        if (s.closed) return;
        const level = Math.max(s.micMeter ? s.micMeter() : 0, s.outMeter ? s.outMeter() : 0);
        s.lvl += (Math.min(level * 3, 1) - s.lvl) * 0.35;
        document.documentElement.style.setProperty("--lvl", s.lvl.toFixed(3));
        s.raf = requestAnimationFrame(paint);
      };
      paint();
    } catch (e) {
      console.error(e);
      showError(String(e.message || e));
      stop("");
    }
  }

  function watch(s) {
    if (s.closed) return;
    const t = now();
    const out = s.outMeter ? s.outMeter() : 0;
    const mic = s.micMeter && s.ready ? s.micMeter() : 0;
    if (out > 0.02) { s.lastOut = t; s.last = t; }
    if (mic > 0.06) s.last = t;
    const speaking = t - s.lastOut < 350;

    if (!s.ready) {
      setState("connecting");
      if (t - s.begin > CONNECT_MS) { showError("Connexion trop longue, réessayez."); stop(""); }
      return;
    }
    setState(speaking ? "speaking" : (s.responding || s.busy > 0) ? "thinking" : "listening");

    if (s.endRequested && !speaking && t - s.endAt > 800 && t - s.lastOut > 1200) return stop("À bientôt");
    if (!speaking && !s.responding && s.busy === 0 && t - s.last > IDLE_MS) return stop("Retour en veille");
    if (t - s.begin > MAX_MS) return stop("Durée maximale atteinte");
  }

  function stop(msg) {
    const s = S;
    if (!s || s.closed) return;
    s.closed = true;
    S = null;
    clearInterval(s.iv);
    cancelAnimationFrame(s.raf);
    clearTimeout(s.readyTimer);
    try { if (s.dc) s.dc.close(); } catch (_) { /* déjà fermé */ }
    try { if (s.pc) s.pc.close(); } catch (_) { /* déjà fermé */ }
    if (s.stream) s.stream.getTracks().forEach((t) => t.stop());
    audioEl.srcObject = null;
    try { if (s.ctx) s.ctx.close(); } catch (_) { /* déjà fermé */ }
    try { if (s.wake) s.wake.release(); } catch (_) { /* déjà relâché */ }
    document.documentElement.style.setProperty("--lvl", "0");
    setState("idle", msg || LABELS.idle);
    clearTimeout(idleTimer);
    if (msg) idleTimer = setTimeout(() => { if (!S) setState("idle"); }, 3500);
  }

  orb.addEventListener("click", () => { if (S) stop(""); else start(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden && S) stop("En pause"); });

  // Pour les tests automatiques
  window.__jarvis = { get state() { return document.body.dataset.state; } };
})();
