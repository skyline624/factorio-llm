"""Client RCON natif (Source RCON protocol) thread-safe.

Tous les agents partagent une seule instance de ce client (singleton via
get_rcon()). Le socket RCON n'est pas thread-safe : on serialise l'acces via
un verrou. Reconnexion transparente en cas de deconnexion.

Protocol Source RCON :
  packet = size:int32 (LE) | id:int32 | type:int32 | body | \\x00\\x00
  Types : AUTH=3, EXECCOMMAND=2, RESPONSE_VALUE=0, AUTH_RESPONSE=2

Pour gerer les reponses multi-paquets (etat JSON qui peut depasser 4096 o), on
envoie une commande sentinel apres la commande reelle et on lit jusqu'a voir
l'id du sentinel.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Optional

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_AUTH_RESPONSE = 2


class RconError(Exception):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._next_id = 10

    # ----- connexion / auth -----

    def _connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        self._auth()

    def _auth(self) -> None:
        pid = self._next_packet_id()
        self._send(pid, SERVERDATA_AUTH, self.password)
        # Factorio renvoie un AUTH_RESPONSE (type 2) avec id=pid (succes) ou id=-1
        # (echec). Certains serveurs Source envoient d'abord un RESPONSE_VALUE de
        # garde (type 0) : on lit donc jusqu'a l'AUTH_RESPONSE (robuste aux deux).
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                rid, rtype, _body = self._recv()
            except socket.timeout:
                raise RconError("auth: aucune reponse du serveur")
            if rtype == SERVERDATA_AUTH_RESPONSE:
                if rid == -1 or rid != pid:
                    raise RconError("auth refuse (mauvais mot de passe RCON)")
                return
            # RESPONSE_VALUE de garde : on continue de lire.
        raise RconError("auth: timeout en attendant AUTH_RESPONSE")

    def _ensure_connected(self) -> None:
        if self._sock is None:
            self._connect()

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    # ----- paquets bas niveau -----

    def _next_packet_id(self) -> int:
        self._next_id += 1
        if self._next_id > 1_000_000:
            self._next_id = 10
        return self._next_id

    def _send(self, packet_id: int, packet_type: int, body) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        body_null = body + b"\x00\x00"
        size = 4 + 4 + len(body_null)  # id + type + body+2null
        packet = struct.pack("<iii", size, packet_id, packet_type) + body_null
        if self._sock is None:
            raise RconError("socket non connecte")
        self._sock.sendall(packet)

    def _recv_n(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connexion fermee par le serveur")
            buf += chunk
        return buf

    def _recv(self):
        header = self._recv_n(4)
        size = struct.unpack("<i", header)[0]
        if size < 10:
            raise RconError(f"taille de paquet invalide: {size}")
        data = self._recv_n(size)
        packet_id, packet_type = struct.unpack("<ii", data[:8])
        body = data[8:-2]  # on retire les 2 octets nuls terminaux
        return packet_id, packet_type, body.decode("utf-8", errors="replace")

    # ----- API publique (thread-safe) -----

    def query(self, command: str) -> str:
        """Envoie une commande console RCON, retourne la sortie (rcon.print)."""
        with self._lock:
            last_err = None
            for attempt in range(2):
                try:
                    self._ensure_connected()
                    return self._query_locked(command)
                except (RconError, OSError) as e:
                    last_err = e
                    self._close()
                    if attempt == 0:
                        continue
            raise RconError(f"echec RCON apres reconnexion: {last_err}")

    def query_lua(self, lua_code: str, silent: bool = True) -> str:
        """Execute du Lua via /silent-command (defaut) ou /command."""
        prefix = "/silent-command " if silent else "/c "
        return self.query(prefix + lua_code)

    def close(self) -> None:
        with self._lock:
            self._close()

    # ----- logique de requete (sous verrou) -----

    def _query_locked(self, command: str) -> str:
        # Factorio renvoie la reponse entiere dans UN seul paquet RESPONSE_VALUE
        # (pas d'eclatement multi-paquets comme le Source RCON classique ; confirme
        # par la lib factorio-rcon qui lit un seul paquet). On lit donc un paquet et
        # on retourne son body -> rapide (pas d'attente de sentinelle).
        cid = self._next_packet_id()
        self._send(cid, SERVERDATA_EXECCOMMAND, command)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                _pid, rtype, body = self._recv()
            except socket.timeout:
                raise RconError("pas de reponse RCON (timeout)")
            if rtype == SERVERDATA_RESPONSE_VALUE:
                return body
            # autres types (ex. AUTH_RESPONSE residuel) : ignore et relit.
        raise RconError("pas de reponse RCON (timeout)")


# ----- singleton global partage par les agents -----

_rcon_singleton: Optional[RconClient] = None
_rcon_lock = threading.Lock()


def get_rcon(host: str = "127.0.0.1", port: int = 27015, password: str = "factoriollm",
             timeout: float = 10.0) -> RconClient:
    """Retourne le client RCON singleton (cree a la premiere demande)."""
    global _rcon_singleton
    with _rcon_lock:
        if _rcon_singleton is None:
            _rcon_singleton = RconClient(host, port, password, timeout)
        return _rcon_singleton


def reset_rcon() -> None:
    """Oublie le client singleton. A appeler apres tout REDEMARRAGE du serveur.

    Le singleton survit au serveur : il garde une socket morte, et chaque appel paie
    alors deux tentatives de reconnexion a dix secondes avant d'echouer. Une boucle
    d'attente qui sonde toutes les trois secondes devient ainsi plus lente que le
    demarrage qu'elle surveille -- mesure, une restauration d'etat depassait dix minutes
    alors que le serveur etait revenu en trente secondes.
    """
    global _rcon_singleton
    with _rcon_lock:
        if _rcon_singleton is not None:
            try:
                _rcon_singleton.close()
            except Exception:
                pass
        _rcon_singleton = None