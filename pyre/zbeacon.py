#   =========================================================================
#   zbeacon - LAN service announcement and discovery
#
#   -------------------------------------------------------------------------
#   Copyright (c) 1991-2013 iMatix Corporation <www.imatix.com>
#   Copyright other contributors as noted in the AUTHORS file.
#
#   This file is part of PyZyre, the ZYRE Python implementation:
#   http://github.com/sphaero/pyzyre & http://czmq.zeromq.org.
#
#   This is free software; you can redistribute it and/or modify it under
#   the terms of the GNU Lesser General Public License as published by the
#   Free Software Foundation; either version 3 of the License, or (at your
#   option) any later version.
#
#   This software is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABIL-
#   ITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General
#   Public License for more details.
#
#   You should have received a copy of the GNU Lesser General Public License
#   along with this program. If not, see <http://www.gnu.org/licenses/>.
#   =========================================================================
from __future__ import unicode_literals

import socket
import zmq
import time
import struct
import ipaddress
import sys
from sys import platform
import logging

import netifaces

# local modules
from . import zhelper

BEACON_MAX = 255      # Max size of beacon data
INTERVAL_DFLT = 1.0   # Default interval = 1 second

logger = logging.getLogger(__name__)


class ZBeacon(object):

    def __init__(self, ctx, port_nbr):
        self._ctx = ctx
        self._port_nbr = port_nbr
        # Start beacon background agent
        self._pipe = zhelper.zthread_fork(
                        self._ctx,
                        ZBeaconAgent,
                        self._port_nbr,
                    )
        # Configure agent with arguments
        # TODO: already done in constructor
        # self._pipe.send_unicode("%d" %port_nbr)
        # Agent replies with our host name
        self._hostname = self._pipe.recv_unicode()

    def __del__(self):
        self._pipe.send_unicode("$TERM")
        # wait for confirmation
        msg = b''

        while msg != b'OK':
            msg = self._pipe.recv()

        logger.debug("Terminating zbeacon")

    # Set broadcast interval in milliseconds (default is 1000 msec)
    def set_interval(self, interval=INTERVAL_DFLT):
        self._pipe.send_unicode("INTERVAL", flags=zmq.SNDMORE)
        self._pipe.send_unicode(interval)

    # Filter out any beacon that looks exactly like ours
    def noecho(self):
        self._pipe.send_unicode("NOECHO")

    # Start broadcasting beacon to peers at the specified interval
    def publish(self, transmit):
        self._pipe.send_unicode("PUBLISH", flags=zmq.SNDMORE)
        self._pipe.send(transmit)

    # Stop broadcasting beacons
    def silence(self):
        self._pipe.send("SILENCE")

    # Start listening to other peers; zero-sized filter means get everything
    def subscribe(self, filter):
        self._pipe.send_unicode("SUBSCRIBE", flags=zmq.SNDMORE)
        if (len(filter) > BEACON_MAX):
            logger.debug("Filter size is too big")

        else:
            self._pipe.send(filter)

    # Stop listening to other peers
    def unsubscribe(self, filter):
        self._pipe.send_unicode("UNSUBSCRIBE")

    # Get beacon ZeroMQ socket, for polling or receiving messages
    def get_socket(self):
        return self._pipe

    # Return our own IP address as printable string
    def get_hostname(self):
        return self._hostname


