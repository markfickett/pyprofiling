#!/usr/bin/env python
"""A Nuke Snake server, to host network-enabled game play."""

import argparse
import select
import socket

import Pyro4

import common
import controller


_SOCKET_READ_TIMEOUT = min(controller.UPDATE_INTERVAL/2, 0)  # 0 = non-blocking


def _RunNetworkServer(game_controller, localhost_only):
  common.RegisterProtoSerialization()

  hostname = 'localhost' if localhost_only else socket.gethostname()
  ip_addr = (
      '127.0.0.1' if localhost_only
      else Pyro4.socketutil.getIpAddress(None, workaround127=True))
  ns_uri, ns_daemon, broadcast_server = Pyro4.naming.startNS(host=ip_addr)
  if not localhost_only:
    assert broadcast_server
  pyro_daemon = Pyro4.core.Daemon(host=hostname)
  game_controller_uri = pyro_daemon.register(game_controller)
  ns_daemon.nameserver.register(common.SERVER_URI_NAME, game_controller_uri)
  print 'registered', common.SERVER_URI_NAME, game_controller_uri

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
      game_controller.Update()
  except KeyboardInterrupt:
    print 'Server shutting down.'
  finally:
    if broadcast_server:
      broadcast_server.close()
    ns_daemon.close()
    pyro_daemon.close()


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      '-l', '--localhost-only', action='store_true', dest='localhost_only',
      help=(
          'Bind the listening socket to localhost. Useful when there is no '
          'WAN connection available.'))
  controller.AddControllerArgs(parser)
  args = parser.parse_args()

  game_controller = controller.Controller(args.width, args.height)

  _RunNetworkServer(game_controller, args.localhost_only)
