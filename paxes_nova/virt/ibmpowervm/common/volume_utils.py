# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

"""
Implements common cinder volume helper functions needed for ibmpowervm
virt driver(both IVM and HMC)
"""

from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import loopingcall as loopingcall

from oslo.config import cfg

from paxes_nova.conductor import api as conductor
from paxes_nova.virt.ibmpowervm.common.constants \
    import IMAGE_BOOT_DEVICE_PROPERTY
from paxes_nova.virt.ibmpowervm.common import exception as pvcex
from paxes_nova.volume import cinder as volume

from paxes_nova import _

LOG = logging.getLogger(__name__)
MAX_VOLUME_CHECK_RETRY = 10
IMAGE_META_VERSION = '2'


CONF = cfg.CONF
CONF.import_opt('volume_status_check_max_retries',
                'paxes_nova.compute.manager')
CONF.import_opt('host_storage_type',
                'paxes_nova.virt.ibmpowervm.ivm')


class PowerVCNovaVirtDriverVolumeAPI(object):
    """
    PowerVC virtual driver volume helper functions.
    """

    def __init__(self):
        self.volume_api = volume.PowerVCVolumeAPI()
        self.conductor_api = conductor.PowerVCConductorAPI()

    def _get_boot_volume_bdms(self, bdms, boot_volume_id):
        """Return only bdms that have a volume_id."""
        return [bdm for bdm in bdms if bdm['volume_id']
                and bdm['volume_id'] == boot_volume_id]

    def get_instance_boot_volume_bdms(self, context, instance):
        return self._get_boot_volume_bdms(
            self.conductor_api.block_device_mapping_get_all_by_instance(
                context, instance), instance["default_ephemeral_device"])

    def _valid_image(self, image):
        """
        double check the image meta data passed in.
        """
        if image.get('id') and image.get('properties') \
                and not image['properties'].get('volume_mapping') \
                and image.get('name'):
            return True
        else:
            return False

    def _valid_boot_volume(self, volume, volume_id):
        if (not volume or not volume.get('id') or volume['id'] != volume_id
                or volume['status'].lower() != 'in-use'):
            return False
        else:
            return True

    def _wait_for_clone(self, context, image_volume, count):
        count['count'] += 1
        tries = count['count']
        vol = self.volume_api.get(context, image_volume['id'])
        if vol['status'] == 'available' or vol['status'] == 'in-use':
            msg = (_("The allocation of the image volume was successful: "
                     "image volume %(image_volume)s after tries=%(tries)s") %
                   locals())
            LOG.info(msg)
            raise loopingcall.LoopingCallDone()
        elif (vol['status'] == 'error' or tries >
              CONF.volume_status_check_max_retries):
            msg = (_("The allocation of the image volume either failed or "
                     "timed out: "
                     "image volume %(image_volume)s tries=%(tries)s") %
                   locals())
            LOG.warn(msg)
            raise loopingcall.LoopingCallDone()

    def _check_volume_status(self, context, volume, method, interval=1,
                             initial_delay=None):
        tries = {'count': 0}
        timer = loopingcall.FixedIntervalLoopingCall(method,
                                                     context, volume,
                                                     tries)
        timer.start(interval, initial_delay).wait()
        timer.stop()
        vol = self.volume_api.get(context, volume['id'])
        return vol['status']

    def _generate_image_metadata_properties(self, image, image_volume,
                                            root_device_name,
                                            meta_version=IMAGE_META_VERSION):
        """
        Update glance image metadata for the captured boot volume
        This function needs to be updated if the volume_mapping format
        is changed. The powervc.compute.manager._process_powervc_image_meta()
        is the function that parse the image metadata correct here.
        Make sure they are in sync.

        :param image: glance image object
        :param image_volume: cloned boot image volume object
        :param root_device_name: The instance root_device_name
        :return: updated glance image object
        """
        image_prop = {}
        volmap = {}
        volmap['device_name'] = '/dev/sda' if not root_device_name\
            else root_device_name
        volmap['source_volid'] = image_volume['id']
        volmap['volume_type'] = None  # The volume has type associated already
        volmap['delete_on_termination'] = True

        volume_mapping = [volmap]
        image_prop['volume_mapping'] = jsonutils.dumps(volume_mapping)
        image_prop['meta_version'] = meta_version
        image_prop[IMAGE_BOOT_DEVICE_PROPERTY] = '/dev/sda' \
            if not root_device_name else root_device_name

        return image_prop

    def _wait_for_extend(self, context, volume, count):
        count['count'] += 1
        tries = count['count']
        vol = self.volume_api.get(context, volume['id'])
        if vol['status'] == 'in-use':
            raise loopingcall.LoopingCallDone()
        elif (vol['status'] == 'error_extending' or vol['status'] == 'error'):
            msg = (_("Error increasing size of volume: "
                     "volume %(volume)s tries=%(tries)s") %
                   locals())
            LOG.warn(msg)
            raise loopingcall.LoopingCallDone()

    def extend_volume(self, context, volume_id, size):
        volume = {'id': volume_id}
        self.volume_api.extend_volume(context, volume_id, size)
        # _check_volume_status is green thread safe blocking call
        status = self._check_volume_status(context, volume,
                                           self._wait_for_extend, 200, 60)
        # Only error state concerns us here.
        if status == "error" or status == 'error_extending':
            ex_args = {'volume_id': volume_id}
            raise pvcex.IBMPowerVMErrorExtendingVolume(**ex_args)

    def delete_volume(self, context, volume_id):
        """
        Deletes volume by given volume id.
        :param context: Auth Context
        :param volume_id: Volume-Id to be resized
        """
        self.volume_api.delete(context, volume_id)

    def instance_boot_volume_capture(self, context, instance, image):
        """
        Helper function to capture the instance's boot volume by clone
        it into a image volume and set volume meta for the image volume.
        Return image object with the updated meta data based on the
        captured information.

        In order to capture boot_volume, the BDM will be retrieved
        from instance and use instance['default_ephemeral_device'] to
        find the volume in the BDM that is the boot volume.

        ASSUMPTIONS:
        ============
        1. virtual optical device has been removed when this function
           is called.
        2. image passed in has been created.

        :param context: The security context.
        :param instance: The instance object
        :param image: The glance image object
        :return: updated image object otherwise exception raised
        """

        context = context.elevated()

        if not image or not self._valid_image(image):
            # image is empty or malformed, give up.
            raise pvcex.IBMPowerVMInvalidImageObject(instance_name=
                                                     instance['name'])

        is_localdisk = CONF.host_storage_type.lower() != 'san'

        boot_bdms = self.get_instance_boot_volume_bdms(context, instance)

        if len(boot_bdms) != 1:
            # boot_device_name is missing from instance or it has
            # more than one boot volume which we are not ready to handle.
            ex_args = {'instance_name': instance['name'],
                       'instance_uuid': instance['uuid']}
            raise pvcex.IBMPowerVMNoInstanceBootDeviceDefined(**ex_args)
        try:
            boot_volume = self.volume_api.get(context,
                                              boot_bdms[0]['volume_id'])
        except Exception:
            boot_volume = None

        if (is_localdisk or
            not self._valid_boot_volume(boot_volume,
                                        boot_bdms[0]['volume_id'])):
            ex_args = {'volume': boot_volume,
                       'instance_uuid': instance['uuid'],
                       'instance_name': instance['name']}
            LOG.error(_("Cannot capture the image due to invalid boot volume "
                        "or it is local disk for host storage. "
                        "boot volume: %(vol)s, is_localdisk: %(islocal)s") %
                      dict(vol=boot_volume, islocal=is_localdisk))
            raise pvcex.IBMPowerVMInvalidBootVolume(**ex_args)

        # set image volume meta data
        image_volume_meta = {'image_id': image['id'],
                             'instance_uuid': instance['uuid'],
                             'is_image_volume': 'True',
                             }
        display_name = "Image %s" % image['name']
        description = "Volume for image %s" % image['name']

        # volume_type will be replicated from boot_volume
        image_volume = self.volume_api.create(context, boot_volume['size'],
                                              display_name, description,
                                              source_vol=boot_volume,
                                              metadata=image_volume_meta)

        # if the type is "cluster" then this is ssp
        try:
            stg_type = self.volume_api.get_storage_type_for_volume(
                context, boot_volume['id']
            )
            if stg_type == 'cluster':
                # wait 360 seconds for each 10 GB of image size being copied
                wait_interval = ((boot_volume['size'] + 9) / 10) * 6
            else:
                wait_interval = 1

        except Exception:
            wait_interval = 2

        # _check_volume_clone_status is green thread safe blocking call
        status = self._check_volume_status(context, image_volume,
                                           self._wait_for_clone, wait_interval)
        # Only error state concerns us here.
        if status == "error" or status == 'creating':
            ex_args = {'image_volume': image_volume,
                       'instance_uuid': instance['uuid'],
                       'instance_name': instance['name']}
            raise pvcex.IBMPowerVMBootVolumeCloneFailure(**ex_args)

        # The image volume is ready. Need to update the image metadata
        # accordingly for the image volume.

        rootdev = boot_bdms[0]['device_name']
        image_properties = self._generate_image_metadata_properties(
            image, image_volume, rootdev
        )
        image_props = image['properties']
        image_props.update(image_properties)

        image['location'] = "cinder://%s" % image_volume['id']
        # min_disk is set by the Nova API layer to be the greater of the
        # instance's instance type root_gb value or the parent image's
        # min_disk. We need to reset min_disk (in GB) to be the same
        # as the image size (in bytes) since our images
        # are Cinder volume backed.
        image['min_disk'] = image_volume['size']

        return image

    def image_volume_capture(self, context,image, image_data):

        context = context.elevated()
        image_volume = self.volume_api.get(context, image_data['volume_id'])
        if image_volume['status'] == "error":
            ex_args = {'image_volume': image_data['volume_id'],
                       'instance_uuid': None,
                       'instance_name': None}
            raise pvcex.IBMPowerVMBootVolumeCloneFailure(**ex_args)

        # The image volume is ready. Need to update the image metadata
        # accordingly for the image volume.

        rootdev = '/dev/sda'
        image_properties = self._generate_image_metadata_properties(
            image, image_volume, rootdev
        )
        image_props = image['properties']
        image_props.update(image_properties)

        image['location'] = "cinder://%s" % image_volume['id']
        # min_disk is set by the Nova API layer to be the greater of the
        # instance's instance type root_gb value or the parent image's
        # min_disk. We need to reset min_disk (in GB) to be the same
        # as the image size (in bytes) since our images
        # are Cinder volume backed.
        image['min_disk'] = image_volume['size']

        return image

