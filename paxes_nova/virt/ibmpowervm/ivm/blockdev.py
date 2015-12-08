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
import hashlib
import re
import time

from nova.compute import flavors
from nova.compute import task_states

from nova.image import glance

from nova.openstack.common import log as logging
from nova.openstack.common.lockutils import synchronized
from nova.openstack.common import excutils
from nova.openstack.common import jsonutils

from nova.virt import images

from oslo.config import cfg

from paxes_nova.virt.ibmpowervm.common import exception as common_ex
from paxes_nova import logcall
from paxes_nova.virt.ibmpowervm.common.volume_utils \
    import PowerVCNovaVirtDriverVolumeAPI 
from paxes_nova.virt.ibmpowervm.common.volume_utils \
    import PowerVCNovaISCSIVirtDriverVolumeAPI
from paxes_nova.virt.ibmpowervm.common.volume_utils \
    import PowerVCNovaLOCALVirtDriverVolumeAPI
from paxes_nova.virt.ibmpowervm.ivm import constants
from paxes_nova.virt.ibmpowervm.ivm import exception
from paxes_nova.virt.ibmpowervm.ivm import common
from paxes_nova.virt.ibmpowervm.ivm import command
from paxes_nova.volume import cinder as volume_api

from paxes_nova import _

LOG = logging.getLogger(__name__)

san_opts = [
    cfg.StrOpt('san_storage_pool',
               default='',
               help='Default storage pool to assign for volumes'),
    cfg.StrOpt('san_disk_prefix',
               default='nova',
               help='Prefix for newly created disks'),
    cfg.StrOpt('image_san_disk_prefix',
               default='image-',
               help='Prefix for newly created image disks'),
    cfg.StrOpt('first_choice_vg',
               default='datavg',
               help='First choice vg name,if this vg not exist in VIOS,will random select vg')
]

CONF = cfg.CONF
CONF.register_opts(san_opts)


