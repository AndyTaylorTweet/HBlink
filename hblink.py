#!/usr/bin/env python
#
###############################################################################
#   Copyright (C) 2016-2018 Cortney T. Buffington, N0MJS <n0mjs@me.com>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
###############################################################################

'''
This program does very little on it's own. It is intended to be used as a module
to build applications on top of the HomeBrew Repeater Protocol. By itself, it
will only act as a peer or master for the systems specified in its configuration
file (usually hblink.cfg). It is ALWAYS best practice to ensure that this program
works stand-alone before troubleshooting any applications that use it. It has
sufficient logging to be used standalone as a troubleshooting application.
'''

from __future__ import print_function

# Specifig functions from modules we need
from binascii import b2a_hex as ahex
from binascii import a2b_hex as bhex
from random import randint
from hashlib import sha256, sha1
from hmac import new as hmac_new, compare_digest
from time import time
from bitstring import BitArray
from importlib import import_module
import socket

# Twisted is pretty important, so I keep it separate
from twisted.internet.protocol import DatagramProtocol, Factory, Protocol
from twisted.protocols.basic import NetstringReceiver
from twisted.internet import reactor, task

# Other files we pull from -- this is mostly for readability and segmentation
import hb_log
import hb_config
from dmr_utils.utils import int_id, hex_str_4

# Imports for the reporting server
import cPickle as pickle
from reporting_const import *

# Does anybody read this stuff? There's a PEP somewhere that says I should do this.
__author__     = 'Cortney T. Buffington, N0MJS'
__copyright__  = 'Copyright (c) 2016-2018 Cortney T. Buffington, N0MJS and the K0USY Group'
__credits__    = 'Colin Durbridge, G4EML, Steve Zingman, N4IRS; Mike Zingman, N4IRR; Jonathan Naylor, G4KLX; Hans Barthen, DL5DI; Torsten Shultze, DG1HT'
__license__    = 'GNU GPLv3'
__maintainer__ = 'Cort Buffington, N0MJS'
__email__      = 'n0mjs@me.com'


# Global variables used whether we are a module or __main__
systems = {}

# Timed loop used for reporting HBP status
#
# REPORT BASED ON THE TYPE SELECTED IN THE MAIN CONFIG FILE
def config_reports(_config, _logger, _factory):                 
    if True: #_config['REPORTS']['REPORT']:
        def reporting_loop(_logger, _server):
            _logger.debug('Periodic reporting loop started')
            _server.send_config()
            
        _logger.info('HBlink TCP reporting server configured')
        
        report_server = _factory(_config, _logger)
        report_server.clients = []
        reactor.listenTCP(_config['REPORTS']['REPORT_PORT'], report_server)
        
        reporting = task.LoopingCall(reporting_loop, _logger, report_server)
        reporting.start(_config['REPORTS']['REPORT_INTERVAL'])
    
    return report_server


# Shut ourselves down gracefully by disconnecting from the masters and peers.
def hblink_handler(_signal, _frame, _logger):
    for system in systems:
        _logger.info('SHUTDOWN: DE-REGISTER SYSTEM: %s', system)
        systems[system].dereg()


# Import subscriber registration ACL
# REG_ACL may be a single list of subscriber IDs
# Global action is to allow or deny them. Multiple lists with different actions and ranges
# are not yet implemented.
def build_reg_acl(_reg_acl, _logger):
    REG_ACL = set()
    try:
        acl_file = import_module(_reg_acl)
        _logger.info('Registration ACL file found, importing entries. This will take about 1.5 seconds per 1 million IDs')
        sections = acl_file.REG_ACL.split(':')
        REG_ACL_ACTION = sections[0]
        entries_str = sections[1]
        
        for entry in entries_str.split(','):
            if '-' in entry:
                start,end = entry.split('-')
                start,end = int(start), int(end)
                for id in range(start, end+1):
                    REG_ACL.add(hex_str_4(id))
            else:
                id = int(entry)
                REG_ACL.add(hex_str_4(id))
        
        _logger.info('Registration ACL loaded: action "{}" for {:,} registration IDs'.format( REG_ACL_ACTION, len(REG_ACL)))
    
    except ImportError:
        _logger.info('Registration ACL file not found or invalid - all IDs may register with this system')
        REG_ACL_ACTION = 'NONE'

    # Depending on which type of REG_ACL is used (PERMIT, DENY... or there isn't one)
    # define a differnet function to be used to check the ACL
    global allow_reg
    if REG_ACL_ACTION == 'PERMIT':
        def allow_reg(_id):
            if _id in REG_ACL:
                return True
            else:
                return False
    elif REG_ACL_ACTION == 'DENY':
        def allow_reg(_id):
            if _id not in REG_ACL:
                return True
            else:
                return False
    else:
        def allow_reg(_id):
            return True
    
    return REG_ACL

