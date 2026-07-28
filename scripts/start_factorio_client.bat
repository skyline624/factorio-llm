@echo off
REM Client de TEST factorio-llm : lance Factorio avec le MEME mod-directory que le
REM serveur (mods alignes -> aucun ModsMismatch) et un write-data SEPARE (client-data)
REM -> peut tourner cote a c avec le serveur dedie ET le Factorio normal de l'utilisateur.
REM
REM Le joueur connecte EST l'IA : en production, le mod lui donne le kit de depart
REM (via on_player_joined_game) et l'orchestrateur Python pilote son character via RCON.
REM
REM 1) Lance d'abord le serveur : scripts\start_factorio_dedicated.bat
REM 2) Puis ce client, et dans le menu : Multijoueur -> Connect -> 127.0.0.1:34197
REM
REM NB : ne tue PAS factorio.exe (pour ne pas arreter le serveur). Si tu as deja un
REM     client de test ouvert, ferme-le avant de relancer celui-ci.

setlocal

set "ROOT=%~dp0.."

REM FACTORIO_EXE : auto-detection parmi les emplacements Steam courants (meme liste
REM que le serveur dedie). NB : chemins avec "Program Files (x86)" -> PAS de bloc if(...)
REM (les parentheses ferment le bloc meme entre guillemets) ; on enchaene des if exist.
set "FACTORIO_EXE="
if exist "C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
if defined FACTORIO_EXE goto :exefound
if exist "D:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=D:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe"
if defined FACTORIO_EXE goto :exefound
if exist "D:\Steam\steamapps\common\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=D:\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
if defined FACTORIO_EXE goto :exefound
if exist "E:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=E:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe"
if defined FACTORIO_EXE goto :exefound
if exist "C:\Program Files\Steam\steamapps\common\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=C:\Program Files\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
if defined FACTORIO_EXE goto :exefound
goto :noexe
:exefound
echo [start] factorio.exe : %FACTORIO_EXE%

set "MODS_DIR=%ROOT%\mods"
set "CONFIG=%ROOT%\client\config\config.ini"

REM Cree le dossier mods et une junction factorio-llm -> mod (hot-reload dev).
if not exist "%MODS_DIR%" mkdir "%MODS_DIR%"
if not exist "%MODS_DIR%\factorio-llm" mklink /J "%MODS_DIR%\factorio-llm" "%ROOT%\mod"

REM Dossier write-data client SEPARE (evite conflit .lock avec le Factorio Steam de
REM l'utilisateur ET avec le serveur dedie). Cree la config si absente (sinon le
REM client retombe sur %APPDATA%\Factorio -> mods du projet NON charges).
if not exist "%ROOT%\client-data" mkdir "%ROOT%\client-data"
if not exist "%ROOT%\client\config" mkdir "%ROOT%\client\config"
if not exist "%CONFIG%" goto :writeconfig
goto :startclient

:writeconfig
echo [path] > "%CONFIG%"
echo read-data=__PATH__system-read-data__ >> "%CONFIG%"
echo write-data=%ROOT%\client-data >> "%CONFIG%"

:startclient
echo [start] lancement du client de test (mods du projet, write-data client-data) ...
echo [start] Dans le menu : Multijoueur ^> Connect ^> 127.0.0.1:34197
echo [start] (cette fenetre reste ouverte pendant la partie ; ferme Factorio pour la liberer)
"%FACTORIO_EXE%" --config "%CONFIG%" --mod-directory "%MODS_DIR%"
goto :eof

:noexe
echo [start] factorio.exe introuvable dans les emplacements testes.
echo [start] edite ce .bat et ajoute ton chemin dans le bloc d'auto-detection.
exit /b 1