---
name: tradutor
description: Tradução simultânea ao vivo entre PT-BR e Espanhol via microfone, em dois modos — (1) CLI no terminal, mic via sounddevice; (2) servidor web local (FastAPI + WebSocket) com UI no navegador (mic via Web Audio API/AudioWorklet). Em ambos os modos: VAD com webrtcvad → faster-whisper para STT → deep-translator (Google online) ou argostranslate (offline) para tradução. Detecta o idioma falado automaticamente e traduz para o oposto (PT→ES ou ES→PT). Use sempre que o usuário pedir "tradução simultânea", "tradução ao vivo", "traduz o que eu falar", "tradução por microfone", "live translation", "interpretação", "servidor de tradução", "tradutor web", "abre no navegador", ou invocar `/traduzir`. O usuário fala português brasileiro — responda em PT-BR.
---

# Tradutor — Tradução Simultânea PT ↔ ES

Skill que abre uma sessão de tradução ao vivo: ouve o microfone continuamente, detecta quando alguém termina de falar (VAD), transcreve com Whisper, identifica o idioma e traduz para o oposto. Foco em PT-BR ↔ Espanhol mas funciona com qualquer par que o Whisper reconheça.

Tem **dois modos**:

- **CLI** (`translate_live.py`) — captura mic via `sounddevice`, imprime no terminal. Bom pra debug/script.
- **Servidor web** (`server.py`) — sobe um servidor local (FastAPI + WebSocket), o navegador captura o mic e mostra as traduções numa UI. Bom pra demonstrar/testar e pra usar em qualquer dispositivo da rede.

## Quando acionar

- Usuário invoca `/traduzir` (com ou sem argumentos)
- Usuário pede: "tradução simultânea", "tradução ao vivo", "traduz o que eu falar", "live translation", "interpretação por microfone"
- Usuário pede "servidor de tradução", "tradutor no navegador", "abre uma página" → use o modo servidor
- Usuário quer traduzir áudio captado do próprio computador (reunião, vídeo, etc.) — nesse caso oriente a configurar mic de loopback (ver troubleshooting). No modo navegador, o mic vem do `getUserMedia`, então o loopback precisa estar configurado como dispositivo de gravação default do Windows.

## Como funciona (pipeline)

CLI:

```
microfone ─► sounddevice (16kHz mono int16)
          ─► webrtcvad (detecta voz, agrupa em frases de ~0.3-30s)
          ─► faster-whisper (transcrição + detecção de idioma)
          ─► deep-translator/Google (ou argostranslate offline)
          ─► stdout do terminal
```

Servidor web:

```
navegador (Web Audio + AudioWorklet) ─► PCM int16 16kHz por WebSocket
          ─► webrtcvad ─► faster-whisper ─► tradutor
          ─► JSON pela WebSocket ─► UI no navegador (live update)
```

A detecção de idioma é automática: se você fala português, traduz para espanhol. Se fala espanhol, traduz para português. Pra forçar uma direção específica, use `--src` e `--tgt`.

## Primeira vez — setup

```powershell
# 1. Instalar dependências (modo online, padrão)
python "C:\Users\wwf3\.claude\skills\tradutor\scripts\setup.py"

# 1b. Ou modo offline (também instala argostranslate + modelos PT<->ES)
python "C:\Users\wwf3\.claude\skills\tradutor\scripts\setup.py" --offline

# 2. Verificar quais mics existem
python "C:\Users\wwf3\.claude\skills\tradutor\scripts\translate_live.py" --list-devices
```

Na primeira execução do `translate_live.py`, o faster-whisper baixa o modelo (~480MB pro `small`). Cai em `~/.cache/huggingface/`. Depois fica em cache.

## Como executar — comandos comuns

### Modo servidor web (recomendado pra testar)

```powershell
python "C:\Users\wwf3\.claude\skills\tradutor\scripts\server.py"
# → abra http://localhost:8765 no navegador
```

Flags:

```powershell
python server.py --host 127.0.0.1 --port 8765 --model small --backend google
# --host 0.0.0.0  -> aceita conexões de outros dispositivos na LAN
# --model medium  -> qualidade melhor (mais lento em CPU)
# --backend argos -> tradução offline (requer setup --offline)
```

A UI tem:
- Botão **Iniciar/Parar** (pede permissão do mic do navegador na primeira vez)
- Seletor de **direção**: Auto / PT→ES / ES→PT
- Seletor de **agressividade do VAD**: 0–3
- Medidor de nível do mic (pra confirmar que está captando)
- Log das frases traduzidas (mais recente em cima)

Detalhes técnicos:
- O navegador captura mic em 16kHz mono via `AudioWorklet`, envia frames PCM int16 de 30ms (960 bytes) pela WebSocket `/ws`.
- Mensagens de controle por JSON (`{type:"config", src, tgt, vad}`) ajustam direção e VAD em tempo real, sem reconectar.
- Em browsers que ignoram `sampleRate: 16000` no `AudioContext`, a UI avisa e continua usando o SR nativo (pode degradar; idealmente use Chrome/Edge moderno).

### Tradução ao vivo (caso padrão)

```powershell
python "C:\Users\wwf3\.claude\skills\tradutor\scripts\translate_live.py" --device <N>
```

Onde `<N>` é o index do mic (saída do `--list-devices`). Sem `--device` ele usa o default do Windows.