#************************************************
#     AMBE CLASS: Used to parse out AMBE and send to gateway
#************************************************

class AMBE:
    def __init__(self, _config, _logger):
        self._CONFIG = _config
        self._logger = _logger
         
        self._sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        self._exp_ip = self._CONFIG['AMBE']['EXPORT_IP']
        self._exp_port = self._CONFIG['AMBE']['EXPORT_PORT']

    def parseAMBE(self, _peer, _data):
        _seq = int_id(_data[4:5])
        _srcID = int_id(_data[5:8])
        _dstID = int_id(_data[8:11])
        _rptID = int_id(_data[11:15])
        _bits = int_id(_data[15:16])       # SCDV NNNN (Slot|Call type|Data|Voice|Seq or Data type)
        _slot = 2 if _bits & 0x80 else 1
        _callType = 1 if (_bits & 0x40) else 0
        _frameType = (_bits & 0x30) >> 4
        _voiceSeq = (_bits & 0x0f)
        _streamID = int_id(_data[16:20])
        self._logger.debug('(%s) seq: %d srcID: %d dstID: %d rptID: %d bits: %0X slot:%d callType: %d frameType:  %d voiceSeq: %d streamID: %0X',
        _peer, _seq, _srcID, _dstID, _rptID, _bits, _slot, _callType, _frameType, _voiceSeq, _streamID )

        #logger.debug('Frame 1:(%s)', self.ByteToHex(_data))
        _dmr_frame = BitArray('0x'+ahex(_data[20:]))
        _ambe = _dmr_frame[0:108] + _dmr_frame[156:264]
        #_sock.sendto(_ambe.tobytes(), ("127.0.0.1", 31000))

        ambeBytes = _ambe.tobytes()
        self._sock.sendto(ambeBytes[0:9], (self._exp_ip, self._exp_port))
        self._sock.sendto(ambeBytes[9:18], (self._exp_ip, self._exp_port))
        self._sock.sendto(ambeBytes[18:27], (self._exp_ip, self._exp_port))


#************************************************
#    OPENBRIDGE CLASS
#************************************************

class OPENBRIDGE(DatagramProtocol):
    def __init__(self, _name, _config, _logger, _report):
        # Define a few shortcuts to make the rest of the class more readable
        self._CONFIG = _config
        self._system = _name
        self._logger = _logger
        self._report = _report
        self._config = self._CONFIG['SYSTEMS'][self._system]
        self._localsock = (self._config['IP'], self._config['PORT'])
        self._targetsock = (self._config['TARGET_IP'], self._config['TARGET_PORT'])
	print(self._config['NETWORK_ID'])

    def dereg(self):
        self._logger.info('(%s) is mode OPENBRIDGE. No De-Registration required, continuing shutdown', self._system)
    
    def send_system(self, _packet):
        if _packet[:4] == 'DMRD':
            self.transport.write(_packet, (self._config['TARGET_IP'], self._config['TARGET_PORT']))

    def dmrd_received(self, _peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data):
        pass
        #print(int_id(_peer_id), int_id(_rf_src), int_id(_dst_id), int_id(_seq), _slot, _call_type, _frame_type, repr(_dtype_vseq), int_id(_stream_id))
        
        _dmrd = _data[:53]
        _hash = _data[53:]

        _ckhs = hmac_new(self._config['PASSPHRASE'],_dmrd,sha1).digest()
        if compare_digest(_hash, _ckhs):
            print('PEER:', int_id(_peer_id), 'RF SOURCE:', int_id(_rf_src), 'DESTINATION:', int_id(_dst_id), 'SLOT', _slot, 'SEQ:', int_id(_seq), 'STREAM:', int_id(_stream_id))
        else:
            self._logger.info('(%s) OpenBridge HMAC failed, packet discarded', self._system)

    # Aliased in __init__ to datagramReceived if system is a master
    def datagramReceived(self, _data, _sockaddr):
        # Keep This Line Commented Unless HEAVILY Debugging!
        # self._logger.debug('(%s) RX packet from %s -- %s', self._system, _sockaddr, ahex(_data))
        
        # Extract the command, which is various length, all but one 4 significant characters -- RPTCL
        _command = _data[:4]

        if _command == 'DMRD':    # DMRData -- encapsulated DMR data frame
            _peer_id = _data[11:15]
            if _sockaddr == self._targetsock:
                _seq = _data[4]
                _rf_src = _data[5:8]
                _dst_id = _data[8:11]
                _bits = int_id(_data[15])
                _slot = 2 if (_bits & 0x80) else 1
                _call_type = 'unit' if (_bits & 0x40) else 'group'
                _frame_type = (_bits & 0x30) >> 4
                _dtype_vseq = (_bits & 0xF) # data, 1=voice header, 2=voice terminator; voice, 0=burst A ... 5=burst F
                _stream_id = _data[16:20]
                #self._logger.debug('(%s) DMRD - Seqence: %s, RF Source: %s, Destination ID: %s', self._system, int_id(_seq), int_id(_rf_src), int_id(_dst_id))

                # Userland actions -- typically this is the function you subclass for an application
                self.dmrd_received(_peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data)


