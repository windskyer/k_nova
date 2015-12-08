'''
Created on Feb 27, 2015

@author: root
'''

from paxes_nova.virt.ibmpowervm.vif.ivm import driver_ivm
from paxes_nova.virt.ibmpowervm.ivm import local_operator
from paxes_nova.virt.ibmpowervm.vif.ivm import local_topo_ivm

class IBMPowerVMVlanVIFDriverLocal(driver_ivm.IBMPowerVMVlanVIFDriver):
    def __init__(self, host_name, pvm_op=None):
        if pvm_op:
            self._pvmops = pvm_op
        else:
            self._pvmops = local_operator.IVMLocalOperator()
            
        self._topo = local_topo_ivm.IBMPowerVMNetworkTopoIVMLocal(host_name, self._pvmops)
        
        self.current_topo = None
        self.current_topo_time = None