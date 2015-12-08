# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

import numbers

from nova import conductor

from nova.openstack.common.lockutils import synchronized
from nova.openstack.common import log as logging

from paxes_nova.virt.ibmpowervm.common import exception as pvcex

from paxes_nova import _

LOG = logging.getLogger(__name__)

# progress dictionary
# reading from DB is too expensive, so we store the progress in memory
__INST_PROGRESS__ = {}


class ProgressUpdater(object):
    """
    The ProgressUpdater is intended to update instance progress
    during deploy operation.

    User should only initiate the object prior to an operation and update
    the progress as the operation progress. Once the operation is completed,
    complete_progress() has to be called to set the progress to 100%
    and clean up the instance_uuid entry from __INST_PROGRESS__.

    The object should be initiated per operation. It should not be reused.
    The object is synchronized using the instance uuid to guarantee the data
    integrity. We have to prefix the lock file with progress_, otherwise,
    the deploy operation would stuck. It's because there is already another
    lock file with just the instance_uuid being used during deploy (manager.py)

    If compute service is restarted, we will lose the progress information.
    """

    def __init__(self, context, inst_uuid, progress_val=0):
        """
        initiate instance progress stored in the dictionary.
        :param context: context to use
        :param inst_uuid: instance uuid
        :param progress_val: initial progress value
        """
        @synchronized('progress_'.join(inst_uuid), 'nova-prog-')
        def _init_inst_progress():
            global __INST_PROGRESS__
            if inst_uuid in __INST_PROGRESS__:
                # raise an exception since the uuid has been used
                LOG.error(_("The progress updater has already started for "
                            "virtual machine %s.") % inst_uuid)
                raise pvcex.IBMPowerVMProgressUpdateError(uuid=inst_uuid)
            self._inst_uuid = inst_uuid
            self._context = context
            progress = progress_val
            if not isinstance(progress, numbers.Number):
                progress = round(0)
            else:
                progress = round(progress_val)
            if progress > 100:
                progress = round(100)
            __INST_PROGRESS__[self._inst_uuid] = progress
            conductor.API().instance_update(self._context, self._inst_uuid,
                                            progress=progress)

        if not inst_uuid:
            # raise an exception since the uuid is blank
            LOG.error(_("The progress updater requires virtual machine uuid"))
            raise pvcex.IBMPowerVMProgressUpdateError(uuid='')
        _init_inst_progress()

    def get_progress(self):
        @synchronized('progress_'.join(self._inst_uuid), 'nova-prog-')
        def _get_progress():
            global __INST_PROGRESS__
            return __INST_PROGRESS__.get(self._inst_uuid)

        return _get_progress()

    def increment_progress(self, increment_by):
        """
        Increment the instance progress by specified value
        :param increment_by: numeric value of how much we should increment
                             the progress. If the value is <= 0, it's ignored.
                             If sum of current progress and increment_by > 100,
                             it's capped at 100.
        """
        @synchronized('progress_'.join(self._inst_uuid), 'nova-prog-')
        def _inc_progress():
            global __INST_PROGRESS__
            if not self._inst_uuid in __INST_PROGRESS__:
                LOG.error(_("The progress updater has not started"
                            " for virtual machine %s.") % self._inst_uuid)
                raise pvcex.IBMPowerVMProgressUpdateError(uuid=self._inst_uuid)
            # make sure it's a positive numeric value
            if increment_by > 0 and isinstance(increment_by, numbers.Number):
                progress = round(increment_by)
                progress += __INST_PROGRESS__[self._inst_uuid]
                LOG.debug("current progress %s" %
                          __INST_PROGRESS__[self._inst_uuid])
                if progress > 100:
                    progress = round(100)
                    LOG.debug("deploy_progress_update %s" % progress)
                conductor.API().instance_update(self._context,
                                                self._inst_uuid,
                                                progress=progress)
                __INST_PROGRESS__[self._inst_uuid] = progress

        _inc_progress()

    def set_progress(self, progress_val):
        """
        Set the instance progress to specified value. It will ignore the
        progress value if it's set to lower than existing progress.
        :param progress_val: numeric value of instance progress. If the value
                             is < then existing progress or > 100,
                             it's ignored.
        """
        @synchronized('progress_'.join(self._inst_uuid), 'nova-prog-')
        def _set_progress():
            global __INST_PROGRESS__
            if not self._inst_uuid in __INST_PROGRESS__:
                LOG.error(_("The progress updater has not started"
                            " for virtual machine %s.") % self._inst_uuid)
                raise pvcex.IBMPowerVMProgressUpdateError(uuid=self._inst_uuid)
            progress = progress_val

            # NOP if progress_val is not numeric value
            if not isinstance(progress, numbers.Number):
                return

            progress = round(progress_val)
            if progress > __INST_PROGRESS__[self._inst_uuid]:
                # make sure the progress is never over 100
                if progress > 100:
                    progress = round(100)
                conductor.API().instance_update(self._context, self._inst_uuid,
                                                progress=progress)
                __INST_PROGRESS__[self._inst_uuid] = progress

        _set_progress()

    def complete_progress(self):
        """
        The method is called when operation is completed. It will set the
        progress to 100 and remove the instance uuid entry from
        __INST_PROGRESS__.
        """
        @synchronized('progress_'.join(self._inst_uuid), 'nova-prog-')
        def _complete_inst_progress():
            """
            Update instance progress to 100. This will also clean up
            the dictionary entry
            """
            global __INST_PROGRESS__
            if not self._inst_uuid in __INST_PROGRESS__:
                LOG.error(_("The progress updater has not started"
                            " for virtual machine %s.") % self._inst_uuid)
                raise pvcex.IBMPowerVMProgressUpdateError(uuid=self._inst_uuid)

            # before clean up, make sure we set the progress to 100
            if __INST_PROGRESS__[self._inst_uuid] != 100:
                conductor.API().instance_update(self._context, self._inst_uuid,
                                                progress=round(100))
            del __INST_PROGRESS__[self._inst_uuid]

        _complete_inst_progress()