class ZBeaconAgent(object):

    def __init__(self, ctx, pipe, port, announce_addr="255.255.255.255"):
        # Socket to talk back to application
        self._pipe = pipe
        # UDP socket for send/recv
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # UDP port number we work on
        self._port = port
        # Beacon broadcast interval
        self._interval = INTERVAL_DFLT
        # Are we broadcasting?
        self._enabled = True
        # Ignore own (unique) beacons?
        self._noecho = True
        # API shut us down
        self._terminated = False
        # Next broadcast time
        self._ping_at = 0   # start bcast immediately
        # Beacon transmit data
        # struct.pack('cccb16sIb', b'Z',b'R',b'E', 1, uuid.bytes, self._port_nbr, 1)
        self.transmit = None
        # Beacon filter data
        self._filter = self.transmit  # not used?
        # Our own address
        self.address = None
        # Our network address
        self.network_address = None
        # Our broadcast address
        self.broadcast_address = None
        # Our interface name
        self.interface_name = None

        self.announce_address = ipaddress.ip_address(announce_addr)
        # find a non local ipaddress 
        # TODO: only choose highest available ipaddress

        interfaces = netifaces.interfaces()
        # TODO: sort the interfaces by preference like eth*, wlan* etc. Maybe even optionally let the user give the interface.

        logger.debug("Available interfaces: {0}".format(interfaces))

        for interface_name in interfaces:
            # Loop over the interfaces and their settings to try to find the broadcast address.
            # ipv4 only currently and needs a valid broadcast address
            logger.debug("Checking out interface {0}.".format(interface_name))

            # Grab all the settings for this interface.
            interface_settings = netifaces.ifaddresses(interface_name)

            # Grab all the assigned IPv4 addresses for that interface
            inet_addresses = interface_settings.get(netifaces.AF_INET) 

            if not inet_addresses:
                logger.debug("No addresses found for interface {0}.".format(interface_name))
                continue

            # Loop over all the address sets to try to get one with an address and netmask
            address_str = None
            netmask_str = None

            for address_set in inet_addresses:
                if "addr" in address_set.keys() and "netmask" in address_set.keys():
                    address_str = address_set.get("addr")
                    netmask_str = address_set.get("netmask")
                    break

            if not address_str or not netmask_str:
                logger.debug("Address or netmask not found for interface {0}.".format(interface_name))
                continue

            # Do some type casting to ensure it is all unicode
            if isinstance(address_str, bytes):
                address_str = address_str.decode("utf8")

            if isinstance(netmask_str, bytes):
                netmask_str = netmask_str.decode("utf8")

            # Create an interface object
            interface_string = "{0}/{1}".format(address_str, netmask_str)
            interface = ipaddress.ip_interface(interface_string)

            # Check if it is a loopback device and skip the interface if it is.
            if interface.is_loopback:
                logger.debug("Interface {0} is a loopback device.".format(interface_name))
                continue

            # Otherwise all is good, grab the needed information and break
            self.address = interface.ip
            self.network_address = interface.network.network_address
            self.broadcast_address = interface.network.broadcast_address
            self.interface_name = interface_name

            logger.debug("Address: {0}".format(self.address))
            logger.debug("Network: {0}".format(self.network_address))
            logger.debug("Broadcast: {0}".format(self.broadcast_address))
            logger.debug("Interface name: {0}".format(self.interface_name))
            break

        logger.debug("Finished scanning interfaces.")

        if not self.address:
            logger.error("No suitable interface found.")
            # TODO: error out when there is not a usable interface found. But my guess is to just use the loopback device then.

        self._init_socket()
        self._pipe.send_unicode(str(self.address))
        self.run()

    def _init_socket(self):
        try:
            if self.announce_address.is_multicast:
                # TTL
                self._udp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

                # TODO: This should only be used if we do not have inproc method!
                self._udp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

                # Usually, the system administrator specifies the
                # default interface multicast datagrams should be
                # sent from. The programmer can override this and
                # choose a concrete outgoing interface for a given
                # socket with this option.
                #
                # this results in the loopback address?
                # host = socket.gethostbyname(socket.gethostname())
                # self._udp_sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF, socket.inet_aton(host))
                # You need to tell the kernel which multicast groups
                # you are interested in. If no process is interested
                # in a group, packets destined to it that arrive to
                # the host are discarded.
                # You can always fill this last member with the
                # wildcard address (INADDR_ANY) and then the kernel
                # will deal with the task of choosing the interface.
                #
                # Maximum memberships: /proc/sys/net/ipv4/igmp_max_memberships
                # self._udp_sock.setsockopt(socket.SOL_IP, socket.IP_ADD_MEMBERSHIP,
                #       socket.inet_aton("225.25.25.25") + socket.inet_aton(host))

                group = socket.inet_aton("{0}".format(self.announce_address))
                mreq = struct.pack('4sL', group, socket.INADDR_ANY)

                self._udp_sock.setsockopt(socket.SOL_IP,
                                          socket.IP_ADD_MEMBERSHIP, mreq)

                self._udp_sock.setsockopt(socket.SOL_SOCKET,
                                          socket.SO_REUSEADDR, 1)

                #  On some platforms we have to ask to reuse the port
                try:
                    socket.self._udp_sock.setsockopt(socket.SOL_SOCKET,
                                                     socket.SO_REUSEPORT, 1)

                except AttributeError:
                    pass

                self._udp_sock.bind((str(self.address), self._port))

            else:
                # Only for broadcast
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

                #  On some platforms we have to ask to reuse the port
                try:
                    self._udp_sock.setsockopt(socket.SOL_SOCKET,
                                              socket.SO_REUSEPORT, 1)

                except AttributeError:
                    pass

                # Platform specifics
                if platform.startswith("win"):
                    self.announce_address = self.broadcast_address
                    self._udp_sock.bind(("", self._port))

                # Not sure if freebsd should be included
                elif platform.startswith("darwin") or platform.startswith("freebsd"):
                    self.announce_address = self.broadcast_address
                    self._udp_sock.bind(("", self._port))

                else:
                    # on linux we bind to the broadcast address and send to
                    # the broadcast address
                    self.announce_address = self.broadcast_address
                    self._udp_sock.bind((str(self.broadcast_address), self._port))

                logger.debug("Set up a broadcast beacon to {0}:{1}".format(self.announce_address, self._port))


        except socket.error:
            logger.exception("Initializing of {0} raised an exception".format(self.__class__.__name__))

    def __del__(self):
        self._udp_sock.close()

    def get_interface(self):
        # Get the actual network interface we're working on
        # Currently implemented for POSIX and for Windows
        # This is required for getting broadcastaddresses...
        # Subnet broadcast addresses don't work on some platforms but is
        # assumed to work if the interface is specified.
        # TODO
        pass

    def api_command(self):
        cmds = self._pipe.recv_multipart()

        #logger.debug("ZBeaconApiCommand: {0}".format(cmds))

        cmd = cmds.pop(0)
        cmd = cmd.decode('UTF-8')
        if cmd == "INTERVAL":
            self._interval = int(cmds.pop(0))

        elif cmd == "NOECHO":
            self._noecho = True

        elif cmd == "PUBLISH":
            self.transmit = cmds.pop(0)
            # start broadcasting immediately
            self._ping_at = time.time()

        elif cmd == "SILENCE":
            self.transmit = None

        elif cmd == "SUBSCRIBE":
            self._filter = cmds.pop(0)

        elif cmd == "UNSUBSCRIBE":
            self.filter = None

        elif cmd == "$TERM":
            self._terminated = True
            self._pipe.send_unicode("OK")

        else:
            logger.debug("Unexpected API command {0}, {1}".format(cmd, cmds))

    def send(self):
        try:
            self._udp_sock.sendto(self.transmit, (str(self.announce_address), self._port))

        except OSError:
            logger.debug("Network seems gone, reinitialising the socket")

            self._init_socket()
            # if failed after reinit an exception will be raised
            self._udp_sock.sendto(self.transmit, (str(self.announce_address), self._port))

    def recv(self):
        try:
            data, addr = self._udp_sock.recvfrom(BEACON_MAX)

        except socket.error:
            logger.exception("Exception while receiving")

        # Get sender address as printable string
        peername = addr[0]
        # If filter is set, check that beacon matches it
        if self._filter:
            if len(self._filter) < len(data):
                match_data = data[:len(self._filter)]

                if (match_data != self._filter):
                    logger.debug("Received beacon doesn't match filter, discarding")
                    return

        # If noEcho is set, check if beacon is our own and ignore it.
        if self._noecho:
            if self.transmit == data:
                return

        # send the data onto the pipe
        self._pipe.send_unicode(peername, zmq.SNDMORE)
        self._pipe.send(data)

    def run(self):
        logger.debug("ZBeacon runnning")

        self.poller = zmq.Poller()
        self.poller.register(self._pipe, zmq.POLLIN)
        self.poller.register(self._udp_sock, zmq.POLLIN)

        # not interrupted
        while(True):
            timeout = -1
            if self.transmit:
                timeout = self._ping_at - time.time()
                if timeout < 0:
                    timeout = 0

            items = dict(self.poller.poll(timeout * 1000))

            if self._pipe in items and items[self._pipe] == zmq.POLLIN:
                self.api_command()

            if self._udp_sock.fileno() in items and items[self._udp_sock.fileno()] == zmq.POLLIN:
                self.recv()

            if self.transmit and time.time() >= self._ping_at:
                self.send()
                self._ping_at = time.time() + self._interval

            if self._terminated:
                break

        logger.debug("ZBeaconAgent terminated")


if __name__ == '__main__':
    ctx = zmq.Context()
    beacon = ZBeacon(ctx, 1200)
    import uuid
    transmit = struct.pack('cccb16sH', b'Z', b'R', b'E',
                           1, uuid.uuid4().bytes,
                           socket.htons(1300))

    beacon.publish(transmit)
    beacon_pipe = beacon.get_socket()

    # Create a StreamHandler for debugging
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.DEBUG)

    while True:
        try:
            msg = beacon_pipe.recv()
            logger.debug("BEACONMSG: %s".format(msg))

        except (KeyboardInterrupt, SystemExit):
            break

    del(beacon)

    logger.debug("FINISHED")