#************************************************
#     HB MASTER CLASS
#************************************************

class HBSYSTEM(DatagramProtocol):
    def __init__(self, _name, _config, _logger, _report):
        # Define a few shortcuts to make the rest of the class more readable
        self._CONFIG = _config
        self._system = _name
        self._logger = _logger
        self._report = _report
        self._config = self._CONFIG['SYSTEMS'][self._system]
        
        # Define shortcuts and generic function names based on the type of system we are
        if self._config['MODE'] == 'MASTER':
            self._peers = self._CONFIG['SYSTEMS'][self._system]['PEERS']
            self.send_system = self.send_peers
            self.maintenance_loop = self.master_maintenance_loop
            self.datagramReceived = self.master_datagramReceived
            self.dereg = self.master_dereg
        
        elif self._config['MODE'] == 'PEER':
            self._stats = self._config['STATS']
            self.send_system = self.send_master
            self.maintenance_loop = self.peer_maintenance_loop
            self.datagramReceived = self.peer_datagramReceived
            self.dereg = self.peer_dereg
        
        # Configure for AMBE audio export if enabled
        if self._config['EXPORT_AMBE']:
            self._ambe = AMBE(_config, _logger)

    def startProtocol(self):
        # Set up periodic loop for tracking pings from peers. Run every 'PING_TIME' seconds
        self._system_maintenance = task.LoopingCall(self.maintenance_loop)
        self._system_maintenance_loop = self._system_maintenance.start(self._CONFIG['GLOBAL']['PING_TIME'])
    
    # Aliased in __init__ to maintenance_loop if system is a master
    def master_maintenance_loop(self):
        self._logger.debug('(%s) Master maintenance loop started', self._system)
        for peer in self._peers:
            _this_peer = self._peers[peer]
            # Check to see if any of the peers have been quiet (no ping) longer than allowed
            if _this_peer['LAST_PING']+self._CONFIG['GLOBAL']['PING_TIME']*self._CONFIG['GLOBAL']['MAX_MISSED'] < time():
                self._logger.info('(%s) Peer %s (%s) has timed out', self._system, _this_peer['CALLSIGN'], _this_peer['RADIO_ID'])
                # Remove any timed out peers from the configuration
                del self._CONFIG['SYSTEMS'][self._system]['PEERS'][peer]
    
    # Aliased in __init__ to maintenance_loop if system is a peer           
    def peer_maintenance_loop(self):
        self._logger.debug('(%s) Peer maintenance loop started', self._system)
        if self._stats['PING_OUTSTANDING']:
            self._stats['NUM_OUTSTANDING'] += 1
        # If we're not connected, zero out the stats and send a login request RPTL
        if self._stats['CONNECTION'] != 'YES' or self._stats['NUM_OUTSTANDING'] >= self._CONFIG['GLOBAL']['MAX_MISSED']:
            self._stats['PINGS_SENT'] = 0
            self._stats['PINGS_ACKD'] = 0
            self._stats['NUM_OUTSTANDING'] = 0
            self._stats['PING_OUTSTANDING'] = False
            self._stats['CONNECTION'] = 'RPTL_SENT'
            self.send_master('RPTL'+self._config['RADIO_ID'])
            self._logger.info('(%s) Sending login request to master %s:%s', self._system, self._config['MASTER_IP'], self._config['MASTER_PORT'])
        # If we are connected, sent a ping to the master and increment the counter
        if self._stats['CONNECTION'] == 'YES':
            self.send_master('RPTPING'+self._config['RADIO_ID'])
            self._logger.debug('(%s) RPTPING Sent to Master. Total Sent: %s, Total Missed: %s, Currently Outstanding: %s', self._system, self._stats['PINGS_SENT'], self._stats['PINGS_SENT'] - self._stats['PINGS_ACKD'], self._stats['NUM_OUTSTANDING'])
            self._stats['PINGS_SENT'] += 1
            self._stats['PING_OUTSTANDING'] = True

    def send_peers(self, _packet):
        for _peer in self._peers:
            self.send_peer(_peer, _packet)
            #self._logger.debug('(%s) Packet sent to peer %s', self._system, self._peers[_peer]['RADIO_ID'])

    def send_peer(self, _peer, _packet):
        if _packet[:4] == 'DMRD':
            _packet = _packet[:11] + _peer + _packet[15:]
        self.transport.write(_packet, self._peers[_peer]['SOCKADDR'])
        # KEEP THE FOLLOWING COMMENTED OUT UNLESS YOU'RE DEBUGGING DEEPLY!!!!
        #self._logger.debug('(%s) TX Packet to %s on port %s: %s', self._peers[_peer]['RADIO_ID'], self._peers[_peer]['IP'], self._peers[_peer]['PORT'], ahex(_packet))

    def send_master(self, _packet):
        if _packet[:4] == 'DMRD':
            _packet = _packet[:11] + self._config['RADIO_ID'] + _packet[15:]
        self.transport.write(_packet, self._config['MASTER_SOCKADDR'])
        # KEEP THE FOLLOWING COMMENTED OUT UNLESS YOU'RE DEBUGGING DEEPLY!!!!
        # self._logger.debug('(%s) TX Packet to %s:%s -- %s', self._system, self._config['MASTER_IP'], self._config['MASTER_PORT'], ahex(_packet))

    def dmrd_received(self, _peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data):
        pass
    
    def master_dereg(self):
        for _peer in self._peers:
            self.send_peer(_peer, 'MSTCL'+_peer)
            self._logger.info('(%s) De-Registration sent to Peer: %s (%s)', self._system, self._peers[_peer]['CALLSIGN'], self._peers[_peer]['RADIO_ID'])
            
    def peer_dereg(self):
        self.send_master('RPTCL'+self._config['RADIO_ID'])
        self._logger.info('(%s) De-Registration sent to Master: %s:%s', self._system, self._config['MASTER_SOCKADDR'][0], self._config['MASTER_SOCKADDR'][1])
    
    # Aliased in __init__ to datagramReceived if system is a master
    def master_datagramReceived(self, _data, _sockaddr):
        # Keep This Line Commented Unless HEAVILY Debugging!
        # self._logger.debug('(%s) RX packet from %s -- %s', self._system, _sockaddr, ahex(_data))

        # Extract the command, which is various length, all but one 4 significant characters -- RPTCL
        _command = _data[:4]

        if _command == 'DMRD':    # DMRData -- encapsulated DMR data frame
            _peer_id = _data[11:15]
            if _peer_id in self._peers \
                        and self._peers[_peer_id]['CONNECTION'] == 'YES' \
                        and self._peers[_peer_id]['SOCKADDR'] == _sockaddr:
                _seq = _data[4]
                _rf_src = _data[5:8]
                _dst_id = _data[8:11]
                _bits = int_id(_data[15])
                _slot = 2 if (_bits & 0x80) else 1
                _call_type = 'unit' if (_bits & 0x40) else 'group'
                _frame_type = (_bits & 0x30) >> 4
                _dtype_vseq = (_bits & 0xF) # data, 1=voice header, 2=voice terminator; voice, 0=burst A ... 5=burst F
                _stream_id = _data[16:20]
                #self._logger.debug('(%s) DMRD - Seqence: %s, RF Source: %s, Destination ID: %s', self._system, int_id(_seq), int_id(_rf_src), int_id(_dst_id))

                # If AMBE audio exporting is configured...
                if self._config['EXPORT_AMBE']:
                    self._ambe.parseAMBE(self._system, _data)

                # The basic purpose of a master is to repeat to the peers
                if self._config['REPEAT'] == True:
                    for _peer in self._peers:
                        if _peer != _peer_id:
                            #self.send_peer(_peer, _data)
                            self.send_peer(_peer, _data[:11] + _peer + _data[15:])
                            #self.send_peer(_peer, _data[:11] + self._config['RADIO_ID'] + _data[15:])
                            #self._logger.debug('(%s) Packet on TS%s from %s (%s) for destination ID %s repeated to peer: %s (%s) [Stream ID: %s]', self._system, _slot, self._peers[_peer_id]['CALLSIGN'], int_id(_peer_id), int_id(_dst_id), self._peers[_peer]['CALLSIGN'], int_id(_peer), int_id(_stream_id))

                # Userland actions -- typically this is the function you subclass for an application
                self.dmrd_received(_peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data)

        elif _command == 'RPTL':    # RPTLogin -- a repeater wants to login
            _peer_id = _data[4:8]
            if allow_reg(_peer_id):                    # Check for valid Radio ID
                self._peers.update({_peer_id: {      # Build the configuration data strcuture for the peer
                    'CONNECTION': 'RPTL-RECEIVED',
                    'PINGS_RECEIVED': 0,
                    'LAST_PING': time(),
                    'SOCKADDR': _sockaddr,
                    'IP': _sockaddr[0],
                    'PORT': _sockaddr[1],
                    'SALT': randint(0,0xFFFFFFFF),
                    'RADIO_ID': str(int(ahex(_peer_id), 16)),
                    'CALLSIGN': '',
                    'RX_FREQ': '',
                    'TX_FREQ': '',
                    'TX_POWER': '',
                    'COLORCODE': '',
                    'LATITUDE': '',
                    'LONGITUDE': '',
                    'HEIGHT': '',
                    'LOCATION': '',
                    'DESCRIPTION': '',
                    'SLOTS': '',
                    'URL': '',
                    'SOFTWARE_ID': '',
                    'PACKAGE_ID': '',
                }})
                self._logger.info('(%s) Repeater Logging in with Radio ID: %s, %s:%s', self._system, int_id(_peer_id), _sockaddr[0], _sockaddr[1])
                _salt_str = hex_str_4(self._peers[_peer_id]['SALT'])
                self.send_peer(_peer_id, 'RPTACK'+_salt_str)
                self._peers[_peer_id]['CONNECTION'] = 'CHALLENGE_SENT'
                self._logger.info('(%s) Sent Challenge Response to %s for login: %s', self._system, int_id(_peer_id), self._peers[_peer_id]['SALT'])
            else:
                self.transport.write('MSTNAK'+_peer_id, _sockaddr)
                self._logger.warning('(%s) Invalid Login from Radio ID: %s Denied by Registation ACL', self._system, int_id(_peer_id))

        elif _command == 'RPTK':    # Repeater has answered our login challenge
            _peer_id = _data[4:8]
            if _peer_id in self._peers \
                        and self._peers[_peer_id]['CONNECTION'] == 'CHALLENGE_SENT' \
                        and self._peers[_peer_id]['SOCKADDR'] == _sockaddr:
                _this_peer = self._peers[_peer_id]
                _this_peer['LAST_PING'] = time()
                _sent_hash = _data[8:]
                _salt_str = hex_str_4(_this_peer['SALT'])
                _calc_hash = bhex(sha256(_salt_str+self._config['PASSPHRASE']).hexdigest())
                if _sent_hash == _calc_hash:
                    _this_peer['CONNECTION'] = 'WAITING_CONFIG'
                    self.send_peer(_peer_id, 'RPTACK'+_peer_id)
                    self._logger.info('(%s) Peer %s has completed the login exchange successfully', self._system, _this_peer['RADIO_ID'])
                else:
                    self._logger.info('(%s) Peer %s has FAILED the login exchange successfully', self._system, _this_peer['RADIO_ID'])
                    self.transport.write('MSTNAK'+_peer_id, _sockaddr)
                    del self._peers[_peer_id]
            else:
                self.transport.write('MSTNAK'+_peer_id, _sockaddr)
                self._logger.warning('(%s) Login challenge from Radio ID that has not logged in: %s', self._system, int_id(_peer_id))

        elif _command == 'RPTC':    # Repeater is sending it's configuraiton OR disconnecting
            if _data[:5] == 'RPTCL':    # Disconnect command
                _peer_id = _data[5:9]
                if _peer_id in self._peers \
                            and self._peers[_peer_id]['CONNECTION'] == 'YES' \
                            and self._peers[_peer_id]['SOCKADDR'] == _sockaddr:
                    self._logger.info('(%s) Peer is closing down: %s (%s)', self._system, self._peers[_peer_id]['CALLSIGN'], int_id(_peer_id))
                    self.transport.write('MSTNAK'+_peer_id, _sockaddr)
                    del self._peers[_peer_id]

            else:
                _peer_id = _data[4:8]      # Configure Command
                if _peer_id in self._peers \
                            and self._peers[_peer_id]['CONNECTION'] == 'WAITING_CONFIG' \
                            and self._peers[_peer_id]['SOCKADDR'] == _sockaddr:
                    _this_peer = self._peers[_peer_id]
                    _this_peer['CONNECTION'] = 'YES'
                    _this_peer['LAST_PING'] = time()
                    _this_peer['CALLSIGN'] = _data[8:16]
                    _this_peer['RX_FREQ'] = _data[16:25]
                    _this_peer['TX_FREQ'] =  _data[25:34]
                    _this_peer['TX_POWER'] = _data[34:36]
                    _this_peer['COLORCODE'] = _data[36:38]
                    _this_peer['LATITUDE'] = _data[38:46]
                    _this_peer['LONGITUDE'] = _data[46:55]
                    _this_peer['HEIGHT'] = _data[55:58]
                    _this_peer['LOCATION'] = _data[58:78]
                    _this_peer['DESCRIPTION'] = _data[78:97]
                    _this_peer['SLOTS'] = _data[97:98]
                    _this_peer['URL'] = _data[98:222]
                    _this_peer['SOFTWARE_ID'] = _data[222:262]
                    _this_peer['PACKAGE_ID'] = _data[262:302]

                    self.send_peer(_peer_id, 'RPTACK'+_peer_id)
                    self._logger.info('(%s) Peer %s (%s) has sent repeater configuration', self._system, _this_peer['CALLSIGN'], _this_peer['RADIO_ID'])
                else:
                    self.transport.write('MSTNAK'+_peer_id, _sockaddr)
                    self._logger.warning('(%s) Peer info from Radio ID that has not logged in: %s', self._system, int_id(_peer_id))

        elif _command == 'RPTP':    # RPTPing -- peer is pinging us
                _peer_id = _data[7:11]
                if _peer_id in self._peers \
                            and self._peers[_peer_id]['CONNECTION'] == "YES" \
                            and self._peers[_peer_id]['SOCKADDR'] == _sockaddr:
                    self._peers[_peer_id]['PINGS_RECEIVED'] += 1
                    self._peers[_peer_id]['LAST_PING'] = time()
                    self.send_peer(_peer_id, 'MSTPONG'+_peer_id)
                    self._logger.debug('(%s) Received and answered RPTPING from peer %s (%s)', self._system, self._peers[_peer_id]['CALLSIGN'], int_id(_peer_id))
                else:
                    self.transport.write('MSTNAK'+_peer_id, _sockaddr)
                    self._logger.warning('(%s) Peer info from Radio ID that has not logged in: %s', self._system, int_id(_peer_id))

        else:
            self._logger.error('(%s) Unrecognized command. Raw HBP PDU: %s', self._system, ahex(_data))

    # Aliased in __init__ to datagramReceived if system is a peer
    def peer_datagramReceived(self, _data, _sockaddr):
        # Keep This Line Commented Unless HEAVILY Debugging!
        # self._logger.debug('(%s) RX packet from %s -- %s', self._system, _sockaddr, ahex(_data))

        # Validate that we receveived this packet from the master - security check!
        if self._config['MASTER_SOCKADDR'] == _sockaddr:
            # Extract the command, which is various length, but only 4 significant characters
            _command = _data[:4]
            if   _command == 'DMRD':    # DMRData -- encapsulated DMR data frame
                _peer_id = _data[11:15]
                if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                    _seq = _data[4:5]
                    _rf_src = _data[5:8]
                    _dst_id = _data[8:11]
                    _bits = int_id(_data[15])
                    _slot = 2 if (_bits & 0x80) else 1
                    _call_type = 'unit' if (_bits & 0x40) else 'group'
                    _frame_type = (_bits & 0x30) >> 4
                    _dtype_vseq = (_bits & 0xF) # data, 1=voice header, 2=voice terminator; voice, 0=burst A ... 5=burst F
                    _stream_id = _data[16:20]
                    self._logger.debug('(%s) DMRD - Sequence: %s, RF Source: %s, Destination ID: %s', self._system, int_id(_seq), int_id(_rf_src), int_id(_dst_id))

                    # If AMBE audio exporting is configured...
                    if self._config['EXPORT_AMBE']:
                        self._ambe.parseAMBE(self._system, _data)

                    # Userland actions -- typically this is the function you subclass for an application
                    self.dmrd_received(_peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data)

            elif _command == 'MSTN':    # Actually MSTNAK -- a NACK from the master
                _peer_id = _data[6:10]
                if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                    self._logger.warning('(%s) MSTNAK Received. Resetting connection to the Master.', self._system)
                    self._stats['CONNECTION'] = 'NO' # Disconnect ourselves and re-register

            elif _command == 'RPTA':    # Actually RPTACK -- an ACK from the master
                # Depending on the state, an RPTACK means different things, in each clause, we check and/or set the state
                if self._stats['CONNECTION'] == 'RPTL_SENT': # If we've sent a login request...
                    _login_int32 = _data[6:10]
                    self._logger.info('(%s) Repeater Login ACK Received with 32bit ID: %s', self._system, int_id(_login_int32))
                    _pass_hash = sha256(_login_int32+self._config['PASSPHRASE']).hexdigest()
                    _pass_hash = bhex(_pass_hash)
                    self.send_master('RPTK'+self._config['RADIO_ID']+_pass_hash)
                    self._stats['CONNECTION'] = 'AUTHENTICATED'

                elif self._stats['CONNECTION'] == 'AUTHENTICATED': # If we've sent the login challenge...
                    _peer_id = _data[6:10]
                    if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                        self._logger.info('(%s) Repeater Authentication Accepted', self._system)
                        _config_packet =  self._config['RADIO_ID']+\
                                          self._config['CALLSIGN']+\
                                          self._config['RX_FREQ']+\
                                          self._config['TX_FREQ']+\
                                          self._config['TX_POWER']+\
                                          self._config['COLORCODE']+\
                                          self._config['LATITUDE']+\
                                          self._config['LONGITUDE']+\
                                          self._config['HEIGHT']+\
                                          self._config['LOCATION']+\
                                          self._config['DESCRIPTION']+\
                                          self._config['SLOTS']+\
                                          self._config['URL']+\
                                          self._config['SOFTWARE_ID']+\
                                          self._config['PACKAGE_ID']

                        self.send_master('RPTC'+_config_packet)
                        self._stats['CONNECTION'] = 'CONFIG-SENT'
                        self._logger.info('(%s) Repeater Configuration Sent', self._system)
                    else:
                        self._stats['CONNECTION'] = 'NO'
                        self._logger.error('(%s) Master ACK Contained wrong ID - Connection Reset', self._system)

                elif self._stats['CONNECTION'] == 'CONFIG-SENT': # If we've sent out configuration to the master
                    _peer_id = _data[6:10]
                    if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                        self._logger.info('(%s) Repeater Configuration Accepted', self._system)
                        if self._config['OPTIONS']:
                            self.send_master('RPTO'+self._config['RADIO_ID']+self._config['OPTIONS'])
                            self._stats['CONNECTION'] = 'OPTIONS-SENT'
                            self._logger.info('(%s) Sent options: (%s)', self._system, self._config['OPTIONS'])
                        else:
                            self._stats['CONNECTION'] = 'YES'
                            self._logger.info('(%s) Connection to Master Completed', self._system)
                    else:
                        self._stats['CONNECTION'] = 'NO'
                        self._logger.error('(%s) Master ACK Contained wrong ID - Connection Reset', self._system)

                elif self._stats['CONNECTION'] == 'OPTIONS-SENT': # If we've sent out options to the master
                    _peer_id = _data[6:10]
                    if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                        self._logger.info('(%s) Repeater Options Accepted', self._system)
                        self._stats['CONNECTION'] = 'YES'
                        self._logger.info('(%s) Connection to Master Completed with options', self._system)
                    else:
                        self._stats['CONNECTION'] = 'NO'
                        self._logger.error('(%s) Master ACK Contained wrong ID - Connection Reset', self._system)

            elif _command == 'MSTP':    # Actually MSTPONG -- a reply to RPTPING (send by peer)
                _peer_id = _data[7:11]
                if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                    self._stats['PING_OUTSTANDING'] = False
                    self._stats['NUM_OUTSTANDING'] = 0
                    self._stats['PINGS_ACKD'] += 1
                    self._logger.debug('(%s) MSTPONG Received. Pongs Since Connected: %s', self._system, self._stats['PINGS_ACKD'])

            elif _command == 'MSTC':    # Actually MSTCL -- notify us the master is closing down
                _peer_id = _data[5:9]
                if self._config['LOOSE'] or _peer_id == self._config['RADIO_ID']: # Validate the Radio_ID unless using loose validation
                    self._stats['CONNECTION'] = 'NO'
                    self._logger.info('(%s) MSTCL Recieved', self._system)

            else:
                self._logger.error('(%s) Received an invalid command in packet: %s', self._system, ahex(_data))

