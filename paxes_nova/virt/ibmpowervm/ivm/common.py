#
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import uuid
import paramiko
import pexpect
import time

from paxes_nova import _

from nova.openstack.common import log as logging
from nova.openstack.common.lockutils import synchronized
from nova.openstack.common import processutils

from oslo.config import cfg

from paramiko import SSHException

from paxes_nova.virt.ibmpowervm.common.connection import SSHConnection
from paxes_nova.virt.ibmpowervm.ivm import constants
from paxes_nova.virt.ibmpowervm.ivm import exception


LOG = logging.getLogger(__name__)

ibmpowervm_opts = [
    cfg.IntOpt('ibmpowervm_ssh_cmd_retry_num',
               default=2,
               help='Number of retries when SSH errors are encountered.'),
    cfg.IntOpt('ibmpowervm_odm_cmd_retry_num',
               default=1,
               help='Number of retries when odm errors are encountered.')
]

CONF = cfg.CONF
CONF.register_opts(ibmpowervm_opts)
# Maximum wait time to complete the transfer
# Using 999999 as the image file taking more than 2hours
# if paxes and host are in different subnet
MAX_TRANSFER_TIME = 999999


class Connection(object):

    def __init__(self, host, username, password, port=22, keyfile=None):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.keyfile = keyfile


def ssh_connect(connection):
    """Method to connect to remote system using ssh protocol.

    :param connection: a Connection object.
    :returns: paramiko.SSHClient -- an active ssh connection.
    :raises: PowerVMConnectionFailed
    """
    try:
        ssh = SSHConnection(connection.host, connection.port,
                            connection.username, connection.password,
                            connection.keyfile,
                            constants.IBMPOWERVM_CONNECTION_TIMEOUT,
                            CONF.ibmpowervm_known_hosts_path)
        ssh.open_connection()

        # send TCP keepalive packets every 20 seconds
        ssh.set_keep_alive(20)

        return ssh
    except Exception:
        LOG.exception(_('SSH Connection error while connection to host'))
        raise exception.IBMPowerVMConnectionFailed()


def check_connection(conn):

    transport = conn.get_transport()
    conn_data = conn.conn_data
    try:
        # if we have a dead connection
        # build a new one and return

        if (not transport) or (not transport.is_active()):
            conn = ssh_connect(conn_data)
            conn.conn_data = conn_data
    except Exception:
        # try to make a connection still
        time.sleep(5)
        conn = ssh_connect(conn_data)
        conn.conn_data = conn_data

    return conn


def ssh_command_as_root(ssh_connection, cmd, check_exit_code=True):
    """Method to execute remote command as root.

    :param connection: an active paramiko.SSHClient connection.
    :param command: string containing the command to run.
    :returns: Tuple -- a tuple of (stdout, stderr)
    :raises: processutils.ProcessExecutionError
    """
    LOG.debug('Running cmd (SSH-as-root): %s' % cmd)
    chan = ssh_connection.open_session()
    # This command is required to be executed
    # in order to become root.
    chan.exec_command('oem_setup_env')
    bufsize = -1
    stdin = chan.makefile('wb', bufsize)
    stdout = chan.makefile('rb', bufsize)
    stderr = chan.makefile_stderr('rb', bufsize)
    # We run the command and then call 'exit' to exit from
    # super user environment.
    stdin.write('%s\n%s\n' % (cmd, 'exit'))
    stdin.flush()
    exit_status = chan.recv_exit_status()

    # Lets handle the error just like processutils.ssh_execute does.
    if exit_status != -1:
        LOG.debug('Result was %s' % exit_status)
        if check_exit_code and exit_status != 0:
            # TODO(mikal): I know this is weird, but it needs to be consistent
            # with processutils.execute. I will move this method to oslo in
            # a later commit.
            raise processutils.ProcessExecutionError(exit_code=exit_status,
                                                     stdout=stdout,
                                                     stderr=stderr,
                                                     cmd=''.join(cmd))

    return (stdout, stderr)


