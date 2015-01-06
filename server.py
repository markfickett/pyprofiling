import random
import select
import socket

import Pyro4

import common
import messages_pb2  # generate with: protoc --python_out=. messages.proto


_SOCKET_READ_TIMEOUT = 3.0  # 0 for non-blocking


class Server(object):
  def __init__(self):
    self._players = {}
    self._player_names = set()
    self._size = messages_pb2.Coordinate(x=80, y=24)

  def Register(self, req):
    if req.player_secret in self._players:
      p = self._players[req.player_secret]
      raise RuntimeError('Player %s (%s) already registered.' % ())
    if req.player_name in self._player_names:
      raise RuntimeError('Player name %s is already taken.' % req.player_name)
    self._player_names.add(req.player_name)
    pos = messages_pb2.Coordinate(
        x=random.randint(0, self._size.x - 1),
        y=random.randint(0, self._size.y - 1))
    self._players[req.player_secret] = messages_pb2.Player(
        secret=req.player_secret,
        name=req.player_name,
        pos=pos)
    print 'registered', self._players[req.player_secret]

  def Unregister(self, req):
    player = self._players.pop(req.player_secret, None)
    if player:
      self._player_names.remove(player.name)

  def Move(self, req):
    player = self._players[req.player_secret]
    if abs(req.move.x) > 1 or abs(req.move.y) > 1:
      raise RuntimeError('Illegal move %s with value > 1.' % req.move)
    player.pos.x = (player.pos.x + req.move.x) % self._size.x
    player.pos.y = (player.pos.y + req.move.y) % self._size.y

  def GetGameState(self):
    state = messages_pb2.GameState(size=self._size)
    for player in self._players.itervalues():
      state.player.add(name=player.name, pos=player.pos)
    return state


if __name__ == '__main__':
  common.RegisterProtoSerialization()

  hostname = socket.gethostname()
  ip_addr = Pyro4.socketutil.getIpAddress(None, workaround127=True)
  ns_uri, ns_daemon, broadcast_server = Pyro4.naming.startNS(host=ip_addr)
  assert broadcast_server
  pyro_daemon = Pyro4.core.Daemon(host=hostname)
  server_uri = pyro_daemon.register(Server())
  ns_daemon.nameserver.register(common.SERVER_URI_NAME, server_uri)
  print 'registered', common.SERVER_URI_NAME, server_uri

  try:
    ns_sockets = set(ns_daemon.sockets)
    pyro_sockets = set(pyro_daemon.sockets)
    sockets_to_read = (
        [broadcast_server] + ns_daemon.sockets + pyro_daemon.sockets)
    while True:
      ready_read_sockets, _, _ = select.select(
          sockets_to_read, (), (), _SOCKET_READ_TIMEOUT)
      for s in ready_read_sockets:
        if s in pyro_sockets:
          pyro_daemon.events((s,))
        elif s in ns_sockets:
          ns_daemon.events((s,))
        elif s is broadcast_server:
          broadcast_server.processRequest()
  finally:
    ns_daemon.close()
    broadcast_server.close()
    pyro_daemon.close()
