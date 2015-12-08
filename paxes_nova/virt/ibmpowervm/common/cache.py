# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================
"""
Implements a generic data cache object

Callers must implement their own locking since we wouldn't want
all instances to lock on a single lock.

"""

import time

from nova.openstack.common import log as logging

from paxes_nova import logcall

LOG = logging.getLogger(__name__)


class Cache(object):
    def __init__(self, interval=15):
        self.invalidate()
        self.set_interval(interval)
        #static_data to hold information which will not be invalidated for
        #lifetime of cache object
        self._static_data = None

    def invalidate(self):
        self._data = None
        self._time = None

    def set_data(self, data):
        """Set the data associated with this cache

        :param data:        the data to cache
        """
        self._data = data
        self._time = time.time()

    def set_interval(self, interval):
        self._interval = interval

    def set_static_data(self, static_data):
        """
        static_data to hold information which will not be invalidated for
        lifetime of cache object
        """
        self._static_data = static_data

    @logcall
    def get_data(self, interval=0, since=None):
        """Get the current cached data

        Returns the cached data

        :param interval:    the valid interval for the data,
                            -1: return data regardless of interval
                            0: use default interval
        :param since:       return the data if it's been updated
                            since the given time stamp.  'since' is
                            a floating point as returned by time.time()

        """

        # if the caller doesn't care about the interval, just return the data
        if interval == -1:
            return self._data
        # Make sure we have a time and interval
        if self._interval is None or self._time is None:
            return None

        # If the specified interval is zero then use default interval
        if interval == 0:
            interval = self._interval

        curr_time = time.time()
        # Check if the clock was set backwards and if so invalidate the data
        if curr_time < self._time:
            self.invalidate()
        else:
            # The 'since' parameter takes precedence.
            if since is not None:
                if self._time >= since:
                    return self._data
                else:
                    return None

            # Calculate the valid time based on the interval and current time
            valid_time = self._time + interval
            if valid_time >= curr_time:
                LOG.debug("Cache is valid- time difference : %d seconds" %
                         (valid_time - curr_time))
                return self._data
            else:
                LOG.debug("Cache invalid, diff : %d seconds" %
                         (valid_time - curr_time))

        return None

    @logcall
    def get_old_data(self):
        """
        Forcefully get the old data
        """
        return self._data

    @logcall
    def get_static_data(self):
        return self._static_data
