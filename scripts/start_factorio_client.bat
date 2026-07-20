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
set "FACTORIO_EXE=C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
set "MODS_DIR=%ROOT%\mods"
set "CONFIG=%ROOT%\client\config\config.ini"

if not exist "%FACTORIO_EXE%" goto :noexe
if not exist "%MODS_DIR%\factorio-llm" mklink /J "%MODS_DIR%\factorio-llm" "%ROOT%\mod"
if not exist "%ROOT%\client-data" mkdir "%ROOT%\client-data"

echo [start] lancement du client de test (mods du projet, write-data client-data) ...
echo [start] Dans le menu : Multijoueur ^> Connect ^> 127.0.0.1:34197
echo [start] (cette fenetre reste ouverte pendant la partie ; ferme Factorio pour la liberer)
"%FACTORIO_EXE%" --config "%CONFIG%" --mod-directory "%MODS_DIR%"
goto :eof

:noexe
echo [start] factorio.exe introuvable : %FACTORIO_EXE%
exit /b 1