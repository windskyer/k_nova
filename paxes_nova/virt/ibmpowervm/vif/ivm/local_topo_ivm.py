'''
Created on Feb 27, 2015

@author: root
'''
from paxes_nova.virt.ibmpowervm.vif.ivm import topo_ivm
from paxes_nova.virt.ibmpowervm.ivm import local_operator as ivm_local_oper
from paxes_nova.virt.ibmpowervm.vif.common import exception as excp
from paxes_nova.virt.ibmpowervm.vif.common import ras
import logging
LOG = logging.getLogger(__name__)

class IBMPowerVMNetworkTopoIVMLocal(topo_ivm.IBMPowerVMNetworkTopoIVM):
    def __init__(self, host_name, operator=None):
        if operator:
            self._pvmops = operator
        else:
            self._pvmops = ivm_local_oper.IVMLocalOperator()
        
        self.host = None
        if not host_name:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'DEVNAME_INVALID') %
                                    {'devname': host_name})
            raise excp.IBMPowerVMInvalidHostConfig(attr='host_name')
        self.host_name = host_name
        self.operator = operator
