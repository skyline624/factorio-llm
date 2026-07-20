@echo off
REM Lancement du serveur dedie Factorio headless avec RCON pour factorio-llm.
REM
REM IMPORTANT (mode production / connexion d'un joueur) :
REM   - write-data SEPARE (server\config\config.ini -> server-data) : le client Steam
REM     peut tourner en parallele SANS conflit de .lock sur %APPDATA%\Factorio.
REM   - Les logs sont rediriges vers logs\server.log (lisibles par l'orchestrateur).
REM
REM Pour jouer / observer : lancer un client Factorio avec le MEME mod-directory que le
REM serveur (voir scripts\start_factorio_client.bat) puis rejoindre 127.0.0.1:34197.
REM En mode production, le joueur connecte EST l'IA (le mod lui donne le kit de depart).
REM
REM NB : PAS de bloc if(...) multiligne dans ce script : le chemin d'install contient
REM      "C:\Program Files (x86)\..." et les parentheses ferment le bloc meme entre
REM      guillemets. On utilise des goto + lignes simples a la place.

setlocal

set "ROOT=%~dp0.."
set "FACTORIO_EXE=C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
set "MODS_DIR=%ROOT%\mods"
set "SAVE=%ROOT%\saves\fl-dev.zip"
set "SETTINGS=%ROOT%\scripts\server-settings.json"
set "CONFIG=%ROOT%\server\config\config.ini"
set "LOG=%ROOT%\logs\server.log"
set "RCON_PORT=27015"
set "RCON_PASSWORD=factoriollm"

if not exist "%FACTORIO_EXE%" goto :noexe

REM Tue toute instance Factorio en cours (sinon la save est verrouillee / port pris).
taskkill /IM factorio.exe /F >nul 2>&1

REM Cree le dossier mods et une junction factorio-llm -> mod (hot-reload dev).
if not exist "%MODS_DIR%" mkdir "%MODS_DIR%"
if not exist "%MODS_DIR%\factorio-llm" mklink /J "%MODS_DIR%\factorio-llm" "%ROOT%\mod"

REM Dossier logs + write-data serveur separes.
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%ROOT%\server-data" mkdir "%ROOT%\server-data"
if not exist "%ROOT%\server\config" mkdir "%ROOT%\server\config"
if not exist "%CONFIG%" goto :writeconfig
goto :checksave

:writeconfig
echo [path] > "%CONFIG%"
echo read-data=__PATH__system-read-data__ >> "%CONFIG%"
echo write-data=%ROOT%\server-data >> "%CONFIG%"

:checksave
if not exist "%SAVE%" goto :createsave
goto :startserver

:createsave
if not exist "%ROOT%\saves" mkdir "%ROOT%\saves"
echo [start] creation de la save %SAVE% ...
"%FACTORIO_EXE%" --create "%SAVE%" --mod-directory "%MODS_DIR%" --server-settings "%SETTINGS%"

:startserver
echo [start] demarrage du serveur dedie (RCON %RCON_PORT%, mdp '%RCON_PASSWORD%')
echo [start] logs : %LOG%
echo [start] Pour jouer : scripts\start_factorio_client.bat puis rejoindre 127.0.0.1:34197
echo [start] (Ctrl+C pour arreter le serveur)
"%FACTORIO_EXE%" --config "%CONFIG%" --start-server "%SAVE%" --rcon-port %RCON_PORT% --rcon-password %RCON_PASSWORD% --mod-directory "%MODS_DIR%" --server-settings "%SETTINGS%" > "%LOG%" 2>&1
goto :eof

:noexe
echo [start] factorio.exe introuvable : %FACTORIO_EXE%
echo [start] adapte FACTORIO_EXE dans ce script.
exit /b 1