Saída típica:
```
[setup] Carregando Whisper 'small' (int8)...
[setup] Whisper pronto.
[setup] Backend de traducao: google
============================================================
Traducao ao vivo iniciada. Fale no microfone. Ctrl+C para sair.
============================================================

[14:32:01] (pt->es | audio 2.4s | stt 0.6s | mt 0.31s)
  PT: Oi, tudo bem? Como você está hoje?
  ES: Hola, ¿qué tal? ¿Cómo estás hoy?

[14:32:09] (es->pt | audio 3.1s | stt 0.8s | mt 0.28s)
  ES: Muy bien, gracias por preguntar.
  PT: Muito bem, obrigado por perguntar.
```

### Forçar direção (sem auto-detect)

```powershell
# So PT->ES, ignora o auto-detect
python translate_live.py --src pt --tgt es

# So ES->PT
python translate_live.py --src es --tgt pt
```

### Modelo maior (mais qualidade, mais lento)

```powershell
python translate_live.py --model medium    # ~1.5GB, melhor pra sotaques
python translate_live.py --model large-v3  # ~3GB, melhor qualidade (mas CPU vai sofrer)
```

### Modo offline (sem internet)

```powershell
# Pré-requisito: setup.py --offline ja rodado
python translate_live.py --backend argos
```

### Ajustar sensibilidade do VAD

Se ele corta frases curtas no meio ou demora muito pra "fechar" uma frase:

```powershell
# Mais agressivo (corta silencios mais cedo)
python translate_live.py --vad 3

# Mais permissivo (segura mais tempo, bom em ambiente barulhento)
python translate_live.py --vad 1
```

## Parâmetros disponíveis (referência rápida)

| Flag | Default | Significado |
|---|---|---|
| `--model` | `small` | Tamanho Whisper: `tiny`/`base`/`small`/`medium`/`large-v3` |
| `--device` | sistema | Index do mic (`--list-devices` mostra) |
| `--backend` | `google` | `google` (online) ou `argos` (offline) |
| `--src` | auto | Força idioma de origem (`pt`, `es`, `en`, etc.) |
| `--tgt` | oposto | Força idioma de destino |
| `--vad` | `2` | Agressividade VAD: 0 (permissivo) → 3 (agressivo) |
| `--compute-type` | `int8` | Quantização: `int8` (CPU), `float16` (GPU), `float32` (preciso) |

## Trade-offs e escolhas

### Whisper `small` vs `medium` vs `large`

- **small** (~480MB): default. ~0.5-1s de latência por frase em CPU moderno. Qualidade boa pra PT/ES claros.
- **medium** (~1.5GB): ~2-3x mais lento, mas pega sotaques e ruído melhor.
- **large-v3** (~3GB): só vale em GPU. Em CPU fica ~10s por frase, mata o "ao vivo".

### Google vs Argos

- **Google (deep-translator)**: melhor qualidade idiomática, mas online. Não tem rate limit oficial mas pode bloquear se você gritar muito (centenas/min).
- **Argos**: offline, privacidade total, mas qualidade um pouco abaixo. Bom pra dados sensíveis ou sem internet.

### Latência total esperada (CPU moderno, `small`, Google)

- Frase de 3-5s falada → 0.6-1.2s STT → 0.2-0.5s tradução → **~1s total após você parar de falar**

## Cuidados / armadilhas

- **Privacidade**: backend `google` envia o texto transcrito pra Google Translate. Pra conversas sensíveis, use `--backend argos`.
- **Whisper alucina em silêncio**: se o VAD passar um frame muito curto com só ruído, o modelo às vezes inventa "Obrigado por assistir!" ou "Subscribe". O `MIN_PHRASE_FRAMES` mitiga isso, mas se ver alucinação repetida, suba `--vad` pra 3.
- **Idioma errado detectado**: em frases muito curtas (1-2 palavras), o Whisper pode confundir PT e ES (são próximos). Pra travar a direção, use `--src pt --tgt es` ou vice-versa.
- **Mic correto**: no Windows, `--list-devices` pode mostrar dezenas. Procure o que tem `MME` ou `Microphone Array` no nome e teste. O default do sistema nem sempre é o mic certo.
- **Não funciona com áudio de outro app/reunião direto**: pra capturar áudio do Zoom/Teams/etc. você precisa de um "Stereo Mix" / VB-Cable / VoiceMeeter como dispositivo de loopback. Veja `references/troubleshooting.md`.

## Workflow típico de invocação

Quando o usuário falar "`/traduzir`" sem argumentos OU pedir "começa a tradução ao vivo":

1. **Cheque o setup**: rode `setup.py --check`. Se faltar algo, ofereça rodar o setup completo.
2. **Liste os mics**: rode `--list-devices` e mostre a lista pro usuário escolher.
3. **Inicie a sessão**: `translate_live.py --device <N>` no terminal **em primeiro plano** (não use `run_in_background` — o usuário precisa ver o output e mandar Ctrl+C).
4. **Avise sobre o primeiro download**: na primeira vez, o Whisper baixa o modelo. Demora ~30-60s.

Quando o usuário pedir uma direção específica (ex: "traduz do espanhol pro português"), passe `--src es --tgt pt` direto, sem perguntar.

Quando o usuário quiser sem internet, passe `--backend argos` e confirme se o setup offline já foi feito.

## Arquivos da skill

```
~/.claude/skills/tradutor/
├── SKILL.md                       # este arquivo
├── scripts/
│   ├── pipeline.py                # módulo comum: VAD, Whisper, tradutor
│   ├── translate_live.py          # CLI (terminal, mic via sounddevice)
│   ├── server.py                  # servidor FastAPI + WebSocket
│   ├── setup.py                   # instala deps Python + modelos argos
│   └── static/
│       └── index.html             # UI web (AudioWorklet + WebSocket)
└── references/
    └── troubleshooting.md         # erros comuns + loopback Windows
```
