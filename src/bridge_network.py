#!/usr/bin/env python3
"""Create project-owned macvlan interfaces without reconfiguring the parent."""
import os
import ipaddress
import socket
import subprocess
import time
from bridge_config import load_config

IP = '/sbin/ip'


def command(args):
    return subprocess.check_output([IP] + args, stderr=subprocess.STDOUT).decode('utf-8')


def bind_device(sock, config, camera):
    if config['network']['mode'] == 'macvlan':
        # Linux SO_BINDTODEVICE; inherited by accepted TCP connections.
        sock.setsockopt(socket.SOL_SOCKET, 25,
                        camera['interface_name'].encode('ascii') + b'\0')


def interface_alias(config, camera):
    return 'onvif-rtsp-bridge:{}:{}'.format(config['bridge']['id'], camera['id'])


def plan(config):
    parent = config['network']['parent_interface']
    for camera in config['cameras']:
        name = camera['interface_name']
        subnet = str(ipaddress.IPv4Interface('{}/{}'.format(camera['ip'], camera['prefix_length'])).network)
        commands = [
            ['link', 'add', 'link', parent, 'name', name, 'address', camera['mac'],
             'type', 'macvlan', 'mode', 'bridge'],
            ['link', 'set', 'dev', name, 'alias', interface_alias(config, camera)],
            ['address', 'add', '{}/{}'.format(camera['ip'], camera['prefix_length']),
             'dev', name, 'noprefixroute'],
            ['link', 'set', 'dev', name, 'up'],
            ['route', 'add', subnet, 'dev', name, 'src', camera['ip'],
             'metric', str(camera['route_metric'])]]
        if config['network']['gateway']:
            commands.append(gateway_command(config, camera))
        yield camera, commands


def gateway_command(config, camera):
    return ['route', 'add', 'default', 'via', config['network']['gateway'],
            'dev', camera['interface_name'], 'src', camera['ip'],
            'metric', str(camera['route_metric'])]


def ensure_gateway(config, camera):
    if not config['network']['gateway']:
        return
    routes = command(['-4', 'route', 'show', 'default', 'dev', camera['interface_name']]).splitlines()
    for route in routes:
        fields = route.split()
        if ('via' in fields and fields[fields.index('via') + 1] == config['network']['gateway']
                and 'metric' in fields and int(fields[fields.index('metric') + 1]) == camera['route_metric']):
            return
    if routes:
        raise RuntimeError('Unexpected child default route; recreate the interface after checking DHCP exclusion')
    command(gateway_command(config, camera))


def owned(config, camera):
    try:
        with open('/sys/class/net/{}/ifalias'.format(camera['interface_name'])) as handle:
            return handle.read().strip() == interface_alias(config, camera)
    except OSError:
        return False


def matches(config, camera):
    name = camera['interface_name']
    parent = config['network']['parent_interface']
    def read(device, field):
        with open('/sys/class/net/{}/{}'.format(device, field)) as handle:
            return handle.read().strip()
    addresses = command(['-o', '-4', 'address', 'show', 'dev', name]).splitlines()
    expected = '{}/{}'.format(camera['ip'], camera['prefix_length'])
    subnet = str(ipaddress.IPv4Interface(expected).network)
    routes = command(['-4', 'route', 'show', 'dev', name]).splitlines()
    route_ok = any(line.split()[0] == subnet
                   and 'metric {}'.format(camera['route_metric']) in line
                   for line in routes if line.strip())
    assigned = [line.split('inet ', 1)[1].split()[0] for line in addresses if 'inet ' in line]
    return (owned(config, camera) and read(name, 'address').lower() == camera['mac']
            and read(name, 'iflink') == read(parent, 'ifindex')
            and assigned == [expected]
            and all('noprefixroute' in line for line in addresses)
            and route_ok
            and 'macvlan mode bridge' in command(['-d', 'link', 'show', 'dev', name]))


def child_sysctls(name):
    # Affect only newly created project interfaces, never the parent or conf/all.
    for setting, value in (('arp_ignore', '1'), ('arp_announce', '2'), ('rp_filter', '2')):
        with open('/proc/sys/net/ipv4/conf/{}/{}'.format(name, setting), 'w') as handle:
            handle.write(value + '\n')


def check_parent_arp(config):
    # Linux owns addresses at host scope. A parent answering ARP for children
    # would defeat their distinct MAC addresses and device-bound TCP sockets.
    values = []
    for name in ('all', config['network']['parent_interface']):
        with open('/proc/sys/net/ipv4/conf/{}/arp_ignore'.format(name)) as handle:
            values.append(int(handle.read().strip()))
    if max(values) not in (1, 2):
        raise RuntimeError('Parent ARP policy is incompatible with same-namespace macvlan. '
                           'Read the README ARP prerequisite; parent settings were not changed.')


