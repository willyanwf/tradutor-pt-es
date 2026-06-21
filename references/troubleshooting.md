# Troubleshooting — tradutor

Erros comuns e soluções. Diagnóstico primeiro, fix depois.

## Setup / instalação

### `pip install webrtcvad` falha no Windows ("Microsoft Visual C++ Build Tools required")

`webrtcvad` puro precisa compilar C. Use o pacote pré-compilado:

```powershell
pip install webrtcvad-wheels
```

O `setup.py` da skill já usa isso. Se você instalou manualmente o errado, faça:

```powershell
pip uninstall webrtcvad -y
pip install webrtcvad-wheels
```

### `pip install sounddevice` instala mas import falha com "PortAudio library not found"

Raro no Windows (vem embedded), mas se acontecer:

```powershell
pip install --upgrade --force-reinstall sounddevice
```

No Linux: `sudo apt install libportaudio2`.

### `faster-whisper` reclama de "ctranslate2" ou OpenMP

Reinstale forçando rebuild:

```powershell
pip install --upgrade --force-reinstall faster-whisper ctranslate2
```

Se persistir, instale o Microsoft Visual C++ Redistributable mais recente (vcredist).

### Modelos argos não baixam (`setup.py --offline` trava)

Geralmente é DNS/firewall. Teste:

```powershell
python -c "import urllib.request; print(urllib.request.urlopen('https://www.argosopentech.com/argospm/index/').status)"
```

Se travar, o índice argos está fora ou bloqueado. Use Google (`--backend google`) ou tente novamente depois.

---

## Em runtime

### "Saiu uma frase em inglês que eu não falei!" (alucinação Whisper)

Em silêncio prolongado ou frames muito curtos, Whisper às vezes "completa" frases conhecidas (`Obrigado por assistir`, `Thanks for watching`, `Subscribe to my channel`).

Mitigações:

1. **Suba VAD pra 3** (mais agressivo, descarta silêncio melhor):
   ```powershell
   python translate_live.py --vad 3
   ```
2. **Force o idioma de origem** (evita ele inventar "en"):
   ```powershell
   python translate_live.py --src pt
   ```
3. **Use modelo maior** (`medium` alucina menos que `small`).

### Idioma detectado errado em frases curtas

Frases tipo "Sim", "Não", "Hola" são genéricas demais. O Whisper chuta.

Fix: trave a direção:

```powershell
python translate_live.py --src pt --tgt es
```

### Latência alta (5s+ pra aparecer tradução)

Possíveis causas:

1. **Modelo grande demais pro CPU**. Volte pro `small` ou `base`.
2. **Compute type errado**. Force `int8`:
   ```powershell
   python translate_live.py --compute-type int8
   ```
3. **Backend `google` lento** (rede ruim). Teste `argos` ou outro DNS.
4. **VAD muito permissivo** segurando a frase. Suba `--vad`:
   ```powershell
   python translate_live.py --vad 3
   ```

### Não capta o microfone certo (silêncio eterno, nada aparece)

1. Liste devices: `python translate_live.py --list-devices`
2. Procure linhas marcadas com `>` (input default) e veja se é o mic que você espera.
3. Force pelo index:
   ```powershell
   python translate_live.py --device 4
   ```
4. Teste se o mic está vivo fora da skill: abra Configurações do Windows → Sistema → Som → Microfone → faça o teste.

### "PortAudioError: Error opening InputStream: Invalid sample rate"

Seu mic não suporta 16kHz direto. Alguns mics USB só fazem 44.1k ou 48k. Solução: deixa o sounddevice ressamplear — edite `translate_live.py` e mude `SAMPLE_RATE` pra 48000, **mas** webrtcvad só aceita 8/16/32/48k então isso funciona, só dobre os tamanhos de frame.

Ou compre um mic decente — qualquer headset moderno faz 16k.

---

## Loopback (capturar áudio de outro app, ex: reunião do Teams/Zoom)

A skill captura o **mic**. Pra traduzir áudio que vem de outro programa (alguém falando numa reunião), você precisa de um dispositivo virtual que recebe o som de saída do sistema e expõe como input.

### Opção 1 — Stereo Mix (built-in, varia por placa)

1. Botão direito no ícone de som → Sons → aba Gravação.
2. Botão direito na área vazia → Mostrar dispositivos desativados.
3. Procure "Mixagem estéreo" / "Stereo Mix" / "What U Hear" → Ativar.
4. Use como `--device` na skill.

Nem toda placa de som tem. Se não aparece, parte pra opção 2.

### Opção 2 — VB-CABLE (virtual cable gratuito)

1. Baixe e instale: https://vb-audio.com/Cable/
2. Aparece `CABLE Input` (playback) e `CABLE Output` (recording).
3. No app fonte (Zoom, browser, etc.) → defina **CABLE Input** como saída de áudio.
4. Na skill: `--device <index do CABLE Output>`.

Desvantagem: você não ouve mais o áudio. Pra ouvir e capturar ao mesmo tempo, use VoiceMeeter (mais complexo).

### Opção 3 — VoiceMeeter (mais flexível)

Permite ouvir + capturar simultaneamente, com mix de mic + sistema. Setup mais chato mas é o padrão pra streaming/podcast.

https://vb-audio.com/Voicemeeter/

---

## Quando nada disso resolve

Rode com saída detalhada e cole o erro:

```powershell
$env:PYTHONIOENCODING="utf-8"
python translate_live.py --device <N> 2>&1 | Tee-Object -FilePath tradutor_debug.log
```

E mande o `tradutor_debug.log`.
