// Laguna Translator — i18n (PT-BR / EN)
// Chaves referenciadas via data-i18n, data-tip-i18n, data-i18n-opt, data-i18n-html.

window.LAGUNA_I18N = {
  pt: {
    // Header / badges
    "subtitle": "tradução de voz em tempo real · 100% local",
    "badge.devices": "Dispositivos",
    "tip.laguna_badge": "Status dos dispositivos virtuais (VB-CABLE ou Laguna). Verde = renomeado para Laguna. Amarelo = instalado mas não renomeado. Vermelho = não detectado.",
    "tip.gpu_badge": "Indicador de GPU/CPU usado pelo modelo STT.",
    "tip.lang_toggle": "Alternar idioma do painel (PT ↔ EN).",
    "tip.theme_toggle": "Alternar tema claro/escuro (atalho: Shift+T).",

    // FALAR
    "falar.title": "FALAR",
    "falar.subtitle": "você → Discord (tradução sai como se fosse seu mic)",
    "falar.passthrough": "🔀 Também enviar minha voz original ao Discord (mix com a tradução)",
    "falar.also_fone": "Tocar também no meu fone",

    // ESCUTAR
    "escutar.title": "ESCUTAR",
    "escutar.subtitle": "Discord → você (tradução toca no seu fone)",
    "escutar.loopback": "Captura loopback (WASAPI) — escuta saída do PC",
    "escutar.passthrough": "🔀 Também tocar áudio original no fone (Discord + tradução juntos)",

    // status
    "status.idle": "parado",
    "status.loading": "carregando",
    "status.running": "rodando",
    "status.error": "erro",
    "status.loading_models": "Carregando modelos ({model}/{device})...",
    "status.warmup": "Aquecendo...",
    "status.listening": "Ouvindo",
    "status.passthrough_duplex_fallback": "passthrough duplex falhou ({detail}); usando pipeline queue",
    "error.loopback_unavailable": "loopback indisponível: {detail}",
    "error.input_stream": "InputStream: {detail}",
    "error.passthrough_invalid_devices": "passthrough: dispositivos inválidos (src={src}, dst={dst})",
    "error.passthrough_query": "passthrough query_devices: {detail}",
    "error.passthrough_generic": "passthrough: {detail}",
    "error.play": "play(dev={dev}): {detail}",
    "error.invalid_direction": "direction deve ser falar|escutar",
    "note.worker_stopped_gain_deferred": "worker parado; ganho será aplicado no próximo start",

    // meters
    "meter.in": "entrada",
    "meter.out": "saída",
    "tip.meter_in": "Nível de áudio de ENTRADA (captura do microfone ou loopback).",
    "tip.meter_out": "Nível de áudio de SAÍDA (tradução sintetizada + eventual passthrough).",

    // labels
    "label.langs": "Idioma falado → alvo",
    "label.mic": "🎤 Microfone (captura)",
    "label.virtual_out": "📡 Saída virtual (Discord escuta aqui)",
    "label.fone": "🎧 Fone/Alto-falante",
    "label.escutar_capture": "🔊 Captura (loopback ou mic virtual)",
    "label.where_listen": "🎧 Saída (onde você ouve)",
    "label.advanced": "avançado",
    "label.model": "Modelo STT",
    "label.device": "Device",
    "label.skip_same": "Pular tradução quando detectar o idioma alvo",

    // tips
    "tip.langs": "Escolha o idioma que você fala e o idioma para qual traduzir. Se forem iguais, nada é traduzido.",
    "tip.falar.mic": "Microfone real que capta sua voz. Ex: headset, webcam.",
    "tip.falar.virtual": "Dispositivo virtual onde a tradução é reproduzida. O Discord deve ouvir este dispositivo como microfone.",
    "tip.falar.passthrough": "Envia também sua voz original (mixada com a tradução) para o Discord, para quem entende as duas línguas.",
    "tip.falar.also_fone": "Também toca a tradução no seu fone/alto-falante para você conferir o que está saindo.",
    "tip.escutar.langs": "Idioma falado pelas pessoas no Discord e o idioma para qual traduzir para você.",
    "tip.escutar.capture": "De onde capturar o áudio. Se estiver usando loopback, selecione seu fone/saída real.",
    "tip.loopback": "Captura direto da saída do Windows (WASAPI loopback). Útil quando o Discord não expõe um mic virtual.",
    "tip.escutar.fone": "Onde você vai ouvir a tradução (seu fone real).",
    "tip.escutar.passthrough": "Também toca o áudio original do Discord misturado com a tradução no seu fone.",
    "tip.advanced": "Ajustes avançados: modelo STT, dispositivo (CPU/GPU), idioma.",
    "tip.model": "Modelo do Whisper para reconhecimento de voz. Maior = mais preciso, mais pesado.",
    "tip.device": "auto detecta CUDA se disponível, senão CPU. Force cuda ou cpu se precisar.",
    "tip.skip_same": "Quando o idioma detectado já é o alvo, economiza tempo e não traduz.",
    "tip.start": "Inicia este lado (carrega modelos na primeira vez — pode demorar alguns segundos).",
    "tip.stop": "Para este lado e libera os recursos.",
    "tip.stt_block": "Transcrição do que foi captado (texto do STT).",
    "tip.mt_block": "Tradução gerada (antes de virar voz).",
    "tip.metrics": "Latência ponta-a-ponta: p50 (mediana), p95 (cauda longa), n (amostras).",

    // modelos
    "model.tiny": "tiny (rápido, menos preciso)",
    "model.small": "small (recomendado)",
    "model.medium": "medium (mais pesado)",

    // live
    "live.stt": "Transcrição",
    "live.mt": "Tradução",

    // buttons
    "btn.start": "▶ Iniciar",
    "btn.stop": "■ Parar",

    // volumes
    "vol.out": "🔊 Volume da tradução",
    "vol.pt": "🔀 Volume do passthrough",
    "tip.vol_out": "Ajusta o volume da voz traduzida (TTS) enviada para a(s) saída(s). 0 dB = sem alteração; negativo atenua, positivo amplifica (com clip a +12 dB).",
    "tip.vol_pt": "Ajusta o volume do áudio original (passthrough). Útil para destacar ou atenuar a voz crua em relação à tradução.",

    // tips (rodapé)
    "tips.setup": "Configurar Discord com Laguna Translator (passo a passo)",
    "tips.steps": `
      <li>Instale VB-CABLE (grátis): <code>https://vb-audio.com/Cable/</code> e reinicie.</li>
      <li>Abra <b>Windows Sound Settings → Mais configurações de som</b>.</li>
      <li>Em <b>Gravação</b>, clique direito em "CABLE Output" → Propriedades → aba <b>Geral</b> → renomeie para <code>Laguna Translator Mic</code>.</li>
      <li>Em <b>Reprodução</b>, clique direito em "CABLE Input" → Propriedades → renomeie para <code>Laguna Translator Output</code>.</li>
      <li>Recarregue esta página. Os devices aparecem marcados com 🌊.</li>
      <li>No Discord → Configurações → Voz e Vídeo:
        <ul>
          <li><b>Dispositivo de entrada:</b> Laguna Translator Mic</li>
          <li><b>Dispositivo de saída:</b> escolha o fone real (não o virtual)</li>
        </ul>
      </li>
    `,

    // alerts (app.js)
    "alert.need_capture": "Selecione um dispositivo de captura",
    "alert.need_fone_for_passthrough": "Marque uma saída (fone) antes de ativar o passthrough.",
    "alert.feedback": "Captura e fone não podem ser o mesmo device com passthrough (causa feedback).",
    "alert.same_langs": "Idioma de origem e alvo são iguais — nada a traduzir.",
    "mt.skipped": "(mesmo idioma — sem tradução)",
    "badge.laguna_ok": "🌊 Laguna: dispositivos renomeados OK",
    "badge.laguna_cable": "🎛 VB-CABLE detectado (renomeie para \"Laguna\")",
    "badge.laguna_none": "⚠ Sem dispositivo virtual (instale VB-CABLE)",
    "placeholder.mic": "Selecione um microfone",
    "placeholder.virtual_out": "Selecione a saída virtual",
    "placeholder.fone": "Fone/alto-falante",
    "placeholder.capture": "Selecione captura",
  },

  en: {
    // Header / badges
    "subtitle": "real-time voice translation · 100% local",
    "badge.devices": "Devices",
    "tip.laguna_badge": "Virtual device status (VB-CABLE or Laguna). Green = renamed to Laguna. Yellow = installed but not renamed. Red = not detected.",
    "tip.gpu_badge": "GPU/CPU indicator used by the STT model.",
    "tip.lang_toggle": "Toggle UI language (PT ↔ EN).",
    "tip.theme_toggle": "Toggle light/dark theme (shortcut: Shift+T).",

    // FALAR
    "falar.title": "SPEAK",
    "falar.subtitle": "you → Discord (translation goes out as your mic)",
    "falar.passthrough": "🔀 Also send my original voice to Discord (mix with translation)",
    "falar.also_fone": "Also play on my headphones",

    // ESCUTAR
    "escutar.title": "LISTEN",
    "escutar.subtitle": "Discord → you (translation plays on your headphones)",
    "escutar.loopback": "Loopback capture (WASAPI) — listen to PC output",
    "escutar.passthrough": "🔀 Also play the original audio on headphones (Discord + translation together)",

    // status
    "status.idle": "idle",
    "status.loading": "loading",
    "status.running": "running",
    "status.error": "error",
    "status.loading_models": "Loading models ({model}/{device})...",
    "status.warmup": "Warming up...",
    "status.listening": "Listening",
    "status.passthrough_duplex_fallback": "passthrough duplex failed ({detail}); using queue pipeline",
    "error.loopback_unavailable": "loopback unavailable: {detail}",
    "error.input_stream": "InputStream: {detail}",
    "error.passthrough_invalid_devices": "passthrough: invalid devices (src={src}, dst={dst})",
    "error.passthrough_query": "passthrough query_devices: {detail}",
    "error.passthrough_generic": "passthrough: {detail}",
    "error.play": "play(dev={dev}): {detail}",
    "error.invalid_direction": "direction must be falar|escutar",
    "note.worker_stopped_gain_deferred": "worker stopped; gain will be applied on next start",

    // meters
    "meter.in": "input",
    "meter.out": "output",
    "tip.meter_in": "INPUT audio level (mic or loopback capture).",
    "tip.meter_out": "OUTPUT audio level (synthesized translation + optional passthrough).",

    // labels
    "label.langs": "Spoken language → target",
    "label.mic": "🎤 Microphone (capture)",
    "label.virtual_out": "📡 Virtual output (Discord listens here)",
    "label.fone": "🎧 Headphones/Speaker",
    "label.escutar_capture": "🔊 Capture (loopback or virtual mic)",
    "label.where_listen": "🎧 Output (where you listen)",
    "label.advanced": "advanced",
    "label.model": "STT Model",
    "label.device": "Device",
    "label.skip_same": "Skip translation when detected language matches target",

    // tips
    "tip.langs": "Pick the language you speak and the target language. If equal, nothing is translated.",
    "tip.falar.mic": "Real microphone that captures your voice. E.g. headset, webcam.",
    "tip.falar.virtual": "Virtual device where the translation is played. Discord must listen to it as its mic.",
    "tip.falar.passthrough": "Also sends your original voice (mixed with the translation) to Discord, for people who understand both.",
    "tip.falar.also_fone": "Also plays the translation on your headphones/speaker so you can monitor it.",
    "tip.escutar.langs": "Language spoken by people on Discord and the target language to translate for you.",
    "tip.escutar.capture": "Where to capture audio from. If using loopback, pick your real headphone/output device.",
    "tip.loopback": "Captures directly from Windows output (WASAPI loopback). Useful when Discord doesn't expose a virtual mic.",
    "tip.escutar.fone": "Where you'll hear the translation (your real headphones).",
    "tip.escutar.passthrough": "Also plays the original Discord audio mixed with the translation on your headphones.",
    "tip.advanced": "Advanced settings: STT model, device (CPU/GPU), language.",
    "tip.model": "Whisper model for speech recognition. Larger = more accurate, heavier.",
    "tip.device": "auto picks CUDA if available, else CPU. Force cuda or cpu if needed.",
    "tip.skip_same": "When the detected language already matches the target, it saves time and skips translation.",
    "tip.start": "Starts this side (loads models the first time — may take a few seconds).",
    "tip.stop": "Stops this side and releases resources.",
    "tip.stt_block": "Transcription of what was captured (STT text).",
    "tip.mt_block": "Generated translation (before becoming voice).",
    "tip.metrics": "End-to-end latency: p50 (median), p95 (long tail), n (samples).",

    // models
    "model.tiny": "tiny (fast, less accurate)",
    "model.small": "small (recommended)",
    "model.medium": "medium (heavier)",

    // live
    "live.stt": "Transcription",
    "live.mt": "Translation",

    // buttons
    "btn.start": "▶ Start",
    "btn.stop": "■ Stop",

    // volumes
    "vol.out": "🔊 Translation volume",
    "vol.pt": "🔀 Passthrough volume",
    "tip.vol_out": "Adjusts the translated voice (TTS) volume going to the output(s). 0 dB = unchanged; negative attenuates, positive amplifies (clipped at +12 dB).",
    "tip.vol_pt": "Adjusts the original audio (passthrough) volume. Useful to emphasize or soften the raw voice relative to the translation.",

    // footer tips
    "tips.setup": "Set up Discord with Laguna Translator (step by step)",
    "tips.steps": `
      <li>Install VB-CABLE (free): <code>https://vb-audio.com/Cable/</code> and reboot.</li>
      <li>Open <b>Windows Sound Settings → More sound settings</b>.</li>
      <li>Under <b>Recording</b>, right-click "CABLE Output" → Properties → <b>General</b> tab → rename to <code>Laguna Translator Mic</code>.</li>
      <li>Under <b>Playback</b>, right-click "CABLE Input" → Properties → rename to <code>Laguna Translator Output</code>.</li>
      <li>Reload this page. Devices appear tagged with 🌊.</li>
      <li>In Discord → Settings → Voice & Video:
        <ul>
          <li><b>Input device:</b> Laguna Translator Mic</li>
          <li><b>Output device:</b> pick your real headphones (not the virtual one)</li>
        </ul>
      </li>
    `,

    // alerts (app.js)
    "alert.need_capture": "Select a capture device",
    "alert.need_fone_for_passthrough": "Pick an output (headphones) before enabling passthrough.",
    "alert.feedback": "Capture and headphones can't be the same device with passthrough (causes feedback).",
    "alert.same_langs": "Source and target languages are equal — nothing to translate.",
    "mt.skipped": "(same language — no translation)",
    "badge.laguna_ok": "🌊 Laguna: devices renamed OK",
    "badge.laguna_cable": "🎛 VB-CABLE detected (rename to \"Laguna\")",
    "badge.laguna_none": "⚠ No virtual device (install VB-CABLE)",
    "placeholder.mic": "Select a microphone",
    "placeholder.virtual_out": "Select virtual output",
    "placeholder.fone": "Headphones/speaker",
    "placeholder.capture": "Select capture",
  },
};

