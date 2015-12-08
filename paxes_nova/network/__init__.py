# vim: tabstop=4 shiftwidth=4 softtabstop=4

# All Rights Reserved.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

import oslo.config.cfg

# Importing full names to not pollute the namespace and cause possible
# collisions with use of 'from nova.network import <foo>' elsewhere.
import nova.openstack.common.importutils


def placement_module():
    importutils = nova.openstack.common.importutils
    host_type = oslo.config.cfg.CONF.host_type.lower()
    if 'powervm' in host_type:
        placement_class = 'paxes_nova.network.ibmpowervm.placement_pvm.' +\
            'IBMPowerVMNetworkPlacementPVM'
    elif 'kvm' in host_type:
        placement_class = 'paxes_nova.network.powerkvm.placement_kvm.' +\
            'IBMPowerVMNetworkPlacementKVM'
    cls = importutils.import_class(placement_class)
    return cls()
