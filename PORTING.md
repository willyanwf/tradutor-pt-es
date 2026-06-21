# PORTING — Tradutor PT↔ES → outro projeto

Documento auto-suficiente pra portar o tradutor pra outro projeto/máquina **sem
quebrar o projeto de destino**. Cole o conteúdo abaixo no Claude Code (ou
qualquer dev) na pasta do projeto destino.

---

## 📋 Resumo executivo

**O que é**: tradução simultânea PT↔ES ao vivo via mic, com display público
acessível por URL HTTPS. Stack:

- **STT**: faster-whisper large-v3 em GPU CUDA (float16)
- **MT**: NLLB-200-distilled-600M local em GPU (via ctranslate2)
- **Cache**: SQLite persistente com 727+ pares evangelical pré-traduzidos
- **Servidor**: FastAPI + WebSocket (porta 8765)
- **UI**: HTML/JS puro (operador + display público projetável)
- **Tunnel**: rathole reverse tunnel via Coolify → domínio público

**Origem**: `C:\Users\wwf3\.claude\skills\tradutor\` (skill Claude Code)
**Hardware exigido no destino**: GPU NVIDIA (~4GB VRAM mínimo) + Windows 11

---

## 🗂 Arquivos a portar (27 arquivos, ~270KB de código)

Todos relativos a `scripts/`:

| Arquivo | KB | Função |
|---|---|---|
| `server.py` | 39 | Servidor FastAPI + WebSocket. Endpoints `/`, `/display`, `/ws`, `/ws/display`, `/api/telemetry`, `/api/audio-devices`, `/api/open-browser`, `/selftest`, `/cache/stats`, `/qr` |
| `pipeline.py` | 14 | StreamingAccumulator (VAD+buffer) + PhraseDetector + transcribe_and_translate |
| `religious.py` | 20 | Hotwords evangelical + initial_prompt + normalize_bible_refs + post_edit_es + dicionário 112 pares + add_emojis_es |
| `profanity.py` | 5 | Filtro de palavrões PT + ES (descarta frase inteira) |
| `marian_translator.py` | 5 | Wrapper NLLB-200 ctranslate2 GPU |
| `translation_cache.py` | 10 | SQLite cache L2 + IncrementalTranslator (delta MT) |
| `system_capture.py` | 7 | WASAPI loopback / mic local via soundcard |
| `tradutor_app.py` | 7 | Wrapper desktop (splash + subprocess server + Edge --app) |
| `splash.py` | 8 | Tela tkinter de loading |
| `translate_live.py` | 4 | CLI mode (sem servidor) |
| `setup.py` | 3 | Instalador pip de deps |
| `setup_marian.py` | 2 | Download + convert NLLB-200 pra CT2 float16 |
| `make_icon.py` | 4 | Gera tradutor.ico via PIL |
| `install_shortcut.ps1` | 2 | Cria atalho na área de trabalho |
| `tunnel.py` | 4 | Helper Cloudflare quick tunnel (opcional) |
| `static/index.html` | 56 | UI do operador (controles + log + modal config) |
| `static/display.html` | 22 | UI tela pública (cards empilhados, fontes grandes) |
| `tradutor.ico` | 29 | Ícone do atalho (PNG/ICO multi-tamanho) |
| `tradutor_icon_preview.png` | 7 | PNG 256x256 (usado pelo splash) |
| `tunnel/server.toml` | 1 | Config rathole server (Coolify) |
| `tunnel/client.toml` | 1 | Config rathole client (máquina local) |
| `tunnel/docker-compose.coolify.yml` | 3 | Compose pra subir rathole no Coolify |
| `tunnel/docker-compose.client.yml` | 1 | Compose pra rodar cliente via Docker Desktop |
| `tunnel/start_client.ps1` | 2 | Subir cliente rathole nativo |
| `tunnel/README.md` | 6 | Passo-a-passo do tunnel |

Mais um arquivo na raiz da skill:
- `SKILL.md` (10KB) — descrição da skill no formato Claude Code

E **dados que NÃO são código** mas precisam ir junto:
- `data/marian/bible-ct2/` (~600MB) — modelo NLLB-200 já convertido pra CT2 float16
  - `model.bin`, `config.json`, `shared_vocabulary.json`
- `data/translations.db` (~180KB) — cache SQLite (já tem 727 pares pré-traduzidos)

---

## 📁 Estrutura proposta no projeto destino

Sugiro criar uma subpasta **isolada** dentro do projeto destino:

```
<projeto-destino>/
├── (arquivos do projeto destino — não tocar)
└── tradutor/                          ← TUDO da skill vai aqui
    ├── SKILL.md
    ├── PORTING.md                     ← este arquivo
    ├── references/
    │   └── troubleshooting.md
    ├── scripts/
    │   ├── server.py
    │   ├── pipeline.py
    │   ├── religious.py
    │   ├── profanity.py
    │   ├── marian_translator.py
    │   ├── translation_cache.py
    │   ├── system_capture.py
    │   ├── tradutor_app.py
    │   ├── splash.py
    │   ├── translate_live.py
    │   ├── setup.py
    │   ├── setup_marian.py
    │   ├── make_icon.py
    │   ├── install_shortcut.ps1
    │   ├── tunnel.py
    │   ├── tradutor.ico
    │   ├── tradutor_icon_preview.png
    │   ├── static/
    │   │   ├── index.html
    │   │   └── display.html
    │   └── tunnel/
    │       ├── server.toml
    │       ├── client.toml
    │       ├── docker-compose.coolify.yml
    │       ├── docker-compose.client.yml
    │       ├── start_client.ps1
    │       └── README.md
    └── data/                          ← gerado em runtime, NÃO commitar no git
        ├── marian/
        │   └── bible-ct2/             ← baixe via setup_marian.py
        ├── translations.db            ← gerado no 1º start
        ├── tradutor.log
        └── server.log
