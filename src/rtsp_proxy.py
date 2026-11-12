#!/usr/bin/env python3

import os
import threading
import select
import socket
import socketserver

from bridge_config import load_config
from bridge_network import bind_device

BUFFER_SIZE = 65536


class RTSPHandler(socketserver.BaseRequestHandler):
    def log_event(self, event):
        print('RTSP {} ({}): {}'.format(self.server.camera_id,
              self.server.server_address[0], event), flush=True)

    def handle(self):
        client = self.request

        self.log_event('client connected from {}'.format(self.client_address[0]))

        try:
            upstream = socket.create_connection(
                self.server.upstream_address,
                timeout=10
            )
        except OSError as error:
            self.log_event('upstream connection failed: {}'.format(type(error).__name__))
            return

        try:
            sockets = [client, upstream]
            operation = 'waiting for data'

            while True:
                readable, _, _ = select.select(
                    sockets, [], [], 30
                )

                for source in readable:
                    operation = 'reading client' if source is client else 'reading upstream'
                    data = source.recv(BUFFER_SIZE)

                    if not data:
                        self.log_event('client closed' if source is client else 'upstream closed')
                        return

                    destination = (
                        upstream if source is client else client
                    )

                    operation = 'writing upstream' if source is client else 'writing client'
                    destination.sendall(data)

        except OSError as error:
            self.log_event('{} failed: {}'.format(operation, type(error).__name__))

        finally:
            upstream.close()
            self.log_event('connection ended')


class RTSPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(config, camera):
    server = RTSPServer((camera['ip'], config['bridge']['rtsp_port']),
                        RTSPHandler, bind_and_activate=False)
    server.upstream_address = (config['upstream']['host'], config['upstream']['rtsp_port'])
    server.camera_id = camera['id']
    try:
        bind_device(server.socket, config, camera)
        server.server_bind()
        server.server_activate()
    except Exception:
        server.server_close()
        raise
    return server


def serve(server):
    try:
        server.serve_forever()
    except Exception:
        print('RTSP listener failed.', flush=True)
        os._exit(1)


def main():
    config = load_config()
    servers = []
    try:
        for camera in config['cameras']:
            servers.append(create_server(config, camera))
        for server in servers:
            thread = threading.Thread(target=serve, args=(server,))
            thread.daemon = True
            thread.start()
        print('RTSP ready: {} address(es).'.format(len(servers)), flush=True)
        while True:
            threading.Event().wait(60)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.server_close()


if __name__ == '__main__':
    main()
