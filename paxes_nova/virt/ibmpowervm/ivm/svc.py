# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

import os
import paramiko
import random
import re
import time
import types
import string
import uuid

from nova import exception
from nova.openstack.common import log as logging

from oslo.config import cfg
from paramiko.ssh_exception import SSHException
from paxes_nova import logcall

from paxes_nova import _

SSH_KEY_PRIVATE_KEY = 'private_key'
SSH_KEY_HOST = 'host'
SSH_PORT = 'port'

SSH_KEY_USERNAME = 'username'
SSH_KEY_PASSWORD = 'password'
SSH_KEEP_ALIVE = 'keepalive'

SVC_VOLUME_NAME_PREFIX = 'nova'
SVC_CLUSTER_ID = 'id'
SVC_KEY_VDISK_UID = 'vdisk_UID'
SVC_KEY_VDISK_ID = 'id'
SVC_KEY_VDISK_NAME = 'name'
SVC_KEY_VOLUME_GROUP = 'mdisk_grp_name'
SVC_KEY_MDISK_GROUP_NAME = 'name'
SVC_KEY_MDISK_GROUP_FREE_CAPACITY = 'free_capacity'
SVC_KEY_VDISK_CAPACITY = 'capacity'
SVC_KEY_HOST_NAME = 'name'
SVC_KEY_HOST_ID = 'id'
SVC_KEY_MAPPED_HOST_NAME = 'host_name'
SVC_KEY_FC_TARGET_VDISK_NAME = 'target_vdisk_name'
SVC_KEY_FC_PROGRESS = 'progress'
SVC_KEY_FC_MAP_ID = 'id'
SVC_KEY_MAP_SCSI_ID = 'SCSI_id'
SVC_KEY_PORT_WWPN = 'WWPN'
SVC_KEY_STATUS = 'status'

LOG = logging.getLogger(__name__)

svc_opts = [
    cfg.StrOpt('svc_host',
               default='',
               help='Hostname or IP address of SAN controller'),
    cfg.IntOpt('svc_port',
               default=22,
               help='IP port of SAN controller'),
    cfg.IntOpt('svc_keep_alive',
               default=20,
               help='SVC transport keep alive interval'),
    cfg.StrOpt('svc_login',
               default='admin',
               help='Username for SAN controller'),
    cfg.StrOpt('svc_password',
               secret=True,
               help='Password for SAN controller'),
    cfg.StrOpt('svc_private_key',
               help='Filename of private key to use for SSH authentication'),
    cfg.IntOpt('svc_ssh_connect_retries',
               default=10,
               help='Number of times to retry the SSH connection to SVC.'),
    cfg.IntOpt('svc_rmfcmap_timeout',
               default=1800,
               help='Timeout for waiting fcmap to be at stopped state'),
    cfg.IntOpt('svc_stopfcmap_sleep_period',
               default=30,
               help=('Poll interval in seconds for waiting fcmap to be '
                     'at stopped state')),
    cfg.StrOpt('svc_known_hosts_path',
               default='/opt/ibm/powervc/data/known_hosts',
               help='Path to known hosts key file')
]

CONF = cfg.CONF
CONF.register_opts(svc_opts)