window.LAGUNA_T = function (key) {
  const lang = window.LAGUNA_LANG || "pt";
  const dict = window.LAGUNA_I18N[lang] || window.LAGUNA_I18N.pt;
  return dict[key] ?? window.LAGUNA_I18N.pt[key] ?? key;
};

window.applyI18n = function (lang) {
  window.LAGUNA_LANG = lang;
  document.documentElement.setAttribute("lang", lang === "pt" ? "pt-BR" : "en");

  // text content
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    el.textContent = window.LAGUNA_T(key);
  });

  // tooltips
  document.querySelectorAll("[data-tip-i18n]").forEach((el) => {
    const key = el.getAttribute("data-tip-i18n");
    el.setAttribute("data-tip", window.LAGUNA_T(key));
  });

  // options ( <option data-i18n-opt="..."> )
  document.querySelectorAll("option[data-i18n-opt]").forEach((el) => {
    const key = el.getAttribute("data-i18n-opt");
    el.textContent = window.LAGUNA_T(key);
  });

  // blocos HTML (ol/div) via data-i18n-html
  document.querySelectorAll("[data-i18n-html]").forEach((el) => {
    const key = el.getAttribute("data-i18n-html");
    el.innerHTML = window.LAGUNA_T(key);
  });

  // rótulo do botão de idioma
  const langBtn = document.getElementById("lang-toggle");
  if (langBtn) langBtn.textContent = lang === "pt" ? "🇧🇷" : "🇺🇸";
};