#
# Socket-based reporting section
#
class report(NetstringReceiver):
    def __init__(self, factory):
        self._factory = factory

    def connectionMade(self):
        self._factory.clients.append(self)
        self._factory._logger.info('HBlink reporting client connected: %s', self.transport.getPeer())

    def connectionLost(self, reason):
        self._factory._logger.info('HBlink reporting client disconnected: %s', self.transport.getPeer())
        self._factory.clients.remove(self)

    def stringReceived(self, data):
        self.process_message(data)

    def process_message(self, _message):
        opcode = _message[:1]
        if opcode == REPORT_OPCODES['CONFIG_REQ']:
            self._factory._logger.info('HBlink reporting client sent \'CONFIG_REQ\': %s', self.transport.getPeer())
            self.send_config()
        else:
            self._factory._logger.error('got unknown opcode')
        
class reportFactory(Factory):
    def __init__(self, config, logger):
        self._config = config
        self._logger = logger
        
    def buildProtocol(self, addr):
        if (addr.host) in self._config['REPORTS']['REPORT_CLIENTS'] or '*' in self._config['REPORTS']['REPORT_CLIENTS']:
            self._logger.debug('Permitting report server connection attempt from: %s:%s', addr.host, addr.port)
            return report(self)
        else:
            self._logger.error('Invalid report server connection attempt from: %s:%s', addr.host, addr.port)
            return None
            
    def send_clients(self, _message):
        for client in self.clients:
            client.sendString(_message)
            
    def send_config(self):
        serialized = pickle.dumps(self._config['SYSTEMS'], protocol=pickle.HIGHEST_PROTOCOL)
        self.send_clients(REPORT_OPCODES['CONFIG_SND']+serialized)
        