class SVCOperator(object):
    """
    Documentation for SVCOperator class.
    SVCOperator will manage all communication with SVC system handling SAN.
    """
    def __init__(self):
        """
        The constructor.
        :param self The object pointer.
        """
        self.dft_stg_pool = CONF.san_storage_pool
        # Make an empty string dft_stg_pool equal to None
        # for easier checking later
        if len(self.dft_stg_pool) == 0:
            self.dft_stg_pool = None
        svc_host = CONF.svc_host
        svc_port = CONF.svc_port
        svc_keep_alive = CONF.svc_keep_alive
        svc_user = CONF.svc_login
        svc_key = CONF.svc_private_key
        svc_password = CONF.svc_password
        if (svc_host and svc_port and svc_user and
           (svc_key or svc_password)):
            svc_connection = {SSH_KEY_HOST: svc_host,
                              SSH_PORT: svc_port,
                              SSH_KEEP_ALIVE: svc_keep_alive,
                              SSH_KEY_USERNAME: svc_user}
            if svc_key:
                svc_connection[SSH_KEY_PRIVATE_KEY] = svc_key
            if svc_password:
                svc_connection[SSH_KEY_PASSWORD] = svc_password
        else:
            LOG.info(_("Not enough information provided in the nova.conf "
                     " file to be able to connect to an SVC."))
            raise SVCConnectionInfo

        self._connection = None
        self._svc_connect_data = svc_connection
        self._set_svc_connection(skip_connect=True)

        # Build cleanup translation tables for host names
        invalid_ch_in_host = ''
        for num in range(0, 128):
            ch = str(chr(num))
            if(not ch.isalnum() and ch != ' ' and ch != '.'
               and ch != '-' and ch != '_'):
                invalid_ch_in_host = invalid_ch_in_host + ch

        self._string_host_name_filter = string.maketrans(
            invalid_ch_in_host, '-' * len(invalid_ch_in_host))

        self._unicode_host_name_filter = dict((ord(unicode(char)), u'-')
                                              for char in invalid_ch_in_host)

    @logcall
    def get_active_target_wwpns(self):
        """
        Get storage target WWPNs that are connected to SAN fabric.

        :return: A list of active target WWPNs in the form of uppercase
                 hex string or None
        """
        # lsportfc is supported since SVC f/w version 6.4.0.0
        cmd = 'svcinfo lsportfc -filtervalue status=active:type=fc -delim :'

        output, error = self._svc_command(cmd)
        # output looks like this:
        # id:fc_io_port_id:port_id:type:port_speed:node_id:node_name:WWPN:
        # nportid:status:attachment
        # 0:1:1:fc:8Gb:1:node1:500507680304104A:010200:active:switch
        # 1:2:2:fc:8Gb:1:node1:500507680308104A:010200:active:switch

        if len(output) < 2:
            return None
        if error:
            msg = _("get_active_target_wwpns() failure, cmd=%(cmd)s"
                    " error=%(error)s") % locals()
            LOG.error(msg)
            return None

        target_wwpns = []
        header = output.pop(0).split(':')
        wwpn_idx = header.index(SVC_KEY_PORT_WWPN)

        for line in output:
            info = line.split(':')
            target_wwpns.append(info[wwpn_idx])

        return target_wwpns

    @logcall
    def get_uid_by_vdisk_map(self, wwpns, scsi_id):
        """
        return the vdisk uid based on the initiator's
        vdiskhostmap information

        :param wwpns: initiator's wwpns
        :param scsi_id: vdisk mapped scsi id
        :return vdisk_uid: vdisk uid
        """
        if not wwpns or not scsi_id:
            return None

        host_name = None
        for wwpn in wwpns:
            host_name = self.get_host_name(wwpn)
            if host_name:
                break
        if not host_name:
            return

        cmd = "svcinfo lshostvdiskmap -delim : %s" % host_name
        output, err_output = self._svc_command(cmd)

        if err_output:
            msg = (_("get_uid_by_vdisk_map() failure: svc cmd: %(cmd)s "
                     "error: %(error)s") %
                   {'cmd': cmd,
                    'error': err_output})
            LOG.exception(msg)
            ex_args = {'cmd': cmd, 'e': err_output}
            raise SVCCommandException(**ex_args)

        if len(output) < 2:
            return None

        header = output.pop(0).split(':')
        scsi_id_idx = header.index(SVC_KEY_MAP_SCSI_ID)
        vdisk_uid_idx = header.index(SVC_KEY_VDISK_UID)

        for line in output:
            info = line.split(':')
            if info[scsi_id_idx] == scsi_id:
                return info[vdisk_uid_idx]

        return None

    @logcall
    def get_vdisk_map_by_uid(self, vdisk_uid):
        """
        return the vdisk's hostmap information

        :param vdisk_uid: vdisk_UID to lookup
        :return: host,scsi_id dictionary or None
        """

        if not vdisk_uid:
            msg = _("Invalid parameter: vdisk_uid is None ")
            LOG.exception(msg)
            raise SVCInvalidVdiskUID

        map_info = {}

        cmd = 'svcinfo lshostvdiskmap -delim :'

        output, err_output = self._svc_command(cmd)

        if err_output:
            msg = (_("get_vdisk_map_scsi_id() failure: svc cmd: %(cmd)s"
                     " error: %(error)s") % {'cmd': cmd, 'error': err_output})
            LOG.exception(msg)
            ex_args = {'cmd': cmd, 'e': err_output}
            raise SVCCommandException(**ex_args)

        if len(output) < 2:
            msg = _("_add_svc_hostmap() failed to create "
                    "vdisk host mapping on storage")
            LOG.warn(msg)
            return map_info

        header = output.pop(0).split(':')
        scsi_id_idx = header.index(SVC_KEY_MAP_SCSI_ID)
        vdisk_uid_idx = header.index(SVC_KEY_VDISK_UID)
        hostname_idx = header.index(SVC_KEY_HOST_NAME)

        for line in output:
            info = line.split(':')
            host = info[hostname_idx]
            vdisk_UID = info[vdisk_uid_idx]
            scsi_id = info[scsi_id_idx]

            # we may have one LUN mapped to multiple hosts during LPM
            if vdisk_UID == vdisk_uid:
                map_info[host] = scsi_id

        if len(map_info) == 0:
            map_info = None

        LOG.debug("vdisk hostmap: %(map_info)s"
                  % {'map_info': str(map_info)})
        return map_info

    @logcall
    def get_mdisk_grp_by_size(self, size):
        """
        Function that returns an available mdisk group which fits size

        :param self The object pointer.
        :param size the minimum necessary mdisk size
        :return mdisk name
        """
        cmd = "svcinfo lsmdiskgrp -bytes -filtervalue status=online -delim :"
        output = self._svc_command(cmd)[0]
        listSize = len(output)
        if listSize < 2:
            return None

        header = output.pop(0).split(':')
        mdiskNameIndex = header.index(SVC_KEY_MDISK_GROUP_NAME)
        capacityIndex = header.index(
            SVC_KEY_MDISK_GROUP_FREE_CAPACITY)
        name = None
        for values in output:
            valList = values.split(':')
            if float(size) <= float(valList[capacityIndex]):
                name = valList[mdiskNameIndex]
                break
        return name

    def get_uid(self, volume_name):
        """
        Function that returns unique id of SAN volume given the volume name.
        :param volume_name: string representing the SAN volume name
        :returns: String with unique id.
        """
        output = self._svc_command(
            "svcinfo lsvdisk -filtervalue name=%s -delim :" %
            (volume_name))[0]

        if len(output) != 2:
            return None

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_KEY_VDISK_UID)
        return values[index]

    @logcall
    def get_volume_info(self, uid):
        """
        Function that returns dictionary with volume_info.
        :param self The object pointer.
        :param uid The unique id of the volume.
        :return Dictionary with volume info.
        """
        LOG.debug("Entering")
        cmd = "svcinfo lsvdisk -bytes -filtervalue vdisk_UID=%s -delim :" % uid
        output = self._svc_command(cmd)[0]

        if len(output) != 2:
            raise SVCVolumeNotFound(
                _("Couldn't find volume information for UID %s") % uid)

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_KEY_VDISK_ID)
        diskId = values[index]
        index = header.index(SVC_KEY_VDISK_NAME)
        name = values[index]
        index = header.index(SVC_KEY_VOLUME_GROUP)
        volumeGroup = values[index]
        index = header.index(SVC_KEY_VDISK_CAPACITY)
        capacity = values[index]

        info = {SVC_KEY_VDISK_ID: diskId,
                SVC_KEY_VDISK_NAME: name,
                SVC_KEY_VOLUME_GROUP: volumeGroup,
                SVC_KEY_VDISK_CAPACITY: capacity}

        LOG.debug("Exiting")
        return info

    def get_volume_name(self, uid):
        vol_info = self.get_volume_info(uid)
        vol_name = vol_info.get(SVC_KEY_VDISK_NAME)
        return vol_name

    def _get_new_volume_name(self, volume):
        """
        Function that returns volume name not used in san.
        :param self The object pointer.
        :param volume Reference volume name.
        :return String with new volume name.
        """

        newVolumeName = '%s-%s' % (CONF.san_disk_prefix,
                                   uuid.uuid4().hex)

        return newVolumeName

    def create_new_volume(self, volumeInfo, change_name=True):
        """
        Function that creates new volume.
        :param self The object pointer.
        :param volumeInfo Characteristics of the new volume.
        :return String with new volume name.
        """
        size = volumeInfo.get(SVC_KEY_VDISK_CAPACITY)
        if (change_name):
            new_volume_name = self._get_new_volume_name(
                volumeInfo.get(SVC_KEY_VDISK_NAME))
        else:
            new_volume_name = volumeInfo.get(SVC_KEY_VDISK_NAME)
        if SVC_KEY_VOLUME_GROUP in volumeInfo:
            volumeGroup = volumeInfo.get(SVC_KEY_VOLUME_GROUP)
        elif self.dft_stg_pool:
            volumeGroup = self.dft_stg_pool
        else:
            volumeGroup = self.get_mdisk_grp_by_size(size)

        if volumeGroup is None:
            raise SVCNoSANStoragePoolException

        # iogrp parameter should not use name since it could be
        # customized. It is always safe to use iogrp 0.
        cmd = "svctask mkvdisk -name %s -iogrp 0 -mdiskgrp %s " \
            "-size %s -unit b" % (new_volume_name, volumeGroup, size)

        output, err_output = self._svc_command(cmd)

        volume_uid = self.get_uid(new_volume_name)

        # Check if it got created
        if not volume_uid:
            # The SVC message of out of space is not really user friendly.
            # So, we will manully check whether the pool ran out of space
            free_capacity = self.get_mdisk_grp_size(volumeGroup)

            if float(size) > float(free_capacity):
                ex_args = {'pool_name': volumeGroup,
                           'size': size,
                           'free_capacity': free_capacity}
                raise SVCVolumeGroupOutOfSpace(**ex_args)
            if err_output:
                ex_args = {'new_volume_name': new_volume_name,
                           'err_output': err_output}
                raise SVCVolumeCreationFailed(**ex_args)
            else:
                # failed to create volume but with no error msg
                # really shouldn't hit this condition
                ex_args = {'cmd': cmd,
                           'e': _("No error available")}
                raise SVCCommandException(**ex_args)

        return new_volume_name, volume_uid

    def resize_volume(self, delta_disk, vdisk_name):
        """Resizes a current volume. Only called when expanding volumes

        :param delta_disk: size in GB to expand by
        :param vdisk_name: name of the volume disk
        """
        LOG.debug("Entering")
        cmd = "svctask expandvdisksize -size %s " \
            "-unit b %s" % (delta_disk, vdisk_name)

        output = self._svc_command(cmd)[0]
        LOG.debug("Exiting")

    def vdisk_in_flashcopy(self, diskname):
        """Check to see if vdisk is in flashcopy state

        :param diskname: name of volume disk
        :returns: tuple containing (flashcopy progress,
                        flashcopy mapping id)
        """
        LOG.debug("Entering")
        cmd = ''.join(["svcinfo lsfcmap -filtervalue ",
                       "target_vdisk_name=%s -delim :" % diskname])
        output = self._svc_command(cmd)[0]

        if len(output) != 2:
            return(100, None)

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index('progress')
        progress = values[index]
        index = header.index('id')
        map_id = values[index]

        LOG.debug("Exiting (progress = %s, map_id = %s)" % (progress, map_id))
        return progress, map_id

    def set_fc_map(self, map_id, copyrate=None, autodelete=None,
                   cleanrate=None):

        """Adjust flashcopy settings on the fly

        :param copyrate: copyrate write priority
        :param autodelete: autodelete after flashcopy finishes
        :param cleanrate: clean rate for mapping
        """
        command = []
        # Append argument if it is passed
        command.append('svctask chfcmap')
        if copyrate:
            args = ''.join([" -copyrate %s" % copyrate])
            command.append(args)
        if autodelete:
            args = ''.join([" -autodelete %s" % autodelete])
            command.append(args)
        if cleanrate:
            args = ''.join([" -cleanrate %s" % cleanrate])
            command.append(args)
        command.append(' %s' % map_id)
        cmd = ''.join(command)

        self._svc_command(cmd)

    def start_flash_copy(self, source, target):
        """
        Function that start flash copy of a volume.
        :param self The object pointer.
        :param source Volume to copy from.
        :param target Volume to copy to.
        """
        cmd = "svctask mkfcmap -source %s -target %s " \
            "-copyrate 100 -autodelete -cleanrate 50" % (source, target)
        output = self._svc_command(cmd)[0]

        if len(output) != 1:
            return None

        taskId = re.search("\d+", output[0]).group(0)

        cmd = "svctask startfcmap -prep %s" % (taskId)

        output = self._svc_command(cmd)[0]

    def clone_volume(self, uid, new_name=None):
        """
        Function that clones a volume.
        :param self The object pointer.
        :param uid Unique identifier of volume to clone.
        :param new_name name of new volume to clone
        :return tuple of:
                String with new volume name,
                String with new volume unique identifier.
        """
        volumeInfo = self.get_volume_info(uid)
        if self.dft_stg_pool is not None:
            # Remove the volume group so the default is used.
            del volumeInfo[SVC_KEY_VOLUME_GROUP]

        source_volume_name = volumeInfo.get(SVC_KEY_VDISK_NAME)
        if new_name:
            volumeInfo[SVC_KEY_VDISK_NAME] = new_name
            new_volume_name, volume_uid = self.create_new_volume(volumeInfo,
                                                                 False)
        else:
            new_volume_name, volume_uid = self.create_new_volume(volumeInfo)

        if not source_volume_name or not new_volume_name:
            raise Exception

        self.start_flash_copy(source_volume_name, new_volume_name)

        newVolumeInfo = {
            SVC_KEY_VDISK_UID: volume_uid,
            SVC_KEY_VDISK_NAME: new_volume_name,
            SVC_KEY_VDISK_CAPACITY: volumeInfo[SVC_KEY_VDISK_CAPACITY]
        }

        return newVolumeInfo

    def get_clone_progress(self, sourceUid, targetUid):
        """Function that checks clone operation progress.
        :param sourceUid: Unique identifier of source volume.
        :param targetUid: Unique identifier of target volume.
        :returns: String with progress percentage.
        """
        sourceVolName = self.get_volume_info(sourceUid).get(
            SVC_KEY_VDISK_NAME)
        targetVolName = self.get_volume_info(targetUid).get(
            SVC_KEY_VDISK_NAME)

        cmd = "lsvdiskfcmapcopies -delim : %s" % (sourceVolName)
        output = self._svc_command(cmd)[0]

        if len(output) < 2:
            return None

        header = output.pop(0).split(':')
        targetNameIndex = header.index(
            SVC_KEY_FC_TARGET_VDISK_NAME)
        progresIndex = header.index(SVC_KEY_FC_PROGRESS)
        progress = None

        for values in output:
            valList = values.split(':')
            if valList[targetNameIndex] == targetVolName:
                progress = valList[progresIndex]
                break

        return progress

    def _hostname_prefix(self, hostname_str):
        """Translate connector info to storage system host name.

        Create a storage compatible host name based on the host
        shortname, replacing any invalid characters (at most 55
        Characteristics) and adding a random 8-character suffix to avoid
        collisions. The total length should be at most 63 characters.
        :param hostname_str: host shortname in string format
        :return: generated hostname for storage
        """

        if not hostname_str or len(hostname_str) == 0:
            msg = _("Invalid Hostname: %(hostname_str)s for storage") % \
                locals()
            LOG.exception(msg)
            ex_args = {'hostname': hostname_str}
            raise SVCInvalidHostnameError(**ex_args)
        if isinstance(hostname_str, unicode):
            hostname_str = hostname_str.translate(
                self._unicode_host_name_filter)
        elif isinstance(hostname_str, str):
            hostname_str = hostname_str.translate(
                self._string_host_name_filter)
        else:
            msg = _("Cannot clean host name: %(hostname_str)s for storage") % \
                locals()
            LOG.exception(msg)
            ex_args = {'hostname': hostname_str}
            raise SVCInvalidHostnameError(**ex_args)
        hostname_str = str(hostname_str)
        return hostname_str[:55]

    @logcall
    def create_host(self, wwpns, hostname):
        """
        Create a new host on the storage system
        :param wwpns: The wwpns for the initiator as a hex string
        :param hostname: initiator's hostname.
        :param is_retry: whether this create_host operation is a retry.
                         If so, raising exception if it fails.
        :return: host name defined on SVC storage if successfully created.
        """

        if not wwpns or len(wwpns) == 0 or not hostname or len(hostname) == 0:
            ex_args = {'wwpns': wwpns,
                       'hostname': hostname}
            raise SVCCreateHostParameterError(**ex_args)

        ports = ':'.join(wwpns)
        # get the host shortname.
        hostname_str = hostname.split('.')[0]
        LOG.debug("enter: create_host(): wwpns=%(wwpns)s"
                  " hostname=%(hostname)s"
                  % {'wwpns': ports, 'hostname': hostname_str})

        rand_id = str(random.randint(0, 99999999)).zfill(8)
        host_name = '%s-%s' % (self._hostname_prefix(hostname_str), rand_id)

        cmd = 'mkhost -name %(host_name)s -hbawwpn %(ports)s -force' % locals()

        output, err_output = self._svc_command(cmd)

        if err_output:
            # err_output should be a list type
            if isinstance(err_output, types.ListType):
                err_msg = err_output[0]
            else:
                err_msg = err_output
            err_code = err_msg.split()[0]

            if err_code and err_code == 'CMMVC6035E':
                # host has been defined on the storage, but we don't see it.
                # return None and ask caller to run cfgdev to relogin to SAN
                # and retry get_host_from_wwpns().
                return None

            msg = (_("create_host() failure cmd=%(cmd)s, error:%(err_output)s."
                     " Make sure host and storage are zoned properly and check"
                     " SAN fabric connectivity") % locals())

            LOG.exception(msg)
            ex_args = {'host_name': hostname_str,
                       'err_output': err_output}
            raise SVCCreateHostFailed(**ex_args)

        return host_name

    def get_host_name(self, wwpn):
        """
        Function that returns a host name.
        :param self The object pointer.
        :param wwpn Fiber channel port of host
        :return String with host name or None if not found
        """
        cmd = "svcinfo lsfabric -wwpn=%s -delim :" % (wwpn)
        output = self._svc_command(cmd)[0]

        if len(output) < 2:
            return None

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_KEY_HOST_NAME)
        name = values[index]
        return name

    def get_host_id(self, hostName):
        """
        Function that returns host id.
        :param self The object pointer.
        :param hostName Name of host to retrieve id from.
        :return String with host id.
        """
        cmd = "svcinfo lshost -filtervalue name=%s -delim :" % (hostName)
        output = self._svc_command(cmd)[0]

        if len(output) != 2:
            return None

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_KEY_HOST_ID)
        hostId = values[index]
        return hostId

    def get_cluster_id(self):
        """
        Function return cluster ID

        :param self: the object pointer
        """
        cmd = "svcinfo lscluster -delim :"

        output = self._svc_command(cmd)[0]

        if len(output) != 2:
            return None

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_CLUSTER_ID)
        cluster_id = values[index]
        return cluster_id

    @logcall
    def get_host_from_wwpns(self, wwpns):
        """
        Search through a list of WWPNs for a host name
        :param wwpns: list of WWPNs to search
        :returns: tuple containing
                  (hostname, wwpn that returned a host name)
        """

        hostName = None
        found_wwpn = None
        for wwpn in wwpns:
            hostName = self.get_host_name(wwpn)
            if hostName:
                found_wwpn = wwpn
                break

        return (hostName, found_wwpn)

    def map_volume(self, initiator, volume_name):
        """
        Add a hostmapping between a host and volume
        :param initiator: host name of initiator (place to attach volume)
        :param volume_name: the volume name to map
        """
        hostID = self.get_host_id(initiator)
        uid = self.get_uid(volume_name)
        volInfo = self.get_volume_info(uid)
        volID = volInfo.get(SVC_KEY_VDISK_ID)

        cmd = "svctask mkvdiskhostmap -host %s -force %s" % (hostID, volID)
        self._svc_command(cmd)

    def unmap_volume(self, host_name, volume_name):
        """
        Removes a hostmap between a volume from a host.
        :param host_name: The attached host.
        :param volume_name: The volume name.
        """
        cmd = "svctask rmvdiskhostmap -host %s %s" % \
            (host_name, volume_name)
        self._svc_command(cmd)

    def get_mapped_host(self, vol_name):
        """
        Function that obtains name of host mapped to volume.
        :param self The object pointer.
        :param vol_name: volume name.
        :return String with host name
        """
        cmd = "svcinfo lsvdiskhostmap -delim : %s" % (vol_name)
        output = self._svc_command(cmd)[0]

        if len(output) != 2:
            return None

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_KEY_MAPPED_HOST_NAME)
        hostName = values[index]

        return hostName

    def remove_fcmapping(self, uid):
        """
        Function that deletes flash copy mapping, if any, for a volume.
        :param self The object pointer.
        :param uid Unique identifier of volume.
        """
        volInfo = self.get_volume_info(uid)
        volName = volInfo.get(SVC_KEY_VDISK_NAME)
        lsfcmap_cmd = ("lsfcmap -delim : -filtervalue target_vdisk_name=%s"
                       % (volName))
        output = self._svc_command(lsfcmap_cmd)[0]

        if len(output) != 2:
            return

        header = output[0].split(':')
        values = output[1].split(':')
        index = header.index(SVC_KEY_FC_MAP_ID)
        mapId = values[index]
        index = header.index(SVC_KEY_STATUS)
        status = values[index]

        if status != 'stopped':
            cmd = "svctask stopfcmap %s" % (mapId)

            self._svc_command(cmd)
            # sleep for 1 seconds before we check the status
            time.sleep(1)
            # query for status every 30 seconds
            # until the status is at stopped or
            # hit the timeout
            remaining_time = CONF.svc_rmfcmap_timeout
            sleep_period = CONF.svc_stopfcmap_sleep_period
            while remaining_time >= 0:
                output = self._svc_command(lsfcmap_cmd)[0]
                # if flash copy is completed and removed,
                # set the mapId to None so we won't try to remove it
                if len(output) != 2:
                    mapId = None
                    break
                header = output[0].split(':')
                values = output[1].split(':')
                index = header.index(SVC_KEY_STATUS)
                status = values[index]
                if status == 'stopped':
                    break

                # decrement remaining_time
                remaining_time -= sleep_period

                if remaining_time < 0:
                    break
                time.sleep(sleep_period)

        # if we are not able to stop the flash copy within the timeout
        # raise exception
        if status != 'stopped' and mapId is not None:
            raise SVCFlashCopyStopFailed(vuid=uid)

        if mapId:
            cmd = "svctask rmfcmap -force %s" % (mapId)
            self._svc_command(cmd)

    def delete_volume(self, uid):
        """
        Function that deletes volume from san.
        :param self The object pointer.
        :param uid Unique identifier of volume.
        """
        try:
            volInfo = self.get_volume_info(uid)
        except SVCVolumeNotFound as ex:
            LOG.warn(_("No volume with UID %s found.") % uid)
            # assume deleted if not found
            return

        volID = volInfo.get(SVC_KEY_VDISK_ID)
        self.remove_fcmapping(uid)
        cmd = "svctask rmvdisk -force %s" % (volID)
        self._svc_command(cmd)

    @logcall
    def get_mdisk_grp_size(self, mdiskgrp):
        """
        Function that returns an available mdisk group size

        :param self The object pointer.
        :param mdiskgrp name
        :return float representation of free capacity in bytes
        """
        cmd = ("svcinfo lsmdiskgrp -bytes -filtervalue name=%s -delim :"
               % mdiskgrp)
        output = self._svc_command(cmd)[0]
        listSize = len(output)
        if listSize < 2:
            return None

        header = output.pop(0).split(':')
        capacityIndex = header.index(
            SVC_KEY_MDISK_GROUP_FREE_CAPACITY)

        valList = output.pop(0).split(':')
        return valList[capacityIndex]

    def __del__(self):
        """
        The destructor. Will close all active sessions and connection when ref
        count reaches 0
        :param self The object pointer.
        """
        try:
            self._connection.get_transport().close()
            LOG.debug("Closed SVC SSH transport.")
        except Exception:
            LOG.debug("No SVC SSH transport to close.")
        try:
            self._connection.close()
            LOG.debug("Closed SVC SSH connection.")
        except Exception:
            LOG.debug("No SSH connection to close.")

    def _set_svc_connection(self, skip_connect=False):
        if not self._connection:
            self._connection = self._svc_connect(self._svc_connect_data,
                                                 skip_connect)

    def _svc_connect(self, connectionDic, skip_connect=False):
        """
        Method to connect to remote system using ssh protocol.
        It support both password and key authentication

        :param connectionDic: A dictionary with connection info
        :returns: Active ssh connection
        """
        max_retries = CONF.svc_ssh_connect_retries

        for retry_count in range(max_retries):
            try:
                ssh = paramiko.SSHClient()
                ssh.load_host_keys(CONF.svc_known_hosts_path)
                ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
                port = connectionDic[SSH_PORT]
                if not skip_connect:
                    if SSH_KEY_PRIVATE_KEY in connectionDic:
                        keyPath = connectionDic[SSH_KEY_PRIVATE_KEY]
                        key_file = os.path.expanduser(keyPath)
                        # Paramiko doesn't support DSA keys
                        private_key = paramiko.RSAKey.from_private_key_file(
                            key_file)
                        ssh.connect(connectionDic[SSH_KEY_HOST],
                                    username=connectionDic[SSH_KEY_USERNAME],
                                    port=port, pkey=private_key)
                    else:
                        ssh.connect(connectionDic[SSH_KEY_HOST],
                                    username=connectionDic[SSH_KEY_USERNAME],
                                    password=connectionDic[SSH_KEY_PASSWORD],
                                    port=port)

                    # send TCP keepalive packets every 20 seconds
                    transport = ssh.get_transport()
                    transport.set_keepalive(int(connectionDic[SSH_KEEP_ALIVE]))

                    if not transport.is_active():
                        LOG.debug("svc connection failed with %s"
                                  % connectionDic[SSH_KEY_HOST])
                    else:
                        LOG.debug("svc connection established with %s"
                                  % connectionDic[SSH_KEY_HOST])

                # add connection info for re-establishing if broken
                ssh.connDict = connectionDic

                return ssh
            except SSHException as ssh:
                if((retry_count < max_retries - 1) and
                   ('Error reading SSH protocol banner' in ssh.message)):
                    LOG.debug("SSH connection to SVC failed. Retrying.")
                else:
                    LOG.exception(_("SSH Exception while trying to connect"))
                    raise
            except Exception:
                LOG.exception(_("Unknown exception while trying to connect"))
                raise

    def _check_connection(self):
        for attempt in range(5):
            try:
                self._validate_transport()
                transport = self._connection.get_transport()
                if transport and transport.is_active():
                    return
                else:
                    LOG.warn(_('Transport missing or not active: %s')
                             % attempt)
            except Exception:
                # Sleep and re-attempt
                time.sleep(5)
        LOG.warn(_('Max conn loop exceeded to SVC.'))

        return

    def _validate_transport(self):

        # Get the transport so we can see if it's active
        transport = self._connection.get_transport()

        try:
            # if we have a dead connection
            # build a new one and return
            if not transport or (not transport.is_active()):
                self._connection = self._svc_connect(self._svc_connect_data)
                transport = self._connection.get_transport()
                LOG.debug("No SVC SSH transport or transport is not active, "
                          "reconnected.")

        except Exception:
            LOG.exception(_('Connection error connecting to SVC.'))
            raise Exception(_("Error connecting to SVC"))

        return

    def _svc_command(self, command):
        """
        Method to execute command to remote system using ssh protocol.
        It support both password and key authentication

        :param connection: An active ssh connection
        :param command: Command text to execute
        :returns: List of lines returned on stdout
        """
        self._set_svc_connection()
        self._check_connection()

        try:
            LOG.debug("Running cmd: %s" % command)
            stdin, stdout, stderr = self._connection.exec_command(command)
            output = stdout.read().splitlines()
            err_output = stderr.read().splitlines()
            LOG.debug("SVC command [%s] returned stdout: %s stderr: %s"
                      % (command, output, err_output))
            if err_output:
                LOG.warn(_("Command %(cmd)s returned with stderr: %(err)s") %
                         dict(cmd=command, err=err_output))
            return (output, err_output)
        except Exception, e:
            ex_args = {'cmd': command,
                       'e': e}
            LOG.exception(_("Error while running command %(cmd)s: %(e)s") %
                          ex_args)
            raise SVCCommandException(**ex_args)