def scp_command(connection, local_path, remote_path, scp_operation):
    """Method to transfer a file via scp command.

    :param connection: a Connection object.
    :param local_path: path to source file If scp_operation='put'
                       path to destination If scp_operation='get'
    :param remote_path: path to destination If scp_operation='put'
                       path to source file If scp_operation='get'
    :param scp_operation: operation to perform (PUT or GET)
    :raises: IBMPowerVMSCPTransferFailed
    """
    try:
        scp_cmd = '/usr/bin/scp -o StrictHostKeyChecking=yes' + \
            ' -o PubkeyAuthentication=no' + \
            ' -o UserKnownHostsFile=%s' % CONF.ibmpowervm_known_hosts_path
        if scp_operation.lower() == 'put':
            scp_cmd = '{0} {4} {1}@{2}:{3}'.format(scp_cmd,
                                                   connection.username,
                                                   connection.host,
                                                   remote_path, local_path)
        elif scp_operation.lower() == 'get':
            scp_cmd = '{0} {1}@{2}:{3} {4}'.format(scp_cmd,
                                                   connection.username,
                                                   connection.host,
                                                   remote_path, local_path)

        p = pexpect.spawn(scp_cmd)
        result = p.expect(['[pP]assword:', pexpect.EOF])
        if result == 0:
            p.sendline(connection.password)
            p.expect(pexpect.EOF, timeout=MAX_TRANSFER_TIME)
        elif result == 1:
            msg = _("Error:Connection to {0}  "
                    "host is timed out".format(connection.host))
            LOG.error(msg)
            raise Exception(msg)
        else:
            msg = _("Error:Failed to connect to host {0} "
                    "host".format(connection.host))
            raise Exception(msg)
    except pexpect.TIMEOUT:
        p.close()
        msg = _("File could not be transferred within the time")
        raise exception.IBMPowerVMSCPTransferFailed(source_path=local_path,
                                                    dest_path=remote_path,
                                                    error_msg=msg)
    except Exception as e:
        LOG.error(_('File transfer to PowerVM manager failed'))
        raise exception.IBMPowerVMSCPTransferFailed(source_path=local_path,
                                                    dest_path=remote_path,
                                                    error_msg=e)


def aix_path_join(path_one, path_two):
    """Ensures file path is built correctly for remote UNIX system

    :param path_one: string of the first file path
    :param path_two: string of the second file path
    :returns: a uniform path constructed from both strings
    """
    if path_one.endswith('/'):
        path_one = path_one.rstrip('/')

    if path_two.startswith('/'):
        path_two = path_two.lstrip('/')

    final_path = path_one + '/' + path_two
    return final_path


def ssh_command(connection, command, log_warning=True):
    """
    Method to execute command to remote system using ssh protocol.
    It support both password and key authentication

    :param connection: An active ssh connection
    :param command: Command text to execute
    :returns: List of lines returned on stdout
    """

    def _exec_ssh_cmd():
        """
        This method is created to allow retry within
        the ssh_command.
        It also check for odm commands and synchronize it
        """
        # check if the command is an ODM command
        if 'ioscli' in command:
            # conn_data = connection.conn_data
            host = connection.get_hostname()

            @synchronized(host, 'pvm-odm-lock')
            def _run_odm_commands(host):
                # declare the varibles
                cmdin, cmdout, cmderr = None, None, None
                for odm_retries in range(CONF.ibmpowervm_odm_cmd_retry_num):
                    cmdin, cmdout, cmderr = connection.exec_command(command)
                    if cmderr:
                        # if cmderr contains 0514-516 or retry
                        # it means that Device configuration database lock
                        # service timed out. Please retry the command later
                        if (any('0514-516' in err for err in cmderr) or
                            any('Please retry the command later' in err
                                for err in cmderr)):
                            if(odm_retries <
                               CONF.ibmpowervm_odm_cmd_retry_num):
                                time.sleep(30)
                            continue
                    return cmdin, cmdout, cmderr

                return cmdin, cmdout, cmderr

            stdin, stdout, stderr = _run_odm_commands(host)
        else:
            stdin, stdout, stderr = connection.exec_command(command)

        output = stdout.read().splitlines()
        err_output = stderr.read().splitlines()
        LOG.debug("SSH command [%(command)s] returned stdout: %(output)s "
                  "stderr: %(err_output)s" % locals())
        if err_output and log_warning:
            LOG.warn(_("Command %(command)s returned with stderr: "
                       "%(err_output)s") % locals())
        return (output, err_output)

    # connection = check_connection(connection)

    try:
        LOG.debug("Running cmd: %s" % command)

        for num_retries in range(CONF.ibmpowervm_ssh_cmd_retry_num):
            if num_retries > 0:
                LOG.debug("SSH command retried %(num_retries)s with command "
                          "%(command)s" % locals())
            try:
                return _exec_ssh_cmd()
            except SSHException as ssh_ex:
                try:
                    LOG.exception(_("Error while running a cmd: %s") % ssh_ex)
                except Exception:
                    LOG.exception(_("Error logging exception %(e_class)s "
                                    "while running command %(command)s") %
                                  ({'e_class': ssh_ex.__class__.__name__,
                                    'command': command}))

        LOG.debug("SSH command retried and failed with command %s"
                  % command)
    except Exception as e:
        # if there is issue converting the exception to string
        # log the exception class
        try:
            LOG.exception(_("Error while running a cmd: %s") % e)
        except Exception:
            LOG.exception(_("Error logging exception %(e_class)s "
                            "while running command %(command)s") %
                          ({'e_class': e.__class__.__name__,
                            'command': command}))


