@echo off
REM ============================================================
REM Instalador do Tradutor PT-ES — Igreja Batista Graça e Paz
REM
REM O que esse script faz:
REM   1. Verifica/baixa Python 3.11 embeddable (portátil)
REM   2. Instala dependências do tradutor (faster-whisper, marian, etc)
REM   3. Baixa modelos (Whisper small + Marian PT-ES)
REM   4. Cria atalho na área de trabalho
REM
REM Modo: CPU-only (sem GPU). Funciona em qualquer Windows 10/11.
REM ============================================================

setlocal enabledelayedexpansion
title Instalador Tradutor PT-ES — Igreja Batista Graça e Paz

echo.
echo  ===============================================
echo   TRADUTOR PT-ES — Igreja Batista Graça e Paz
echo   Instalador automático (modo CPU)
echo  ===============================================
echo.

REM ===== Pasta de instalação =====
set "INSTALL_DIR=%USERPROFILE%\TradutorIgreja"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
cd /d "%INSTALL_DIR%"

echo  Pasta de instalação: %INSTALL_DIR%
echo.

REM ===== 1. Python embeddable =====
set "PY_DIR=%INSTALL_DIR%\python"
set "PY_EXE=%PY_DIR%\python.exe"

if exist "%PY_EXE%" (
    echo  [1/5] Python já está instalado. Pulando download.
) else (
    echo  [1/5] Baixando Python 3.11 embeddable ^(~25MB^)...
    set "PY_ZIP=%INSTALL_DIR%\python.zip"
    powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile '%PY_ZIP%' -UseBasicParsing}"
    if not exist "%PY_ZIP%" (
        echo  ERRO: falha ao baixar Python. Verifique sua internet.
        pause & exit /b 1
    )
    echo  Extraindo Python...
    powershell -Command "Expand-Archive -Path '%PY_ZIP%' -DestinationPath '%PY_DIR%' -Force"
    del "%PY_ZIP%"

    REM Habilitar pip no embeddable (remove o ._pth bloqueio)
    powershell -Command "(Get-Content '%PY_DIR%\python311._pth') -replace '#import site', 'import site' | Set-Content '%PY_DIR%\python311._pth'"

    REM Baixar get-pip.py
    echo  Instalando pip...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%PY_DIR%\get-pip.py' -UseBasicParsing"
    "%PY_EXE%" "%PY_DIR%\get-pip.py" --no-warn-script-location
)

echo.

REM ===== 2. Dependências do tradutor =====
echo  [2/5] Instalando dependências (~300MB)...
echo       (faster-whisper + transformers + fastapi + outros)

"%PY_EXE%" -m pip install --upgrade pip --quiet --no-warn-script-location

REM Pacotes core (CPU-only - sem CUDA)
"%PY_EXE%" -m pip install ^
    numpy ^
    sounddevice ^
    webrtcvad-wheels ^
    faster-whisper ^
    deep-translator ^
    fastapi "uvicorn[standard]" ^
    transformers sentencepiece ^
    soundcard ^
    psutil ^
    structlog ^
    --quiet --no-warn-script-location

if errorlevel 1 (
    echo  ERRO: falha ao instalar dependências. Veja o erro acima.
    pause & exit /b 1
)

echo  Dependências OK.
echo.

REM ===== 3. Scripts do tradutor =====
echo  [3/5] Copiando scripts do tradutor...
set "SCRIPTS_DIR=%INSTALL_DIR%\scripts"
if not exist "%SCRIPTS_DIR%" mkdir "%SCRIPTS_DIR%"

REM Cópia dos arquivos (assume que install.bat está na pasta installer\ ao lado de scripts\)
set "SRC_SCRIPTS=%~dp0..\scripts"
xcopy /Y /Q /I /E "%SRC_SCRIPTS%\*" "%SCRIPTS_DIR%\" >nul
REM Nao leva segredos pros PCs da igreja (chave do short.io e' so do operador)
if exist "%SCRIPTS_DIR%\shortio_config.json" del /q "%SCRIPTS_DIR%\shortio_config.json"

echo  Scripts copiados.
echo.

REM ===== 4. Modelos =====
echo  [4/5] Baixando modelos na primeira execução (~800MB)...
echo       Whisper small (480MB) + Marian PT-ES (300MB)
echo       Isso é uma vez só. Pode demorar 5-10 minutos dependendo da internet.

REM Aciona download dos modelos rodando o setup
"%PY_EXE%" -c "from faster_whisper import WhisperModel; print('Baixando Whisper small...'); WhisperModel('small', device='cpu', compute_type='int8'); print('OK')"
if errorlevel 1 (
    echo  AVISO: Whisper falhou no pre-download. Ele vai tentar de novo quando você abrir o tradutor.
)

"%PY_EXE%" -c "from transformers import MarianMTModel, MarianTokenizer; print('Baixando Marian PT-ES...'); MarianTokenizer.from_pretrained('Helsinki-NLP/opus-mt-roa-en'); print('OK Marian PT')"

echo.

REM ===== 5. Atalho na área de trabalho =====
echo  [5/5] Criando atalho na área de trabalho...

REM Cria .vbs temporário pra gerar o atalho
set "VBS_FILE=%TEMP%\make_shortcut.vbs"
(
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = oWS.SpecialFolders^("Desktop"^) ^& "\Tradutor PT-ES.lnk"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%PY_EXE%"
    echo oLink.Arguments = """%SCRIPTS_DIR%\tradutor_app.py"""
    echo oLink.WorkingDirectory = "%SCRIPTS_DIR%"
    echo oLink.Description = "Tradutor PT-ES — Igreja Batista Graça e Paz"
    echo oLink.IconLocation = "%SCRIPTS_DIR%\tradutor_icon_preview.png"
    echo oLink.Save
) > "%VBS_FILE%"
cscript //nologo "%VBS_FILE%"
del "%VBS_FILE%"

echo.
echo  ===============================================
echo   INSTALAÇÃO COMPLETA!
echo  ===============================================
echo.
echo   Atalho criado na sua área de trabalho:
echo     Tradutor PT-ES
echo.
echo   Pra usar:
echo     1. Duplo-clique no atalho da área de trabalho
echo     2. Aguarde o navegador abrir
echo     3. Clique em "Iniciar" e fale no microfone
echo.
echo   Pasta de instalação: %INSTALL_DIR%
echo.
pause
