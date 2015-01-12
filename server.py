import itertools
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
_MAX_ROCKET_AGE = 100
_ROCKETS_PER_AMMO = 3
_AMMO_RARITY = 300  # 1 in this many blocks is ammo.
_MINE_RARITY = 500
_HEAD_MOVE_INTERVAL = 3

_B = messages_pb2.Block


class Server(object):
  def __init__(self):
    self._size = messages_pb2.Coordinate(
        x=max(4, config.WIDTH),
        y=max(4, config.HEIGHT))
    self._static_blocks_grid = _MakeGrid(self._size)
    self._player_tails = []  # a subset of static blocks; to track expiration
    self._rockets = []

    self._player_heads_by_secret = {}
    self._next_player_id = 0
    self._player_infos_by_secret = {}

    self._stage = None
    self._state_hash = 0
    self._last_update = time.time()
    self._tick = 0

    self._SetStage(messages_pb2.GameState.COLLECT_PLAYERS)

  def Register(self, req):
    if self._stage != messages_pb2.GameState.COLLECT_PLAYERS:
      raise RuntimeError('Cannot join during stage %s.' % self._stage)
    if req.player_secret in self._player_infos_by_secret:
      raise RuntimeError(
          'Player %s already registered as %s.' % (
              req.player_secret,
              self._player_infos_by_secret[req.player_secret]))
    if not req.player_name:
      raise RuntimeError('Player name %r not allowed!' % req.player_name)
    if req.player_name in [
        info.name for info in self._player_infos_by_secret.values()]:
      raise RuntimeError('Player name %s is already taken.' % req.player_name)

    info = messages_pb2.PlayerInfo(
        player_id=self._next_player_id,
        name=req.player_name,
        alive=True,
        score=0)
    self._player_infos_by_secret[req.player_secret] = info
    self._AddPlayerHead(req.player_secret, info)
    self._next_player_id += 1
    self._RebuildClientFacingState()
    return messages_pb2.RegisterResponse(player=info)

  def _AddPlayerHead(self, player_secret, player_info):
    if player_secret in self._player_heads_by_secret:
      return

    starting_pos = _RandomPosWithin(self._size)
    head = _B(
        type=_B.PLAYER_HEAD,
        pos=starting_pos,
        direction=messages_pb2.Coordinate(x=1, y=0),
        player_id=player_info.player_id,
        created_tick=self._tick)
    self._player_heads_by_secret[player_secret] = head

  @Pyro4.oneway
  def Unregister(self, req):
    self._player_infos_by_secret.pop(req.player_secret, None)
    head = self._player_heads_by_secret.pop(req.player_secret, None)
    if head:
      self._KillPlayer(head.player_id)
      self._RebuildClientFacingState()

  def _StartRound(self):
    if len(self._player_infos_by_secret) <= 1:
      self._SetStage(messages_pb2.GameState.COLLECT_PLAYERS)
      return

    for secret, info in self._player_infos_by_secret.iteritems():
      self._AddPlayerHead(secret, info)
      info.alive = True

    self._rockets = []
    self._static_blocks_grid = _MakeGrid(self._size)
    self._BuildStaticBlocks()

    self._SetStage(messages_pb2.GameState.ROUND_START)

  def _BuildStaticBlocks(self):
    def _Wall(x, y):
      return _B(
          type=_B.WALL,
          pos=messages_pb2.Coordinate(x=x, y=y))

    if config.WALLS:
      for x in range(0, self._size.x):
        for y in (0, self._size.y - 1):
          self._static_blocks_grid[x][y] = _Wall(x, y)
      for y in range(0, self._size.y):
        for x in (0, self._size.x - 1):
          self._static_blocks_grid[x][y] = _Wall(x, y)

    if not config.INFINITE_AMMO:
      for _ in xrange(self._size.x * self._size.y / _AMMO_RARITY):
        pos = _RandomPosWithin(self._size)
        self._static_blocks_grid[pos.x][pos.y] = _B(type=_B.AMMO, pos=pos)

    if config.MINES:
      for _ in xrange(self._size.x * self._size.y / _MINE_RARITY):
        pos = _RandomPosWithin(self._size)
        self._static_blocks_grid[pos.x][pos.y] = _B(type=_B.MINE, pos=pos)

  @Pyro4.oneway
  def Move(self, req):
    if abs(req.move.x) > 1 or abs(req.move.y) > 1:
      raise RuntimeError('Illegal move %s with value > 1.' % req.move)
    if not (req.move.x or req.move.y):
      raise RuntimeError('Cannot stand still.')
    player_head = self._player_heads_by_secret.get(req.player_secret)
    if player_head:
      player_head.direction.MergeFrom(req.move)

  @Pyro4.oneway
  def Action(self, req):
    if self._stage == messages_pb2.GameState.COLLECT_PLAYERS:
      self._StartRound()
    elif self._stage == messages_pb2.GameState.ROUND:
      player_head = self._player_heads_by_secret.get(req.player_secret)
      if player_head:
        if config.INFINITE_AMMO:
          has_rocket = True
        else:
          info = self._player_infos_by_secret[req.player_secret]
          if info.inventory and info.inventory[-1] == _B.ROCKET:
            new_inventory = info.inventory[:-1]
            del info.inventory[:]
            info.inventory.extend(new_inventory)
            has_rocket = True
          else:
            has_rocket = False
        if has_rocket:
          self._AddRocket(
              player_head.pos, player_head.direction, player_head.player_id)

  def _AddRocket(self, origin, direction, player_id):
    rocket_pos = messages_pb2.Coordinate(
        x=origin.x + direction.x, y=origin.y + direction.y)
    self._rockets.append(_B(
        type=_B.ROCKET,
        pos=rocket_pos,
        direction=direction,
        created_tick=self._tick,
        player_id=player_id))
    self._RebuildClientFacingState()

  def _AdvanceBlock(self, block):
    block.pos.x = (block.pos.x + block.direction.x) % self._size.x
    block.pos.y = (block.pos.y + block.direction.y) % self._size.y

  def Update(self):
    t = time.time()
    while t - self._last_update > _SERVER_UPDATE_INTERVAL:
      if self._stage == messages_pb2.GameState.ROUND_START:
        self._pause_ticks += 1
        if self._pause_ticks > _PAUSE_TICKS:
          self._SetStage(messages_pb2.GameState.ROUND)
      elif self._stage == messages_pb2.GameState.ROUND:
        self._Tick()
      if self._stage == messages_pb2.GameState.ROUND_END:
        self._pause_ticks += 1
        if self._pause_ticks > _PAUSE_TICKS:
          self._StartRound()
      self._last_update += _SERVER_UPDATE_INTERVAL
      self._tick += 1

  def _Tick(self):
    if self._tick % _HEAD_MOVE_INTERVAL == 0:
      # Add new tail segments, move heads.
      for head in self._player_heads_by_secret.itervalues():
        tail = _B(
            type=_B.PLAYER_TAIL,
            pos=head.pos,
            created_tick=self._tick,
            player_id=head.player_id)
        self._player_tails.append(tail)
        self._static_blocks_grid[tail.pos.x][tail.pos.y] = tail
      for head in self._player_heads_by_secret.values():
        self._AdvanceBlock(head)

    for rocket in self._rockets:
      self._AdvanceBlock(rocket)

    # Expire tails.
    tail_expiry = _HEAD_MOVE_INTERVAL * (
        _STARTING_TAIL_LENGTH + self._tick / 50)
    rm_indices = []
    for i, tail in enumerate(self._player_tails):
      if self._tick - tail.created_tick >= tail_expiry:
        self._static_blocks_grid[tail.pos.x][tail.pos.y] = None
        rm_indices.append(i)
    for i in reversed(rm_indices):
      del self._player_tails[i]

    # Expire rockets.
    rm_indices = []
    for i, rocket in enumerate(self._rockets):
      if self._tick - rocket.created_tick >= _MAX_ROCKET_AGE:
        rm_indices.append(i)
    for i in reversed(rm_indices):
      del self._rockets[i]

    self._ProcessCollisions()

    if len(filter(
        lambda p: p.alive, self._player_infos_by_secret.values())) <= 1:
      self._SetStage(messages_pb2.GameState.ROUND_END)
      # TODO: Scores.

    self._RebuildClientFacingState()

  def _ProcessCollisions(self):
    destroyed = []
    moving_blocks_grid = _MakeGrid(self._size)
    for b in itertools.chain(
        self._player_heads_by_secret.values(), self._rockets):
      hit = None
      for target_grid in (moving_blocks_grid, self._static_blocks_grid):
        hit = target_grid[b.pos.x][b.pos.y]
        if hit:
          destroyed.append(hit)
          if not self._CheckAddAmmo(b, hit):
            destroyed.append(b)
      if not hit:
        moving_blocks_grid[b.pos.x][b.pos.y] = b
    for b in destroyed:
      if b.type == _B.PLAYER_HEAD:
        self._KillPlayer(b.player_id)
      elif b.type == _B.ROCKET:
        self._rockets.remove(b)
      elif self._static_blocks_grid[b.pos.x][b.pos.y] is b:
        self._static_blocks_grid[b.pos.x][b.pos.y] = None
        if b.type == _B.PLAYER_TAIL and b in self._player_tails:
          # If two players die at once, tails might already be removed.
          self._player_tails.remove(b)
        elif b.type == _B.MINE:
          for i in range(-1, 2):
            for j in range(-1, 2):
              if i == 0 and j == 0:
                continue
              self._AddRocket(
                  b.pos, messages_pb2.Coordinate(x=i, y=j), b.player_id)

  def _CheckAddAmmo(self, head, ammo):
    if not (
        not config.INFINITE_AMMO and
        head.type == _B.PLAYER_HEAD and ammo.type == _B.AMMO):
      return False
    info = None
    for player in self._player_infos_by_secret.values():
      if head.player_id == player.player_id:
        info = player
        break
    if info:
      info.inventory.extend([_B.ROCKET] * _ROCKETS_PER_AMMO)
      return True
    return False

  def _KillPlayer(self, player_id):
    secret = None
    for secret, head in self._player_heads_by_secret.iteritems():
      if head.player_id == player_id:
        break
    if secret:
      del self._player_heads_by_secret[secret]
    # will no longer update, already in statics
    self._player_tails = filter(
        lambda p: p.player_id != player_id, self._player_tails)
    self._player_infos_by_secret[secret].alive = False
    for info in self._player_infos_by_secret.itervalues():
      if info.alive:
        info.score += 1

  def _SetStage(self, stage):
      if self._stage != stage:
        self._stage = stage
        self._pause_ticks = 0
        self._RebuildClientFacingState()

  def _RebuildClientFacingState(self):
    static_blocks = []
    for row in self._static_blocks_grid:
      static_blocks += filter(bool, row)
    self._client_facing_state = messages_pb2.GameState(
        hash=self._state_hash,
        size=self._size,
        player_info=self._player_infos_by_secret.values(),
        block=(
            static_blocks +
            self._rockets +
            self._player_heads_by_secret.values()),
        stage=self._stage)
    self._state_hash += 1

  def GetGameState(self, req):
    return self._client_facing_state if req.hash != self._state_hash else None


def _MakeGrid(size):
  grid = []
  for x in range(size.x):
    grid.append([None] * size.y)
  return grid


def _RandomPosWithin(world_size):
  return messages_pb2.Coordinate(
      x=random.randint(1, world_size.x - 2),
      y=random.randint(1, world_size.y - 2))


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