```

**Importante**: tudo é auto-contido na pasta `tradutor/`. Não toca em nada do
projeto destino.

---

## 🛠 Passo-a-passo da portabilidade

### 1. Copiar arquivos de código

Copie a pasta inteira `C:\Users\wwf3\.claude\skills\tradutor\` (exceto `data/`
e `__pycache__/`) pra `<projeto-destino>\tradutor\`.

Pode usar:
```powershell
robocopy "C:\Users\wwf3\.claude\skills\tradutor" "<DESTINO>\tradutor" /E /XD __pycache__ data
```

### 2. Instalar dependências Python

Os pacotes Python são compartilhados entre projetos no mesmo `site-packages`,
então se você já rodou no projeto original, **provavelmente já está instalado**.
Pra garantir:

```powershell
cd <DESTINO>\tradutor\scripts
python setup.py            # base
python -m pip install pythonet torch transformers sentencepiece psutil soundcard qrcode
```

**Pacotes obrigatórios** (~5GB total se for primeira instalação):
- `numpy`, `sounddevice`, `webrtcvad-wheels`, `faster-whisper`, `deep-translator`
- `fastapi`, `uvicorn[standard]`
- `transformers`, `sentencepiece` (pro NLLB tokenizer)
- `torch` (CPU, só pra converter NLLB)
- `ctranslate2` (vem com faster-whisper)
- `psutil` (telemetria)
- `soundcard` (WASAPI loopback)
- `qrcode` (gera QR pro display público)
- `Pillow` (vem com qrcode)

**Pacotes CUDA via pip** (~1.3GB, instala uma vez por máquina):
```powershell
python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 torch --index-url https://download.pytorch.org/whl/cpu
```

> Sem isso, o `faster-whisper` em GPU não acha `cublas64_12.dll` no Windows.

### 3. Baixar e converter o modelo NLLB-200

```powershell
cd <DESTINO>\tradutor\scripts
python setup_marian.py --quantization float16
```

Isso baixa `facebook/nllb-200-distilled-600M` (~1.2GB do HuggingFace) e
converte pra `<DESTINO>\tradutor\data\marian\bible-ct2\` em float16 (~600MB).

> **Alternativa rápida**: copie a pasta `data\marian\bible-ct2\` já convertida
> do projeto origem direto pro destino. Evita re-baixar.

### 4. Whisper large-v3

O modelo Whisper é baixado **on-demand** na primeira execução do `server.py`.
Cai em `~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/`
(~3GB). Compartilhado entre projetos — se já baixou, não baixa de novo.

### 5. Validar com self-test

```powershell
cd <DESTINO>\tradutor\scripts
python server.py --host 127.0.0.1 --port 8765 --device cuda --model large-v3 --backend marian --partial-ms 1000 --silence-ms 700
```

Aguarda ~15s pro Whisper carregar. Em outro terminal:

```powershell
curl http://127.0.0.1:8765/selftest
```

Deve retornar `all_ok: true` com 5 checks (static files, whisper, NLLB, VRAM, cache).

### 6. Criar atalho na área de trabalho (opcional)

```powershell
cd <DESTINO>\tradutor\scripts
powershell -ExecutionPolicy Bypass -File install_shortcut.ps1
```

Cria `Tradutor PT-ES.lnk` na área de trabalho apontando pra `tradutor_app.py`
desse projeto. **Se já tinha um atalho do projeto original, ele será
sobrescrito** — se quiser manter os dois, renomeie o destino no script
(`Tradutor PT-ES.lnk` → `Tradutor PT-ES (NovoProjeto).lnk`).

### 7. Configurar tunnel (URL pública pra audiência)

A URL pública é provida por um **Cloudflare Named Tunnel** apontando
`tradutor.siaccon.com.br` → `localhost:8766` (o domínio `siaccon.com.br` já
está no Cloudflare). O `cloudflared` roda na mesma máquina do servidor.

> Segredos (token do túnel, chaves de API) ficam **fora do repositório** —
> veja `.gitignore`. Não coloque token/chave em arquivos versionados.

---

## ⚠️ Cuidados pra NÃO atrapalhar o projeto destino

1. **Porta 8765**: o tradutor escuta nessa porta. Confirme que não está em uso
   no destino. Se estiver, mude com `--port <outra>` e ajuste `tunnel/server.toml`
   `bind_addr` correspondentemente.

2. **Pasta `data/`**: gera arquivos (DB, logs, modelos) — adicione no
   `.gitignore` do projeto destino:
   ```
   tradutor/data/
   tradutor/scripts/__pycache__/
   tradutor/scripts/**/__pycache__/
   ```

3. **Cache HuggingFace compartilhado**: `~/.cache/huggingface/` é global por
   usuário. Outros projetos que usem Whisper/NLLB vão **reusar** os mesmos
   pesos — não duplica.

4. **DLLs CUDA**: `nvidia-cublas-cu12` etc são pacotes pip globais. Múltiplos
   projetos compartilham. Sem conflito.

5. **Banco SQLite**: `data/translations.db` é local ao tradutor — não interfere
   no Firebird ou Postgres do projeto destino.

6. **Atalho na área de trabalho**: se rodar `install_shortcut.ps1` em mais de
   um projeto, o atalho **sobrescreve**. Renomeie o `.lnk` se quiser ter dois.

7. **Subdomínio do tunnel**: se reusar `tradutor.bi.siaccon.com.br`, garanta
   que **só um cliente rathole** está rodando por vez — dois clientes brigando
   pelo mesmo serviço cria comportamento errático.

---

## 🚀 Como rodar no destino (resumo)

**Cenário 1 — Operador desktop (recomendado pro dia a dia)**:
```
Duplo-clique no atalho "Tradutor PT-ES" na área de trabalho
→ splash de loading
→ Edge --app abre em http://127.0.0.1:8765
→ operador opera, audiência acessa via tunnel
```

**Cenário 2 — Manual via terminal**:
```powershell
cd <DESTINO>\tradutor\scripts
python server.py --device cuda --model large-v3 --backend marian
# Abra http://localhost:8765 num navegador
```

**Cenário 3 — Modo CLI (só terminal, sem UI)**:
```powershell
python translate_live.py --model small --backend google
```

---

## 🧪 Auto-teste pós-portabilidade

Depois de portar, valide:

```powershell
# 1. Servidor sobe
curl http://127.0.0.1:8765/health
# → {"status":"ok",...}