def ssh_interactive(connection, commands):
    """
    Method to execute remote commands interactively.
    Returns a list with the commands outputs

    :param disk: An active ssh connection
    :param commands: List of commands
    """

    try:
        first_command = commands.pop(0)
        stdin, stdout, stderr = connection.exec_command(first_command)
        remainder_cmds = '\n'.join(commands) + '\n'
        LOG.debug("Running cmd: %s" % remainder_cmds)
        stdin.write(remainder_cmds)
        stdin.flush()
        output = stdout.read().splitlines()
        LOG.debug("stdout: %s stderr: %s" %
                  (output, stderr.read().splitlines()))
        return output
    except Exception as e:
        LOG.exception(_("Error while running a cmd: %s") % e)


def ensure_vios_to_vios_auth(source, dest_conn_info, conn_info):
    """Method allowing for SSH between VIOS partitions

    It builds an SSH key on the source host, put the key
    into the authorized_keys on the destination host.

    :param source: source IP or DNS name
    :param dest_conn_info: dictionary object with SSH connection
                      information for target host
    :param conn_info: dictionary object with SSH connection
                      information for source host
    """
    keypair_uuid = uuid.uuid4()
    src_conn_obj = ssh_connect(conn_info)
    src_conn_obj.conn_data = conn_info

    dest_conn_obj = ssh_connect(dest_conn_info)
    dest_conn_obj.conn_data = dest_conn_info

    def run_as_root(conn_obj, cmd):
        cmds = ['oem_setup_env', cmd, 'exit']
        return ssh_interactive(conn_obj, cmds)

    def build_keypair_on_source():
        # Check whether id_rsa key files already exists
        ls_rsa = 'ls ~/.ssh/id_rsa ~/.ssh/id_rsa.pub'
        rsa_out, rsa_err = ssh_command(src_conn_obj, ls_rsa)
        if rsa_err:
            mkkey = ('ssh-keygen -f ~/.ssh/id_rsa -N "" -C %s' %
                     keypair_uuid.hex)
            # make sure the id_rsa files are owned by user
            chown_cmd = ('chown %s ~/.ssh/id_rsa*' %
                         conn_info.username)
            ssh_key_cmds = '%(mkkey)s; %(chown_cmd)s' % locals()
            run_as_root(src_conn_obj, ssh_key_cmds)

        cat_key = 'cat ~/.ssh/id_rsa.pub'

        pubkey_out, err_out = ssh_command(src_conn_obj, cat_key)

        return pubkey_out[0]

    def insert_into_authorized_keys(public_key):
        echo_key = 'echo "%s" >> ~/.ssh/authorized_keys' % public_key
        run_as_root(dest_conn_obj, echo_key)

    def check_for_ssh_auth():
        """
        Test whether key exchange already completed to the target
        """
        mkauthkeys = ('mkauthkeys --test -u %s --ip %s' %
                      (dest_conn_info.username,
                       dest_conn_info.host))
        output, err_out = ssh_command(src_conn_obj, mkauthkeys)
        if err_out:
            return False
        else:
            return True

    if not check_for_ssh_auth():
        public_key = build_keypair_on_source()
        insert_into_authorized_keys(public_key)