class PowerVCNovaISCSIVirtDriverVolumeAPI(PowerVCNovaVirtDriverVolumeAPI):
    def __init__(self):
        super(PowerVCNovaISCSIVirtDriverVolumeAPI,self).__init__()
        
    def instance_boot_volume_capture(self, context, instance, image):
        """
        Helper function to capture the instance's boot volume by clone
        it into a image volume and set volume meta for the image volume.
        Return image object with the updated meta data based on the
        captured information.

        In order to capture boot_volume, the BDM will be retrieved
        from instance and use instance['default_ephemeral_device'] to
        find the volume in the BDM that is the boot volume.

        ASSUMPTIONS:
        ============
        1. virtual optical device has been removed when this function
           is called.
        2. image passed in has been created.

        :param context: The security context.
        :param instance: The instance object
        :param image: The glance image object
        :return: updated image object otherwise exception raised
        """

        context = context.elevated()

        if not image or not self._valid_image(image):
            # image is empty or malformed, give up.
            raise pvcex.IBMPowerVMInvalidImageObject(instance_name=
                                                     instance['name'])

        is_localdisk = CONF.host_storage_type.lower() == 'local'

        boot_bdms = self.get_instance_boot_volume_bdms(context, instance)

        if len(boot_bdms) != 1:
            # boot_device_name is missing from instance or it has
            # more than one boot volume which we are not ready to handle.
            ex_args = {'instance_name': instance['name'],
                       'instance_uuid': instance['uuid']}
            raise pvcex.IBMPowerVMNoInstanceBootDeviceDefined(**ex_args)
        try:
            boot_volume = self.volume_api.get(context,
                                              boot_bdms[0]['volume_id'])
        except Exception:
            boot_volume = None

        if (is_localdisk or
            not self._valid_boot_volume(boot_volume,
                                        boot_bdms[0]['volume_id'])):
            ex_args = {'volume': boot_volume,
                       'instance_uuid': instance['uuid'],
                       'instance_name': instance['name']}
            LOG.error(_("Cannot capture the image due to invalid boot volume "
                        "or it is local disk for host storage. "
                        "boot volume: %(vol)s, is_localdisk: %(islocal)s") %
                      dict(vol=boot_volume, islocal=is_localdisk))
            raise pvcex.IBMPowerVMInvalidBootVolume(**ex_args)

        # set image volume meta data
        image_volume_meta = {'image_id': image['id'],
                             'instance_uuid': instance['uuid'],
                             'is_image_volume': 'True',
                             }
        display_name = "Image %s" % image['name']
        description = "Volume for image %s" % image['name']

        # volume_type will be replicated from boot_volume
        image_volume = self.volume_api.create(context, boot_volume['size'],
                                              display_name, description,
                                              source_vol=boot_volume,
                                              metadata=image_volume_meta)
        wait_interval = 2
        # _check_volume_clone_status is green thread safe blocking call
        status = self._check_volume_status(context, image_volume,
                                           self._wait_for_clone, wait_interval)
        # Only error state concerns us here.
        if status == "error":
            ex_args = {'image_volume': image_volume,
                       'instance_uuid': instance['uuid'],
                       'instance_name': instance['name']}
            raise pvcex.IBMPowerVMBootVolumeCloneFailure(**ex_args)

        # The image volume is ready. Need to update the image metadata
        # accordingly for the image volume.

        rootdev = boot_bdms[0]['device_name']
        image_properties = self._generate_image_metadata_properties(
            image, image_volume, rootdev
        )
        image_props = image['properties']
        image_props.update(image_properties)

        image['location'] = "cinder://%s" % image_volume['id']
        # min_disk is set by the Nova API layer to be the greater of the
        # instance's instance type root_gb value or the parent image's
        # min_disk. We need to reset min_disk (in GB) to be the same
        # as the image size (in bytes) since our images
        # are Cinder volume backed.
        image['min_disk'] = image_volume['size']

        return image
    