# 2. Self-test passa
curl http://127.0.0.1:8765/selftest
# → "all_ok": true (5 checks: static, whisper, translator, vram, cache)

# 3. Audio devices detectados
curl http://127.0.0.1:8765/api/audio-devices
# → microphones + loopbacks

# 4. Telemetria responde
curl http://127.0.0.1:8765/api/telemetry
# → cpu_pct, ram_pct, disk_free_gb, vram_used_mb, displays_connected
```

Abra `http://127.0.0.1:8765` no navegador, clique **Iniciar**, fale uma frase
em português. Deve aparecer tradução ES no log + display `/display`.

---

## 📚 Referências internas (após portar)

- **Documentação detalhada**: `tradutor/SKILL.md`
- **Troubleshooting**: `tradutor/references/troubleshooting.md`
- **Setup do tunnel**: `tradutor/scripts/tunnel/README.md`
- **Logs**: `tradutor/data/tradutor.log` (rotativo, 10MB × 5 backups)

---

## 📝 Estado das features implementadas

Todas as 20+ melhorias do roadmap original estão neste código. As principais:

- ✅ Whisper large-v3 GPU + NLLB-200 GPU
- ✅ Streaming com LocalAgreement-2 (ghost text)
- ✅ Cache SQLite L1+L2+L3 (LRU + SQLite + Google/Marian)
- ✅ IncrementalTranslator (delta translation pra parciais)
- ✅ Vocabulário evangelical (60 hotwords + initial_prompt + 112 pares seed)
- ✅ Filtro de palavrões PT+ES (descarta frase)
- ✅ Hotkeys (F1 panic, Space pause, Ctrl+M mute, Ctrl+L clear)
- ✅ WS auto-reconnect + wakeLock
- ✅ Watchdog de timeout (15s/30s) + alerta após 3 falhas
- ✅ Telemetria CPU/RAM/VRAM/disk/battery
- ✅ Endpoint `/selftest` pre-flight
- ✅ Logs rotativos com request_id UUID
- ✅ Profiles/Scenes (localStorage + export/import JSON)
- ✅ Tela pública: layout BBC-style + fonte Atkinson Hyperlegible + fatigue mitigation + 3-dot heartbeat
- ✅ CB-safe colors (azul/âmbar em vez de verde/laranja)
- ✅ Captura: mic browser, WASAPI loopback (Zoom/Teams), mic local
- ✅ Modal de config visual + collapse de header
- ✅ Wrapper desktop (Edge --app + splash + atalho)
- ✅ Botão "Abrir no navegador" pra fallback rápido

---

## 🤖 Prompt pronto pra colar em Claude Code no destino

```
Estou portando uma skill de tradução PT↔ES (Whisper GPU + NLLB-200 local +
WebSocket) pra este projeto. O projeto original está em
C:\Users\wwf3\.claude\skills\tradutor\.

Leia C:\Users\wwf3\.claude\skills\tradutor\PORTING.md primeiro. Depois:

1. Copie todos os arquivos relevantes pra <projeto>/tradutor/ usando robocopy
   (exclua __pycache__/ e data/)
2. Garanta que o .gitignore inclui tradutor/data/ e tradutor/scripts/__pycache__/
3. Rode setup_marian.py pra baixar+converter NLLB (~10 min, ~1.2GB)
4. Suba o servidor com: python server.py --device cuda --model large-v3
   --backend marian --partial-ms 1000 --silence-ms 700
5. Valide com /selftest
6. Configure tunnel se for expor audiência via URL pública

Confirme cada passo antes de prosseguir. Não toque nos arquivos do projeto
destino que não estão na pasta tradutor/.
```
