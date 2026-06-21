# Tradutor PT → ES ao vivo — Igreja Batista Graça e Paz

Tradução simultânea de pregação **português → espanhol** em tempo real. O pastor
prega em PT e a audiência hispanofalante lê a tradução **no celular**, ao vivo.

## Arquitetura

```
IGREJA (PC da transmissão)        INTERNET          CASA (PC com GPU)          AUDIÊNCIA
  mesa de som / OBS                                  Whisper large-v3 (STT)      celular
        │                                            NLLB / Marian (PT→ES)        abre /v2
        ▼                          túnel Cloudflare        │                        ▲
  TradutorIgreja.exe ── áudio ──►  wss://tradutor.   ──►  server.py (FastAPI)  ────┘
  (capta + envia PCM)              siaccon.com.br/ws      serve /v2 + WebSocket
```

- **`server.py`** — servidor FastAPI + WebSocket (porta 8766). Recebe áudio (PCM
  16k mono), roda Whisper (STT) + tradução, e serve a tela pública `/v2`.
- **`church_sender.py`** (empacotado como `TradutorIgreja.exe`) — roda no PC da
  igreja: capta o áudio do culto e envia pro servidor de casa pelo WebSocket.
- **`static/display-v2.html`** — tela que a audiência abre no celular (lê no
  próprio ritmo: "A mi ritmo").
- **`static/index.html`** — painel do operador.

O "cérebro" (Whisper large-v3) **precisa de GPU NVIDIA**. Por isso roda no PC de
casa; o PC da igreja só capta e envia (não precisa de GPU).

## Componentes principais (`scripts/`)

| Arquivo | Função |
|---|---|
| `server.py` | Servidor FastAPI + WebSocket (operador `/`, audiência `/v2`) |
| `pipeline.py` | Pipeline STT → tradução |
| `marian_translator.py` | Tradução local (Marian/CTranslate2) |
| `religious.py` | Glossário/vocabulário evangélico PT→ES |
| `system_capture.py` | Captura de áudio local (WASAPI loopback / input) |
| `church_sender.py` | App da igreja (capta + envia áudio pro PC de casa) |
| `youtube_lyrics.py` | Modo música: busca no YouTube + legenda traduzida |
| `shortio_update.py` | Atualiza o link curto (short.io) pro túnel atual |
| `translation_cache.py` | Cache SQLite de traduções |

## Rede / acesso público

O PC de casa fica acessível na internet via **Cloudflare Named Tunnel** apontando
`tradutor.siaccon.com.br` → `localhost:8766`. O domínio `siaccon.com.br` já está
no Cloudflare.

## Rodar com Docker / Coolify (porta 3080)

Modo **CPU** (Coolify não tem GPU): usa Whisper `small` + NLLB local. Mais lento
que GPU (~2-3s por frase), mas funciona.

```bash
docker compose up -d --build
# tela da audiência:  http://SEU_HOST:3080/v2
```

> O **build é demorado na 1ª vez** — baixa Whisper small + o modelo NLLB
> (~1.1GB) e embute na imagem. Depois sobe rápido.

No **Coolify**: importe o `docker-compose.yml`; a porta interna é **3080** e o
Coolify cuida do domínio/HTTPS. Variáveis ajustáveis sem rebuild (`DEVICE`,
`MODEL`, `BACKEND`, etc. — ver `docker-compose.yml`). Pra usar GPU (se a máquina
tiver): `DEVICE=cuda`, `MODEL=large-v3`, `COMPUTE_TYPE=float16`.

## Setup nativo (PC de casa, com GPU)

```powershell
python scripts/setup.py            # instala dependências
python scripts/setup_marian.py     # baixa o modelo de tradução
python scripts/server.py --device auto --model large-v3 --backend marian
# abre http://localhost:8766
```

## Configuração com segredos

Arquivos com segredos **não estão no repositório** (ver `.gitignore`):
- `scripts/shortio_config.json` — chave da API do short.io. Use o
  `shortio_config.example.json` como modelo.

## Observações

- `data/` (modelos, cache, logs) e `scripts/dist|build/` (PyInstaller) não são
  versionados — são regeneráveis.