class PowerVCNovaLOCALVirtDriverVolumeAPI(PowerVCNovaVirtDriverVolumeAPI):
    def __init__(self):
        super(PowerVCNovaLOCALVirtDriverVolumeAPI,self).__init__()
        
    def instance_boot_volume_capture(self, context, instance, image):
        """
        Helper function to capture the instance's boot volume by clone
        it into a image volume and set volume meta for the image volume.
        Return image object with the updated meta data based on the
        captured information.

        In order to capture boot_volume, the BDM will be retrieved
        from instance and use instance['default_ephemeral_device'] to
        find the volume in the BDM that is the boot volume.

        ASSUMPTIONS:
        ============
        1. virtual optical device has been removed when this function
           is called.
        2. image passed in has been created.

        :param context: The security context.
        :param instance: The instance object
        :param image: The glance image object
        :return: updated image object otherwise exception raised
        """

        context = context.elevated()

        if not image or not self._valid_image(image):
            # image is empty or malformed, give up.
            raise pvcex.IBMPowerVMInvalidImageObject(instance_name=
                                                     instance['name'])

        is_localdisk = CONF.host_storage_type.lower() == 'local'
        if CONF.local_or_cinder:
            is_localdisk = False

        boot_bdms = self.get_instance_boot_volume_bdms(context, instance)

        if len(boot_bdms) != 1:
            # boot_device_name is missing from instance or it has
            # more than one boot volume which we are not ready to handle.
            ex_args = {'instance_name': instance['name'],
                       'instance_uuid': instance['uuid']}
            raise pvcex.IBMPowerVMNoInstanceBootDeviceDefined(**ex_args)
        try:
            boot_volume = self.volume_api.get(context,
                                              boot_bdms[0]['volume_id'])
        except Exception:
            boot_volume = None

        if (is_localdisk or
            not self._valid_boot_volume(boot_volume,
                                        boot_bdms[0]['volume_id'])):
            ex_args = {'volume': boot_volume,
                       'instance_uuid': instance['uuid'],
                       'instance_name': instance['name']}
            LOG.error(_("Cannot capture the image due to invalid boot volume "
                        "or it is local disk for host storage. "
                        "boot volume: %(vol)s, is_localdisk: %(islocal)s") %
                      dict(vol=boot_volume, islocal=is_localdisk))
            raise pvcex.IBMPowerVMInvalidBootVolume(**ex_args)

        # set image volume meta data
        image_volume_meta = {'image_id': image['id'],
                             'instance_uuid': instance['uuid'],
                             'is_image_volume': 'True',
                             }
        display_name = "Image %s" % image['name']
        description = "Volume for image %s" % image['name']

        # volume_type will be replicated from boot_volume
        image_volume = self.volume_api.create(context, boot_volume['size'],
                                              display_name, description,
                                              source_vol=boot_volume,
                                              metadata=image_volume_meta)
        wait_interval = 2
        # _check_volume_clone_status is green thread safe blocking call
        status = self._check_volume_status(context, image_volume,
                                           self._wait_for_clone, wait_interval)
        # Only error state concerns us here.
        if status == "error":
            ex_args = {'image_volume': image_volume,
                       'instance_uuid': instance['uuid'],
                       'instance_name': instance['name']}
            raise pvcex.IBMPowerVMBootVolumeCloneFailure(**ex_args)

        # The image volume is ready. Need to update the image metadata
        # accordingly for the image volume.

        rootdev = boot_bdms[0]['device_name']
        image_properties = self._generate_image_metadata_properties(
            image, image_volume, rootdev
        )
        image_props = image['properties']
        image_props.update(image_properties)

        image['location'] = "cinder://%s" % image_volume['id']
        # min_disk is set by the Nova API layer to be the greater of the
        # instance's instance type root_gb value or the parent image's
        # min_disk. We need to reset min_disk (in GB) to be the same
        # as the image size (in bytes) since our images
        # are Cinder volume backed.
        image['min_disk'] = image_volume['size']

        return image
