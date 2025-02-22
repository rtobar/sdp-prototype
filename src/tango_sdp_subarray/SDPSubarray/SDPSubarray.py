# -*- coding: utf-8 -*-
"""Tango SDPSubarray device module."""
# pylint: disable=invalid-name
# pylint: disable=too-many-lines
# pylint: disable=fixme

import json
import logging
import sys
import time
from enum import IntEnum, unique
from inspect import currentframe, getframeinfo
from math import ceil
import os
from os.path import dirname, join

from jsonschema import exceptions, validate
import tango
from tango import AttrWriteType, AttributeProxy, ConnectionFailed, Database, \
    DbDevInfo, DevState, Except
from tango.server import Device, DeviceMeta, attribute, command, \
    device_property, run

try:
    from ska_sdp_config.config import Config as ConfigDbClient
    from ska_sdp_config.entity import ProcessingBlock
except ImportError:
    ConfigDbClient = None
    ProcessingBlock = None

# from skabase.SKASubarray import SKASubarray

LOG = logging.getLogger('ska.sdp.subarray_ds')


# https://pytango.readthedocs.io/en/stable/data_types.html#devenum-pythonic-usage
@unique
class AdminMode(IntEnum):
    """AdminMode enum."""

    OFFLINE = 0
    ONLINE = 1
    MAINTENANCE = 2
    NOT_FITTED = 3
    RESERVED = 4


@unique
class HealthState(IntEnum):
    """HealthState enum."""

    OK = 0
    DEGRADED = 1
    FAILED = 2
    UNKNOWN = 3


@unique
class ObsState(IntEnum):
    """ObsState enum."""

    IDLE = 0  #: Idle state
    CONFIGURING = 1  #: Configuring state
    READY = 2  #: Ready state
    SCANNING = 3  #: Scanning state
    PAUSED = 4  #: Paused state
    ABORTED = 5  #: Aborted state
    FAULT = 6  #: Fault state


@unique
class FeatureToggle(IntEnum):
    """Feature Toggles."""

    CONFIG_DB = 1  #: Enable / Disable the Config DB
    CBF_OUTPUT_LINK = 2  #: Enable / Disable use of of the CBF OUTPUT LINK
    AUTO_REGISTER = 3  #: Enable / Disable tango db auto-registration