class PowerVMDiskAdapter(object):

    """PowerVM disk adapter interface
    Provides a contract to implement multiple ways to generate
    and attach volumes to virtual machines using local and/or
    external storage
    """

    def __init__(self, connection):
        self._connection = None
        self.connection_data = connection

    def _set_connection(self):
        # create a new connection or verify an existing connection
        # and re-establish if the existing connection is dead
        self._connection = common.check_connection(self._connection,
                                                   self.connection_data)

    @logcall
    def handle_stale_disk(self, volume_info):
        """
        Detect and cleanup stale disk at the location Specified by
        volume_info. This function may be called before run_cfg_dev
        to discover the newly mapped disk. This function is called
        on the normal path. It is using lspath, which is a lightweight
        ODM operation to discover the stale disk. And use rmdev
        to remove it.

        :param volume_info: It is a dictionary that has target_wwn and
                            target lun. Look like this:
                            {'target_wwn': <uppercase hex string>,
                             'target_lun': <decimal scsi id>}
        """

        if not volume_info or not isinstance(volume_info, dict):
            msg = _("volume_info is null or not a dictionary")
            raise exception.IBMPowerVMInvalidParameters(err=msg)

        if(len(volume_info.keys()) < 2 or
           not 'target_wwn' in volume_info.keys() or
           not 'target_lun' in volume_info.keys()):
            msg = (_("Invalid volume_info: %(volume_info)s") % locals())
            raise exception.IBMPowerVMInvalidParameters(err=msg)

        aix_conn = self.pvm_operator.get_volume_aix_conn_info(volume_info)

        # Trying to find any devices exists at the current mapping.
        # This includes disk in all states at the mapped location.
        try:
            dev_info = self.pvm_operator.get_devname_by_aix_conn(
                aix_conn,
                all_dev_states=True
            )
        except exception.IBMPowerVMInvalidLUNPathInfo:
            msg = ("No stale disk discovered at location: %(aix_conn)s"
                   % locals())
            LOG.debug(msg)
            # This is good path, no stale disk detected.
            dev_info = None

        if dev_info:
            # We found a stale disk, handle it
            hdisk = dev_info['device_name']
            msg = (_("Clean up stale disk %(hdisk)s with connection "
                     "info: %(aix_conn)s") % locals())
            LOG.info(msg)
            # Clear up possible existing vhost mapping.
            try:
                self.pvm_operator.detach_disk_from_vhost(hdisk)
            except Exception:
                # If there was no vhost map, it will throw exception. Move on.
                pass
            self.pvm_operator.remove_disk(hdisk)

    def create_volume(self, size):
        """Creates a volume with a minimum size

        :param size: size of the volume in bytes
        :returns: string -- the name of the disk device.
      """
        pass

    def delete_volume(self, volume_info):
        """Removes the disk and its associated vSCSI connection

        :param volume_info: dictionary with volume info including name of
        disk device in /dev/
        """
        pass

    def create_volume_from_image(self, context, instance, image_id):
        """Creates a Volume and copies the specified image to it

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :returns: dictionary with the name of the created
                  disk device in 'device_name' key
        """
        pass

    def create_image_from_volume(self, instance, device_name, context,
                                 image_id, image_meta, update_task_state):
        """Capture the contents of a volume and upload to glance

        :param instance: the VM instance
        :param device_name: device in /dev/ on IVM to capture
        :param context: nova context for operation
        :param image_id: image reference to pre-created image in glance
        :param image_meta: metadata for new image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state
        """
        pass

    def migrate_volume(self, lv_name, src_host, dest, image_path,
                       instance_name=None):
        """Copy a logical volume to file, compress, and transfer

        :param lv_name: volume device name
        :param src_host: source IP or DNS name.
        :param dest: destination IP or DNS name
        :param image_path: path to remote image storage directory
        :param instance_name: name of instance that is being migrated
        :returns: file path on destination of image file that was moved
        """
        pass

    def attach_volume_to_host(self, *args, **kargs):
        """
        Attaches volume to host using info passed in *args and **kargs
        """
        pass

    @logcall
    def detach_volume_from_host(self, *args, **kargs):

        # We're passed possibly multiple hdisks
        for disk in args:
            hdisk = disk['device_name']
            LOG.debug("Volume to detach: %s" % hdisk)

            # Get the UID before removing from the LPAR
            # This will be used if delete is called next
            if not 'volume_uid' in disk:
                disk['volume_uid'] = \
                    self.pvm_operator.get_disk_uid_by_name(hdisk)

            # First detach it from the LPAR
            self.pvm_operator.detach_disk_from_vhost(hdisk)
            if disk['volume_uid']:
                # Detach from the host
                self.pvm_operator.remove_disk(hdisk)

    def _md5sum_remote_file(self, remote_path):
        # AIX6/VIOS cannot md5sum files with sizes greater than ~2GB
        cmd = ("perl -MDigest::MD5 -e 'my $file = \"%s\"; open(FILE, $file); "
               "binmode(FILE); "
               "print Digest::MD5->new->addfile(*FILE)->hexdigest, "
               "\" $file\n\";'" % remote_path)

        output = self.run_vios_command_as_root(cmd)
        return output[0]

    def _checksum_local_file(self, source_path):
        """Calculate local file checksum.

        :param source_path: source file path
        :returns: string -- the md5sum of local file
        """
        with open(source_path, 'r') as img_file:
            hasher = hashlib.md5()
            block_size = 0x10000
            buf = img_file.read(block_size)
            while len(buf) > 0:
                hasher.update(buf)
                buf = img_file.read(block_size)
            source_cksum = hasher.hexdigest()
        return source_cksum

    def copy_image_file(self, source_path, remote_path, decompress=False):
        """Copy file to VIOS, decompress it, and return its new size and name.

        :param source_path: source file path
        :param remote_path remote file path
        :param decompress: if True, de-compresses the file after copying;
                           if False (default), just copies the file
        """
        # Calculate source image checksum
        source_cksum = self._checksum_local_file(source_path)

        comp_path = os.path.join(remote_path, os.path.basename(source_path))
        if comp_path.endswith(".gz"):
            uncomp_path = os.path.splitext(comp_path)[0]
        else:
            uncomp_path = comp_path
        if not decompress:
            final_path = comp_path
        else:
            final_path = uncomp_path

        # Check whether the image is already on IVM
        output = self.run_vios_command("ls %s" % final_path,
                                       check_exit_code=False)

        # If the image does not exist already
        if not output:
            try:
                # Copy file to IVM
                common.scp_command(self.connection_data,
                                   source_path, remote_path, 'put')
            except exception.IBMPowerVMFileTransferFailed:
                with excutils.save_and_reraise_exception():
                    cmd = "/usr/bin/rm -f %s" % final_path
                    self.run_vios_command_as_root(cmd)

            # Verify image file checksums match
            output = self._md5sum_remote_file(final_path)
            if not output:
                LOG.error(_("Unable to get checksum"))
                # Cleanup inconsistent remote file
                cmd = "/usr/bin/rm -f %s" % final_path
                self.run_vios_command_as_root(cmd)

                raise exception.\
                    IBMPowerVMFileTransferFailed(file_path=final_path)
            if source_cksum != output.split(' ')[0]:
                LOG.error(_("Image checksums do not match"))
                # Cleanup inconsistent remote file
                cmd = "/usr/bin/rm -f %s" % final_path
                self.run_vios_command_as_root(cmd)

                raise exception.\
                    IBMPowerVMFileTransferFailed(file_path=final_path)

            if decompress:
                # Unzip the image
                cmd = "/usr/bin/gunzip %s" % comp_path
                output = self.run_vios_command_as_root(cmd)

                # Remove existing image file
                cmd = "/usr/bin/rm -f %s.*" % uncomp_path
                output = self.run_vios_command_as_root(cmd)

                # Rename unzipped image
                cmd = "/usr/bin/mv %s %s" % (uncomp_path, final_path)
                output = self.run_vios_command_as_root(cmd)

                # Remove compressed image file
                cmd = "/usr/bin/rm -f %s" % comp_path
                output = self.run_vios_command_as_root(cmd)

        else:
            LOG.debug("Image found on host at '%s'" % final_path)

        # Calculate file size in multiples of 512 bytes
        output = self.run_vios_command("ls -o %s|awk '{print $4}'" %
                                       final_path, check_exit_code=False)
        if output:
            size = int(output[0])
        else:
            LOG.error(_("Uncompressed image file not found"))
            msg_args = {'file_path': final_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        if (size % 512 != 0):
            size = (int(size / 512) + 1) * 512

        return final_path, size

    def run_vios_command(self, cmd, check_exit_code=True):
        """Run a remote command using an active ssh connection.

        :param command: String with the command to run.
        """
        return self.pvm_operator.run_vios_command(cmd, check_exit_code)

    def run_vios_command_as_root(self, command, check_exit_code=True):
        """Run a remote command as root using an active ssh connection.

        :param command: List of commands.
        """
        return self.pvm_operator.run_vios_command_as_root(command,
                                                          check_exit_code)

    def update_to_glance(self,  destfile, 
                         image_meta, image_data):
        # get glance service
        image_id = image_data.get('image_id', None)
        context = image_data.get('context', None)
        if not image_id or not context:
            raise
        glance_service, image_id = glance.get_remote_image_service(
            context, image_id)
        glance_file_store = "/var/lib/glance/images"  # CONF.img_repo_path?
        #self._check_space(glance_file_store, capture_file_size)
        is_black = image_data.get('is_back')
        if is_black:
            #glance_service.update(context, image_id, image_meta)
            glance_service.delete(context,image_id)
            glance_service.create(context,image_meta)
        else:
            with open(destfile, 'r') as img_file:
                glance_service.update(context, image_id, image_meta, img_file)
        LOG.debug("local image added to glance.")

    def download_from_glance(self, context, image_meta, image_data=None):
        #get glance service
        @synchronized('ovf_image_fetch', 'localdisk-')
        def _fetch_image_from_glance(context, image_id,
                                     file_path,image_data):
            if not os.path.isfile(file_path):
                # Add a space check
                self._check_space(CONF.powervm_img_local_path,
                                  image_data['size'])

                LOG.debug("Fetching image '%s' from glance" % image_id)
                images.fetch(context, image_id, file_path, None, None)
            else:
                LOG.debug("Using image found at '%s'" % file_path)

        image_id = image_data.get('image_id')
        remote_file_name = None
        try:
            file_name = '.'.join([image_id, 'gz'])
            file_path = os.path.join(CONF.powervm_img_local_path,
                                         file_name)
                # sync call to avoid cksum calc before file fully gets-
                # copied.
            _fetch_image_from_glance(context, image_id,
                                         file_path, image_data)

        except Exception as ex:
            # added for the support of 3997
            LOG.error(_("Unable to transfer image"))
            msg_args = {'file_path': file_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        
        LOG.debug("Ensuring image '%s' exists on IVM" % file_path)
        #if image_data['is_back']:
            #    return file_path
        remote_path = CONF.powervm_img_remote_path
        remote_file_name, image_size = self.\
            copy_image_file(file_path,
                            remote_path,
                            False)
        if remote_file_name.endswith(".gz"):
            uremote_file_name = os.path.splitext(remote_file_name)[0]
            # unzip the image
            cmd = "/usr/bin/gunzip %s" % remote_file_name
            output = self.run_vios_command_as_root(cmd)
            return uremote_file_name
        return remote_file_name

    @logcall
    @synchronized('device-copy', 'localdisk-')
    def _copy_file_to_device(self, source_path, device, decompress=True):
        """Copy file to device.

        :param source_path: path to input source file
        :param device: output device name
        :param decompress: if True (default) the file will be decompressed
                           on the fly while being copied to the drive
        """
        LOG.debug("Copying file %s to device /dev/%s" % (source_path, device))
        if decompress:
            cmd = ('gunzip -c %s > /dev/%s' % (source_path, device))
        else:
            cmd = 'dd if=%s of=/dev/%s bs=1024k' % (source_path, device)
        self.run_vios_command_as_root(cmd)

class SANDiskAdapter(PowerVMDiskAdapter):

    def __init__(self, pvm_operator):
        super(SANDiskAdapter, self).__init__(None)
        self.volume_api = PowerVCNovaVirtDriverVolumeAPI()
        self.pvm_operator = pvm_operator
        self._initiator = None
        self._wwpns = None
        self.connection_data = self.pvm_operator.connection_data

    @logcall
    def create_volume_from_image(self, context, instance, image_id):
        """Creates a SAN Volume and copies the specified image to it

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :returns: dictionary with the name of the created
                  volume device
        """
        LOG.debug("Create volume from image")

        image_meta = self._get_image_meta(context, instance['image_ref'])

        disk_format = None
        if 'disk_format' in image_meta:
            disk_format = image_meta['disk_format']

        image_detail = None
        image_meta_properties = image_meta['properties']
        root_uid = None

        if disk_format and disk_format == 'iso':

            # Verify that an ephemeral disk is present in this flavor
            if 'ephemeral_gb' not in instance or instance['ephemeral_gb'] == 0:
                raise exception.IBMPowerVMNoEphemeralDisk()

            # Prepare the ISO image on the host
            vopt_img_name = \
                self._prepare_iso_image_on_host(context, instance,
                                                CONF.powervm_img_local_path,
                                                CONF.powervm_img_remote_path,
                                                image_meta)

            root_volume_size_in_bytes = instance['ephemeral_gb'] * 1073741824

            volume_name, root_uid = \
                self._create_volume(root_volume_size_in_bytes)
            if not root_uid:
                raise Exception(_("Error creating root volume."))
            image_detail = {'volume_uid': root_uid,
                            'vopt': vopt_img_name}

        # Add ephemeral property to the image detail
        if root_uid:
            image_detail[root_uid] = 'ephemeral'

        return image_detail

    def _check_space(self, dest_dir, size_required):
        st = os.statvfs(dest_dir)
        free_space_kb = st.f_bavail * st.f_frsize
        if size_required > free_space_kb:
            ex_args = {'path': dest_dir,
                       'size': (size_required / (1024 * 1024)),
                       'free': free_space_kb / (1024 * 1024)}
            raise exception.IBMPowerVMManagementServerOutOfSpace(**ex_args)

    @logcall
    @synchronized('iso_prep', 'iso-')
    def _prepare_iso_image_on_host(self, context, instance, local_img_dir,
                                   host_dir, image_meta):
        """
        Prepares an ISO file on a the host.  It will transfer
        the file to the host and create virtual optical media
        object for it.
        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param local_img_dir: Local image directory
        :param host_dir: Remote (host) image directory
        :param image_meta: metadata for the image
        :returns the name of the vopt object for the ISO
        """
        pvm_op = self.pvm_operator

        # Check if media library exists. Error out if it's not there.
        if not pvm_op.check_media_library_exists():
            LOG.error(_("There is no media library"))
            raise exception.IBMPowerVMMediaLibraryNotAvailable()

        # Create vopt with name being the glance ID/UUID.
        vopt_img_name = image_meta['id']
        if not pvm_op.check_vopt_exists(name=vopt_img_name):
            # check for space within CONF.powervm_img_local_path
            self._check_space(local_img_dir, image_meta['size'])

            # It's not VIOS yet:
            # Check available space in IVM staging area and media repository
            free_space = pvm_op.get_vopt_size()
            free_padmin_space = pvm_op.get_staging_size(host_dir)

            free_rep_mb = int(free_space[0])
            free_staging_mb = int(free_padmin_space[0]) / 1024
            iso_size_mb = image_meta['size'] / (1024 * 1024)

            LOG.debug("Free repository space: %s MB" % free_rep_mb)
            LOG.debug("Free VIOS staging space: %s MB" % free_staging_mb)
            LOG.debug("ISO file size: %s MB" % iso_size_mb)

            if iso_size_mb > free_rep_mb:
                raise common_ex.IBMPowerVMMediaRepOutOfSpace(
                    size=iso_size_mb, free=free_rep_mb)

            if iso_size_mb > free_staging_mb:
                raise exception.IBMPowerVMStagingAreaOutOfSpace(
                    dir=host_dir, size=iso_size_mb, free=free_staging_mb)

            # Fetch ISO from Glance

            file_path = '%s/%s.%s.%s' % (local_img_dir,
                                         image_meta['id'],
                                         CONF.host,
                                         image_meta['disk_format'])
            LOG.debug("Fetching image '%(img-name)s' from glance "
                      "to %(img-path)s" %
                      {'img-name': image_meta['name'],
                       'img-path': file_path})
            images.fetch(context, image_meta['id'], file_path,
                         instance['user_id'],
                         instance['project_id'])
            if (os.path.isfile(file_path)):
                # transfer ISO to VIOS
                try:

                    iso_remote_path, iso_size = self.copy_image_file(file_path,
                                                                     host_dir)

                    self.pvm_operator.create_vopt_device(
                        vopt_img_name, iso_remote_path)
                    # The create vopt device command copies
                    # the ISO into the media repository.  The file at
                    # iso_remote_path can be removed now.
                    try:
                        self.pvm_operator.remove_ovf_env_iso(iso_remote_path)
                    except Exception:
                        msg = (_('Error cleaning up ISO:'
                                 ' %(iso_remote_path)s') %
                               locals())
                        LOG.exception(msg)
                finally:
                    os.remove(file_path)
            else:
                raise exception.IBMPowerVMISOFileNotFound(
                    ISO_file=file_path)
        else:
            LOG.debug("Image with id %s already on VIOS."
                      % image_meta['id'])

        return vopt_img_name

    @logcall
    def create_image_from_volume(self, instance, device_name, context,
                                 image_id, image_meta, update_task_state):
        """Capture the contents of a volume and upload to glance

        :param instance: the VM instance
        :param device_name: device in /dev/ on IVM to capture
        :param context: nova context for operation
        :param image_id: image reference to pre-created image in glance
        :param image_meta: metadata for new image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state
        """
        LOG.debug("Capture image from volume")

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)

        # This call requires the id to be in the metadata so we set it
        # from the image entry and pull it out after the call, before we
        # send the properties up to Glance

        image_meta['id'] = image_id

        # Cinder helper method to capture the boot volume, passing image_meta
        # and instance

        image_meta = self.volume_api.instance_boot_volume_capture(context,
                                                                  instance,
                                                                  image_meta)

        del image_meta['id']

        try:
            # get glance service
            glance_service, image_id = \
                glance.get_remote_image_service(context, image_id)

            # Updating instance task state before uploading image
            # Snapshot will complete but instance state will not change
            # to none in compute manager if expected state is not correct
            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                              expected_state=task_states.IMAGE_PENDING_UPLOAD)

            # Update the image metadata in Glance.
            # Set purge_props to false to do an update instead of a
            # complete replace.
            glance_service.update(context, image_id, image_meta,
                                  purge_props=False)

            LOG.debug("Snapshot added to glance.")
        except Exception as e:
            msg = _("Error updating glance image: %(image_id)s with exception "
                    "%(err)s") % dict(image_id=image_id, err=e)
            LOG.exception(msg)
            # Deleting volume in case of error.
            # We get volume mapping from meta_data which was
            # returned from the successful instance_boot_volume_capture call
            volume_mappings_str = image_meta['properties']['volume_mapping']
            volume_mappings = jsonutils.loads(volume_mappings_str)
            volume_id = volume_mappings[0]['source_volid']
            if volume_id is not None:
                self.volume_api.delete_volume(context, volume_id)

    def _get_image_meta(self, context, image_ref):
        # get current image info
        glance_service, old_image_id = glance.get_remote_image_service(
            context, image_ref)
        image_meta = glance_service.show(context, old_image_id)
        return image_meta

    def resize_volume(self, context, volume_id, size):
        LOG.debug("Entering (uid = %s, size = %s)" %
                  (volume_id, size))
        # Fix for 6721: Resize design issue.
        # Let us go through cinder directly rather than
        # going through volume_utils wrapper.
        # Let the exception be thrown from cinder we will catch
        # the exception in the operator san resize method.
        volume = volume_api.PowerVCVolumeAPI()
        volume.extend_volume(context, volume_id, size)
        # self.volume_api.extend_volume(context, volume_id, size)
        LOG.debug("Exiting")

    def _update_fibre_channel_devices(self, fc_device_name=None):
        # update device mappings from fibre channel device
        fc_device_name_list = []
        if not fc_device_name:
            wwpns = self.pvm_operator.get_wwpns()
            # hostname, wwpn = self.san_operator.get_host_from_wwpns(wwpns)
            fc_device_name_list = self.pvm_operator.get_fcs_device_names(wwpns)

        for fc_device_name in fc_device_name_list:
            self.pvm_operator.run_cfg_dev(fc_device_name)

    @logcall
    def discover_volumes_on_destination_host(self, src_wwpns, dest_wwpns,
                                             block_disk_mapping):
        """
        For the volumes that have been mapped to both source and destination
        host during LPM, run cfgdev to discover the disks and set no_reserve
        for each mapped disks. Handle the stale disk as needed

        :param src_wwpns: wwpns for LPM source host
        :param dest_wwpns: wwpns for LPM destination host
        :param block_disk_mapping: block disk mapping for the attached volumes
        """
        def extra_debug():
            """ collect additional debug information during exception.
            It is needed to validate block_device_mapping"""
            cmd = "ioscli lspath"
            debug_msg = self.pvm_operator.run_vios_command(cmd)
            LOG.debug("extra_debug(): lspath: %(debug_msg)s" % locals())

        if(not src_wwpns or not isinstance(src_wwpns, list) or
           not dest_wwpns or not isinstance(dest_wwpns, list)):
            msg = (_("discover_volumes_on_host(): fail to discover. "
                     "Invalid parameters: src_wwpns: %(src_wwpns)s or "
                     "dest_wwpns: %(dest_wwpns)s") % locals())
            LOG.exception(msg)
            ex_args = {'vdisk_uid': None,
                       'initiators': None}
            raise exception.IBMPowerVCDiscoverVolumesFailure(**ex_args)

        for bdm in block_disk_mapping:
            volume_data = bdm['connection_info']['data']
            scsi_id = volume_data['target_lun']
            # handle stale disks
            # fix for 8983 :
            # We need to pass the target_lun and target_wwn got from cinder
            # present in the bdm['connection_info']['data']  to the
            # handle_stale_disks method.It is of the format
            # {'connection_info': { u'data': {..
            # u'target_lun': u'5',
            # u'target_wwn': [u'500507680225BE98', u'500507680225BE97']}
            self.handle_stale_disk(volume_data)

        # run cfgdev to discover newly mapped disks.
        self._update_fibre_channel_devices()
        # change discovered attached volumes to no Reserved
        # for uid in mapped_uids:
        for bdm in block_disk_mapping:
            volume_data = bdm['connection_info']['data']
            scsi_id = volume_data['target_lun']
            # set no reserve
            # disk_name = self.pvm_operator.get_disk_name_by_volume_lun_id(
            #    scsi_id)
            # if disk_name:
            #    self.pvm_operator.set_no_reserve_on_hdisk(disk_name)
            aix_conn = self.pvm_operator.get_volume_aix_conn_info(volume_data)
            attach_info = self.pvm_operator.get_devname_by_aix_conn(aix_conn)
            # no_reserve has to be set before a cinder
            # volume is attached to VM. Otherwise reservation cannot
            # be changed unless the volume is detached and reattached
            # again.

            reserve_policy = self.pvm_operator.get_hdisk_reserve_policy(
                attach_info['device_name'])
            if reserve_policy != 'no_reserve':
                # set reserve_policy to no reserve in order to be
                # LPM capable. The reserve policy has to be set before
                # the disk is opened by vscsi driver to attach to The
                # host. So it cannot be done during migration time.
                self.pvm_operator.set_no_reserve_on_hdisk(
                    attach_info['device_name'])


