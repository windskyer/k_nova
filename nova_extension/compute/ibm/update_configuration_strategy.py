#    Copyright 2013 IBM Corp.
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

import functools
import httplib
import json

from nova import exception
from nova.image import glance
from nova.openstack.common import log as logging

from oslo.config import cfg

from nova.openstack.common.gettextutils import _

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

"""Update strategy for configuration strategy."""


def update_metadata(context, image_id, instance):
    """This will update the image metadata in the case of OVF
       configuration strategy.

       This utility will bypass the Nova database
       as there is a limit to how much metadata can be stored in the
       nova.instance_system_metadata table that is inconsistent with
       the Glance database table glance.image_properties. This issue has
       been raised with the community and will be solved by implementing
       the python-glanceclient V2 API and adjusting the
       nova.instance_system_metadata to be consistent with Glance since
       both tables store the same data.

       :param context: security context
       :param image_id: id of the new snapshot image in Glance
       :param instance: instance to be captured
       """
    # NOTE(ldbragst) Check here to see if configuration_strategy exists
    # in image_meta, if true then update the new image added to Glance
    # to include the metadata in the new snapshot. This will be fixed with
    # python-glanceclient V2 API and Nova database migration. This is a
    # a workaround fix until community finishes implementation, at which
    # time we will pull in the correct fix and implement with
    # python-glanceclient V2 API.

    system_metadata = instance.get('system_metadata', {})
    if 'image_configuration_strategy' not in system_metadata:
        return  # no need update since there is no configuration strategy

    auth_token = context.auth_token

    send_remove_request = functools.partial(send_request, image_id, auth_token,
                                            'remove')

    if instance['image_ref'] == '':
        uuid = instance['uuid']
        LOG.warn(_('Original image for instance %(uuid)s '
                   'has been deleted and the configuration '
                   'metadata has been lost'), {'uuid': uuid},
                 instance=instance)
        send_remove_request()
        return

    # If the base image is still available, get its metadata
    glance_service, old_img_id = glance.get_remote_image_service(context,
                                instance['image_ref'])
    try:
        old_image_id = glance_service.show(context, old_img_id)
    except exception.ImageNotFound as e:
        LOG.warn(_('Original image for instance cannot be accessed '
                   'and the configuration metadata has been lost: %s')
                    % e, instance=instance)
        send_remove_request()
        return

    if old_image_id['status'] == 'deleted':
        uuid = instance['uuid']
        LOG.warn(_('Original image for instance %(uuid)s '
                   'has been deleted and the configuration '
                   'metadata has been lost'), {'uuid': uuid},
                 instance=instance)
        send_remove_request()
        return

    # Check if new image has configuration strategy, if it does then we need
    # to set 'operation' to replace so that the values are updated and not
    # rewritten with old values
    new_config_data = check_image_properties(context, image_id)
    base_config_data = check_image_properties(context, instance['image_ref'])
    # If neither images have configuration strategy in img_props then we need
    # to return here since we don't have to update any metadata for snapshot
    if new_config_data is None and base_config_data is None:
        return

    operation = 'add'
    if new_config_data is not None:
        operation = 'replace'

    send_request(image_id, auth_token, operation,
                 config_strategy_data=base_config_data)


def check_image_properties(context, image_id):
    """Check the image properties stored in Glance to make sure we
       set the 'operation' policy correctly

    :param context: security context
    :param image_id: id of image to check
    :returns: config_data of image_id or None
    """
    glance_service, img_id = glance.get_remote_image_service(context, image_id)
    image_meta = glance_service.show(context, img_id)

    img_props = image_meta['properties']
    if 'configuration_strategy' in img_props:
        config_data = img_props['configuration_strategy']

    else:
        config_data = None

    return config_data


def send_request(image_id, auth_token, operation, config_strategy_data={}):
    """This method will build a request to be sent to Glance
       for adding metadata to an image

    :param image_id: id of the new image or snapshot
    :param auth_token: authentication token for update
    :param operation: Operation to send in the request. Operations supported
        are add, replace, and remove
    :param config_strategy_data: metadata to be added to the image
    """
    # Grab the Glance host information to build the connection
    api_servers = glance.get_api_servers()
    glance_host = api_servers.next()
    conn = httplib.HTTPConnection(glance_host[0], glance_host[1])

    resource_path = '/v2/images/%s' % image_id

    headers = {
            'X-Auth-Token': auth_token,
            'Content-Type': 'application/openstack-images-v2.0-json-patch'
        }

    # Here we are creating a new dictionary with the operation that
    # we plan to take on the new image/snapshot and the
    # configuration strategy data that was pulled from the Glance database
    # of the image that we are snapshotting from. This is an internal fix
    # that will be removed when the python-glanceclient V2 API work is done.
    data = {}
    data[operation] = '/configuration_strategy'
    if operation != 'remove':
        data['value'] = config_strategy_data

    # Package the dictionary as an array and dump it to a string. We are
    # putting this in an array as a requirement for PATCH.
    data_str = json.dumps([data])

    # Build the request
    conn.request('PATCH', resource_path, body=data_str, headers=headers)
    resp = conn.getresponse()

    if 200 <= resp.status < 300:
        pass
    else:
        # We need to inform the user the transfer failed
        raise exception.InvalidRequest()