class SVCCommandException(exception.NovaException):
    msg_fmt = _("Exception returned from command '%(cmd)s': %(e)s")


class SVCVolumeNotFound(Exception):
    pass


class SVCConnectionInfo(Exception):
    msg_fmt = _('Incomplete SVC connection information')


class SVCNoSANStoragePoolException(exception.NovaException):
    msg_fmt = _("No suitable SAN storage pool was found for volume creation.")


class SVCVolumeCreationFailed(exception.NovaException):
    msg_fmt = _("Unable to create new SAN volume '%(new_volume_name)s' "
                "because of the following error: '%(err_output)s'")


class SVCVolumeGroupOutOfSpace(exception.NovaException):
    msg_fmt = _("Insufficient amount of available storage in SAN storage pool "
                "'%(pool_name)s'. Storage requested: '%(size)s'; available "
                "capacity: '%(free_capacity)s'")


class SVCCreateHostFailed(exception.NovaException):
    msg_fmt = _("Unable to create host %(host_name)s "
                "because of the following error: %(err_output)s")


class SVCCreateHostParameterError(exception.NovaException):
    msg_fmt = _("Invalid parameter for create_host: wwpns=%(wwpns)s, "
                "hostname=%(hostname)s")


class SVCInvalidHostnameError(exception.NovaException):
    msg_fmt = _("Invalid host name for storage: hostname=%(hostname)s")


class SVCInvalidVdiskUID(exception.NovaException):
    msg_fmt = _("Invalid vdisk_UID : None")


class SVCFlashCopyStopFailed(exception.NovaException):
    msg_fmt = _("Failed to stop flash copy and "
                " delete the SAN volume '%(vuid)s'")
