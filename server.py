import random
import select
import socket
import time

import Pyro4

import common
import config
import messages_pb2  # generate with: protoc --python_out=. messages.proto


_SERVER_UPDATE_INTERVAL = max(0.05, config.SPEED)
_SOCKET_READ_TIMEOUT = min(_SERVER_UPDATE_INTERVAL/2, 0)  # 0 = non-blocking
_PAUSE_TICKS = 2 / _SERVER_UPDATE_INTERVAL
_STARTING_TAIL_LENGTH = max(0, config.STARTING_TAIL_LENGTH)
_B = messages_pb2.Block


class Server(object):
  def __init__(self):
    self._stage = messages_pb2.GameState.COLLECT_PLAYERS
    self._killed_player_ids = set()
    self._updating_blocks = []
    self._static_blocks_by_coord = {}

    self._player_heads_by_secret = {}
    self._next_player_id = 0
    self._player_info_by_secret = {}

    self._size = messages_pb2.Coordinate(
        x=max(4, config.WIDTH),
        y=max(4, config.HEIGHT))
    self._last_update = time.time()
    self._tick = 0
    self._pause_ticks = 0

  def Register(self, req):
    if self._stage != messages_pb2.GameState.COLLECT_PLAYERS:
      raise RuntimeError('Cannot join during stage %s.' % self._stage)
    if req.player_secret in self._player_info_by_secret:
      raise RuntimeError(
          'Player %s already registered as %s.' % (
              req.player_secret,
              self._player_info_by_secret[req.player_secret]))
    if req.player_name in [
        info.name for info in self._player_info_by_secret.values()]:
      raise RuntimeError('Player name %s is already taken.' % req.player_name)

    info = messages_pb2.PlayerInfo(
        player_id=self._next_player_id, name=req.player_name)
    self._player_info_by_secret[req.player_secret] = info
    self._AddPlayerHead(req.player_secret, info)
    self._next_player_id += 1
    return messages_pb2.RegisterResponse(player=messages_pb2.PlayerInfo(
        name=req.player_name, player_id=info.player_id))

  def _AddPlayerHead(self, player_secret, player_info):
    if player_secret in self._player_heads_by_secret:
      return

    starting_pos = messages_pb2.Coordinate(
        x=random.randint(1, self._size.x - 2),
        y=random.randint(1, self._size.y - 2))
    head = _B(
        type=_B.PLAYER_HEAD,
        pos=starting_pos,
        direction=messages_pb2.Coordinate(x=1, y=0),
        player_id=player_info.player_id,
        created_tick=self._tick)
    self._player_heads_by_secret[player_secret] = head
    self._updating_blocks.append(head)

  def Unregister(self, req):
    self._player_info_by_secret.pop(req.player_secret, None)
    head = self._player_heads_by_secret.pop(req.player_secret, None)
    if head:
      self._updating_blocks.remove(head)
      self._KillPlayer(head.player_id)

  def Start(self):
    if self._stage == messages_pb2.GameState.COLLECT_PLAYERS:
      self._StartRound()

  def _StartRound(self):
    if len(self._player_info_by_secret) <= 1:
      self._stage = messages_pb2.GameState.COLLECT_PLAYERS
      return

    for secret, info in self._player_info_by_secret.iteritems():
      self._AddPlayerHead(secret, info)

    self._stage = messages_pb2.GameState.ROUND_START
    self._killed_player_ids = set()
    self._pause_ticks = 0

    self._updating_blocks = list(self._player_heads_by_secret.values())
    self._static_blocks_by_coord = {}  # Excludes updating blocks.
    self._BuildStaticBlocks()

  def _BuildStaticBlocks(self):
    def _Wall(x, y):
      return _B(
          type=_B.WALL,
          pos=messages_pb2.Coordinate(x=x, y=y))

    if config.WALLS:
      for x in range(0, self._size.x):
        for y in (0, self._size.y - 1):
          self._static_blocks_by_coord[(x, y)] = _Wall(x, y)
      for y in range(0, self._size.y):
        for x in (0, self._size.x - 1):
          self._static_blocks_by_coord[(x, y)] = _Wall(x, y)


  def Move(self, req):
    if abs(req.move.x) > 1 or abs(req.move.y) > 1:
      raise RuntimeError('Illegal move %s with value > 1.' % req.move)
    if not (req.move.x or req.move.y):
      raise RuntimeError('Cannot stand still.')
    player_head = self._player_heads_by_secret.get(req.player_secret)
    if player_head:
      player_head.direction.MergeFrom(req.move)

  def _AdvanceBlock(self, block):
    block.pos.x = (block.pos.x + block.direction.x) % self._size.x
    block.pos.y = (block.pos.y + block.direction.y) % self._size.y

  def Update(self):
    t = time.time()
    while t - self._last_update > _SERVER_UPDATE_INTERVAL:
      if self._stage == messages_pb2.GameState.ROUND_START:
        self._pause_ticks += 1
        if self._pause_ticks > _PAUSE_TICKS:
          self._stage = messages_pb2.GameState.ROUND
      elif self._stage == messages_pb2.GameState.ROUND:
        self._Tick()
      if self._stage == messages_pb2.GameState.ROUND_END:
        self._pause_ticks += 1
        if self._pause_ticks > _PAUSE_TICKS:
          self._StartRound()
      self._last_update += _SERVER_UPDATE_INTERVAL
      self._tick += 1

  def _Tick(self):
    # Add new tail segments.
    for head in self._player_heads_by_secret.itervalues():
      self._updating_blocks.append(_B(
          type=_B.PLAYER_TAIL,
          pos=head.pos,
          created_tick=self._tick,
          player_id=head.player_id))

    # Move blocks and expire tail sections.
    remaining = []
    tail_length = _STARTING_TAIL_LENGTH + self._tick / 50
    for block in self._updating_blocks:
      if block.direction:
        self._AdvanceBlock(block)
      if (block.type == _B.PLAYER_TAIL
          and self._tick - block.created_tick >= tail_length):
        continue
      remaining.append(block)
    self._updating_blocks = remaining

    self._ProcessCollisions()

    if len(self._player_info_by_secret) - len(self._killed_player_ids) <= 1:
      # TODO: Scores.
      self._pause_ticks = 0
      self._stage = messages_pb2.GameState.ROUND_END

  def _ProcessCollisions(self):
    destroyed = []
    moving_blocks_by_coord = {}
    for b in self._updating_blocks:
      coord = (b.pos.x, b.pos.y)
      hit = None
      for targets in (moving_blocks_by_coord, self._static_blocks_by_coord):
        hit = targets.get(coord)
        if hit:
          destroyed.append(hit)
          destroyed.append(b)
      if not hit:
        moving_blocks_by_coord[coord] = b
    for b in destroyed:
      if b.type == _B.PLAYER_HEAD:
        self._KillPlayer(b.player_id)
      else:
        coord = (b.pos.x, b.pos.y)
        if b in self._updating_blocks:
          self._updating_blocks.remove(b)
        elif self._static_blocks_by_coord.get(coord) is b:
          del self._static_blocks_by_coord[coord]

  def _KillPlayer(self, player_id):
    secret = None
    for secret, head in self._player_heads_by_secret.iteritems():
      if head.player_id == player_id:
        break
    if secret:
      del self._player_heads_by_secret[secret]
    for body in list(self._updating_blocks):
      if body.player_id == player_id:
        self._updating_blocks.remove(body)
        if body.type == _B.PLAYER_TAIL:
          self._static_blocks_by_coord[(body.pos.x, body.pos.y)] = body
    self._killed_player_ids.add(player_id)

  def GetGameState(self):
    state = messages_pb2.GameState(
        size=self._size,
        killed_player_id=list(self._killed_player_ids),
        block=self._updating_blocks + self._static_blocks_by_coord.values(),
        stage=self._stage)
    return state


if __name__ == '__main__':
  common.RegisterProtoSerialization()

  hostname = 'localhost' if config.LOCAL_ONLY else socket.gethostname()
  ip_addr = '127.0.0.1' if config.LOCAL_ONLY else Pyro4.socketutil.getIpAddress(
      None, workaround127=True)
  ns_uri, ns_daemon, broadcast_server = Pyro4.naming.startNS(host=ip_addr)
  if not config.LOCAL_ONLY:
    assert broadcast_server
  pyro_daemon = Pyro4.core.Daemon(host=hostname)
  game_server = Server()
  game_server_uri = pyro_daemon.register(game_server)
  ns_daemon.nameserver.register(common.SERVER_URI_NAME, game_server_uri)
  print 'registered', common.SERVER_URI_NAME, game_server_uri

  try:
    ns_sockets = set(ns_daemon.sockets)
    pyro_sockets = set(pyro_daemon.sockets)
    sockets_to_read = ns_daemon.sockets + pyro_daemon.sockets
    if broadcast_server:
      sockets_to_read.append(broadcast_server)
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
    if broadcast_server:
      broadcast_server.close()
    ns_daemon.close()
    pyro_daemon.close()