# class SDPSubarray(SKASubarray):
class SDPSubarray(Device):
    """SDP Subarray device class.

    .. note::
        This should eventually inherit from SKASubarray but these need
        some work before doing so would add any value to this device.

    """

    # pylint: disable=attribute-defined-outside-init
    # pylint: disable=too-many-instance-attributes
    # pylint: disable=no-self-use

    __metaclass__ = DeviceMeta

    # -----------------
    # Device Properties
    # -----------------

    SdpMasterAddress = device_property(
        dtype='str',
        doc='FQDN of the SDP Master',
        default_value='mid_sdp/elt/master'
    )

    Version = device_property(
        dtype='str',
        doc='Version of the SDP Subarray device'
    )

    # ----------
    # Attributes
    # ----------

    obsState = attribute(
        label='Obs State',
        dtype=ObsState,
        access=AttrWriteType.READ_WRITE,
        doc='The device obs state.',
        polling_period=1000
    )

    adminMode = attribute(
        label='Admin mode',
        dtype=AdminMode,
        access=AttrWriteType.READ_WRITE,
        doc='The device admin mode.',
        polling_period=1000
    )

    healthState = attribute(
        label='Health state',
        dtype=HealthState,
        access=AttrWriteType.READ,
        doc='The health state reported for this device.',
        polling_period=1000
    )

    receiveAddresses = attribute(
        label='Receive Addresses',
        dtype=str,
        access=AttrWriteType.READ,
        doc="Host addresses for the visibility receive workflow given as a "
            "JSON object.",
        polling_period=1000
    )

    # ---------------
    # General methods
    # ---------------

    def init_device(self):
        """Initialise the device."""
        # SKASubarray.init_device(self)
        Device.init_device(self)

        self.set_state(DevState.INIT)
        LOG.info('Initialising SDP Subarray: %s', self.get_name())

        # Initialise attributes
        self._set_obs_state(ObsState.IDLE)
        self._admin_mode = AdminMode.ONLINE
        self._health_state = HealthState.OK
        self._receive_addresses = dict()

        # Initialise instance variables
        self._scanId = None  # The current Scan Id
        self._events_telstate = {}
        self._published_receive_addresses = False
        self._last_cbf_output_link = None
        self._config = dict()  # Dictionary of JSON passed to Configure
        self._cbf_output_link = dict()  # CSP channel - FSP link map
        self._receive_hosts = dict()  # Receive hosts - channels map
        if ConfigDbClient and self.is_feature_active(FeatureToggle.CONFIG_DB):
            self._config_db_client = ConfigDbClient()  # SDP Config db client.
            LOG.debug('Config Db enabled!')
        else:
            LOG.warning('Not writing to SDP Config DB (%s)',
                        ("package 'ska_sdp_config' package not found"
                         if ConfigDbClient is None
                         else 'disabled by feature toggle'))
            self._config_db_client = None

        if self.is_feature_active(FeatureToggle.CBF_OUTPUT_LINK):
            LOG.debug('CBF output link enabled!')
        else:
            LOG.debug('CBF output link disabled!')

        # The subarray device is initialised in the OFF state.
        self.set_state(DevState.OFF)
        LOG.info('SDP Subarray initialised: %s', self.get_name())

    def always_executed_hook(self):
        """Run for on each call."""

    def delete_device(self):
        """Device destructor."""
        LOG.info('Deleting subarray device: %s', self.get_name())

    # ------------------
    # Attributes methods
    # ------------------

    def read_obsState(self):
        """Get the obsState attribute.

        :returns: The current obsState attribute value.
        """
        return self._obs_state

    def read_adminMode(self):
        """Get the adminMode attribute.

        :return: The current adminMode attribute value.
        """
        return self._admin_mode

    def read_receiveAddresses(self):
        """Get the list of receive addresses encoded as a JSON string.

        More details are provided on SKA confluence at the address:
        http://bit.ly/2Gad55Q

        :return: JSON String describing receive addresses
        """
        return json.dumps(self._receive_addresses)

    def read_healthState(self):
        """Read Health State of the device.

        :return: Health State of the device
        """
        return self._health_state

    def write_obsState(self, obs_state):
        """Set the obsState attribute.

        :param obs_state: An observation state enum value.
        """
        self._set_obs_state(obs_state)

    def write_adminMode(self, admin_mode):
        """Set the adminMode attribute.

        :param admin_mode: An admin mode enum value.
        """
        LOG.debug('Setting adminMode to: %s', repr(AdminMode(admin_mode)))
        self._admin_mode = admin_mode
        self.push_change_event("adminMode", self._obs_state)

    # --------
    # Commands
    # --------

    @command(dtype_in=str, doc_in='Resource configuration JSON object')
    def AssignResources(self, config=''):
        """Assign Resources assigned to the subarray device.

        This is currently a noop for SDP!

        Following the description of the SKA subarray device model,
        resources can only be assigned to the subarray device when the
        obsState attribute is IDLE. Once resources are assigned to the
        subarray device, the device state transitions to ON.

        :param config: Resource specification (currently ignored)
        """
        # pylint: disable=unused-argument
        LOG.info('-------------------------------------------------------')
        LOG.info('AssignResources (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')
        self._require_obs_state([ObsState.IDLE])
        self._require_admin_mode([AdminMode.ONLINE, AdminMode.MAINTENANCE,
                                  AdminMode.RESERVED])
        LOG.warning('Assigning resources is currently a noop!')
        LOG.debug('Setting device state to ON')
        self.set_state(DevState.ON)
        LOG.info('-------------------------------------------------------')
        LOG.info('AssignResources Successful!')
        LOG.info('-------------------------------------------------------')

    @command(dtype_in=str, doc_in='Resource configuration JSON object')
    def ReleaseResources(self, config=''):
        """Release resources assigned to the subarray device.

        This is currently a noop for SDP!

        Following the description of the SKA subarray device model,
        when all resources are released the device state should transition to
        OFF. Releasing resources is only allowed when the obsState is IDLE.

        :param config: Resource specification (currently ignored).
        """
        # pylint: disable=unused-argument
        LOG.info('-------------------------------------------------------')
        LOG.info('ReleaseResources (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')
        self._require_obs_state([ObsState.IDLE])
        self._require_admin_mode([AdminMode.OFFLINE, AdminMode.NOT_FITTED],
                                 invert=True)
        LOG.warning('Release resources is currently a noop!')
        LOG.debug('Setting device state to OFF')
        self.set_state(DevState.OFF)
        LOG.info('-------------------------------------------------------')
        LOG.info('ReleaseResources Successful!')
        LOG.info('-------------------------------------------------------')

    @command(dtype_in=str, doc_in='Processing Block configuration JSON object')
    def Configure(self, json_config):
        """Set up (real-time) processing associated with this subarray.

        This is achieved by providing a JSON object containing a Processing
        Block configuration which is used to specify a workflow that should
        be executed.

        This function blocks until the SDP real-time processing is ready!

        .. note: This function mainly serves the role of a placeholder
                 testing the interface to TM and will need a major refactor
                 in the future.

        :param json_config: Processing Block configuration JSON object.
        """
        LOG.info('-------------------------------------------------------')
        LOG.info('Configure (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')

        # 1. Check obsState is IDLE, and set to CONFIGURING
        self._require_obs_state([ObsState.IDLE])
        self._set_obs_state(ObsState.CONFIGURING)
        self.set_state(DevState.ON)

        # 2. Validate the Configure JSON object argument
        config = self._validate_configure_json(json_config)
        # LOG.debug('Configure JSON successfully validated.')
        config = config.get('configure')
        self._config = config  # Store local copy of the configuration dict
        configured_scans = [int(scan_id) for scan_id in
                            self._config['scanParameters'].keys()]
        if configured_scans:
            self._scanId = configured_scans[0]  # Store the current scan id

        # 3. Add the PB configuration to the SDP config database.
        if self._config_db_client and \
                self.is_feature_active(FeatureToggle.CONFIG_DB):
            for txn in self._config_db_client.txn():
                LOG.info('Creating Processing Block id: %s (SBI Id: %s)',
                         config.get('id'), config.get('sbiId'))
                txn.create_processing_block(
                    ProcessingBlock(
                        pb_id=config.get('id'),
                        sbi_id=None,
                        workflow=config.get('workflow'),
                        parameters=config.get('parameters'),
                        scan_parameters=config.get('scanParameters')))
        else:
            LOG.warning('Not writing to SDP Config DB (%s)',
                        ("package 'ska_sdp_config' package not found"
                         if ConfigDbClient is None
                         else 'disabled by feature toggle'))

        # 4. Evaluate the receive addresses.
        self._generate_receive_addresses()

        # 5. Wait for receive addresses to be generated...
        # FIXME(BMo) This is a hack for debugging and not a long term solution.
        start_time = time.time()
        if self._scanId:
            while not self._receive_addresses.get('scanId') == self._scanId:
                LOG.debug('%s', self._receive_addresses)
                LOG.debug('%s == %s?',
                          self._receive_addresses.get('scanId'),
                          self._scanId)
                time.sleep(1.0)
                if time.time() - start_time > 3.0:
                    self._raise_command_error('Timeout reached!!')
                    break

        # 6. Set the obsState to ready.
        self._set_obs_state(ObsState.READY)

        LOG.info('-------------------------------------------------------')
        LOG.info('Configure successful!')
        LOG.info('-------------------------------------------------------')

    @command(dtype_in=str, doc_in="Scan Configuration JSON object")
    def ConfigureScan(self, json_scan_config):
        """Configure the subarray device to execute a scan.

        This allows scan specific, late-binding information to be provided
        to the configured PB workflow.

        ConfigureScan is only allowed in the READY obsState and should
        leave the Subarray device in the READY obsState when configuring
        is complete. While Configuring the Scan the obsState is set to
        CONFIGURING.

        .. note: This function currently does not do anything useful and is
                 just a placeholder to test the interface.

        :param json_scan_config: Scan configuration JSON object.

        """
        LOG.info('-------------------------------------------------------')
        LOG.info('ConfigureScan (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')

        # Check the obsState is READY and set to CONFIGURING
        self._require_obs_state([ObsState.READY])
        self._set_obs_state(ObsState.CONFIGURING)

        # Validate input scan configuration JSON object.
        try:
            schema_path = join(dirname(__file__), 'schema',
                               'configure_scan.json')
            with open(schema_path, 'r') as file:
                schema = json.loads(file.read())
            pb_config = json.loads(json_scan_config)
            validate(pb_config, schema)
        except json.JSONDecodeError:
            pass
        except exceptions.ValidationError:
            pass

        # TODO(BMo) Update receive addresses (if required)
        # self._update_receive_addresses()

        # Set the obsState to READY.
        self._set_obs_state(ObsState.READY)
        LOG.info('-------------------------------------------------------')
        LOG.info('ConfigureScan Successful!')
        LOG.info('-------------------------------------------------------')

    @command
    def StartScan(self):
        """Command issued when a scan is started."""
        LOG.info('-------------------------------------------------------')
        LOG.info('Start Scan (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')
        self._require_obs_state([ObsState.READY])
        self._set_obs_state(ObsState.SCANNING)
        LOG.info('-------------------------------------------------------')
        LOG.info('Start Scan Successful')
        LOG.info('-------------------------------------------------------')

    @command
    def Scan(self):
        """Command issued when a scan is started.

        FIXME(BMo) duplicate of StartScan

        """
        LOG.info('-------------------------------------------------------')
        LOG.info('Start Scan (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')
        self._require_obs_state([ObsState.READY])
        self.set_state(DevState.ON)
        self._set_obs_state(ObsState.SCANNING)
        LOG.info('-------------------------------------------------------')
        LOG.info('Start Scan Successful')
        LOG.info('-------------------------------------------------------')

    @command
    def EndScan(self):
        """Command issued when the scan is ended."""
        LOG.info('-------------------------------------------------------')
        LOG.info('End Scan (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')
        self._require_obs_state([ObsState.SCANNING])
        self._receive_addresses = None
        self._scanId = None
        self._set_obs_state(ObsState.READY)
        LOG.info('-------------------------------------------------------')
        LOG.info('End Scan Successful')
        LOG.info('-------------------------------------------------------')

    @command
    def EndSB(self):
        """Command issued to end the scheduling block."""
        LOG.info('-------------------------------------------------------')
        LOG.info('EndSB (%s)', self.get_name())
        LOG.info('-------------------------------------------------------')
        self._require_obs_state([ObsState.READY])
        self._receive_addresses = None
        self._scanId = None
        self._config = None
        self._set_obs_state(ObsState.IDLE)
        for event_id in list(self._events_telstate.keys()):
            self._events_telstate[event_id].unsubscribe_event(event_id)
        self._last_cbf_output_link = None
        LOG.info('-------------------------------------------------------')
        LOG.info('EndSB Successful')
        LOG.info('-------------------------------------------------------')

    # -------------------------------------
    # Public methods
    # -------------------------------------

    @staticmethod
    def set_feature_toggle_default(feature_name, default):
        """Set the default value of a feature toggle.

        :param feature_name: Name of the feature
        :param default: Default for the feature toggle (if it is not set)

        """
        env_var = SDPSubarray._get_feature_toggle_env_var(feature_name)
        if not os.environ.get(env_var):
            LOG.debug('Setting default for toggle: %s = %s', env_var, default)
            os.environ[env_var] = str(int(default))
        # else:
        #     LOG.debug('Unable to set default for toggle: %s '
        #               '(already set to: %s)', env_var,
        #               ('<True: 1>' if os.environ.get(env_var)
        #                else '<False: 0>'))

    @staticmethod
    def is_feature_active(feature_name):
        """Check if feature is active.

        :param feature_name: Name of the feature.
        :returns: True if the feature toggle is enabled.

        """
        env_var = SDPSubarray._get_feature_toggle_env_var(feature_name)
        env_var_value = os.environ.get(env_var)
        return env_var_value == '1'

    # -------------------------------------
    # Private methods
    # -------------------------------------

    @staticmethod
    def _get_feature_toggle_env_var(feature_name):
        """Get the env var associated with the feature toggle.

        :param feature_name: Name of the feature.
        :returns: environment variable name for feature toggle.

        """
        if isinstance(feature_name, FeatureToggle):
            feature_name = feature_name.name
        env_var = str('toggle_' + feature_name).upper()
        allowed = ['TOGGLE_' + toggle.name for toggle in FeatureToggle]
        if env_var not in allowed:
            message = 'Unknown feature toggle: {} (allowed: {})'\
                .format(env_var, allowed)
            LOG.error(message)
            raise ValueError(message)
        return env_var

    def _set_obs_state(self, value, verbose=True):
        """Set the obsState and issue a change event."""
        if verbose:
            LOG.debug('Setting obsState to: %s', repr(ObsState(value)))
        # LOG.debug('Setting obsState to: %s', value)
        self._obs_state = value
        self.push_change_event("obsState", self._obs_state)
        # if value == ObsState.FAULT:
        #     self.set_state(DevState.FAULT)

    def _validate_configure_json(self, json_str):
        """Validate the JSON object passed to the Configure command.

        :param json_str: JSON object string

        """
        LOG.debug('Validating Configure JSON (PB configuration).')
        if json_str == '':
            self._set_obs_state(ObsState.IDLE)
            self._receive_addresses = None
            self._raise_command_error('Empty JSON configuration!')

        schema_path = join(dirname(__file__), 'schema', 'configure_pb.json')
        config = {}
        try:
            config = json.loads(json_str)
            with open(schema_path, 'r') as file:
                schema = json.loads(file.read())
            validate(config, schema)
        except json.JSONDecodeError as error:
            msg = 'Unable to load JSON configuration: {}'.format(error.msg)
            self._set_obs_state(ObsState.IDLE)
            self._receive_addresses = None
            self._raise_command_error(msg)
        except exceptions.ValidationError as error:
            msg = 'Configure JSON validation error: {}'.format(
                error.message)
            self._set_obs_state(ObsState.IDLE)
            self._receive_addresses = None
            frame_info = getframeinfo(currentframe())
            origin = '{}:{}'.format(frame_info.filename, frame_info.lineno)
            self._raise_command_error(msg, origin)

        LOG.debug('Successfully validated Configure JSON argument!')
        return config

    def _scan_complete(self):
        """Update the obsState to READY when a scan is complete.

        Internal state transition.

        """
        self._require_obs_state([ObsState.SCANNING])
        self._set_obs_state(ObsState.READY)

    def _generate_receive_addresses(self):
        """Evaluate mapping between receive hosts and channels.

        .. note: This function will need major refactoring at some point!

        """
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-statements

        # Currently this function is intended to only work with
        # vis_ingest or vis_receive.
        allowed_ids = ['vis_ingest', 'vis_receive']
        if not any([_id == self._config['workflow']['id']
                    for _id in allowed_ids]):
            LOG.warning('Not updating receive addresses. '
                        'Workflow Id not not match a known '
                        'ingest or receive workflow.')
            return

        LOG.debug('Generating receive addresses.')
        self._published_receive_addresses = False

        # List of Scans IDs specified in the Configure JSON object.
        configured_scans = [int(scan_id) for scan_id in
                            self._config['scanParameters'].keys()]

        if not configured_scans:
            self._raise_command_error('No scanParameters specified in '
                                      'Configure JSON')

        # Get the cbf output links map from CSP.
        cbf_output_link = self._get_cbf_output_link_map()
        LOG.debug('Successfully obtained CBF output Link map!')

        # Build channels - CBF FSP map
        channel_fsp_map = self._generate_channels_fsp_map(cbf_output_link)

        # Check CBF output links scan ID has been configured.
        # If this fails there may have been an error reading this attribute.
        if cbf_output_link['scanID'] not in configured_scans:
            message = "Unknown scan ID {} in channel link map. " \
                      "Allowed values {}"\
                .format(cbf_output_link['scanID'], configured_scans)
            LOG.error(message)
            self._set_obs_state(ObsState.FAULT)
            raise RuntimeError(message)

        # Evaluate map of hosts to channels and FSPs
        self._generate_host_channel_map(channel_fsp_map)

        # Generate receive addresses attribute dict
        fsp_ids = list({channel['fspID'] for channel in channel_fsp_map})
        receive_addresses = dict(
            scanId=cbf_output_link['scanID'],
            totalChannels=len(channel_fsp_map),
            receiveAddresses=list()
        )

        # Add receive address object for each FSP.
        for fsp_id in fsp_ids:
            receive_addresses['receiveAddresses'].append(
                dict(phaseBinId=0, fspId=fsp_id, hosts=list()))

        # Add host objects to receive addresses FSP objects
        for host in self._receive_hosts:
            for fsp_id in host['fsp_ids']:
                fsp_host_address = next(
                    item for item in receive_addresses['receiveAddresses']
                    if item['fspId'] == fsp_id
                )
                fsp_host_address['hosts'].append(
                    dict(host=host['ip'], channels=list()))

        # LOG.debug('Recv. addresses\n%s',
        #           json.dumps(receive_addresses, indent=2))

        # Add channels to receive addresses FSP host lists
        pb_params = self._config['parameters']
        chan_ave_factor = pb_params.get('channelAveragingFactor', 1)
        recv_port_offset = pb_params.get('portOffset', 9000)
        for host in self._receive_hosts:
            host_ip = host['ip']

            for channel in host['channels']:
                fsp_id = channel.get('fspID')
                channel_id = channel.get('id')
                LOG.debug('Adding channel: %d (fsp id: %d link id: %d) to '
                          'receive addresses map.',
                          channel_id, fsp_id, channel.get('linkID'))
                fsp_host_address = next(
                    item for item in receive_addresses['receiveAddresses']
                    if item['fspId'] == fsp_id)
                if not fsp_host_address['hosts']:
                    fsp_host_address['hosts'].append(
                        dict(host=host_ip, channels=list()))
                host_config = next(item for item in fsp_host_address['hosts']
                                   if item['host'] == host_ip)

                # Find out if this channel can be added to a channel block
                # ie. if it belongs to an existing list with stride
                #     chan_ave_factor
                # if so, update the channel block.
                channel_config = None
                for host_channels in host_config['channels']:
                    start_chan = host_channels['startChannel']
                    next_chan = (start_chan +
                                 host_channels['numChannels'] *
                                 chan_ave_factor)
                    if next_chan == channel_id:
                        LOG.debug('Channel %d added to existing block with '
                                  'channel start id %d', channel_id,
                                  start_chan)
                        channel_config = next(item for item
                                              in host_config['channels']
                                              if item['startChannel'] ==
                                              start_chan)
                        channel_config['numChannels'] += 1

                # Otherwise, start a new channel block.
                if channel_config is None:
                    start_channel = channel_id
                    port_offset = recv_port_offset + channel_id
                    num_channels = 1
                    channel_config = dict(portOffset=port_offset,
                                          startChannel=start_channel,
                                          numChannels=num_channels)
                    host_config['channels'].append(channel_config)

        # HACK(BMo) Make sure FSPs appear in the receive_addresses map
        # This is needed by CSP if the cbfOutputLink toggle is disabled.
        if not receive_addresses['receiveAddresses']:
            for fsp in cbf_output_link['fsp']:
                receive_addresses['receiveAddresses'].append(
                    dict(fspId=fsp.get('fspID'), hosts=list())
                )

        # LOG.debug('Recv. addresses\n%s',
        #           json.dumps(receive_addresses, indent=2))
        self._receive_addresses = receive_addresses
        self.push_change_event("receiveAddresses",
                               json.dumps(self._receive_addresses))

        self._published_receive_addresses = True
        LOG.debug('Successfully updated receiveAddresses')

    def _update_receive_addresses(self):
        """Update receive addresses map (private method).

        Expected to be called by ConfigureScan when there is a new
        channel link map from CSP.

        NOTE(BMo) This is a placeholder function and will need completely
          reworking at some point!

        """
        self._set_obs_state(ObsState.FAULT)
        raise NotImplementedError()

    def _get_cbf_output_link_map(self):
        """Read and validate the CBF output link map.

        This is read from a CSP subarray device attribute.

        """
        # Read the cbfOutputLink attribute.
        configured_scans = [int(scan_id) for scan_id in
                            self._config['scanParameters'].keys()]

        if self.is_feature_active(FeatureToggle.CBF_OUTPUT_LINK):
            cbf_out_link_str = self._read_cbf_output_link()
        else:
            LOG.warning("CBF Output Link feature disabled! Generating mock "
                        "attribute value.")
            cbf_out_link = dict(
                scanID=configured_scans[0],
                fsp=[
                    dict(cbfOutLink=[],
                         fspID=1,
                         frequencySliceID=1)
                ]
            )
            cbf_out_link_str = json.dumps(cbf_out_link)

        # Check the cbfOutputLink map is not empty!
        if not cbf_out_link_str or cbf_out_link_str == json.dumps(dict()):
            message = 'CBF Output Link map is empty!'
            LOG.error(message)
            self._set_obs_state(ObsState.FAULT)
            raise RuntimeError(message)

        # Convert string to dictionary.
        try:
            cbf_output_link = json.loads(cbf_out_link_str)
            LOG.debug('Successfully loaded cbfOutputLinks JSON object as dict')

        except json.JSONDecodeError as error:
            LOG.error('Channel link map JSON load error: %s '
                      '(line %s, column: %s)', error.msg, error.lineno,
                      error.colno)
            self._set_obs_state(ObsState.FAULT)
            raise

        LOG.debug('Validating cbfOutputLinks ...')

        # Validate schema.
        schema_path = join(dirname(__file__), 'schema', 'cbfOutLink.json')
        with open(schema_path, 'r') as file:
            schema = json.loads(file.read())
        try:
            validate(cbf_output_link, schema)
        except exceptions.ValidationError as error:
            frame = getframeinfo(currentframe())
            message = 'cbfOutputLinks validation error: {}, {}'.format(
                error.message, str(error.absolute_path))
            origin = '{}:{}'.format(frame.filename, frame.lineno)
            LOG.error(message)
            Except.throw_exception(message, message, origin)
        LOG.debug('Channel link map validation successful.')

        self._cbf_output_link = cbf_output_link
        return cbf_output_link

    def _read_cbf_output_link(self):
        """Get the CBF output link map from the CSP subarray device.

        This provides the map of FSP to channels needed to construct
        the receive address map.

        :return: Channel link map string as read from CSP.

        """
        LOG.debug('Reading cbfOutputLink attribute ...')
        attribute_fqdn = self._config.get('cspCbfOutlinkAddress', None)
        if attribute_fqdn is None:
            msg = "'cspCbfOutlinkAddress' not found in PB configuration"
            self._set_obs_state(ObsState.FAULT, verbose=False)
            self._raise_command_error(msg)
        LOG.debug('Reading cbfOutLink from: %s', attribute_fqdn)
        attribute_proxy = AttributeProxy(attribute_fqdn)
        attribute_proxy.ping()
        LOG.debug('Waiting for cbfOutputLink to provide config for scanId: %s',
                  self._scanId)
        cbf_out_link_dict = {}
        start_time = time.time()
        # FIXME(BMo) Horrible hack to poll the CSP device until the scanID \
        # matches - use events instead!!
        cbf_out_link = ''
        while cbf_out_link_dict.get('scanId') != self._scanId:
            cbf_out_link = attribute_proxy.read().value
            cbf_out_link_dict = json.loads(cbf_out_link)
            time.sleep(1.0)
            elapsed = time.time() - start_time
            LOG.debug('Waiting for cbfOutputLink attribute (elapsed: %2.4f s) '
                      ': %s', elapsed, cbf_out_link)
            if elapsed >= 20.0:
                self._set_obs_state(ObsState.FAULT, verbose=False)
                self._raise_command_error(
                    'Timeout reached while reading cbf output link!')
                break
        # event_id = attribute_proxy.subscribe_event(
        #     tango.EventType.CHANGE_EVENT,
        #     self._cbf_output_link_callback
        # )
        # self._events_telstate[event_id] = attribute_proxy
        # print(attribute_proxy.is_event_queue_empty())
        # events = attribute_proxy.get_events()
        LOG.debug('Channel link map (str): "%s"', cbf_out_link)
        return cbf_out_link

    def _generate_host_channel_map(self, channel_fsp_map):
        """Evaluate channel map for receive hosts.

        This function should generate a map of receive hosts to channels.
        This will eventually need to depend on the deployment of the
        receive EE processes / containers.

        .. note: This is a placeholder function and will need completely
                 reworking at some point!

        """
        LOG.debug('Generating list of receive hosts mapped to channels and '
                  'FSPs for scan Id %d', self._cbf_output_link['scanID'])

        self._receive_hosts = list()

        # Work out how many hosts are needed on workflow parameters.
        # Note(BMo) Eventually this should be a function of the workflow.
        pb_params = self._config.get('parameters')
        num_channels = pb_params.get('numChannels', 0)
        max_channels_per_host = pb_params.get('maxChannelsPerHost', 400)
        num_hosts = ceil(num_channels / max_channels_per_host)
        LOG.debug('No. channels: %d, No. hosts: %d, channels / host: %d',
                  num_channels, num_hosts, max_channels_per_host)

        # FIXME(BMo) HACK: Generate fictitious receive hosts.
        # Note(BMo): Assume each host does not care about which channels it
        #            is assigned, only the number of channels ... This is a
        #            bad assumption!
        for host_index in range(num_hosts):
            host_ip = '192.168.{}.{}'.format(
                host_index//256, host_index - ((host_index//256) * 256))
            host_num_channels = min(
                num_channels - (max_channels_per_host * host_index),
                max_channels_per_host)
            host = dict(ip=host_ip, num_channels=host_num_channels)
            self._receive_hosts.append(host)

        # Associate channels and FSPs ids with hosts.
        # This also revises the number of channels based on the allocated
        # channel ids
        channel_start = 0
        for host in self._receive_hosts:
            channel_end = channel_start + host['num_channels']
            host_channels = channel_fsp_map[channel_start:channel_end]
            host['channels'] = host_channels
            host['channel_ids'] = [
                channel['id'] for channel
                in channel_fsp_map[channel_start:channel_end]
            ]
            host['num_channels'] = len(host_channels)
            host['fsp_ids'] = list(
                {channel['fspID'] for channel
                 in channel_fsp_map[channel_start:channel_end]}
            )
            LOG.debug('Host: %s num_channels: %s', host['ip'],
                      host['num_channels'])
            channel_start += host['num_channels']

    def _generate_channels_fsp_map(self, cbf_output_links):
        """Evaluate a map of channels <-> FSPs.

        This is used as an intermediate data structure for generating
        receiveAddresses.

        :param cbf_output_links: Channel link map (from CSP)

        :returns: map of channels to FSPs

        """
        LOG.debug('Evaluating channel - FSP mapping.')
        # Build map of channels <-> FSP's
        channels = list()
        for fsp in cbf_output_links['fsp']:
            for link in fsp['cbfOutLink']:
                for channel in link['channel']:
                    channel = dict(id=channel['chanID'],
                                   bw=channel['bw'],
                                   cf=channel['cf'],
                                   fspID=fsp['fspID'],
                                   linkID=link['linkID'])
                    channels.append(channel)

        # Sort the channels by Id and fspID
        channels = sorted(channels, key=lambda key: key['id'])
        channels = sorted(channels, key=lambda key: key['fspID'])

        # The number of channels in the channel link map should be <= number
        # of channels in the receive parameters.
        pb_params = self._config['parameters']
        pb_num_channels = pb_params.get('numChannels', 0)
        if len(channels) > pb_num_channels:
            self._set_obs_state(ObsState.FAULT)
            msg = 'Vis Receive configured for fewer channels than defined in '\
                  'the CSP channel link map! (link map: {}, workflow: {})'\
                  .format(len(channels), pb_num_channels)
            self._raise_command_error(msg)
        if len(channels) < pb_num_channels:
            LOG.warning("Workflow '%s:%s' configured with more channels "
                        "than defined in the CBF output! "
                        "(CBF output: %d, workflow: %d)",
                        self._config['workflow']['id'],
                        self._config['workflow']['version'],
                        len(channels), pb_num_channels)
        return channels

    def _require_obs_state(self, allowed_states, invert=False):
        """Require specified obsState values.

        Checks if the current obsState matches the specified allowed values.

        If invert is False (default), throw an exception if the obsState
        is not in the list of specified states.

        If invert is True, throw an exception if the obsState is NOT in the
        list of specified allowed states.

        :param allowed_states: List of allowed obsState values
        :param invert: If True require that the obsState is not in one of
                       specified allowed states

        """
        # Fail if obsState is NOT in one of the allowed_obs_states
        if not invert and self._obs_state not in allowed_states:
            self._set_obs_state(ObsState.FAULT)
            msg = 'obsState ({}) must be in {}'.format(
                self._obs_state, allowed_states)
            self._raise_command_error(msg)

        # Fail if obsState is in one of the allowed_obs_states
        if invert and self._obs_state in allowed_states:
            self._set_obs_state(ObsState.FAULT)
            msg = 'The device must NOT be in one of the ' \
                  'following obsState values: {}'.format(allowed_states)
            self._raise_command_error(msg)

    def _require_admin_mode(self, allowed_modes, invert=False):
        """Require specified adminMode values.

        Checks if the current adminMode matches the specified allowed values.

        If invert is False (default), throw an exception if not in the list
        of specified states / modes.

        If invert is True, throw an exception if the adminMode is NOT in the
        list of specified allowed states.

        :param allowed_modes: List of allowed adminMode values
        :param invert: If True require that the adminMode is not in one of
                       specified allowed states

        """
        # Fail if adminMode is NOT in one of the allowed_modes
        if not invert and self._admin_mode not in allowed_modes:
            msg = 'adminMode ({}) must be in: {}'.format(
                repr(self._admin_mode), allowed_modes)
            LOG.error(msg)
            self._raise_command_error(msg)

        # Fail if obsState is in one of the allowed_obs_states
        if invert and self._admin_mode in allowed_modes:
            msg = 'adminMode ({}) must NOT be in: {}'.format(
                repr(self._admin_mode), allowed_modes)
            LOG.error(msg)
            self._raise_command_error(msg)

    @staticmethod
    def _cbf_output_link_callback(event):
        """Change event call-back for the cbfOutputLink attribute."""
        # https://github.com/ska-telescope/mid-cbf-mcs/blob/e076159ecad8258d4558605e178b0f9e73955d81/tangods/CbfSubarray/CbfSubarray/CbfSubarray.py#L127
        # CHECK IF IN THE RIGHT device and obs state
        if not event.err:
            LOG.debug('EVENT: %s -- %s',
                      event.device, event.attr_name)
            LOG.debug('%s', event.attr_value.value)
        else:
            for item in event.errors:
                message = item.reason + ": on attribute " + \
                          str(event.attr_name)
                LOG.error(message)

    def _raise_command_error(self, desc, origin=''):
        """Raise a command error.

        :param desc: Error message / description.
        :param origin: Error origin (optional).

        """
        self._raise_error(desc, reason='Command error', origin=origin)

    def _raise_error(self, desc, reason='', origin=''):
        """Raise an error.

        :param desc: Error message / description.
        :param reason: Reason for the error.
        :param origin: Error origin (optional).

        """
        if reason != '':
            LOG.error(reason)
        LOG.error(desc)
        if origin != '':
            LOG.error(origin)
        tango.Except.throw_exception(reason, desc, origin,
                                     tango.ErrSeverity.ERR)


def delete_device_server(instance_name="*"):
    """Delete (unregister) SDPSubarray device server instance(s).

    :param instance_name: Optional, name of the device server instance to
                          remove. If not specified all service instances will
                          be removed.

    """
    try:
        tango_db = Database()
        class_name = 'SDPSubarray'
        server_name = '{}/{}'.format(class_name, instance_name)
        for server_name in list(tango_db.get_server_list(server_name)):
            LOG.info('Removing device server: %s', server_name)
            tango_db.delete_server(server_name)
    except ConnectionFailed:
        pass


def register(instance_name, *device_names):
    """Register device with a SDPSubarray device server instance.

    If the device is already registered, do nothing.

    :param instance_name: Instance name SDPSubarray device server
    :param device_names: Subarray device names to register

    """
    # pylint: disable=protected-access
    try:
        tango_db = Database()
        class_name = 'SDPSubarray'
        server_name = '{}/{}'.format(class_name, instance_name)
        devices = list(tango_db.get_device_name(server_name, class_name))
        device_info = DbDevInfo()
        device_info._class = class_name
        device_info.server = server_name
        for device_name in device_names:
            device_info.name = device_name
            if device_name in devices:
                LOG.debug("Device '%s' already registered", device_name)
                continue
            LOG.info('Registering device: %s (server: %s, class: %s)',
                     device_info.name, server_name, class_name)
            tango_db.add_device(device_info)
    except ConnectionFailed:
        pass


def init_logger(level='DEBUG', name='ska'):
    """Initialise stdout logger for the ska.sdp logger.

    :param level: Logging level, default: 'DEBUG'
    :param name: Logger name to initialise. default: 'ska.sdp'.

    """
    log = logging.getLogger(name)
    log.propagate = False
    # make sure there are no duplicate handlers.
    for handler in log.handlers:
        log.removeHandler(handler)
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(message)s')
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    log.addHandler(handler)
    log.setLevel(level)


def main(args=None, **kwargs):
    """Run server."""
    # Set default values for feature toggles.
    SDPSubarray.set_feature_toggle_default(FeatureToggle.CONFIG_DB, False)
    SDPSubarray.set_feature_toggle_default(FeatureToggle.CBF_OUTPUT_LINK,
                                           False)
    SDPSubarray.set_feature_toggle_default(FeatureToggle.AUTO_REGISTER, True)

    log_level = 'INFO'
    if len(sys.argv) > 2 and '-v' in sys.argv[2]:
        log_level = 'DEBUG'
    init_logger(log_level)

    # If the feature is enabled, attempt to auto-register the device
    # with the tango db.
    if SDPSubarray.is_feature_active(FeatureToggle.AUTO_REGISTER):
        if len(sys.argv) > 1:
            # delete_device_server("*")
            devices = ['mid_sdp/elt/subarray_{:d}'.format(i+1)
                       for i in range(1)]
            register(sys.argv[1], *devices)

    return run((SDPSubarray,), args=args, **kwargs)


if __name__ == '__main__':
    main()