class PowerVMLocalVolumeAdapter(PowerVMDiskAdapter):

    """Default block device providor for PowerVM

    This disk adapter uses logical volumes on the hosting VIOS
    to provide backing block devices for instances/LPARs
    """

    def __init__(self, connection, ld_ivm_operator):
        super(PowerVMLocalVolumeAdapter, self).__init__(connection)

        self.command = command.IVMCommand()
        self.pvm_operator = ld_ivm_operator
        self._connection = None
        self.connection_data = connection
        self.volume_api = PowerVCNovaLOCALVirtDriverVolumeAPI()

    def _set_connection(self):
        # create a new connection or verify an existing connection
        # and re-establish if the existing connection is dead
        self._connection = common.check_connection(self._connection,
                                                   self.connection_data)

    def create_volume(self, size):
        """Creates a logical volume with a minimum size

        :param size: size of the logical volume in bytes
        :returns: string -- the name of the new logical volume.
        :raises: PowerVMNoSpaceLeftOnVolumeGroup
        """
        return self._create_logical_volume(size)

    def delete_volume(self, volume_info):
        """Removes the Logical Volume and its associated vSCSI connection

        :param volume_info: Dictionary with volume info including name of
        Logical Volume device in /dev/ via device_name key
        """
        disk_name = volume_info["device_name"]
        if disk_name.strip():
            LOG.debug("Removing the logical volume '%s'" % disk_name)
            self._remove_logical_volume(disk_name)

    def _touch_file(self, final_path):
        '''
        Runs a command to modify timestamp of passed file
        to current time.
        '''
        commands = ['oem_setup_env',
                    ' touch %s' % final_path,
                    'exit']
        LOG.debug("Going to  Touch %s" % final_path)
        self.pvm_operator.run_interactive(commands)
        LOG.debug("Touched '%s' on remotepath" % final_path)

    # (ppedduri) Method is overridden to ensure,
    # we do touch the file if it exists & to have single lock
    @logcall
    @synchronized('image-files-manipulation', 'localdisk-')
    def _copy_image_file(self, source_path, remote_path, context, image_meta,
                         decompress=False):
        """Copy file to VIOS, decompress it, and return its new size and name.

        :param source_path: source file path
        :param remote_path remote file path
        :param decompress: if True, decompressess the file after copying;
                           if False (default), just copies the file
        """
        # Calculate source image checksum
        hasher = hashlib.md5()
        block_size = 0x10000
        img_file = file(source_path, 'r')
        buf = img_file.read(block_size)
        while len(buf) > 0:
            hasher.update(buf)
            buf = img_file.read(block_size)
        source_cksum = hasher.hexdigest()

        comp_path = os.path.join(remote_path, os.path.basename(source_path))
        if comp_path.endswith(".gz"):
            uncomp_path = os.path.splitext(comp_path)[0]
        else:
            uncomp_path = comp_path
        if not decompress:
            final_path = comp_path
        else:
            final_path = uncomp_path

        # Check whether the image is already on IVM
        output = self.run_vios_command("ls %s" % final_path,
                                       check_exit_code=False)

        # If the image does not exist already
        if not output:

            LOG.debug("Image %s not found on host" % final_path)
            # calculate required size
            img_size_mb = image_meta['size'] / (1024 * 1024)
            # Have a check if we have space on IVM
            ivm_free_space_kb = self.\
                pvm_operator.get_staging_free_space()
            ivm_free_space_mb = ivm_free_space_kb / 1024
            if ivm_free_space_mb < img_size_mb:
                # need to free-up cache area on ivm
                if not self.pvm_operator.\
                        attempt_to_delete_image_cache(context, 1,
                                                      img_size_mb):
                    # can not proceed with ftp transfer.
                    # TODO: raise a right message, with req memory details.
                    raise exception.IBMPowerVMStagingAreaOutOfSpace(
                        dir=remote_path, size=img_size_mb,
                        free=ivm_free_space_mb)

            LOG.debug(
                "Initiating ftp, from %(source_path)s to %(remote_path)s"
                % locals())
            # Copy file to IVM
            common.scp_command(self.connection_data,
                               source_path, remote_path, 'put')
            LOG.debug(
                "ftp from %(source_path)s to %(remote_path)s done" % locals())

            # Verify image file checksums match
            output = self._md5sum_remote_file(final_path)
            if not output:
                LOG.error(_("Unable to get checksum"))
                msg_args = {'file_path': final_path}
                raise exception.IBMPowerVMFileTransferFailed(**msg_args)
            target_cksum = output.split(' ')[0]
            if source_cksum != target_cksum:
                LOG.error(_("Image checksums do not match"
                            "source cksum %(source_cksum)s"
                            "target cksum %(target_cksum)s") % locals())
                msg_args = {'file_path': final_path}
                raise exception.IBMPowerVMFileTransferFailed(**msg_args)

            if decompress:
                # Unzip the image
                cmd = "/usr/bin/gunzip %s" % comp_path
                output = self.run_vios_command_as_root(cmd)

                # Remove existing image file
                cmd = "/usr/bin/rm -f %s.*" % uncomp_path
                output = self.run_vios_command_as_root(cmd)

                # Rename unzipped image
                cmd = "/usr/bin/mv %s %s" % (uncomp_path, final_path)
                output = self.run_vios_command_as_root(cmd)

                # Remove compressed image file
                cmd = "/usr/bin/rm -f %s" % comp_path
                output = self.run_vios_command_as_root(cmd)

        else:
            LOG.debug("Image found on host at '%s'" % final_path)
            # touch the file to indicate usage.
            self._touch_file(final_path)

        # Calculate file size in multiples of 512 bytes
        output = self.run_vios_command("ls -o %s|awk '{print $4}'" %
                                       final_path, check_exit_code=False)
        if output:
            size = int(output[0])
        else:
            LOG.error(_("Uncompressed image file not found"))
            msg_args = {'file_path': final_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        if (size % 512 != 0):
            size = (int(size / 512) + 1) * 512

        return final_path, size

    @logcall
    def create_volume_from_image_v2(self, context, instance, image_id):
        """ dd copy glance server raw disk to cinder lv
        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :returns: dictionary with the name of the created
                  Logical Volume device in 'device_name' key
        """
        @synchronized('ovf_image_fetch', 'localdisk-')
        def _fetch_image_from_glance(context, instance, image_id, file_path,
                                     image_meta):
            if not os.path.isfile(file_path):
                # Add a space check
                self._check_space(CONF.powervm_img_local_path,
                                  image_meta['size'])

                LOG.debug("Fetching image '%s' from glance" % image_id)
                images.fetch(context, image_id, file_path,
                             instance['user_id'],
                             instance['project_id'])
            else:
                LOG.debug("Using image found at '%s'" % file_path)

        remote_file_name = None
        # calculate root device size in bytes
        # we respect the minimum root device size in constants
        
        image_meta = self._get_image_meta(context, instance['image_ref'])
        if image_meta.get('meta_version', None) >= 2:
            return

        try:
            file_name = '.'.join([image_id, 'gz'])
            file_path = os.path.join(CONF.powervm_img_local_path,
                                         file_name)
                # sync call to avoid cksum calc before file fully gets-
                # copied.
            _fetch_image_from_glance(context, instance, image_id,
                                         file_path, image_meta)

        except Exception as ex:
            # added for the support of 3997
            LOG.error(_("Unable to transfer image"))
            msg_args = {'file_path': file_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        
        LOG.debug("Ensuring image '%s' exists on IVM" % file_path)
        remote_path = CONF.powervm_img_remote_path
        remote_file_name, image_size = self.\
            _copy_image_file(file_path,
                             remote_path,
                             context,
                             image_meta)
        return remote_file_name

    @logcall
    def copy_image_file_to_cinder_lv(self, image_id, attach_info):
        
        if attach_info['is_boot_volume']:
            disk_name = attach_info['device_name']
            file_name = '.'.join([image_id, 'gz'])
            remote_file_name = os.path.join(CONF.powervm_img_remote_path,
                                        file_name)
            if not os.path.exists(remote_file_name):
                msg_args = {'image_id' : image_id, 'file_dir': CONF.powervm_img_remote_path}
                #raise exception.IBMPowerVMImageFile(**msg_args)
                LOG.debug(_("Not Found %(image_id)s.gz file in %(file_dir)s") % msg_args)
            else:
                LOG.debug("Copying image to the device '%s'" % disk_name)
                self._copy_file_to_device(remote_file_name, disk_name)
                time.sleep(10)

    @logcall
    def create_volume_from_image(self, context, instance, image_id):
        """Creates a Logical Volume and copies the specified image to it

        :param context: nova context used to retrieve image from glance
        :param instance: instance to create the volume for
        :param image_id: image_id reference used to locate image in glance
        :returns: dictionary with the name of the created
                  Logical Volume device in 'device_name' key
        """
        # inner method to block race condition in source chsum calculation.
        @synchronized('ovf_image_fetch', 'localdisk-')
        def _fetch_image_from_glance(context, instance, image_id, file_path,
                                     image_meta):
            if not os.path.isfile(file_path):
                # Add a space check
                self._check_space(CONF.powervm_img_local_path,
                                  image_meta['size'])

                LOG.debug("Fetching image '%s' from glance" % image_id)
                images.fetch(context, image_id, file_path,
                             instance['user_id'],
                             instance['project_id'])
            else:
                LOG.debug("Using image found at '%s'" % file_path)

        remote_file_name = None
        # calculate root device size in bytes
        # we respect the minimum root device size in constants
        size_gb = 0
        instance_type = flavors.extract_flavor(instance)
        if instance_type['root_gb'] > 0:
            size_gb = max(
                instance_type['root_gb'],
                constants.POWERVM_MIN_ROOT_GB)
        elif instance_type['ephemeral_gb'] > 0:
            size_gb = max(
                instance_type['ephemeral_gb'],
                constants.POWERVM_MIN_ROOT_GB)
        else:
            size_gb = constants.POWERVM_MIN_ROOT_GB
        size = size_gb * 1024 * 1024 * 1024

        image_meta = self._get_image_meta(context, instance['image_ref'])

        disk_format = None
        if 'disk_format' in image_meta:
            disk_format = image_meta['disk_format']

        image_detail = {}
        disk_name = None
        try:
            if disk_format == 'iso':
                vopt_img_name = self.\
                    _prepare_iso_image_on_host(context, instance,
                                               CONF.powervm_img_local_path,
                                               CONF.powervm_img_remote_path,
                                               image_meta)
                # command = self.command.mkvopt('-name %s -file %s -ro' %
                #                            (vopt_img_name, remote_file_name))
                # self.run_vios_command(command)
                image_detail['vopt'] = vopt_img_name

                # decompress = True
            else:
                try:
                    file_name = '.'.join([image_id, 'gz'])
                    file_path = os.path.join(CONF.powervm_img_local_path,
                                             file_name)
                    # sync call to avoid cksum calc before file fully gets-
                    # copied.
                    _fetch_image_from_glance(context, instance, image_id,
                                             file_path, image_meta)

                except Exception as ex:
                    # added for the support of 3997
                    LOG.error(_("Unable to transfer image"))
                    msg_args = {'file_path': file_path}
                    raise exception.IBMPowerVMFileTransferFailed(**msg_args)
                # TODO: add try block here
                LOG.debug("Ensuring image '%s' exists on IVM" % file_path)
                remote_path = CONF.powervm_img_remote_path
                remote_file_name, image_size = self.\
                    _copy_image_file(file_path,
                                     remote_path,
                                     context,
                                     image_meta)

            LOG.debug("Creating logical volume of size %s bytes" % size)
            disk_name = self._create_logical_volume(size)
            image_detail['device_name'] = disk_name
            if remote_file_name is not None:
                LOG.debug("Copying image to the device '%s'" % disk_name)
                self._copy_file_to_device(remote_file_name, disk_name)
        except Exception:
            LOG.error(_("Error while creating logical volume from image. "
                        "Will attempt cleanup."))
            # attempt cleanup of logical volume before re-raising exception
            with excutils.save_and_reraise_exception():
                if disk_name is not None:
                    try:
                        self.delete_volume(image_detail)
                    except Exception:
                        msg = _('Error while attempting cleanup of failed '
                                'deploy to logical volume.')
                        LOG.exception(msg)

        return image_detail

    def delete_file_from_mgmt_server(self, file_path):
        try:
            os.remove(file_path)
        except OSError as ose:
            LOG.warn(_("Failed to clean up file "
                       "%(file_path)s") % locals())
    
    def create_image_from_volume_v2(self, instance, device_name, context,
                                 image_id, image_meta, update_task_state):
        """Capture the contents of a volume and upload to glance
    
        :param instance: the VM instance
        :param device_name: device in /dev/ on IVM to capture
        :param context: nova context for operation
        :param image_id: image reference to pre-created image in glance
        :param image_meta: metadata for new image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state
        """
        LOG.debug("Capture image from volume")
    
        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
    
        # This call requires the id to be in the metadata so we set it
        # from the image entry and pull it out after the call, before we
        # send the properties up to Glance

        image_meta['id'] = image_id
    
        # Cinder helper method to capture the boot volume, passing image_meta
        # and instance
    
        image_meta = self.volume_api.instance_boot_volume_capture(context,
                                                                  instance,
                                                                  image_meta)
    
        del image_meta['id']
    
        try:
            # get glance service
            glance_service, image_id = \
                glance.get_remote_image_service(context, image_id)

            # Updating instance task state before uploading image
            # Snapshot will complete but instance state will not change
            # to none in compute manager if expected state is not correct
            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                              expected_state=task_states.IMAGE_PENDING_UPLOAD)
    
            # Update the image metadata in Glance.
            # Set purge_props to false to do an update instead of a
            # complete replace.
            glance_service.update(context, image_id, image_meta,
                                   purge_props=False)
    
            LOG.debug("Snapshot added to glance.")
        except Exception as e:
            msg = _("Error updating glance image: %(image_id)s with exception "
                    "%(err)s") % dict(image_id=image_id, err=e)
            LOG.exception(msg)
            # Deleting volume in case of error.
            # We get volume mapping from meta_data which was
            # returned from the successful instance_boot_volume_capture call
            volume_mappings_str = image_meta['properties']['volume_mapping']
            volume_mappings = jsonutils.loads(volume_mappings_str)
            volume_id = volume_mappings[0]['source_volid']
            if volume_id is not None:
                self.volume_api.delete_volume(context, volume_id)

    def create_image_from_volume(self, instance, device_name, context,
                                 image_id, image_meta, update_task_state):
        """Capture the contents of a volume and upload to glance

        :param device_name: device in /dev/ to capture
        :param context: nova context for operation
        :param image_id: image reference to pre-created image in glance
        :param image_meta: metadata for new image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state.
        """
        # Updating instance task state before capturing instance as a file
        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)

        # do the disk copy
        dest_file_path = common.aix_path_join(CONF.powervm_img_remote_path,
                                              image_id)

        # new call - dd o/p to gzip
        # compress and copy the file back to the nova-compute host
        snapshot_file_path, capture_file_size = self.\
            _copy_compressed_image_file_from_host(device_name, dest_file_path,
                                                  CONF.powervm_img_local_path,
                                                  compress=True)

        # get glance service
        glance_service, image_id = glance.get_remote_image_service(
            context, image_id)

        # Updating instance task state before uploading image
        # Snapshot will complete but instance state will not change
        # to none in compute manager if expected state is not correct
        update_task_state(task_state=task_states.IMAGE_UPLOADING,
                          expected_state=task_states.IMAGE_PENDING_UPLOAD)
        # (ppedduri): calculate if Management server has the size we-
        # need for the glance upload. proceed with upload only if space is-
        # available.if glance and nova are on different hosts, we will have to-
        # modify the checks.check if required space is available on Management
        # Server. raise an exception if capture_file_size is a constraint.
        glance_file_store = "/var/lib/glance/images"  # CONF.img_repo_path?
        #self._check_space(glance_file_store, capture_file_size)

        # add property to specify min size required while deploying
        target_min_disk = max(instance['root_gb'], instance['ephemeral_gb'])
        # or read it from instance type template and set it.
        image_meta['min_disk'] = target_min_disk
        # upload snapshot file to glance
        with open(snapshot_file_path, 'r') as img_file:
            glance_service.update(context, image_id, image_meta, img_file)
            LOG.debug("Snapshot added to glance.")

        # clean up local image file
        self.delete_file_from_mgmt_server(snapshot_file_path)

    def migrate_volume(self, lv_name, src_host, dest, image_path,
                       instance_name=None):
        """Copy a logical volume to file, compress, and transfer

        :param lv_name: logical volume device name
        :param dest: destination IP or DNS name
        :param image_path: path to remote image storage directory
        :param instance_name: name of instance that is being migrated
        :returns: file path on destination of image file that was moved
        """
        # removed old and unused code, to fix defect 22069
        pass

    def _get_image_meta(self, context, image_ref):
        # get current image info
        glance_service, old_image_id = glance.get_remote_image_service(
            context, image_ref)
        image_meta = glance_service.show(context, old_image_id)
        return image_meta

    @logcall
    @synchronized('iso_prep', 'iso-')
    def _prepare_iso_image_on_host(self, context, instance, local_img_dir,
                                   host_dir, image_meta):
        """
        Prepares an ISO file on a the host.  It will transfer
        the file to the host and create virtual optical media
        object for it.
        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param local_img_dir: Local image directory
        :param host_dir: Remote (host) image directory
        :param image_meta: metadata for the image
        :returns the name of the vopt object for the ISO
        """
        pvm_op = self.pvm_operator

        # Check if media library exists. Error out if it's not there.
        if not pvm_op.check_media_library_exists():
            LOG.error(_("There is no media library"))
            raise exception.IBMPowerVMMediaLibraryNotAvailable()

        # Create vopt with name being the glance ID/UUID.
        vopt_img_name = image_meta['id']
        if not pvm_op.check_vopt_exists(name=vopt_img_name):

            # check for space within CONF.powervm_img_local_path
            self._check_space(local_img_dir, image_meta['size'])

            # It's not VIOS yet:
            # Check available space in IVM staging area and media repository
            free_space = pvm_op.get_vopt_size()
            free_padmin_space = pvm_op.get_staging_size(host_dir)

            free_rep_mb = int(free_space[0])
            free_staging_mb = int(free_padmin_space[0]) / 1024
            iso_size_mb = image_meta['size'] / (1024 * 1024)

            LOG.debug("Free repository space: %s MB" % free_rep_mb)
            LOG.debug("Free VIOS staging space: %s MB" % free_staging_mb)
            LOG.debug("ISO file size: %s MB" % iso_size_mb)

            if iso_size_mb > free_rep_mb:
                raise common_ex.IBMPowerVMMediaRepOutOfSpace(
                    size=iso_size_mb, free=free_rep_mb)

            if iso_size_mb > free_staging_mb:
                raise exception.IBMPowerVMStagingAreaOutOfSpace(
                    dir=host_dir, size=iso_size_mb, free=free_staging_mb)

            # Fetch ISO from Glance

            file_path = '%s/%s.%s.%s' % (local_img_dir,
                                         image_meta['id'],
                                         CONF.host,
                                         image_meta['disk_format'])
            LOG.debug("Fetching image '%(img-name)s' from glance "
                      "to %(img-path)s" %
                      {'img-name': image_meta['name'],
                       'img-path': file_path})
            images.fetch(context, image_meta['id'], file_path,
                         instance['user_id'],
                         instance['project_id'])
            if (os.path.isfile(file_path)):
                # transfer ISO to VIOS
                try:

                    iso_remote_path, iso_size = self.\
                        _copy_image_file(file_path,
                                         host_dir,
                                         context,
                                         image_meta)

                    self.pvm_operator.create_vopt_device(
                        vopt_img_name, iso_remote_path)
                    # The create vopt device command copies
                    # the ISO into the media repository.  The file at
                    # iso_remote_path can be removed now.
                    try:
                        self.pvm_operator.remove_ovf_env_iso(
                            iso_remote_path)
                    except (exception.IBMPowerVMCommandFailed, Exception) as e:
                        msg = (_('Error cleaning up ISO:'
                                 ' %(iso_remote_path)s') %
                               locals())
                        LOG.exception(msg)
                        raise e
                finally:
                    os.remove(file_path)
            else:
                raise exception.IBMPowerVMISOFileNotFound(
                    ISO_file=file_path)
        else:
            LOG.debug("Image with id %s already on VIOS." % image_meta['id'])

        return vopt_img_name

    def _create_logical_volume_old(self, size):
        """Creates a logical volume with a minimum size.

        :param size: size of the logical volume in bytes
        :returns: string -- the name of the new logical volume.
        :raises: PowerVMNoSpaceLeftOnVolumeGroup
        """
        commands = ['oem_setup_env',
                    'lsvg -o',
                    'exit']
        vgs = self.pvm_operator.run_interactive(commands)
        cmd = self.command.lsvg('%s -field vgname freepps -fmt :' %
                                ' '.join(vgs))
        output = self.run_vios_command(cmd)
        found_vg = None

        # If it's not a multiple of 1MB we get the next
        # multiple and use it as the megabyte_size.
        megabyte = 1024 * 1024
        if (size % megabyte) != 0:
            megabyte_size = int(size / megabyte) + 1
        else:
            megabyte_size = size / megabyte

        # Search for a volume group with enough free space for
        # the new logical volume.
        for vg in output:
            # Returned output example: 'rootvg:396 (25344 megabytes)'
            match = re.search(r'^(\w+):\d+\s\((\d+).+$', vg)
            if match is None:
                continue
            vg_name, avail_size = match.groups()
            if megabyte_size <= int(avail_size):
                found_vg = vg_name
                break

        if not found_vg:
            LOG.error(_('Could not create logical volume. '
                        'No space left on any volume group.'))
            raise exception.PowerVMNoSpaceLeftOnVolumeGroup()

        cmd = self.command.mklv('%s %sB' % (found_vg, size / 512))
        lv_name = self.run_vios_command(cmd)[0]
        return lv_name


    def _get_vg_info(self):
        commands = ['oem_setup_env',
                    'lsvg -o',
                    'exit']
        vgs = self.pvm_operator.run_interactive(commands)
        vglist = []
        for vg in vgs:
            if vg is not None:
                cmd = self.command.lsvg('%s -field vgname freepps -fmt :' % vg)
                output = self.run_vios_command(cmd)
                vginfo = {"vgname":'','avail_size':''}
                for line in output:
                    if line is not None:
                        match = re.search(r'^(\w+):\d+\s\((\d+).+$', line)
                        if match is None:
                            continue
                        vg_name, avail_size = match.groups()
                        vginfo['vgname'] = vg_name
                        vginfo['avail_size'] = avail_size
                        break
                vglist.append(vginfo)
        return vglist

    def _create_logical_volume(self, size):
        """Creates a logical volume with a minimum size.

        :param size: size of the logical volume in bytes
        :returns: string -- the name of the new logical volume.
        :raises: PowerVMNoSpaceLeftOnVolumeGroup
        """
        vginfos = self._get_vg_info()
        first_choice_vg = CONF.first_choice_vg

        megabyte = 1024 * 1024
        if (size % megabyte) != 0:
            megabyte_size = int(size / megabyte) + 1
        else:
            megabyte_size = size / megabyte

        vglist = [vg['vgname'] for vg in vginfos]
        found_vg = None
        #if first_choice_vg in vglist
        if first_choice_vg in vglist:
            for vg in vginfos:
                if vg is not None:
                    if vg['vgname'] == first_choice_vg:
                        avail_size = vg['avail_size']
                        break
            if megabyte_size <= int(avail_size):
                found_vg = first_choice_vg
            else:
                for vg in vginfos:
                    if vg is not None:
                        vgname = vg['vgname']
                        avail_size = vg['avail_size']
                        if megabyte_size <= int(avail_size):
                            found_vg = vgname
                            break
        else:
            for vg in vginfos:
                if vg is not None:
                    vgname = vg['vgname']
                    avail_size = vg['avail_size']
                    if megabyte_size <= int(avail_size):
                        found_vg = vgname
                        break
        if not found_vg:
            LOG.error(_('Could not create logical volume. '
                        'No space left on any volume group.'))
            raise exception.PowerVMNoSpaceLeftOnVolumeGroup()

        cmd = self.command.mklv('%s %sB' % (found_vg, size / 512))
        lv_name = self.run_vios_command(cmd)[0]
        return lv_name




    def _remove_logical_volume(self, lv_name):
        """Removes the lv and the connection between its associated vscsi.

        :param lv_name: a logical volume name
        """
        cmd = self.command.rmvdev('-vdev %s -rmlv' % lv_name)
        self.run_vios_command(cmd)

    @logcall
    @synchronized('device-copy', 'localdisk-')
    def _copy_file_to_device(self, source_path, device, decompress=True):
        """Copy file to device.

        :param source_path: path to input source file
        :param device: output device name
        :param decompress: if True (default) the file will be decompressed
                           on the fly while being copied to the drive
        """
        LOG.debug("Copying file %s to device /dev/%s" % (source_path, device))
        if decompress:
            cmd = ('gunzip -c %s > /dev/%s' % (source_path, device))
        else:
            cmd = 'dd if=%s of=/dev/%s bs=1024k' % (source_path, device)
        self.run_vios_command_as_root(cmd)

    @logcall
    @synchronized('device-copy', 'localdisk-')
    def _copy_device_to_file(self, device, file_path):
        """Copy a device to a file using dd

        :param device: device name to copy from
        :param file_path: output file path
        """
        LOG.debug("Copying device /dev/%s to file %s" % (device, file_path))
        cmd = 'dd if=/dev/%s of=%s bs=1024k' % (device, file_path)
        self.run_vios_command_as_root(cmd)

    def _copy_compressed_image_file_from_host(self, device_name,
                                              remote_source_path,
                                              local_dest_dir, compress=False):
        """
        Copy a file from IVM to the nova-compute host,
        and return the location of the copy

        :param device_name device name to copy from
        :param remote_source_path remote source file path
        :param local_dest_dir local destination directory
        :param compress: if True, compress the file before transfer;
                         if False (default), copy the file as is
        """

        temp_str = common.aix_path_join(local_dest_dir,
                                        os.path.basename(remote_source_path))
        local_file_path = temp_str + '.gz'

        if compress:
            copy_from_path = remote_source_path + '.gz'
        else:
            copy_from_path = remote_source_path

        try:

            try:
                cmd = 'dd if=/dev/%s bs=1024k | /usr/bin/gzip -c > %s ' % (
                    device_name, copy_from_path)
                self.run_vios_command_as_root(cmd)
            except Exception as e:
                # dd command failed, raise an exception.
                free_staging_mb = self.pvm_operator.\
                    get_staging_free_space()
                raise exception.IBMPowerVMInsufficientStagingMemory(
                    operation='command - dd', free=free_staging_mb)

            # Get file checksum
            output = self._md5sum_remote_file(copy_from_path)
            if not output:
                LOG.error(_("Unable to get checksum"))
                msg_args = {'file_path': copy_from_path}
                raise exception.IBMPowerVMFileTransferFailed(**msg_args)
            else:
                source_chksum = output.split(' ')[0]
            # calculate file capture_file_size of copy_from_path.
            # Calculate file capture_file_size in multiples of 512 bytes
            output = self.run_vios_command(
                "ls -o %s|awk '{print $4}'" %
                copy_from_path, check_exit_code=False)
            if output:
                capture_file_size = int(output[0])
            else:
                LOG.error(_("Copy compressed file failed, since"
                            " file %s not found") % copy_from_path)
                raise exception.IBMPowerVMFileTransferFailed(
                    filepath=copy_from_path)
            if (capture_file_size % 512 != 0):
                capture_file_size = (int(capture_file_size / 512) + 1) * 512
            # check if required space is available on Management Server.
            # raise an exception if capture_file_size is a constraint.
            self._check_space(local_dest_dir, capture_file_size * 2)

            # Copy file to host
            common.scp_command(self.connection_data,
                               local_file_path,
                               copy_from_path,
                               'get')

            # Calculate copied image checksum
            dest_chksum = self._checksum_local_file(local_file_path)

            # do comparison
            if source_chksum and dest_chksum != source_chksum:
                LOG.error(_("Image checksums do not match"))
                raise exception.IBMPowerVMFileTransferFailed(
                    file_path=local_file_path)
        finally:
            # Cleanup transferred remote file
            cmd = "/usr/bin/rm -f %s" % copy_from_path
            output = self.run_vios_command_as_root(cmd)
        # return capture_file_size to calling function
        return local_file_path, capture_file_size

    def _check_space(self, dest_dir, size_required):
        st = os.statvfs(dest_dir)
        free_space_kb = st.f_bavail * st.f_frsize
        if size_required > free_space_kb:
            ex_args = {'path': dest_dir,
                       'size': (size_required / (1024 * 1024)),
                       'free': free_space_kb / (1024 * 1024)}
            raise exception.IBMPowerVMManagementServerOutOfSpace(**ex_args)

    def _copy_image_file_from_host(self, remote_source_path,
                                   local_dest_dir, compress=False):
        """
        Copy a file from IVM to the nova-compute host,
        and return the location of the copy

        :param remote_source_path remote source file path
        :param local_dest_dir local destination directory
        :param compress: if True, compress the file before transfer;
                         if False (default), copy the file as is
        """

        temp_str = common.aix_path_join(local_dest_dir,
                                        os.path.basename(remote_source_path))
        local_file_path = temp_str + '.gz'

        if compress:
            copy_from_path = remote_source_path + '.gz'
        else:
            copy_from_path = remote_source_path

        if compress:
            # Gzip the file
            cmd = "/usr/bin/gzip %s" % remote_source_path
            self.run_vios_command_as_root(cmd)

            # Cleanup uncompressed remote file
            cmd = "/usr/bin/rm -f %s" % remote_source_path
            self.run_vios_command_as_root(cmd)

        # Get file checksum
        output = self._md5sum_remote_file(copy_from_path)
        if not output:
            LOG.error(_("Unable to get checksum"))
            msg_args = {'file_path': copy_from_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        else:
            source_chksum = output.split(' ')[0]

        # Copy file to host
        common.scp_command(self.connection_data,
                           local_file_path,
                           copy_from_path,
                           'get')

        # Calculate copied image checksum
        dest_chksum = self._checksum_local_file(local_file_path)

        # do comparison
        if source_chksum and dest_chksum != source_chksum:
            LOG.error(_("Image checksums do not match"))
            raise exception.IBMPowerVMFileTransferFailed(
                file_path=local_file_path)

        # Cleanup transferred remote file
        cmd = "/usr/bin/rm -f %s" % copy_from_path
        output = self.run_vios_command_as_root(cmd)

        return local_file_path

    def get_initiator(self):
        return None

    @logcall
    def handle_stale_disk(self, volume_info):

        if not volume_info or not isinstance(volume_info, dict):
            msg = _("volume_info is null or not a dictionary")
            raise exception.IBMPowerVMInvalidParameters(err=msg)

        if(len(volume_info.keys()) < 1 or
            not 'volume_name' in volume_info.keys()):
            msg = (_("Invalid volume_info: %(volume_info)s") % locals())
            raise exception.IBMPowerVMInvalidParameters(err=msg)

        aix_conn = self.pvm_operator.get_volume_aix_conn_info(volume_info)

        try:
            dev_info = self.pvm_operator.get_devname_by_aix_conn(
                aix_conn,
                all_dev_states=True
            )
        except exception.IBMPowerVMInvalidLUNPathInfo:
            msg = ("No stale disk discovered at location: %(aix_conn)s"
                   % locals())
            LOG.debug(msg)
            # This is good path, no stale disk detected.
            dev_info = None

        if dev_info:
            # We found a stale disk, handle it
            hdisk = dev_info['device_name']
            msg = (_("Clean up stale disk %(hdisk)s with connection "
                     "info: %(aix_conn)s") % locals())
            LOG.info(msg)
            # Clear up possible existing vhost mapping.
            try:
                self.pvm_operator.detach_disk_from_vhost(hdisk)
            except Exception:
                # If there was no vhost map, it will throw exception. Move on.
                pass

class ISCSIDiskAdapter(PowerVMLocalVolumeAdapter):
    def __init__(self, pvm_operator):          
        self.volume_api = PowerVCNovaISCSIVirtDriverVolumeAPI()
        self.pvm_operator = pvm_operator
        self._initiator = None
        self._iscsi = None
            
        self._connection = None
        self.connection_data = self.pvm_operator.connection_data
        
    def _check_space(self, dest_dir, size_required):
        st = os.statvfs(dest_dir)
        free_space_kb = st.f_bavail * st.f_frsize
        if size_required > free_space_kb:
            ex_args = {'path': dest_dir,
                       'size': (size_required / (1024 * 1024)),
                       'free': free_space_kb / (1024 * 1024)}
            raise exception.IBMPowerVMManagementServerOutOfSpace(**ex_args)
            
    @logcall
    @synchronized('iso_prep', 'iso-')
    def _prepare_iso_image_on_host(self, context, instance, local_img_dir,
                                   host_dir, image_meta):
        """
        Prepares an ISO file on a the host.  It will transfer
        the file to the host and create virtual optical media
        object for it.
        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param local_img_dir: Local image directory
        :param host_dir: Remote (host) image directory
        :param image_meta: metadata for the image
        :returns the name of the vopt object for the ISO
        """
        pvm_op = self.pvm_operator

        # Check if media library exists. Error out if it's not there.
        if not pvm_op.check_media_library_exists():
            LOG.error(_("There is no media library"))
            raise exception.IBMPowerVMMediaLibraryNotAvailable()

        # Create vopt with name being the glance ID/UUID.
        vopt_img_name = image_meta['id']
        if not pvm_op.check_vopt_exists(name=vopt_img_name):
            # check for space within CONF.powervm_img_local_path
            self._check_space(local_img_dir, image_meta['size'])

            # It's not VIOS yet:
            # Check available space in IVM staging area and media repository
            free_space = pvm_op.get_vopt_size()
            free_padmin_space = pvm_op.get_staging_size(host_dir)

            free_rep_mb = int(free_space[0])
            free_staging_mb = int(free_padmin_space[0]) / 1024
            iso_size_mb = image_meta['size'] / (1024 * 1024)

            LOG.debug("Free repository space: %s MB" % free_rep_mb)
            LOG.debug("Free VIOS staging space: %s MB" % free_staging_mb)
            LOG.debug("ISO file size: %s MB" % iso_size_mb)

            if iso_size_mb > free_rep_mb:
                raise common_ex.IBMPowerVMMediaRepOutOfSpace(
                    size=iso_size_mb, free=free_rep_mb)

            if iso_size_mb > free_staging_mb:
                raise exception.IBMPowerVMStagingAreaOutOfSpace(
                    dir=host_dir, size=iso_size_mb, free=free_staging_mb)

            # Fetch ISO from Glance

            file_path = '%s/%s.%s.%s' % (local_img_dir,
                                         image_meta['id'],
                                         CONF.host,
                                         image_meta['disk_format'])
            LOG.debug("Fetching image '%(img-name)s' from glance "
                      "to %(img-path)s" %
                      {'img-name': image_meta['name'],
                       'img-path': file_path})
            images.fetch(context, image_meta['id'], file_path,
                         instance['user_id'],
                         instance['project_id'])
            if (os.path.isfile(file_path)):
                # transfer ISO to VIOS
                try:

                    iso_remote_path, iso_size = self.copy_image_file(file_path,
                                                                     host_dir)

                    self.pvm_operator.create_vopt_device(
                        vopt_img_name, iso_remote_path)
                    # The create vopt device command copies
                    # the ISO into the media repository.  The file at
                    # iso_remote_path can be removed now.
                    try:
                        self.pvm_operator.remove_ovf_env_iso(iso_remote_path)
                    except Exception:
                        msg = (_('Error cleaning up ISO:'
                                 ' %(iso_remote_path)s') %
                               locals())
                        LOG.exception(msg)
                finally:
                    os.remove(file_path)
            else:
                raise exception.IBMPowerVMISOFileNotFound(
                    ISO_file=file_path)
        else:
            LOG.debug("Image with id %s already on VIOS."
                      % image_meta['id'])

        return vopt_img_name
            
    @logcall
    def create_image_from_volume(self, instance, device_name, context,
                                 image_id, image_meta, update_task_state):
        """Capture the contents of a volume and upload to glance
    
        :param instance: the VM instance
        :param device_name: device in /dev/ on IVM to capture
        :param context: nova context for operation
        :param image_id: image reference to pre-created image in glance
        :param image_meta: metadata for new image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state
        """
        LOG.debug("Capture image from volume")
    
        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
    
        # This call requires the id to be in the metadata so we set it
        # from the image entry and pull it out after the call, before we
        # send the properties up to Glance

        image_meta['id'] = image_id
    
        # Cinder helper method to capture the boot volume, passing image_meta
        # and instance
    
        image_meta = self.volume_api.instance_boot_volume_capture(context,
                                                                  instance,
                                                                  image_meta)
    
        del image_meta['id']
    
        try:
            # get glance service
            glance_service, image_id = \
                glance.get_remote_image_service(context, image_id)

            # Updating instance task state before uploading image
            # Snapshot will complete but instance state will not change
            # to none in compute manager if expected state is not correct
            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                              expected_state=task_states.IMAGE_PENDING_UPLOAD)
    
            # Update the image metadata in Glance.
            # Set purge_props to false to do an update instead of a
            # complete replace.
            glance_service.update(context, image_id, image_meta,
                                   purge_props=False)
    
            LOG.debug("Snapshot added to glance.")
        except Exception as e:
            msg = _("Error updating glance image: %(image_id)s with exception "
                    "%(err)s") % dict(image_id=image_id, err=e)
            LOG.exception(msg)
            # Deleting volume in case of error.
            # We get volume mapping from meta_data which was
            # returned from the successful instance_boot_volume_capture call
            volume_mappings_str = image_meta['properties']['volume_mapping']
            volume_mappings = jsonutils.loads(volume_mappings_str)
            volume_id = volume_mappings[0]['source_volid']
            if volume_id is not None:
                self.volume_api.delete_volume(context, volume_id)
    
    @logcall
    def handle_stale_disk(self, volume_info):
        
        if not volume_info or not isinstance(volume_info, dict):
            msg = _("volume_info is null or not a dictionary")
            raise exception.IBMPowerVMInvalidParameters(err=msg)
            
        if(len(volume_info.keys()) < 2 or 
            not 'target_iqn' in volume_info.keys() or
            not 'target_lun' in volume_info.keys()):
            msg = (_("Invalid volume_info: %(volume_info)s") % locals())
            raise exception.IBMPowerVMInvalidParameters(err=msg)
            
        aix_conn = self.pvm_operator.get_volume_aix_conn_info(volume_info)
            
        try:
            dev_info = self.pvm_operator.get_devname_by_aix_conn(
                aix_conn,
                all_dev_states=True
            )
        except exception.IBMPowerVMInvalidLUNPathInfo:
            msg = ("No stale disk discovered at location: %(aix_conn)s"
                   % locals())
            LOG.debug(msg)
            # This is good path, no stale disk detected.
            dev_info = None
    
        if dev_info:
            # We found a stale disk, handle it
            hdisk = dev_info['device_name']
            msg = (_("Clean up stale disk %(hdisk)s with connection "
                     "info: %(aix_conn)s") % locals())
            LOG.info(msg)
            # Clear up possible existing vhost mapping.
            try:
                self.pvm_operator.detach_disk_from_vhost(hdisk)
            except Exception:
                # If there was no vhost map, it will throw exception. Move on.
                pass
            self.pvm_operator.remove_disk(hdisk)
