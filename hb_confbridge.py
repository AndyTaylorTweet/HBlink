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
This application, in conjuction with it's rule file (hb_confbridge_rules.py) will
work like a "conference bridge". This is similar to what most hams think of as a
reflector. You define conference bridges and any system joined to that conference
bridge will both receive traffic from, and send traffic to any other system
joined to the same conference bridge. It does not provide end-to-end connectivity
as each end system must individually be joined to a conference bridge (a name
you create in the configuraiton file) to pass traffic.

This program currently only works with group voice calls.
'''


from __future__ import print_function

# Python modules we need
import sys
from bitarray import bitarray
from time import time
from importlib import import_module

# Twisted is pretty important, so I keep it separate
from twisted.internet.protocol import Factory, Protocol
from twisted.protocols.basic import NetstringReceiver
from twisted.internet import reactor, task

# Things we import from the main hblink module
from hblink import HBSYSTEM, OPENBRIDGE, systems, hblink_handler, reportFactory, REPORT_OPCODES, build_reg_acl
from dmr_utils.utils import hex_str_3, int_id, get_alias
from dmr_utils import decode, bptc, const
import hb_config
import hb_log
import hb_const

# Stuff for socket reporting
import cPickle as pickle

# Does anybody read this stuff? There's a PEP somewhere that says I should do this.
__author__     = 'Cortney T. Buffington, N0MJS'
__copyright__  = 'Copyright (c) 2016-2018PEER Cortney T. Buffington, N0MJS and the K0USY Group'
__credits__    = 'Colin Durbridge, G4EML, Steve Zingman, N4IRS; Mike Zingman, N4IRR; Jonathan Naylor, G4KLX; Hans Barthen, DL5DI; Torsten Shultze, DG1HT'
__license__    = 'GNU GPLv3'
__maintainer__ = 'Cort Buffington, N0MJS'
__email__      = 'n0mjs@me.com'
__status__     = 'pre-alpha'

# Module gobal varaibles

# Timed loop used for reporting HBP status
#
# REPORT BASED ON THE TYPE SELECTED IN THE MAIN CONFIG FILE
def config_reports(_config, _logger, _factory):                 
    if True: #_config['REPORTS']['REPORT']:
        def reporting_loop(_logger, _server):
            _logger.debug('Periodic reporting loop started')
            _server.send_config()
            _server.send_bridge()
            
        _logger.info('HBlink TCP reporting server configured')
        
        report_server = _factory(_config, _logger)
        report_server.clients = []
        reactor.listenTCP(_config['REPORTS']['REPORT_PORT'], report_server)
        
        reporting = task.LoopingCall(reporting_loop, _logger, report_server)
        reporting.start(_config['REPORTS']['REPORT_INTERVAL'])
    
    return report_server


# Import Bridging rules
# Note: A stanza *must* exist for any MASTER or CLIENT configured in the main
# configuration file and listed as "active". It can be empty, 
# but it has to exist.
def make_bridges(_hb_confbridge_bridges):
    try:
        bridge_file = import_module(_hb_confbridge_bridges)
        logger.info('Routing bridges file found and bridges imported')
    except ImportError:
        sys.exit('Routing bridges file not found or invalid')
    
    # Convert integer GROUP ID numbers from the config into hex strings
    # we need to send in the actual data packets.
    for _bridge in bridge_file.BRIDGES:
        for _system in bridge_file.BRIDGES[_bridge]:
            if _system['SYSTEM'] not in CONFIG['SYSTEMS']:
                sys.exit('ERROR: Conference bridges found for system not configured main configuration')
                
            _system['TGID']       = hex_str_3(_system['TGID'])
            for i, e in enumerate(_system['ON']):
                _system['ON'][i]  = hex_str_3(_system['ON'][i])
            for i, e in enumerate(_system['OFF']):
                _system['OFF'][i] = hex_str_3(_system['OFF'][i])
            _system['TIMEOUT']    = _system['TIMEOUT']*60
            if _system['ACTIVE'] == True:
                _system['TIMER']  = time() + _system['TIMEOUT']
            else:
                _system['TIMER']  = time()

    return bridge_file.BRIDGES


# Import subscriber ACL
# ACL may be a single list of subscriber IDs
# Global action is to allow or deny them. Multiple lists with different actions and ranges
# are not yet implemented.
def build_acl(_sub_acl):
    ACL = set()
    try:
        acl_file = import_module(_sub_acl)
        logger.info('ACL file found, importing entries. This will take about 1.5 seconds per 1 million IDs')
        sections = acl_file.ACL.split(':')
        ACL_ACTION = sections[0]
        entries_str = sections[1]

        
        for entry in entries_str.split(','):
            if '-' in entry:
                start,end = entry.split('-')
                start,end = int(start), int(end)
                for id in range(start, end+1):
                    ACL.add(hex_str_3(id))
            else:
                id = int(entry)
                ACL.add(hex_str_3(id))
        
        logger.info('ACL loaded: action "{}" for {:,} radio IDs'.format(ACL_ACTION, len(ACL)))
    
    except ImportError:
        logger.info('ACL file not found or invalid - all subscriber IDs are valid')
        ACL_ACTION = 'NONE'

    # Depending on which type of ACL is used (PERMIT, DENY... or there isn't one)
    # define a differnet function to be used to check the ACL
    global allow_sub
    if ACL_ACTION == 'PERMIT':
        def allow_sub(_sub):
            if _sub in ACL:
                return True
            else:
                return False
    elif ACL_ACTION == 'DENY':
        def allow_sub(_sub):
            if _sub not in ACL:
                return True
            else:
                return False
    else:
        def allow_sub(_sub):
            return True
    
    return ACL


# Run this every minute for rule timer updates
def rule_timer_loop():
    logger.info('(ALL HBSYSTEMS) Rule timer loop started')
    _now = time()

    for _bridge in BRIDGES:
        for _system in BRIDGES[_bridge]:
            if _system['TO_TYPE'] == 'ON':
                if _system['ACTIVE'] == True:
                    if _system['TIMER'] < _now:
                        _system['ACTIVE'] = False
                        logger.info('Conference Bridge TIMEOUT: DEACTIVATE System: %s, Bridge: %s, TS: %s, TGID: %s', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
                    else:
                        timeout_in = _system['TIMER'] - _now
                        logger.info('Conference Bridge ACTIVE (ON timer running): System: %s Bridge: %s, TS: %s, TGID: %s, Timeout in: %ss,', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']),  timeout_in)
                elif _system['ACTIVE'] == False:
                    logger.debug('Conference Bridge INACTIVE (no change): System: %s Bridge: %s, TS: %s, TGID: %s', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
            elif _system['TO_TYPE'] == 'OFF':
                if _system['ACTIVE'] == False:
                    if _system['TIMER'] < _now:
                        _system['ACTIVE'] = True
                        logger.info('Conference Bridge TIMEOUT: ACTIVATE System: %s, Bridge: %s, TS: %s, TGID: %s', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
                    else:
                        timeout_in = _system['TIMER'] - _now
                        logger.info('Conference Bridge INACTIVE (OFF timer running): System: %s Bridge: %s, TS: %s, TGID: %s, Timeout in: %ss,', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']),  timeout_in)
                elif _system['ACTIVE'] == True:
                    logger.debug('Conference Bridge ACTIVE (no change): System: %s Bridge: %s, TS: %s, TGID: %s', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))
            else:
                logger.debug('Conference Bridge NO ACTION: System: %s, Bridge: %s, TS: %s, TGID: %s', _system['SYSTEM'], _bridge, _system['TS'], int_id(_system['TGID']))

    if CONFIG['REPORTS']['REPORT']:
        report_server.send_clients('bridge updated')


# run this every 10 seconds to trim orphaned stream ids
def stream_trimmer_loop():
    logger.debug('(ALL OPENBRIDGE SYSTEMS) Trimming inactive stream IDs from system lists')
    _now = time()
    
    for system in systems:
        remove_list = []
        if CONFIG['SYSTEMS'][system]['MODE'] == 'OPENBRIDGE':
            for stream_id in systems[system].STATUS:
                if systems[system].STATUS[stream_id]['LAST'] < _now - 5:
                    
                    remove_list.append(stream_id)
        
        for stream_id in remove_list:
            removed = systems[system].STATUS.pop(stream_id)
            logger.debug('Inactive OpenBridge Stream ID removed from System: %s, Stream ID %s', system, int_id(stream_id))

            
class routerOBP(OPENBRIDGE):
    
    def __init__(self, _name, _config, _logger, _report):
        OPENBRIDGE.__init__(self, _name, _config, _logger, _report)
        self.STATUS = {}
        

    def dmrd_received(self, _peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data):
        pkt_time = time()
        dmrpkt = _data[20:53]
        _bits = int_id(_data[15])

        if _call_type == 'group':
            
            # Check for ACL match, and return if the subscriber is not allowed
            if allow_sub(_rf_src) == False:
                self._logger.warning('(%s) Group Voice Packet ***REJECTED BY ACL*** From: %s, HBP Peer %s, Destination TGID %s', self._system, int_id(_rf_src), int_id(_peer_id), int_id(_dst_id))
                return
            
            # Is this a new call stream?   
            if (_stream_id not in self.STATUS):   
                # This is a new call stream
                self.STATUS[_stream_id] = {
                    'START':     pkt_time,
                    'CONTENTION':False,
                    'RFS':       _rf_src,
                    'TGID':      _dst_id,
                }

                # If we can, use the LC from the voice header as to keep all options intact
                if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                    decoded = decode.voice_head_term(dmrpkt)
                    self.STATUS[_stream_id]['LC'] = decoded['LC']
                
                # If we don't have a voice header then don't wait to decode the Embedded LC
                # just make a new one from the HBP header. This is good enough, and it saves lots of time
                else:
                    self.STATUS[_stream_id]['LC'] = const.LC_OPT + _dst_id + _rf_src
                   
                    
                self._logger.info('(%s) *CALL START* STREAM ID: %s SUB: %s (%s) PEER: %s (%s) TGID %s (%s), TS %s', \
                        self._system, int_id(_stream_id), get_alias(_rf_src, subscriber_ids), int_id(_rf_src), get_alias(_peer_id, peer_ids), int_id(_peer_id), get_alias(_dst_id, talkgroup_ids), int_id(_dst_id), _slot)
                if CONFIG['REPORTS']['REPORT']:
                    self._report.send_bridgeEvent('GROUP VOICE,START,{},{},{},{},{},{}'.format(self._system, int_id(_stream_id), int_id(_peer_id), int_id(_rf_src), _slot, int_id(_dst_id)))
            
            
            self.STATUS[_stream_id]['LAST'] = pkt_time


            for _bridge in BRIDGES:
                for _system in BRIDGES[_bridge]:
 
                    if (_system['SYSTEM'] == self._system and _system['TGID'] == _dst_id and _system['TS'] == _slot and _system['ACTIVE'] == True):
                        
                        for _target in BRIDGES[_bridge]:
                            if (_target['SYSTEM'] != self._system) and (_target['ACTIVE']):
                                _target_status = systems[_target['SYSTEM']].STATUS
                                _target_system = self._CONFIG['SYSTEMS'][_target['SYSTEM']]
                                if _target_system['MODE'] == 'OPENBRIDGE':
                                    # Is this a new call stream on the target?  
                                    if (_stream_id not in _target_status):
                                        # This is a new call stream on the target
                                        _target_status[_stream_id] = {
                                            'START':     pkt_time,
                                            'CONTENTION':False,
                                            'RFS':       _rf_src,
                                            'TGID':      _dst_id,
                                        }
                                        # If we can, use the LC from the voice header as to keep all options intact
                                        if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                                            decoded = decode.voice_head_term(dmrpkt)
                                            _target_status[_stream_id]['LC'] = decoded['LC']
                                            self._logger.debug('(%s) Created LC for OpenBridge destination: System: %s, TGID: %s', self._system, _target['SYSTEM'], int_id(_target['TGID']))
            
                                        # If we don't have a voice header then don't wait to decode the Embedded LC
                                        # just make a new one from the HBP header. This is good enough, and it saves lots of time
                                        else:
                                            _target_status[_stream_id]['LC'] = const.LC_OPT + _dst_id + _rf_src
                                            self._logger.info('(%s) Created LC with *LATE ENTRY* for OpenBridge destination: System: %s, TGID: %s', self._system, _target['SYSTEM'], int_id(_target['TGID']))
                                            
                                        _target_status[_stream_id]['H_LC']   = bptc.encode_header_lc(_target_status[_stream_id]['LC'])
                                        _target_status[_stream_id]['T_LC']   = bptc.encode_terminator_lc(_target_status[_stream_id]['LC'])
                                        _target_status[_stream_id]['EMB_LC'] = bptc.encode_emblc(_target_status[_stream_id]['LC'])
                                    
                                    # Record the time of this packet so we can later identify a stale stream   
                                    _target_status[_stream_id]['LAST'] = pkt_time
                                    # Clear the TS bit -- all OpenBridge streams are effectively on TS1
                                    _tmp_bits = _bits & ~(1 << 7)
                                        
                                    # Assemble transmit HBP packet header
                                    _tmp_data = _data[:8] + _target['TGID'] + _data[11:15] + chr(_tmp_bits) + _data[16:20]
                
                                    # MUST TEST FOR NEW STREAM AND IF SO, RE-WRITE THE LC FOR THE TARGET
                                    # MUST RE-WRITE DESTINATION TGID IF DIFFERENT
                                    # if _dst_id != rule['DST_GROUP']:
                                    dmrbits = bitarray(endian='big')
                                    dmrbits.frombytes(dmrpkt)
                                    # Create a voice header packet (FULL LC)
                                    if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                                        dmrbits = _target_status[_stream_id]['H_LC'][0:98] + dmrbits[98:166] + _target_status[_stream_id]['H_LC'][98:197]
                                    # Create a voice terminator packet (FULL LC)
                                    elif _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VTERM:
                                        dmrbits = _target_status[_stream_id]['T_LC'][0:98] + dmrbits[98:166] + _target_status[_stream_id]['T_LC'][98:197]
                                    # Create a Burst B-E packet (Embedded LC)
                                    elif _dtype_vseq in [1,2,3,4]:
                                        dmrbits = dmrbits[0:116] + _target_status[_stream_id]['EMB_LC'][_dtype_vseq] + dmrbits[148:264]
                                    dmrpkt = dmrbits.tobytes()
                                    _tmp_data = _tmp_data + dmrpkt #+ _data[53:55]
                                    
                                else:
                                    # BEGIN CONTENTION HANDLING
                                    #
                                    # The rules for each of the 4 "ifs" below are listed here for readability. The Frame To Send is:
                                    #   From a different group than last RX from this HBSystem, but it has been less than Group Hangtime
                                    #   From a different group than last TX to this HBSystem, but it has been less than Group Hangtime
                                    #   From the same group as the last RX from this HBSystem, but from a different subscriber, and it has been less than stream timeout
                                    #   From the same group as the last TX to this HBSystem, but from a different subscriber, and it has been less than stream timeout
                                    # The "continue" at the end of each means the next iteration of the for loop that tests for matching rules
                                    #
                                    if ((_target['TGID'] != _target_status[_target['TS']]['RX_TGID']) and ((pkt_time - _target_status[_target['TS']]['RX_TIME']) < _target_system['GROUP_HANGTIME'])):
                                        if self.STATUS[_stream_id]['CONTENTION'] == False:
                                            self.STATUS[_stream_id]['CONTENTION'] = True
                                            self._logger.info('(%s) Call not routed to TGID %s, target active or in group hangtime: HBSystem: %s, TS: %s, TGID: %s', self._system, int_id(_target['TGID']), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['RX_TGID']))
                                        continue
                                    if ((_target['TGID'] != _target_status[_target['TS']]['TX_TGID']) and ((pkt_time - _target_status[_target['TS']]['TX_TIME']) < _target_system['GROUP_HANGTIME'])):
                                        if self.STATUS[_stream_id]['CONTENTION'] == False:
                                            self.STATUS[_stream_id]['CONTENTION'] = True
                                            self._logger.info('(%s) Call not routed to TGID%s, target in group hangtime: HBSystem: %s, TS: %s, TGID: %s', self._system, int_id(_target['TGID']), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['TX_TGID']))
                                        continue
                                    if (_target['TGID'] == _target_status[_target['TS']]['RX_TGID']) and ((pkt_time - _target_status[_target['TS']]['RX_TIME']) < hb_const.STREAM_TO):
                                        if self.STATUS[_stream_id]['CONTENTION'] == False:
                                            self.STATUS[_stream_id]['CONTENTION'] = True
                                            self._logger.info('(%s) Call not routed to TGID%s, matching call already active on target: HBSystem: %s, TS: %s, TGID: %s', self._system, int_id(_target['TGID']), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['RX_TGID']))
                                        continue
                                    if (_target['TGID'] == _target_status[_target['TS']]['TX_TGID']) and (_rf_src != _target_status[_target['TS']]['TX_RFS']) and ((pkt_time - _target_status[_target['TS']]['TX_TIME']) < hb_const.STREAM_TO):
                                        if self.STATUS[_stream_id]['CONTENTION'] == False:
                                            self.STATUS[_stream_id]['CONTENTION'] = True
                                            self._logger.info('(%s) Call not routed for subscriber %s, call route in progress on target: HBSystem: %s, TS: %s, TGID: %s, SUB: %s', self._system, int_id(_rf_src), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['TX_TGID']), int_id(_target_status[_target['TS']]['TX_RFS']))
                                        continue
                            
                                    # Set values for the contention handler to test next time there is a frame to forward
                                    _target_status[_target['TS']]['TX_TIME'] = pkt_time
                
                                    if (_target_status[_target['TS']]['TX_RFS'] != _rf_src) or (_target_status[_target['TS']]['TX_TGID'] != _target['TGID']):       
                                        # Record the DST TGID and Stream ID
                                        _target_status[_target['TS']]['TX_TGID'] = _target['TGID']
                                        _target_status[_target['TS']]['TX_STREAM_ID'] = _stream_id
                                        _target_status[_target['TS']]['TX_RFS'] = _rf_src
                                        # Generate LCs (full and EMB) for the TX stream
                                        dst_lc = self.STATUS[_stream_id]['LC'][0:3] + _target['TGID'] + _rf_src
                                        _target_status[_target['TS']]['TX_H_LC'] = bptc.encode_header_lc(dst_lc)
                                        _target_status[_target['TS']]['TX_T_LC'] = bptc.encode_terminator_lc(dst_lc)
                                        _target_status[_target['TS']]['TX_EMB_LC'] = bptc.encode_emblc(dst_lc)
                                        self._logger.debug('(%s) Generating TX FULL and EMB LCs for destination: System: %s, TS: %s, TGID: %s', self._system, _target['SYSTEM'], _target['TS'], int_id(_target['TGID']))
                                        self._logger.info('(%s) Conference Bridge: %s, Call Bridged to: System: %s TS: %s, TGID: %s', self._system, _bridge, _target['SYSTEM'], _target['TS'], int_id(_target['TGID']))
                
                                    # Handle any necessary re-writes for the destination
                                    if _system['TS'] != _target['TS']:
                                        _tmp_bits = _bits ^ 1 << 7
                                    else:
                                        _tmp_bits = _bits
                
                                    # Assemble transmit HBP packet header
                                    _tmp_data = _data[:8] + _target['TGID'] + _data[11:15] + chr(_tmp_bits) + _data[16:20]
                
                                    # MUST TEST FOR NEW STREAM AND IF SO, RE-WRITE THE LC FOR THE TARGET
                                    # MUST RE-WRITE DESTINATION TGID IF DIFFERENT
                                    # if _dst_id != rule['DST_GROUP']:
                                    dmrbits = bitarray(endian='big')
                                    dmrbits.frombytes(dmrpkt)
                                    # Create a voice header packet (FULL LC)
                                    if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                                        dmrbits = _target_status[_target['TS']]['TX_H_LC'][0:98] + dmrbits[98:166] + _target_status[_target['TS']]['TX_H_LC'][98:197]
                                    # Create a voice terminator packet (FULL LC)
                                    elif _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VTERM:
                                        dmrbits = _target_status[_target['TS']]['TX_T_LC'][0:98] + dmrbits[98:166] + _target_status[_target['TS']]['TX_T_LC'][98:197]
                                    # Create a Burst B-E packet (Embedded LC)
                                    elif _dtype_vseq in [1,2,3,4]:
                                        dmrbits = dmrbits[0:116] + _target_status[_target['TS']]['TX_EMB_LC'][_dtype_vseq] + dmrbits[148:264]
                                    dmrpkt = dmrbits.tobytes()
                                    _tmp_data = _tmp_data + dmrpkt + _data[53:55]
                
                                # Transmit the packet to the destination system
                                systems[_target['SYSTEM']].send_system(_tmp_data)
                                #self._logger.debug('(%s) Packet routed by bridge: %s to system: %s TS: %s, TGID: %s', self._system, _bridge, _target['SYSTEM'], _target['TS'], int_id(_target['TGID']))
            
            
            
            # Final actions - Is this a voice terminator?
            if (_frame_type == hb_const.HBPF_DATA_SYNC) and (_dtype_vseq == hb_const.HBPF_SLT_VTERM):
                call_duration = pkt_time - self.STATUS[_stream_id]['START']
                self._logger.info('(%s) *CALL END*   STREAM ID: %s SUB: %s (%s) PEER: %s (%s) TGID %s (%s), TS %s, Duration: %s', \
                        self._system, int_id(_stream_id), get_alias(_rf_src, subscriber_ids), int_id(_rf_src), get_alias(_peer_id, peer_ids), int_id(_peer_id), get_alias(_dst_id, talkgroup_ids), int_id(_dst_id), _slot, call_duration)
                if CONFIG['REPORTS']['REPORT']:
                   self._report.send_bridgeEvent('GROUP VOICE,END,{},{},{},{},{},{},{:.2f}'.format(self._system, int_id(_stream_id), int_id(_peer_id), int_id(_rf_src), _slot, int_id(_dst_id), call_duration))
                removed = self.STATUS.pop(_stream_id)
                self._logger.debug('(%s) OpenBridge sourced call stream end, remove terminated Stream ID: %s', self._system, int_id(_stream_id))
                if not removed:
                    self_logger.error('(%s) *CALL END*   STREAM ID: %s NOT IN LIST -- THIS IS A REAL PROBLEM', self._system, int_id(_stream_id))

class routerHBP(HBSYSTEM):
    
    def __init__(self, _name, _config, _logger, _report):
        HBSYSTEM.__init__(self, _name, _config, _logger, _report)
        
        # Status information for the system, TS1 & TS2
        # 1 & 2 are "timeslot"
        # In TX_EMB_LC, 2-5 are burst B-E
        self.STATUS = {
            1: {
                'RX_START':     time(),
                'RX_SEQ':       '\x00',
                'RX_RFS':       '\x00',
                'TX_RFS':       '\x00',
                'RX_STREAM_ID': '\x00',
                'TX_STREAM_ID': '\x00',
                'RX_TGID':      '\x00\x00\x00',
                'TX_TGID':      '\x00\x00\x00',
                'RX_TIME':      time(),
                'TX_TIME':      time(),
                'RX_TYPE':      hb_const.HBPF_SLT_VTERM,
                'RX_LC':        '\x00',
                'TX_H_LC':      '\x00',
                'TX_T_LC':      '\x00',
                'TX_EMB_LC': {
                    1: '\x00',
                    2: '\x00',
                    3: '\x00',
                    4: '\x00',
                    }
                },
            2: {
                'RX_START':     time(),
                'RX_SEQ':       '\x00',
                'RX_RFS':       '\x00',
                'TX_RFS':       '\x00',
                'RX_STREAM_ID': '\x00',
                'TX_STREAM_ID': '\x00',
                'RX_TGID':      '\x00\x00\x00',
                'TX_TGID':      '\x00\x00\x00',
                'RX_TIME':      time(),
                'TX_TIME':      time(),
                'RX_TYPE':      hb_const.HBPF_SLT_VTERM,
                'RX_LC':        '\x00',
                'TX_H_LC':      '\x00',
                'TX_T_LC':      '\x00',
                'TX_EMB_LC': {
                    1: '\x00',
                    2: '\x00',
                    3: '\x00',
                    4: '\x00',
                    }
                }
            }

    def dmrd_received(self, _peer_id, _rf_src, _dst_id, _seq, _slot, _call_type, _frame_type, _dtype_vseq, _stream_id, _data):
        pkt_time = time()
        dmrpkt = _data[20:53]
        _bits = int_id(_data[15])

        if _call_type == 'group':
            
            # Check for ACL match, and return if the subscriber is not allowed
            if allow_sub(_rf_src) == False:
                self._logger.warning('(%s) Group Voice Packet ***REJECTED BY ACL*** From: %s, HBP Peer %s, Destination TGID %s', self._system, int_id(_rf_src), int_id(_peer_id), int_id(_dst_id))
                return
            
            # Is this a new call stream?   
            if (_stream_id != self.STATUS[_slot]['RX_STREAM_ID']):
                if (self.STATUS[_slot]['RX_TYPE'] != hb_const.HBPF_SLT_VTERM) and (pkt_time < (self.STATUS[_slot]['RX_TIME'] + hb_const.STREAM_TO)) and (_rf_src != self.STATUS[_slot]['RX_RFS']):
                    self._logger.warning('(%s) Packet received with STREAM ID: %s <FROM> SUB: %s PEER: %s <TO> TGID %s, SLOT %s collided with existing call', self._system, int_id(_stream_id), int_id(_rf_src), int_id(_peer_id), int_id(_dst_id), _slot)
                    return
                
                # This is a new call stream
                self.STATUS['RX_START'] = pkt_time
                self._logger.info('(%s) *CALL START* STREAM ID: %s SUB: %s (%s) PEER: %s (%s) TGID %s (%s), TS %s', \
                        self._system, int_id(_stream_id), get_alias(_rf_src, subscriber_ids), int_id(_rf_src), get_alias(_peer_id, peer_ids), int_id(_peer_id), get_alias(_dst_id, talkgroup_ids), int_id(_dst_id), _slot)
                if CONFIG['REPORTS']['REPORT']:
                    self._report.send_bridgeEvent('GROUP VOICE,START,{},{},{},{},{},{}'.format(self._system, int_id(_stream_id), int_id(_peer_id), int_id(_rf_src), _slot, int_id(_dst_id)))
                
                # If we can, use the LC from the voice header as to keep all options intact
                if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                    decoded = decode.voice_head_term(dmrpkt)
                    self.STATUS[_slot]['RX_LC'] = decoded['LC']
                
                # If we don't have a voice header then don't wait to decode it from the Embedded LC
                # just make a new one from the HBP header. This is good enough, and it saves lots of time
                else:
                    self.STATUS[_slot]['RX_LC'] = const.LC_OPT + _dst_id + _rf_src

            for _bridge in BRIDGES:
                for _system in BRIDGES[_bridge]:
 
                    if (_system['SYSTEM'] == self._system and _system['TGID'] == _dst_id and _system['TS'] == _slot and _system['ACTIVE'] == True):
                        
                        for _target in BRIDGES[_bridge]:
                            if _target['SYSTEM'] != self._system:
                                if _target['ACTIVE']:                             
                                    _target_status = systems[_target['SYSTEM']].STATUS
                                    _target_system = self._CONFIG['SYSTEMS'][_target['SYSTEM']]
                                
                                    if _target_system['MODE'] == 'OPENBRIDGE':
                                        # Is this a new call stream on the target?  
                                        if (_stream_id not in _target_status):
                                            # This is a new call stream on the target
                                            _target_status[_stream_id] = {
                                                'START':     pkt_time,
                                                'CONTENTION':False,
                                                'RFS':       _rf_src,
                                                'TGID':      _dst_id,
                                            }
                                            # If we can, use the LC from the voice header as to keep all options intact
                                            if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                                                decoded = decode.voice_head_term(dmrpkt)
                                                _target_status[_stream_id]['LC'] = decoded['LC']
                                                self._logger.debug('(%s) Created LC for OpenBridge destination: System: %s, TGID: %s', self._system, _target['SYSTEM'], int_id(_target['TGID']))
                
                                            # If we don't have a voice header then don't wait to decode the Embedded LC
                                            # just make a new one from the HBP header. This is good enough, and it saves lots of time
                                            else:
                                                _target_status[_stream_id]['LC'] = const.LC_OPT + _dst_id + _rf_src
                                                self._logger.info('(%s) Created LC with *LATE ENTRY* for OpenBridge destination: System: %s, TGID: %s', self._system, _target['SYSTEM'], int_id(_target['TGID']))
                                                
                                            _target_status[_stream_id]['H_LC']   = bptc.encode_header_lc(_target_status[_stream_id]['LC'])
                                            _target_status[_stream_id]['T_LC']   = bptc.encode_terminator_lc(_target_status[_stream_id]['LC'])
                                            _target_status[_stream_id]['EMB_LC'] = bptc.encode_emblc(_target_status[_stream_id]['LC'])
                                        
                                        # Record the time of this packet so we can later identify a stale stream   
                                        _target_status[_stream_id]['LAST'] = pkt_time
                                        # Clear the TS bit -- all OpenBridge streams are effectively on TS1
                                        _tmp_bits = _bits & ~(1 << 7)
                                            
                                        # Assemble transmit HBP packet header
                                        _tmp_data = _data[:8] + _target['TGID'] + _data[11:15] + chr(_tmp_bits) + _data[16:20]
                    
                                        # MUST TEST FOR NEW STREAM AND IF SO, RE-WRITE THE LC FOR THE TARGET
                                        # MUST RE-WRITE DESTINATION TGID IF DIFFERENT
                                        # if _dst_id != rule['DST_GROUP']:
                                        dmrbits = bitarray(endian='big')
                                        dmrbits.frombytes(dmrpkt)
                                        # Create a voice header packet (FULL LC)
                                        if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                                            dmrbits = _target_status[_stream_id]['H_LC'][0:98] + dmrbits[98:166] + _target_status[_stream_id]['H_LC'][98:197]
                                        # Create a voice terminator packet (FULL LC)
                                        elif _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VTERM:
                                            dmrbits = _target_status[_stream_id]['T_LC'][0:98] + dmrbits[98:166] + _target_status[_stream_id]['T_LC'][98:197]
                                        # Create a Burst B-E packet (Embedded LC)
                                        elif _dtype_vseq in [1,2,3,4]:
                                            dmrbits = dmrbits[0:116] + _target_status[_stream_id]['EMB_LC'][_dtype_vseq] + dmrbits[148:264]
                                        dmrpkt = dmrbits.tobytes()
                                        _tmp_data = _tmp_data + dmrpkt #+ _data[53:55]
                                            
                                    else:
                                        # BEGIN STANDARD CONTENTION HANDLING
                                        #
                                        # The rules for each of the 4 "ifs" below are listed here for readability. The Frame To Send is:
                                        #   From a different group than last RX from this HBSystem, but it has been less than Group Hangtime
                                        #   From a different group than last TX to this HBSystem, but it has been less than Group Hangtime
                                        #   From the same group as the last RX from this HBSystem, but from a different subscriber, and it has been less than stream timeout
                                        #   From the same group as the last TX to this HBSystem, but from a different subscriber, and it has been less than stream timeout
                                        # The "continue" at the end of each means the next iteration of the for loop that tests for matching rules
                                        #
                                        if ((_target['TGID'] != _target_status[_target['TS']]['RX_TGID']) and ((pkt_time - _target_status[_target['TS']]['RX_TIME']) < _target_system['GROUP_HANGTIME'])):
                                            if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD and self.STATUS[_slot]['RX_STREAM_ID'] != _seq:
                                                self._logger.info('(%s) Call not routed to TGID %s, target active or in group hangtime: HBSystem: %s, TS: %s, TGID: %s', self._system, int_id(_target['TGID']), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['RX_TGID']))
                                            continue
                                        if ((_target['TGID'] != _target_status[_target['TS']]['TX_TGID']) and ((pkt_time - _target_status[_target['TS']]['TX_TIME']) < _target_system['GROUP_HANGTIME'])):
                                            if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD and self.STATUS[_slot]['RX_STREAM_ID'] != _seq:
                                                self._logger.info('(%s) Call not routed to TGID%s, target in group hangtime: HBSystem: %s, TS: %s, TGID: %s', self._system, int_id(_target['TGID']), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['TX_TGID']))
                                            continue
                                        if (_target['TGID'] == _target_status[_target['TS']]['RX_TGID']) and ((pkt_time - _target_status[_target['TS']]['RX_TIME']) < hb_const.STREAM_TO):
                                            if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD and self.STATUS[_slot]['RX_STREAM_ID'] != _seq:
                                                self._logger.info('(%s) Call not routed to TGID%s, matching call already active on target: HBSystem: %s, TS: %s, TGID: %s', self._system, int_id(_target['TGID']), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['RX_TGID']))
                                            continue
                                        if (_target['TGID'] == _target_status[_target['TS']]['TX_TGID']) and (_rf_src != _target_status[_target['TS']]['TX_RFS']) and ((pkt_time - _target_status[_target['TS']]['TX_TIME']) < hb_const.STREAM_TO):
                                            if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD and self.STATUS[_slot]['RX_STREAM_ID'] != _seq:
                                                self._logger.info('(%s) Call not routed for subscriber %s, call route in progress on target: HBSystem: %s, TS: %s, TGID: %s, SUB: %s', self._system, int_id(_rf_src), _target['SYSTEM'], _target['TS'], int_id(_target_status[_target['TS']]['TX_TGID']), int_id(_target_status[_target['TS']]['TX_RFS']))
                                            continue
                                
                                        # Set values for the contention handler to test next time there is a frame to forward
                                        _target_status[_target['TS']]['TX_TIME'] = pkt_time
                    
                                        if (_stream_id != self.STATUS[_slot]['RX_STREAM_ID']) or (_target_status[_target['TS']]['TX_RFS'] != _rf_src) or (_target_status[_target['TS']]['TX_TGID'] != _target['TGID']):       
                                            # Record the DST TGID and Stream ID
                                            _target_status[_target['TS']]['TX_TGID'] = _target['TGID']
                                            _target_status[_target['TS']]['TX_STREAM_ID'] = _stream_id
                                            _target_status[_target['TS']]['TX_RFS'] = _rf_src
                                            # Generate LCs (full and EMB) for the TX stream
                                            dst_lc = self.STATUS[_slot]['RX_LC'][0:3] + _target['TGID'] + _rf_src
                                            _target_status[_target['TS']]['TX_H_LC'] = bptc.encode_header_lc(dst_lc)
                                            _target_status[_target['TS']]['TX_T_LC'] = bptc.encode_terminator_lc(dst_lc)
                                            _target_status[_target['TS']]['TX_EMB_LC'] = bptc.encode_emblc(dst_lc)
                                            self._logger.debug('(%s) Generating TX FULL and EMB LCs for HomeBrew destination: System: %s, TS: %s, TGID: %s', self._system, _target['SYSTEM'], _target['TS'], int_id(_target['TGID']))
                                            self._logger.info('(%s) Conference Bridge: %s, Call Bridged to: System: %s TS: %s, TGID: %s', self._system, _bridge, _target['SYSTEM'], _target['TS'], int_id(_target['TGID']))
                    
                                        # Handle any necessary re-writes for the destination
                                        if _system['TS'] != _target['TS']:
                                            _tmp_bits = _bits ^ 1 << 7
                                        else:
                                            _tmp_bits = _bits
                    
                                        # Assemble transmit HBP packet header
                                        _tmp_data = _data[:8] + _target['TGID'] + _data[11:15] + chr(_tmp_bits) + _data[16:20]
                    
                                        # MUST TEST FOR NEW STREAM AND IF SO, RE-WRITE THE LC FOR THE TARGET
                                        # MUST RE-WRITE DESTINATION TGID IF DIFFERENT
                                        # if _dst_id != rule['DST_GROUP']:
                                        dmrbits = bitarray(endian='big')
                                        dmrbits.frombytes(dmrpkt)
                                        # Create a voice header packet (FULL LC)
                                        if _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VHEAD:
                                            dmrbits = _target_status[_target['TS']]['TX_H_LC'][0:98] + dmrbits[98:166] + _target_status[_target['TS']]['TX_H_LC'][98:197]
                                        # Create a voice terminator packet (FULL LC)
                                        elif _frame_type == hb_const.HBPF_DATA_SYNC and _dtype_vseq == hb_const.HBPF_SLT_VTERM:
                                            dmrbits = _target_status[_target['TS']]['TX_T_LC'][0:98] + dmrbits[98:166] + _target_status[_target['TS']]['TX_T_LC'][98:197]
                                        # Create a Burst B-E packet (Embedded LC)
                                        elif _dtype_vseq in [1,2,3,4]:
                                            dmrbits = dmrbits[0:116] + _target_status[_target['TS']]['TX_EMB_LC'][_dtype_vseq] + dmrbits[148:264]
                                        dmrpkt = dmrbits.tobytes()
                                        _tmp_data = _tmp_data + dmrpkt + _data[53:55]
                    
                                    # Transmit the packet to the destination system
                                    systems[_target['SYSTEM']].send_system(_tmp_data)
                                    #self._logger.debug('(%s) Packet routed by bridge: %s to system: %s TS: %s, TGID: %s', self._system, _bridge, _target['SYSTEM'], _target['TS'], int_id(_target['TGID']))
            
            
            
            # Final actions - Is this a voice terminator?
            if (_frame_type == hb_const.HBPF_DATA_SYNC) and (_dtype_vseq == hb_const.HBPF_SLT_VTERM) and (self.STATUS[_slot]['RX_TYPE'] != hb_const.HBPF_SLT_VTERM):
                call_duration = pkt_time - self.STATUS['RX_START']
                self._logger.info('(%s) *CALL END*   STREAM ID: %s SUB: %s (%s) PEER: %s (%s) TGID %s (%s), TS %s, Duration: %s', \
                        self._system, int_id(_stream_id), get_alias(_rf_src, subscriber_ids), int_id(_rf_src), get_alias(_peer_id, peer_ids), int_id(_peer_id), get_alias(_dst_id, talkgroup_ids), int_id(_dst_id), _slot, call_duration)
                if CONFIG['REPORTS']['REPORT']:
                   self._report.send_bridgeEvent('GROUP VOICE,END,{},{},{},{},{},{},{:.2f}'.format(self._system, int_id(_stream_id), int_id(_peer_id), int_id(_rf_src), _slot, int_id(_dst_id), call_duration))
                
                #
                # Begin in-band signalling for call end. This has nothign to do with routing traffic directly.
                #
                
                # Iterate the rules dictionary
                
                for _bridge in BRIDGES:
                    for _system in BRIDGES[_bridge]:
                        if _system['SYSTEM'] == self._system:
            
                            # TGID matches a rule source, reset its timer
                            if _slot == _system['TS'] and _dst_id == _system['TGID'] and ((_system['TO_TYPE'] == 'ON' and (_system['ACTIVE'] == True)) or (_system['TO_TYPE'] == 'OFF' and _system['ACTIVE'] == False)):
                                _system['TIMER'] = pkt_time + _system['TIMEOUT']
                                self._logger.info('(%s) Transmission match for Bridge: %s. Reset timeout to %s', self._system, _bridge, _system['TIMER'])
            
                            # TGID matches an ACTIVATION trigger
                            if (_dst_id in _system['ON'] or _dst_id in _system['RESET']) and _slot == _system['TS']:
                                # Set the matching rule as ACTIVE
                                if _dst_id in _system['ON']:
                                    if _system['ACTIVE'] == False:
                                        _system['ACTIVE'] = True
                                        _system['TIMER'] = pkt_time + _system['TIMEOUT']
                                        self._logger.info('(%s) Bridge: %s, connection changed to state: %s', self._system, _bridge, _system['ACTIVE'])
                                        # Cancel the timer if we've enabled an "OFF" type timeout
                                        if _system['TO_TYPE'] == 'OFF':
                                            _system['TIMER'] = pkt_time
                                            self._logger.info('(%s) Bridge: %s set to "OFF" with an on timer rule: timeout timer cancelled', self._system, _bridge)
                                # Reset the timer for the rule
                                if _system['ACTIVE'] == True and _system['TO_TYPE'] == 'ON':
                                    _system['TIMER'] = pkt_time + _system['TIMEOUT']
                                    self._logger.info('(%s) Bridge: %s, timeout timer reset to: %s', self._system, _bridge, _system['TIMER'] - pkt_time)
                        
                            # TGID matches an DE-ACTIVATION trigger
                            if (_dst_id in _system['OFF']  or _dst_id in _system['RESET']) and _slot == _system['TS']:
                                # Set the matching rule as ACTIVE
                                if _dst_id in _system['OFF']:
                                    if _system['ACTIVE'] == True:
                                        _system['ACTIVE'] = False
                                        self._logger.info('(%s) Bridge: %s, connection changed to state: %s', self._system, _bridge, _system['ACTIVE'])
                                        # Cancel the timer if we've enabled an "ON" type timeout
                                        if _system['TO_TYPE'] == 'ON':
                                            _system['TIMER'] = pkt_time
                                            self._logger.info('(%s) Bridge: %s set to ON with and "OFF" timer rule: timeout timer cancelled', self._system, _bridge)
                                # Reset the timer for the rule
                                if _system['ACTIVE'] == False and _system['TO_TYPE'] == 'OFF':
                                    _system['TIMER'] = pkt_time + _system['TIMEOUT']
                                    self._logger.info('(%s) Bridge: %s, timeout timer reset to: %s', self._system, _bridge, _system['TIMER'] - pkt_time)
                                # Cancel the timer if we've enabled an "ON" type timeout
                                if _system['ACTIVE'] == True and _system['TO_TYPE'] == 'ON' and _dst_group in _system['OFF']:
                                    _system['TIMER'] = pkt_time
                                    self._logger.info('(%s) Bridge: %s set to ON with and "OFF" timer rule: timeout timer cancelled', self._system, _bridge)

            #                    
            # END IN-BAND SIGNALLING
            #
                
                
            # Mark status variables for use later
            self.STATUS[_slot]['RX_SEQ']       = _seq
            self.STATUS[_slot]['RX_RFS']       = _rf_src
            self.STATUS[_slot]['RX_TYPE']      = _dtype_vseq
            self.STATUS[_slot]['RX_TGID']      = _dst_id
            self.STATUS[_slot]['RX_TIME']      = pkt_time
            self.STATUS[_slot]['RX_STREAM_ID'] = _stream_id
                
#
# Socket-based reporting section
#
class confbridgeReportFactory(reportFactory):
        
    def send_bridge(self):
        serialized = pickle.dumps(BRIDGES, protocol=pickle.HIGHEST_PROTOCOL)
        self.send_clients(REPORT_OPCODES['BRIDGE_SND']+serialized)
        
    def send_bridgeEvent(self, _data):
        self.send_clients(REPORT_OPCODES['BRDG_EVENT']+_data)


#************************************************
#      MAIN PROGRAM LOOP STARTS HERE
#************************************************

if __name__ == '__main__':
    
    import argparse
    import sys
    import os
    import signal
    from dmr_utils.utils import try_download, mk_id_dict
    
    # Change the current directory to the location of the application
    os.chdir(os.path.dirname(os.path.realpath(sys.argv[0])))

    # CLI argument parser - handles picking up the config file from the command line, and sending a "help" message
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', action='store', dest='CONFIG_FILE', help='/full/path/to/config.file (usually hblink.cfg)')
    parser.add_argument('-l', '--logging', action='store', dest='LOG_LEVEL', help='Override config file logging level.')
    cli_args = parser.parse_args()

    # Ensure we have a path for the config file, if one wasn't specified, then use the default (top of file)
    if not cli_args.CONFIG_FILE:
        cli_args.CONFIG_FILE = os.path.dirname(os.path.abspath(__file__))+'/hblink.cfg'

    # Call the external routine to build the configuration dictionary
    CONFIG = hb_config.build_config(cli_args.CONFIG_FILE)
    
    # Start the system logger
    if cli_args.LOG_LEVEL:
        CONFIG['LOGGER']['LOG_LEVEL'] = cli_args.LOG_LEVEL
    logger = hb_log.config_logging(CONFIG['LOGGER'])
    logger.debug('Logging system started, anything from here on gets logged')
    
    # Set up the signal handler
    def sig_handler(_signal, _frame):
        logger.info('SHUTDOWN: HBROUTER IS TERMINATING WITH SIGNAL %s', str(_signal))
        hblink_handler(_signal, _frame, logger)
        logger.info('SHUTDOWN: ALL SYSTEM HANDLERS EXECUTED - STOPPING REACTOR')
        reactor.stop()
        
    # Set signal handers so that we can gracefully exit if need be
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, sig_handler)
    
    # Build the Access Control List
    REG_ACL = build_reg_acl('reg_acl', logger)
    
    # ID ALIAS CREATION
    # Download
    if CONFIG['ALIASES']['TRY_DOWNLOAD'] == True:
        # Try updating peer aliases file
        result = try_download(CONFIG['ALIASES']['PATH'], CONFIG['ALIASES']['PEER_FILE'], CONFIG['ALIASES']['PEER_URL'], CONFIG['ALIASES']['STALE_TIME'])
        logger.info(result)
        # Try updating subscriber aliases file
        result = try_download(CONFIG['ALIASES']['PATH'], CONFIG['ALIASES']['SUBSCRIBER_FILE'], CONFIG['ALIASES']['SUBSCRIBER_URL'], CONFIG['ALIASES']['STALE_TIME'])
        logger.info(result)
        
    # Make Dictionaries
    peer_ids = mk_id_dict(CONFIG['ALIASES']['PATH'], CONFIG['ALIASES']['PEER_FILE'])
    if peer_ids:
        logger.info('ID ALIAS MAPPER: peer_ids dictionary is available')
        
    subscriber_ids = mk_id_dict(CONFIG['ALIASES']['PATH'], CONFIG['ALIASES']['SUBSCRIBER_FILE'])
    if subscriber_ids:
        logger.info('ID ALIAS MAPPER: subscriber_ids dictionary is available')
    
    talkgroup_ids = mk_id_dict(CONFIG['ALIASES']['PATH'], CONFIG['ALIASES']['TGID_FILE'])
    if talkgroup_ids:
        logger.info('ID ALIAS MAPPER: talkgroup_ids dictionary is available')
    
    # Build the routing rules file
    BRIDGES = make_bridges('hb_confbridge_rules')
    
    # Build the Access Control List
    ACL = build_acl('sub_acl')
    
    # Build the Registration Access Control List
    REG_ACL = build_reg_acl('reg_acl', logger)
    
    # INITIALIZE THE REPORTING LOOP
    report_server = config_reports(CONFIG, logger, confbridgeReportFactory)
    
    # HBlink instance creation
    logger.info('HBlink \'hb_router.py\' (c) 2016 N0MJS & the K0USY Group - SYSTEM STARTING...')
    for system in CONFIG['SYSTEMS']:
        if CONFIG['SYSTEMS'][system]['ENABLED']:
            if CONFIG['SYSTEMS'][system]['MODE'] == 'OPENBRIDGE':
                systems[system] = routerOBP(system, CONFIG, logger, report_server)
            else:
                systems[system] = routerHBP(system, CONFIG, logger, report_server)
            reactor.listenUDP(CONFIG['SYSTEMS'][system]['PORT'], systems[system], interface=CONFIG['SYSTEMS'][system]['IP'])
            logger.debug('%s instance created: %s, %s', CONFIG['SYSTEMS'][system]['MODE'], system, systems[system])
            
    # Initialize the rule timer -- this if for user activated stuff
    rule_timer = task.LoopingCall(rule_timer_loop)
    rule_timer.start(60)
    
    # Initialize the stream trimmer
    stream_trimmer = task.LoopingCall(stream_trimmer_loop)
    stream_trimmer.start(5)

    reactor.run()
