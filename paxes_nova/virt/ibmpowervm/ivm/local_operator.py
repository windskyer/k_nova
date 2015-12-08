'''
Created on Feb 27, 2015

@author: root
'''

from paxes_nova.virt.ibmpowervm.ivm.operator import *
from paxes_nova.virt.ibmpowervm.ivm import blockdev
from paxes_nova.virt.ibmpowervm.ivm.local_blockdev import *
from paxes_nova.virt.ibmpowervm.ivm import command
from nova.openstack.common.lockutils import synchronized
from paxes_nova.virt.ibmpowervm.ivm import exception
from nova.openstack.common import processutils
import time

from oslo.config import cfg
from nova.virt.virtapi import VirtAPI
from boto.cloudformation.stack import Output
import cmd
from cmd import Cmd
CONF = cfg.CONF


class PowerVMSANLocalOperator(PowerVMSANOperator, PowerVMBaseOperator):
    def __init__(self, virtapi):
        operator = IVMLocalOperator()
        disk_adapter = SANDiskLocalAdapter(operator)
        PowerVMBaseOperator.__init__(self, virtapi, operator, disk_adapter)

    @logcall
    def _check_can_live_migrate_destination(self, ctxt, instance_ref,
                                            block_migration=False,
                                            disk_over_commit=False):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results

        :param ctxt: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest: destination host
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit
        """
        # Get host information and add to dest_check_data
        # migrate_data dictionary
        #conn_data = self._operator.connection_data
        conn_data = common.Connection(CONF.powervm_mgr,
                     CONF.powervm_mgr_user,
                     CONF.powervm_mgr_passwd,
                     CONF.powervm_mgr_port)
        hostname = conn_data.host
        username = conn_data.username
        port = conn_data.port
        wwpns = self._operator.get_wwpns()

        # Add the password outside of the 'migrate_data' dictionary.
        # This ensures it is only "seen" by the source validation, and will
        # not propigate through all of the migration methods.
        # The password must be encrypted via STG defect 22143
        #encrypted_pwd = EncryptHandler().encode(conn_data.password) ###xuejinguo comment

        # Get name of management system from destination
        man_sys_name = self._operator.get_management_sys_name()

        # [10227: Ratnaker]Changing host to host_display_name to be
        # consistent with gui messages and for usability.
        # This property change reflects only in notifications
        migrate_data = {'dest_hostname': hostname,
                        'dest_sys_name': CONF.host_display_name,
                        'dest_username': username,
                        'dest_password': conn_data.password, #encrypted_pwd xuejinguo
                        'dest_port': port,
                        'man_sys_name': man_sys_name,
                        'dest_wwpns': wwpns}

        return migrate_data

    @logcall
    def _check_can_live_migrate_source(self, ctxt, instance_ref,
                                       dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        #pvm_op = self._operator
        #conn_data = pvm_op.connection_data
        conn_data = common.Connection(CONF.powervm_mgr,
                     CONF.powervm_mgr_user,
                     CONF.powervm_mgr_passwd,
                     CONF.powervm_mgr_port)
        instance_name = instance_ref['name']
        source_wwpns = self._operator.get_wwpns()

        # Verify the destination host does not equal the source host
        source_host = conn_data.host
        if (source_host == dest_check_data['dest_hostname']):
            error = (_("Cannot live migrate %s because the " +
                       "destination server is the same as the source server") %
                     instance_name)
            LOG.exception(error)
            raise exception.IBMPowerVMLiveMigrationFailed(error)

        # check the default_ephemeral_device in the instance_ref
        # Paxes used default_ephemeral_device to store the
        # VM boot disk's UID. For Paxes 1.1.0.0, only
        # one ephemeral disk is supported per VM.
        ephemeral_uid = instance_ref['default_ephemeral_device']
        if not ephemeral_uid:
            error = (_("Cannot live migrate %(instance_name)s because the "
                       "ephemeral disk is not defined in the instance") %
                     locals())
            LOG.exception(error)
            raise exception.IBMPowerVMLiveMigrationFailed(error)

        # Paxes will establish hostmapping and set no_reserve for the
        # VM's attaced volumes on the target during live partition
        # migration. There are two type of volumes supported by Paxes
        # 1. ephemeral volume:  VM's boot disk(only one for Paxes)
        # 2. attached cinder volumes.
        # Any other volumes that user manually attached to the VM
        # will not be covered by Paxes LPM pre_live_migration code.
        # User has to manually establish hostmapping and set no_reserve
        # for those non-Paxes managed volumes before LPM.

        # Add connection data to inner dictionary
        dest_check_data['migrate_data'] = {}
        copy_keys = ['dest_hostname', 'dest_username',
                     'dest_password', 'dest_wwpns']
        for dest_key in copy_keys:
            dest_check_data['migrate_data'][dest_key] = \
                dest_check_data[dest_key]

        dest_check_data['migrate_data']['source_wwpns'] = source_wwpns

        # Extract destination information and save to global
        # dictionary to be accessed by _live_migration on source
        sys_name = dest_check_data['man_sys_name']
        dest_host = dest_check_data['dest_hostname']
        dest_user = dest_check_data['dest_username']
        dest_wwpns = dest_check_data['dest_wwpns']
        inst_name = instance_ref['name']

        live_migration_data[inst_name] = {}
        inst_migration_data = {'man_sys_name': sys_name,
                               'dest': dest_host,
                               'user': dest_user,
                               'dest_wwpns': dest_wwpns,
                               'source_wwpns': source_wwpns}
        live_migration_data[inst_name] = inst_migration_data

        # The password is stored encrypted (STG defect 22143)
        encrypted_pwd = dest_check_data.get('dest_password')
        destination_password = encrypted_pwd
        #destination_password = EncryptHandler().decode(encrypted_pwd) #xuejinguo comment

        # Create Connection objects for both source & destination
        dest_pvm_conn = common.Connection(dest_host,
                                          dest_user,
                                          destination_password,
                                          dest_check_data['dest_port'])

        # dest_check_data will pass to pre_live_migration() running on
        # the target host. No need to collect attached cinder volume
        # here. It will be passed to pre_live_migration in block_device_info
        # parameter.

        # Make sure sshkey exchange is setup correctly between source and
        # destination host involved in LPM.
        try:
#             common.ensure_vios_to_vios_auth(
#                 self._operator.connection_data.host,
#                 dest_pvm_conn,
#                 self._operator.connection_data)
            common.ensure_vios_to_vios_auth(
                conn_data.host,
                dest_pvm_conn,
                conn_data)
            # Make sure sshkey exchange is established between source
            # and destination hosts
            LOG.debug("Setup sshkey exchange between src_host: "
                      "%(source_host)s and dest_host: %(dest_host)s" %
                      locals())
        except Exception:
            with excutils.save_and_reraise_exception():
                msg = (_("Unable to exchange sshkey between source host: "
                         "%(source_host)s and destination host: %(dest_host)s")
                       % locals())
                LOG.exception(msg)

        dest_check_data['refresh_conn_info'] = True   # Issue 6111
        return dest_check_data


class BaseLocalOperator(BaseOperator):
    def __init__(self):
        self._connection = None
        self._lpar_name_map = dict()

    def run_command(self, cmd, conn=None, check_exit_code=True):
        def _exec_local_command():
            if 'ioscli' in cmd:
                host = CONF.host  # self._host
                @synchronized(host, 'pvm-odm-lock', False)
                def _run_local_odm_commands(host):
                    cmdin, cmdout, cmderr = None, None, None
                    for odm_retries in range(2):
                        cmdout, cmderr = processutils.execute('su', '-', 'padmin', '-c', *cmd, check_exit_code=check_exit_code)
                        # cmdout, cmderr = processutils.execute('su', '-', 'padmin', check_exit_code=check_exit_code, process_input = cmd)
                        if cmderr:
                            if (any('0514-516' in err for err in cmderr) or
                                any('Please retry the command later' in err
                                    for err in cmderr)):
                                if(odm_retries < 2):
                                    time.sleep(30)
                                continue
                        return cmdout, cmderr
                    return cmdout, cmderr
                cmdout, cmderr = _run_local_odm_commands(host)
            else:
                cmdout, cmderr = processutils.execute('su', '-', 'padmin' , '-c', *cmd, check_exit_code=check_exit_code)
                # cmdout, cmderr = processutils.execute('su', '-', 'padmin', check_exit_code=check_exit_code, process_input = cmd)

            if cmdout is not None:
                cmdout = cmdout.split('\n')
                cmdout.pop()

            if len(cmdout) > 0:
                return_code = cmdout.pop()

            if return_code and int(return_code) == 0:
                return cmdout

            raise_exception = check_exit_code
            if raise_exception:
                ex_args = {'command': ' '.join(cmd),
                           'error': cmderr,
                           'stdout': cmdout,
                           'exit_code': int(return_code)}
                raise exception.IBMPowerVMCommandFailed(**ex_args)

            LOG.debug(_("Command: %(cmd)s") % {'cmd': ' '.join(cmd)})
            LOG.debug(_("Exit Code: %(exit_code)s") % {'exit_code': int(return_code)})
            LOG.debug(_("Stdout: %(out)r") % {'out': cmdout})
            LOG.debug(_("Stderr: %(err)r") % {'err': cmderr})
            return None
#         try:
#             for num_retries in range(2):
#                 if num_retries > 0:
#                     LOG.debug("vios local command retried %(num_retries)s with command "
#                               "%(command)s" % locals())
#         if ';echo $?' not in cmd:
#             cmd += ";echo $?"
        cmd = cmd.split()
        cmd.append(';echo')
        cmd.append('$?')
        return _exec_local_command()

    def _decompress_image_file(self, file_path, outfile_path):
        command = "/usr/bin/gunzip -c %s > %s" % (file_path, outfile_path)
        self.run_vios_command_as_root_with_shell(command)

        # Remove compressed image file
        command = "/usr/bin/rm %s" % file_path
        self.run_vios_command_as_root(command)

        return outfile_path

    def run_vios_command_as_root(self, command, check_exit_code=True):
        cmdout = None
        cmderr = None
        cmd = command.split()
        try:
            cmdout, cmderr = processutils.execute(*cmd, check_exit_code=check_exit_code)
        except Exception:
            LOG.exception(_('Problem while command execution '))
            raise exception.IBMPowerVMCommandFailed(command)
        if cmderr:
            LOG.debug("Found error stream for command \"%(command)s\":"
                      " %(error_text)s",
                      {'command': command, 'error_text': cmderr})
        if cmdout is not None:
            cmdout = cmdout.split('\n')
        return cmdout

    def run_vios_command_as_root_with_shell(self, command, check_exit_code=True):
        try:
            cmdout, cmderr = processutils.execute(command, check_exit_code=check_exit_code, shell=True)
        except Exception:
            raise exception.IBMPowerVMCommandFailed(command)
        if cmderr:
            LOG.debug("Found error stream for command \"%(command)s\":"
                      " %(error_text)s",
                      {'command': command, 'error_text': cmderr})
        return (cmdout, cmderr)


class IVMLocalOperator(IVMOperator, BaseLocalOperator):
    def __init__(self):
        self.command = command.IVMCommand()
        BaseLocalOperator.__init__(self)

    def run_interactive(self, commands, conn=None):
        new_cmd = ''
        for item in commands:
            if item != 'oem_setup_env' and item != 'exit':
                new_cmd = item
        new_cmd_lst = new_cmd.split()
        if new_cmd_lst:
            cmdout, cmderr = processutils.execute(*new_cmd_lst, check_exit_code=True)
        if cmdout is not None:
            cmdout = cmdout.split('\n')
            cmdout.pop()
        return cmdout

    @logcall
    def get_wwpns(self):
        commands = ['oem_setup_env', 'lscfg -vp -l fcs*', 'exit']
        wwpns = []
        output = self.run_interactive(commands)
        for out in output:
            match = re.search(r'\bNetwork Address[\.]+([0-9A-F]{16})$', out)
            if not match:
                continue
            wwpns.append(match.group(1))
        return wwpns


class PowerVMLocalDiskLocalOperator(PowerVMLocalDiskOperator, PowerVMBaseOperator):
    def __init__(self, virtapi):
        self._ld_ivm_operator = LocalDiskIVMLocalOperator(virtapi)
        self._disk_adapter = PowerVMLocalVolumeLocalAdapter(self._ld_ivm_operator)
        PowerVMBaseOperator.__init__(self, virtapi, self._ld_ivm_operator, self._disk_adapter)
        
    @logcall
    @synchronized('odm_lock', 'pvm-')
    def attach_volume(self, connection_info, instance, mountpoint):
        try:
            volume_data = connection_info['data']

            LOG.info(_("Attaching volume_name %s") % volume_data['volume_name'])
            # Before run run_cfg_dev, check whether there is any
            # stale disk with the same LUN id. If so, remove it.

            self._disk_adapter.handle_stale_disk(volume_data)

            aix_conn = self._operator.get_volume_aix_conn_info(volume_data)
            attach_info = self._operator.get_devname_by_aix_conn(aix_conn)

            LOG.debug("device to attach: %s" % attach_info['device_name'])

            if CONF.local_or_cinder:
                if volume_data.has_key('is_boot_volume'):
                    attach_info['is_boot_volume'] = volume_data['is_boot_volume']
                    self._disk_adapter.copy_image_file_to_cinder_lv(instance['image_ref'], 
                                                            attach_info)
            lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
            vhost = self._operator.get_vhost_by_instance_id(lpar_id)
            self._operator.attach_disk_to_vhost(attach_info, vhost)
        except Exception as e:
            volume_name = connection_info['data']['volume_name']
            raise exception.\
                IBMPowerVMVolumeAttachFailed(instance,
                                             volume_name,
                                             e)

        
    def detach_volume(self, connection_info, instance, mountpoint):

        volume_data = connection_info['data']

        aix_conn = self._operator.get_volume_aix_conn_info(volume_data)

        if not aix_conn:
            LOG.error(_("No device to detach from volume data:%s") %
                      volume_data)
            return
        LOG.info(_('Detach volume_uuid: %s') % volume_data['volume_id'])
        # We need to find the hdisk name to pass to detach
        # If we don't find it, there's nothing to detach
        try:
            volume_info = self._operator.get_devname_by_aix_conn(aix_conn)
            self._disk_adapter.detach_volume_from_host(volume_info)
        except Exception:
            LOG.info(_("Could not find conn to hdisk mapping, conn:%s") %
                     aix_conn)
            
class LocalDiskIVMLocalOperator(LocalDiskIVMOperator, IVMLocalOperator):
    def __init__(self, virtapi):
        self._virtapi = virtapi
        IVMLocalOperator.__init__(self)
   
            
class ISCSIIVMLocalOperator(ISCSIIVMOperator,IVMLocalOperator):
    def __init__(self,virtapi):
        self._virtapi = virtapi
        IVMLocalOperator.__init__(self)
        

class PowerVMISCSIDiskLocalOperator(PowerVMISCSIDiskOperator, PowerVMBaseOperator):
    def __init__(self,virtapi):
        self._initiator = None
        operator = ISCSIIVMLocalOperator(virtapi)
        disk_adapter = ISCSIDiskLocalAdapter(operator)
        PowerVMBaseOperator.__init__(self, virtapi, operator, disk_adapter)
        
class BaseOperatorFactory(object):
    @staticmethod
    def getOperatorInstance(optype, virtapi):
        if optype == 'san':
            return PowerVMSANLocalOperator(virtapi)
        elif optype == 'iscsi':
            return PowerVMISCSIDiskLocalOperator(virtapi)
        elif optype == 'local':
            return PowerVMLocalDiskLocalOperator(virtapi)
        else:
            LOG.exception(_('Unknown Driver type : %s') % optype)
            raise
