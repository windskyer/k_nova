#
#
# All Rights Reserved.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
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
from nova.api.metadata import base as instance_metadata

from nova.openstack.common import excutils
from nova.openstack.common import log as logging
from nova.openstack.common import processutils
from nova.virt import configdrive
from oslo.config import cfg
from paxes_nova import _

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


def create_config_drive_iso(instance, injected_files,
                            admin_password, network_info):
    """"
    If a config drive is required by the instance it will create
    a config drive ISO file and returns the path to the file.  Otherwise
    it will return None
    @param instance: the VM instance
    @param injected_files: files specified to be injected on the VM spawn
                            method
    @param admin_password: Admin password specified on the VM spawn call
    @param network_info: network_info passed to the VM spawn call
    """

    if configdrive.required_by(instance):

        LOG.info(_('Using config drive'), instance=instance)
        extra_md = {}
        if admin_password:
            extra_md['admin_pass'] = admin_password

        inst_md = instance_metadata.\
            InstanceMetadata(instance,
                             content=injected_files, extra_md=extra_md,
                             network_info=network_info)

        local_img_dir = CONF.powervm_img_local_path
        base_name = '%s_config.iso' % instance['name']
        configdrive_path = os.path.join(local_img_dir, base_name)
        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            LOG.info(_('Creating config drive at %(path)s'),
                     {'path': configdrive_path}, instance=instance)

            try:
                cdb.make_drive(configdrive_path)
            except processutils.ProcessExecutionError as e:
                with excutils.save_and_reraise_exception():
                    LOG.error(_('Creating config drive failed '
                              'with error: %s'),
                              e, instance=instance)
        return configdrive_path