def parent_ready(config):
    parent = config['network']['parent_interface']
    try:
        addresses = command(['-o', '-4', 'address', 'show', 'dev', parent]).splitlines()
        networks = {ipaddress.IPv4Interface(line.split('inet ', 1)[1].split()[0]).network
                    for line in addresses if 'inet ' in line}
        routes = command(['-4', 'route', 'show', 'dev', parent]).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return False
    prefixes = {line.split()[0] for line in routes if line.strip()}
    if config['network']['gateway'] and 'default' not in prefixes:
        return False
    for camera in config['cameras']:
        subnet = ipaddress.IPv4Interface('{}/{}'.format(camera['ip'], camera['prefix_length'])).network
        if subnet not in networks or str(subnet) not in prefixes:
            return False
    return True


def wait_for_parent(config, timeout=60):
    if config['network']['mode'] == 'existing':
        return
    deadline = time.monotonic() + timeout
    if parent_ready(config):
        return
    print('Waiting for parent IPv4 address and routes...', flush=True)
    while time.monotonic() < deadline:
        time.sleep(1)
        if parent_ready(config):
            return
    raise RuntimeError('Parent address/routes did not become ready within {} seconds'.format(timeout))


def apply(config, action):
    if config['network']['mode'] == 'existing':
        print('Existing mode: no interfaces or addresses changed.')
        return
    if action == 'plan':
        for camera, commands in plan(config):
            for args in commands:
                print(' '.join([IP] + args))
        return
    if os.geteuid() != 0:
        raise RuntimeError('Network setup requires root')
    command(['link', 'show', 'dev', config['network']['parent_interface']])
    if action == 'down':
        for camera in reversed(config['cameras']):
            if owned(config, camera):
                command(['link', 'delete', 'dev', camera['interface_name']])
        return
    check_parent_arp(config)
    # Preflight all local conflicts before making changes.
    addresses = command(['-o', '-4', 'address', 'show']).splitlines()
    parent_addresses = command(['-o', '-4', 'address', 'show', 'dev',
                                config['network']['parent_interface']]).splitlines()
    parent_networks = [ipaddress.IPv4Interface(line.split('inet ', 1)[1].split()[0]).network
                       for line in parent_addresses if 'inet ' in line]
    parent_routes = command(['-4', 'route', 'show', 'dev', config['network']['parent_interface']]).splitlines()
    if config['network']['gateway']:
        defaults = [line.split() for line in parent_routes if line.startswith('default ')]
        metrics = [int(fields[fields.index('metric') + 1]) if 'metric' in fields else 0 for fields in defaults]
        if not metrics or min(metrics) >= config['network']['route_metric']:
            raise RuntimeError('Child defaults require an existing lower-metric parent default route')
    reused = set()
    for camera in config['cameras']:
        subnet = ipaddress.IPv4Interface('{}/{}'.format(camera['ip'], camera['prefix_length'])).network
        if subnet not in parent_networks:
            raise RuntimeError('Camera IP and prefix must match a parent interface subnet')
        metrics = []
        for route in parent_routes:
            fields = route.split()
            if fields and fields[0] == str(subnet):
                metrics.append(int(fields[fields.index('metric') + 1]) if 'metric' in fields else 0)
        if not metrics or min(metrics) >= camera['route_metric']:
            raise RuntimeError('Camera route metric must be higher than the parent subnet route metric')
        if os.path.exists('/sys/class/net/' + camera['interface_name']):
            if not matches(config, camera):
                raise RuntimeError('Interface conflicts with configuration; stop with original config first')
            reused.add(camera['interface_name'])
            continue
        for line in addresses:
            if 'inet {}/'.format(camera['ip']) in line:
                raise RuntimeError('Camera IP already assigned locally; use existing mode for primary IP')
    created = []
    try:
        for camera, commands in plan(config):
            if camera['interface_name'] in reused:
                child_sysctls(camera['interface_name'])
                command(['link', 'set', 'dev', camera['interface_name'], 'up'])
                ensure_gateway(config, camera)
                continue
            command(commands[0])
            created.append(camera['interface_name'])
            for args in commands[1:]:
                if args[:2] == ['link', 'set'] and args[-1] == 'up':
                    child_sysctls(camera['interface_name'])
                command(args)
    except Exception:
        for name in reversed(created):
            try:
                command(['link', 'delete', 'dev', name])
            except (OSError, subprocess.CalledProcessError):
                print('Could not remove newly created interface:', name, flush=True)
        raise


def main():
    config, action = load_config(network_action=True)
    try:
        if action == 'up':
            wait_for_parent(config)
        apply(config, action)
    except RuntimeError as error:
        raise SystemExit(str(error))
    except subprocess.CalledProcessError as error:
        # Commands contain network parameters only, never recorder credentials.
        print('Failed command:', ' '.join(error.cmd), flush=True)
        print(error.output.decode('utf-8', errors='replace'), flush=True)
        raise SystemExit('Network setup failed; newly created interfaces were scheduled for rollback.')
    except OSError as error:
        raise SystemExit('Network setup failed: {}'.format(error))


if __name__ == '__main__':
    main()