#************************************************
#      MAIN PROGRAM LOOP STARTS HERE
#************************************************

if __name__ == '__main__':
    # Python modules we need
    import argparse
    import sys
    import os
    import signal
    
    
    # Change the current directory to the location of the application
    os.chdir(os.path.dirname(os.path.realpath(sys.argv[0])))

    # CLI argument parser - handles picking up the config file from the command line, and sending a "help" message
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='store', dest='CONFIG_FILE', help='/full/path/to/config.file (usually hblink.cfg)')
    parser.add_argument('-l', '--logging', action='store', dest='LOG_LEVEL', help='Override config file logging level.')
    cli_args = parser.parse_args()

    # Ensure we have a path for the config file, if one wasn't specified, then use the execution directory
    if not cli_args.CONFIG_FILE:
        cli_args.CONFIG_FILE = os.path.dirname(os.path.abspath(__file__))+'/hblink.cfg'

    # Call the external routine to build the configuration dictionary
    CONFIG = hb_config.build_config(cli_args.CONFIG_FILE)
    
    # Call the external routing to start the system logger
    if cli_args.LOG_LEVEL:
        CONFIG['LOGGER']['LOG_LEVEL'] = cli_args.LOG_LEVEL
    logger = hb_log.config_logging(CONFIG['LOGGER'])
    logger.debug('Logging system started, anything from here on gets logged')

    # Set up the signal handler
    def sig_handler(_signal, _frame):
        logger.info('SHUTDOWN: HBLINK IS TERMINATING WITH SIGNAL %s', str(_signal))
        hblink_handler(_signal, _frame, logger)
        logger.info('SHUTDOWN: ALL SYSTEM HANDLERS EXECUTED - STOPPING REACTOR')
        reactor.stop()
        
    # Set signal handers so that we can gracefully exit if need be
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, sig_handler)
    
    # Build the Registration Access Control List
    REG_ACL = build_reg_acl('reg_acl', logger)
    
    # INITIALIZE THE REPORTING LOOP
    report_server = config_reports(CONFIG, logger, reportFactory)    

    # HBlink instance creation
    logger.info('HBlink \'HBlink.py\' (c) 2016-2018 N0MJS & the K0USY Group - SYSTEM STARTING...')
    for system in CONFIG['SYSTEMS']:
        if CONFIG['SYSTEMS'][system]['ENABLED']:
            if CONFIG['SYSTEMS'][system]['MODE'] == 'OPENBRIDGE':
                systems[system] = OPENBRIDGE(system, CONFIG, logger, report_server)
            else:
                systems[system] = HBSYSTEM(system, CONFIG, logger, report_server)
            reactor.listenUDP(CONFIG['SYSTEMS'][system]['PORT'], systems[system], interface=CONFIG['SYSTEMS'][system]['IP'])
            logger.debug('%s instance created: %s, %s', CONFIG['SYSTEMS'][system]['MODE'], system, systems[system])

    reactor.run()
