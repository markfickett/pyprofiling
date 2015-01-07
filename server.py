import random
import select
import socket
import time

import Pyro4

import common
import messages_pb2  # generate with: protoc --python_out=. messages.proto


_SERVER_UPDATE_INTERVAL = 0.3
_SOCKET_READ_TIMEOUT = min(_SERVER_UPDATE_INTERVAL/2, 3.0)  # 0 for non-blocking
_TAIL_LENGTH = 10


class Server(object):
  def __init__(self):
    self._player_heads_by_secret = {}
    self._updating_blocks = []  # Includes player heads.
    self._static_blocks_by_coord = {}  # Excludes updating blocks.
    self._player_names_by_secret = {}
    self._size = messages_pb2.Coordinate(x=78, y=23)
    self._last_update = time.time()
    self._tick = 0
    self._next_player_id = 0

    self._BuildStaticBlocks()

  def _BuildStaticBlocks(self):
    def _Wall(x, y):
      return messages_pb2.Block(
          type=messages_pb2.Block.WALL,
          pos=messages_pb2.Coordinate(x=x, y=y))

    for x in range(0, self._size.x):
      for y in (0, self._size.y - 1):
        self._static_blocks_by_coord[(x, y)] = _Wall(x, y)
    for y in range(0, self._size.y):
      for x in (0, self._size.x - 1):
        self._static_blocks_by_coord[(x, y)] = _Wall(x, y)

  def Register(self, req):
    if req.player_secret in self._player_heads_by_secret:
      raise RuntimeError(
          'Player %s already registered as %s.' % (
              req.player_secret,
              self._player_names_by_secret[req.player_secret]))
    if req.player_name in self._player_names_by_secret.values():
      raise RuntimeError('Player name %s is already taken.' % req.player_name)
    self._player_names_by_secret[req.player_secret] = req.player_name

    starting_pos = messages_pb2.Coordinate(
        x=random.randint(0, self._size.x - 1),
        y=random.randint(0, self._size.y - 1))
    head = messages_pb2.Block(
        type=messages_pb2.Block.PLAYER_HEAD,
        pos=starting_pos,
        direction=messages_pb2.Coordinate(x=1, y=0),
        player_id=self._next_player_id,
        created_tick=self._tick)
    self._player_heads_by_secret[req.player_secret] = head
    self._updating_blocks.append(head)
    self._next_player_id += 1

  def Unregister(self, req):
    head = self._player_heads_by_secret.pop(req.player_secret, None)
    if head:
      del self._player_names_by_secret[req.player_secret]
      self._updating_blocks.remove(head)

  def Move(self, req):
    if abs(req.move.x) > 1 or abs(req.move.y) > 1:
      raise RuntimeError('Illegal move %s with value > 1.' % req.move)
    if not (req.move.x or req.move.y):
      raise RuntimeError('Cannot stand still.')
    player_head = self._player_heads_by_secret[req.player_secret]
    player_head.direction.MergeFrom(req.move)

  def _AdvanceBlock(self, block):
    block.pos.x = (block.pos.x + block.direction.x) % self._size.x
    block.pos.y = (block.pos.y + block.direction.y) % self._size.y

  def Update(self):
    t = time.time()
    while t - self._last_update > _SERVER_UPDATE_INTERVAL:
      for head in self._player_heads_by_secret.itervalues():
        self._updating_blocks.append(messages_pb2.Block(
            type=messages_pb2.Block.PLAYER_TAIL,
            pos=head.pos,
            created_tick=self._tick,
            player_id=head.player_id))
      remaining = []
      for block in self._updating_blocks:
        if block.direction:
          self._AdvanceBlock(block)
        if (block.type == messages_pb2.Block.PLAYER_TAIL
            and self._tick - block.created_tick >= _TAIL_LENGTH):
          continue
        remaining.append(block)
      self._updating_blocks = remaining
      self._last_update += _SERVER_UPDATE_INTERVAL
      self._tick += 1

  def GetGameState(self):
    state = messages_pb2.GameState(
        size=self._size,
        block=self._updating_blocks + self._static_blocks_by_coord.values())
    return state


if __name__ == '__main__':
  common.RegisterProtoSerialization()

  hostname = socket.gethostname()
  ip_addr = Pyro4.socketutil.getIpAddress(None, workaround127=True)
  ns_uri, ns_daemon, broadcast_server = Pyro4.naming.startNS(host=ip_addr)
  assert broadcast_server
  pyro_daemon = Pyro4.core.Daemon(host=hostname)
  game_server = Server()
  game_server_uri = pyro_daemon.register(game_server)
  ns_daemon.nameserver.register(common.SERVER_URI_NAME, game_server_uri)
  print 'registered', common.SERVER_URI_NAME, game_server_uri

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
      game_server.Update()
  finally:
    ns_daemon.close()
    broadcast_server.close()
    pyro_daemon.close()
