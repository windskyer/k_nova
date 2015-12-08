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

from nova.openstack.common import log as logging
from neutron.openstack.common import jsonutils
from oslo.config import cfg

from powervc_nova import _
from powervc_nova.network.powerkvm.agent import commandlet

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


def cleanup_ovs_tap_ports():
    """
    This method will be periodically invoked to cleans up orphaned OVS tap
    ports.  This prevents Neutron from filling up the logs (and eventually the
    disk) with warnings about the orphaned OVS ports.
    """
    LOG.debug('Entry: OVS TAP port cleanup task')

    # Create command runner
    commandex = commandlet.CommandExecutor

    # Get a list of all OVS ports on the integration bridge
    args = ['list-ports', CONF.integration_bridge]
    result = commandex.run_vsctl(args, check_error=True)
    port_names = []
    if result:
        port_names = result.strip().split('\n')

    # Dump all OVS data with JSON formatting and OpenStack OVS agent data
    args = ['--format=json', '--', '--columns=name,external_ids,ofport',
            'list', 'Interface']
    result = commandex.run_vsctl(args, check_error=True)
    if not result:
        return

    # Parse JSON data into one row per OVS port
    for row in jsonutils.loads(result)['data']:
        # Verify we're looking at a port on the integration bridge
        name = row[0]
        if name not in port_names:
            continue

        # Check the status of the port
        ofport = row[2]
        try:
            int_ofport = int(ofport)
        except (ValueError, TypeError):
            # The status is not an integer, just continue
            continue
        else:
            # A status of 0 or negative means there is a problem
            if int_ofport <= 0:
                LOG.info(_('Deleting openvswitch port: %s'), row)
                args = ['--', '--if-exists', 'del-port',
                        CONF.integration_bridge, name]
                commandex.run_vsctl(args, check_error=True)

    LOG.debug('Exit: OVS TAP port cleanup task')
