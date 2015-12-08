#
#
# All Rights Reserved.
# Copyright 2012 Red Hat, Inc.
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

import gettext
import os
import six
from functools import wraps
from logging import DEBUG
from nova.objects.instance import Instance
from nova.openstack.common import log as logging
from nova.openstack.common.gettextutils import Message

def _instance_filter(*args, **kwds):
    keys_to_check = ['name', 'power_state', 'vm_state', 'task_state', 'host']
    keys_to_copy = keys_to_check + ['default_ephemeral_device']

    def check_keys(arg, inst_keys):
        for key in inst_keys:
            if key not in arg:
                return False
        # Found them all!
        return True

    def check_inst(arg):
        type_ = type(arg)
        # check if this is a Nova Instance object
        if type_ == Instance:
            return True
        # check for a dict with all the right keys to match instance
        if type_ == dict and check_keys(arg, keys_to_check):
            return True
        return False

    def partial_inst(arg):
        # Creates a partial instance from a real instance
        fake_inst = {}
        for key in keys_to_copy:
            try:
                fake_inst[key] = arg[key]
            except KeyError:
                # Don't create attributes that are not present
                pass

        fake_inst_wrapper = dict(PARTIAL_INSTANCE=fake_inst)
        return fake_inst_wrapper

    filtered_args = []
    for arg in args:
        filtered_args.append(arg if not check_inst(arg) else partial_inst(arg))
    return (filtered_args, kwds)


def _logcall(filter_=None, dump_parms=False):
    def func_parms(f):
        @wraps(f)
        def wrapper(*args, **kwds):
            LOG = logging.getLogger(f.__module__)
            # isEnabledFor not introduced until 2.7
            #logging_dbg = LOG.isEnabledFor(DEBUG)
            logging_dbg = LOG.logger.isEnabledFor(DEBUG)
            #logging_dbg = True
            if logging_dbg:
                if dump_parms:
                    d_args, d_kwds = ((args, kwds)
                                      if filter_ is None else filter_(*args,
                                                                      **kwds))
                    LOG.debug("Entering args:%s kwds:%s  '%s' %s" %
                              (d_args, d_kwds, f.__name__, f.__module__))
                else:
                    LOG.debug("Entering '%s' %s" % (f.__name__, f.__module__))

            r = f(*args, **kwds)
            if logging_dbg:
                if dump_parms:
                    LOG.debug("Exiting: return '%s'  '%s' %s" %
                              (r, f.__name__, f.__module__))
                else:
                    LOG.debug("Exiting: return '%s' %s" %
                              (f.__name__, f.__module__))
            return r
        return wrapper
    return func_parms

# Convenience method to log all parameters
logcall_all_parms = _logcall()
# Convenience method to ensure the Instance parameter is logged
logcall_log_instance = _logcall()
# Filter out large Instance dumps.
logcall = _logcall(filter_=_instance_filter)
# Dump parameters and return values - DO NOT ENABLE IN PRODUCTION
# This may dump sensitive information to the log files.
# logcall = _logcall(filter_=_instance_filter, dump_parms=True)

#
# The following code overrides _ to retrieve translated messages from
# paxes_nova message catalogs (default in /usr/share/locale/<locale>)
#
_localedir = os.environ.get('powervc_nova'.upper() + '_LOCALEDIR')
_t = gettext.translation('powervc_nova', localedir=_localedir, fallback=True)

USE_LAZY = False


def enable_lazy():
    """Convenience function for configuring _() to use lazy gettext

    Call this at the start of execution to enable the gettextutils._
    function to use lazy gettext functionality. This is useful if
    your project is importing _ directly instead of using the
    gettextutils.install() way of importing the _ function

    This method is copied from nova.openstack.common.gettextutils
    in case want to set USE_LAZY to True. As of 02/10/14 it stays False.
    """
    global USE_LAZY
    USE_LAZY = True


def _(msg):
    if USE_LAZY:
        return Message(msg, domain='powervc_nova')
    else:
        if six.PY3:
            return _t.gettext(msg)
        return _t.ugettext(msg)
