'''
Created on Feb 27, 2015

@author: root
'''
import time

from paxes_nova.virt.ibmpowervm.ivm import blockdev
from paxes_nova.virt.ibmpowervm.ivm import command
from paxes_nova.virt.ibmpowervm.ivm.blockdev import ISCSIDiskAdapter
from paxes_nova.virt.ibmpowervm.common.volume_utils \
    import PowerVCNovaVirtDriverVolumeAPI
from paxes_nova.virt.ibmpowervm.common.volume_utils \
    import PowerVCNovaISCSIVirtDriverVolumeAPI
from paxes_nova.virt.ibmpowervm.common.volume_utils \
    import PowerVCNovaLOCALVirtDriverVolumeAPI
from paxes_nova.virt.ibmpowervm.ivm import exception
from nova.openstack.common import excutils
from paxes_nova.virt.ibmpowervm.ivm import common
from nova.openstack.common.lockutils import synchronized
from paxes_nova import logcall
from nova.openstack.common import log as logging
import os
import hashlib
LOG = logging.getLogger(__name__)
# class PowerVMDiskLocalAdapter(blockdev.PowerVMDiskAdapter):
#     pass


class SANDiskLocalAdapter(blockdev.SANDiskAdapter):
    def __init__(self, pvm_operator):
        self.volume_api = PowerVCNovaVirtDriverVolumeAPI()
        self.pvm_operator = pvm_operator
        self._initiator = None
        self._wwpns = None
        self._connection = None

    def _md5sum_remote_file(self, remote_path):
        # AIX6/VIOS cannot md5sum files with sizes greater than ~2GB
        cmd = ("perl -MDigest::MD5 -e 'my $file = \"%s\"; open(FILE, $file); "
               "binmode(FILE); "
               "print Digest::MD5->new->addfile(*FILE)->hexdigest, "
               "\" $file\n\";'" % remote_path)

        # output = os.popen(cmd)
        output, err = self.pvm_operator.run_vios_command_as_root_with_shell(cmd)
        return output

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
                # common.scp_command(self.connection_data,
                #                   source_path, remote_path, 'put')
                cp_cmd = 'cp' + ' -f ' + source_path + ' ' + remote_path
                self.run_vios_command(cp_cmd)
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

        # Calculate file size in multiples of 512 bytes |awk '{print $4}'
        output = self.run_vios_command("ls -o %s" % final_path, check_exit_code=False)
        if output:
            out = output[0].split()
            size = int(out[3])
        else:
            LOG.error(_("Uncompressed image file not found"))
            msg_args = {'file_path': final_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        if (size % 512 != 0):
            size = (int(size / 512) + 1) * 512

        return final_path, size


class PowerVMLocalVolumeLocalAdapter(blockdev.PowerVMLocalVolumeAdapter):
    def __init__(self, ld_ivm_operator):
        self.command = command.IVMCommand()
        self.pvm_operator = ld_ivm_operator
        self._connection = None
        self.volume_api = PowerVCNovaLOCALVirtDriverVolumeAPI()

    def _md5sum_remote_file(self, remote_path):
        # AIX6/VIOS cannot md5sum files with sizes greater than ~2GB
        cmd = ("perl -MDigest::MD5 -e 'my $file = \"%s\"; open(FILE, $file); "
               "binmode(FILE); "
               "print Digest::MD5->new->addfile(*FILE)->hexdigest, "
               "\" $file\n\";'" % remote_path)

        # output = os.popen(cmd)
        output, err = self.pvm_operator.run_vios_command_as_root_with_shell(cmd)
        return output

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
                # common.scp_command(self.connection_data,
                #                   source_path, remote_path, 'put')
                cp_cmd = 'cp' + ' -f ' + source_path + ' ' + remote_path
                self.run_vios_command(cp_cmd)
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

        # Calculate file size in multiples of 512 bytes, |awk '{print $4}'
        output = self.run_vios_command("ls -o %s" % final_path, check_exit_code=False)
        if output:
            out = output[0].split()
            size = int(out[3])
        else:
            LOG.error(_("Uncompressed image file not found"))
            msg_args = {'file_path': final_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        if (size % 512 != 0):
            size = (int(size / 512) + 1) * 512

        return final_path, size

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
            # common.scp_command(self.connection_data,
            #                   source_path, remote_path, 'put')
            cp_cmd = 'cp' + ' -f ' + source_path + ' ' + remote_path
            self.run_vios_command(cp_cmd)
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

        # Calculate file size in multiples of 512 bytes |awk '{print $4}'
        output = self.run_vios_command("ls -o %s" % final_path, check_exit_code=False)
        if output:
            out = output[0].split()
            size = int(out[3])
        else:
            LOG.error(_("Uncompressed image file not found"))
            msg_args = {'file_path': final_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        if (size % 512 != 0):
            size = (int(size / 512) + 1) * 512

        return final_path, size

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
                # self.run_vios_command_as_root(cmd)
                self.pvm_operator.run_vios_command_as_root_with_shell(cmd)
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
            # Calculate file capture_file_size in multiples of 512 bytes |awk '{print $4}'
            output = self.run_vios_command("ls -o %s" % copy_from_path, check_exit_code=False)
            if output:
                out = output[0].split()
                capture_file_size = int(out[3])
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
            # common.scp_command(self.connection_data,
            #                   local_file_path,
            #                   copy_from_path,
            #                   'get')
            cp_cmd = 'cp' + ' -f ' + copy_from_path + ' ' + local_file_path
            self.run_vios_command(cp_cmd)
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
#         common.scp_command(self.connection_data,
#                            local_file_path,
#                            copy_from_path,
#                            'get')
        cp_cmd = 'cp' + ' -f ' + copy_from_path + ' ' + local_file_path
        self.run_vios_command(cp_cmd)
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
#             cmd = ('gunzip -c %s > /dev/%s' % (source_path, device))
            cmd = ('dd if=%s bs=1024k | /usr/bin/gunzip -c > /dev/%s' % (source_path, device))
        else:
            cmd = 'dd if=%s of=/dev/%s bs=1024k' % (source_path, device)
        self.pvm_operator.run_vios_command_as_root_with_shell(cmd)

    @logcall
    @synchronized('device-copy', 'localdisk-')
    def _copy_device_to_file(self, device, file_path):
        """Copy a device to a file using dd

        :param device: device name to copy from
        :param file_path: output file path
        """
        LOG.debug("Copying device /dev/%s to file %s" % (device, file_path))
        cmd = 'dd if=/dev/%s of=%s bs=1024k' % (device, file_path)
        self.pvm_operator.run_vios_command_as_root_with_shell(cmd)
     
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
            ##self._remove_disk(hdisk)
            
    def _remove_disk(self, volume_name):
        """
        Remove a volume

        :param disk: the disk name
        """
        cmd = 'rmlv -f %s' % volume_name
        try:
            self.run_command(cmd)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
            if int(exit_code) == 1:
                time.sleep(10)
                self.run_command(cmd)
            else:
                raise cmdex   
            
    @logcall
    def detach_volume_from_host(self, *args, **kargs):
        for disk in args:
            hdisk = disk['device_name']
            LOG.info("Volume to detach : %s" % hdisk)
            self.detach_disk_from_vhost(hdisk)

    @logcall
    def detach_disk_from_vhost(self, vdev):
        if isinstance(vdev, list):
            disk = vdev[0].encode()[0:15]
        else:              
            disk = vdev
        command = self.command.rmvdev('-vdev %s') % disk
        try:
            self.run_vios_command(command)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
                if int(exit_code) == 1:
                    time.sleep(10)
                    self.run_command(command)
                else:
                    raise cmdex


class ISCSIDiskLocalAdapter(blockdev.ISCSIDiskAdapter):
    def __init__(self,pvm_operator):
        self.volume_api = PowerVCNovaISCSIVirtDriverVolumeAPI()
        self.pvm_operator = pvm_operator
        self._initiator = None
        self._iscsi = None
        self._connection = None

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
                # common.scp_command(self.connection_data,
                #                   source_path, remote_path, 'put')
                cp_cmd = 'cp' + ' -f ' + source_path + ' ' + remote_path
                self.run_vios_command(cp_cmd)
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

        # Calculate file size in multiples of 512 bytes |awk '{print $4}'
        output = self.run_vios_command("ls -o %s" % final_path, check_exit_code=False)
        if output:
            out = output[0].split()
            size = int(out[3])
        else:
            LOG.error(_("Uncompressed image file not found"))
            msg_args = {'file_path': final_path}
            raise exception.IBMPowerVMFileTransferFailed(**msg_args)
        if (size % 512 != 0):
            size = (int(size / 512) + 1) * 512

        return final_path, size